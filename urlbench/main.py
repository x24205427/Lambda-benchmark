"""
URL Benchmarker Lambda.

Given a target URL, performs N sequential HTTP(S) requests and measures, per
request, the full timing breakdown (DNS / TCP connect / TLS / time-to-first-byte
/ total), HTTP status, response size and key headers. Returns aggregate stats
(avg / p50 / p95) as JSON.

Stdlib only — no third-party deps, so cold starts stay tiny and packaging is
trivial. Invoked via a Lambda Function URL (CORS-enabled) from the static
dashboard hosted on GitHub Pages.
"""

import json
import socket
import ssl
import time
from urllib.parse import urlparse

# Guardrails so a single invocation can never run long or be abused as a
# request amplifier. All well within the Lambda always-free tier.
MAX_SAMPLES = 20
DEFAULT_SAMPLES = 5
PER_REQUEST_TIMEOUT = 10.0  # seconds
MAX_BODY_READ = 2_000_000   # cap bytes read so huge pages don't blow memory/time

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _normalize_url(raw):
    """Accept 'google.com', 'www.google.com', or a full URL; default to https."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("No URL provided")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("Could not parse a hostname from the URL")
    return parsed


def _time_one_request(parsed):
    """Perform a single request and return a dict of millisecond timings."""
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    result = {
        "dns_ms": None, "connect_ms": None, "tls_ms": None,
        "ttfb_ms": None, "total_ms": None, "status": None,
        "size_bytes": None, "content_type": None, "server": None,
        "error": None,
    }

    t_start = time.perf_counter()
    sock = None
    try:
        # DNS
        t0 = time.perf_counter()
        addrinfo = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        result["dns_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        family, socktype, proto, _, sockaddr = addrinfo[0]

        # TCP connect
        t0 = time.perf_counter()
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(PER_REQUEST_TIMEOUT)
        sock.connect(sockaddr)
        result["connect_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # TLS handshake (https only)
        if parsed.scheme == "https":
            t0 = time.perf_counter()
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            result["tls_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Send request
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: url-benchmarker/1.0\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(req.encode("ascii", "ignore"))

        # Time to first byte
        t0 = time.perf_counter()
        first = sock.recv(65536)
        result["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Drain the rest (capped)
        chunks = [first]
        total_read = len(first)
        while total_read < MAX_BODY_READ:
            buf = sock.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            total_read += len(buf)

        result["total_ms"] = round((time.perf_counter() - t_start) * 1000, 2)

        raw = b"".join(chunks)
        header_blob, _, _ = raw.partition(b"\r\n\r\n")
        header_text = header_blob.decode("iso-8859-1", "replace")
        header_lines = header_text.split("\r\n")

        if header_lines and header_lines[0].startswith("HTTP/"):
            parts = header_lines[0].split(" ", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                result["status"] = int(parts[1])

        headers = {}
        for line in header_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        result["content_type"] = headers.get("content-type")
        result["server"] = headers.get("server")
        result["size_bytes"] = total_read

    except socket.gaierror as e:
        result["error"] = f"DNS resolution failed: {e}"
    except socket.timeout:
        result["error"] = f"Request timed out after {PER_REQUEST_TIMEOUT}s"
    except ssl.SSLError as e:
        result["error"] = f"TLS error: {e}"
    except OSError as e:
        result["error"] = f"Connection error: {e}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return result


def _benchmark(url, samples):
    parsed = _normalize_url(url)
    samples = max(1, min(MAX_SAMPLES, int(samples)))

    runs = [_time_one_request(parsed) for _ in range(samples)]
    ok = [r for r in runs if r["error"] is None and r["total_ms"] is not None]

    def agg(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 2),
            "p50": round(_percentile(vals, 50), 2),
            "p95": round(_percentile(vals, 95), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
        }

    last = ok[-1] if ok else (runs[-1] if runs else {})
    return {
        "url": f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}",
        "host": parsed.hostname,
        "scheme": parsed.scheme,
        "samples_requested": samples,
        "samples_ok": len(ok),
        "samples_failed": samples - len(ok),
        "timings": {
            "dns_ms": agg("dns_ms"),
            "connect_ms": agg("connect_ms"),
            "tls_ms": agg("tls_ms"),
            "ttfb_ms": agg("ttfb_ms"),
            "total_ms": agg("total_ms"),
        },
        "status": last.get("status"),
        "size_bytes": last.get("size_bytes"),
        "content_type": last.get("content_type"),
        "server": last.get("server"),
        "errors": sorted({r["error"] for r in runs if r["error"]}),
        "runs": runs,
    }


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body),
    }


def handler(event, context):
    # CORS preflight
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
    )
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    # Accept the URL from a POST JSON body or a ?url= query param.
    url = None
    samples = DEFAULT_SAMPLES

    raw_body = event.get("body")
    if raw_body:
        try:
            payload = json.loads(raw_body)
            url = payload.get("url")
            samples = payload.get("samples", DEFAULT_SAMPLES)
        except (ValueError, TypeError):
            pass

    if not url:
        params = event.get("queryStringParameters") or {}
        url = params.get("url")
        samples = params.get("samples", samples)

    if not url:
        return _response(400, {"error": "Provide a 'url' (JSON body or ?url= query param)."})

    try:
        return _response(200, _benchmark(url, samples))
    except ValueError as e:
        return _response(400, {"error": str(e)})
    except Exception as e:  # noqa: BLE001 - surface unexpected errors as JSON
        return _response(500, {"error": f"Unexpected error: {e}"})
