"""
Benchmark script for the Memory Allocation Impact study (v2).
Optimized: Bypasses CloudWatch ingestion delays using LogType='Tail'.
"""

import argparse
import base64
import csv
import json
import statistics
import time
import uuid

import boto3

PRICE_PER_GB_SECOND = 0.0000166667
PRICE_PER_REQUEST = 0.0000002


def percentile(data, pct):
    if not data:
        return None
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(data_sorted) - 1)
    if f == c:
        return data_sorted[f]
    return data_sorted[f] + (data_sorted[c] - data_sorted[f]) * (k - f)


def build_event(mem_tag, path_suffix, method="GET", body=None):
    """Build a minimal API-Gateway-v1-shaped event matching what main.py expects."""
    full_path = f"/{mem_tag}{path_suffix}"
    return {
        "resource": full_path,
        "path": full_path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"} if body else {},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "resourcePath": full_path,
            "httpMethod": method,
            "path": f"/prod{full_path}",
            "stage": "prod",
            "identity": {"sourceIp": "127.0.0.1"},
            "domainName": "benchmark-script",
            "apiId": "local",
            "requestId": str(uuid.uuid4()),
        },
        "body": body,
        "isBase64Encoded": False,
    }


def force_cold_start(lambda_client, function_name):
    """Forces a cold start by updating the function's configuration with a dummy environment variable."""
    print(f"  Forcing cold start via configuration update for {function_name}...")
    config = lambda_client.get_function_configuration(FunctionName=function_name)
    env_vars = config.get('Environment', {}).get('Variables', {})
    
    # Append a timestamp to force the container to rebuild
    env_vars['LAST_UPDATED'] = str(time.time())
    
    lambda_client.update_function_configuration(
        FunctionName=function_name,
        Environment={'Variables': env_vars}
    )
    
    # Wait for the Lambda state to become Active again before invoking
    waiter = lambda_client.get_waiter('function_updated_v2')
    waiter.wait(FunctionName=function_name)


def parse_report_line(message):
    """Parse a REPORT log line into its numeric fields."""
    result = {"billed_ms": None, "init_ms": None, "max_memory_mb": None}
    try:
        if "Billed Duration:" in message:
            part = message.split("Billed Duration:")[1].split("ms")[0].strip()
            result["billed_ms"] = float(part)
        if "Init Duration:" in message:
            part = message.split("Init Duration:")[1].split("ms")[0].strip()
            result["init_ms"] = float(part)
        if "Max Memory Used:" in message:
            part = message.split("Max Memory Used:")[1].split("MB")[0].strip()
            result["max_memory_mb"] = float(part)
    except (IndexError, ValueError):
        pass
    return result


def invoke_and_get_metrics(lambda_client, function_name, event):
    """Direct Lambda invoke requesting the tail log to bypass CloudWatch entirely."""
    t0 = time.perf_counter()
    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        LogType="Tail",  # This forces AWS to return the execution log in the response
        Payload=json.dumps(event).encode(),
    )
    client_elapsed_ms = (time.perf_counter() - t0) * 1000
    
    payload = json.loads(resp["Payload"].read())
    status_code = payload.get("statusCode")
    
    if resp.get("FunctionError"):
        print(f"  WARNING: function error on invoke: {payload}")
        
    # Extract and decode the base64 log stream
    log_result = ""
    if "LogResult" in resp:
        log_result = base64.b64decode(resp["LogResult"]).decode("utf-8")
        
    metrics = parse_report_line(log_result)
    
    return status_code, round(client_elapsed_ms, 2), metrics


def estimate_cost_per_1k(memory_mb, avg_billed_ms):
    if avg_billed_ms is None:
        return None
    gb = memory_mb / 1024
    seconds = avg_billed_ms / 1000
    compute_cost = gb * seconds * PRICE_PER_GB_SECOND
    request_cost = PRICE_PER_REQUEST
    return round((compute_cost + request_cost) * 1000, 5)


def run_benchmark(function_prefix, memory_levels, path_suffix, method, body,
                  warm_requests, region, runs=1):
    """Benchmark each memory config. Repeats the full cold+warm sequence `runs`
    times per config; aggregates warm samples across all runs (median + IQR for
    robustness) and returns both the aggregated rows and the raw per-invocation
    samples (used by the statistical analysis script)."""
    lambda_client = boto3.client("lambda", region_name=region)
    rows = []
    raw_samples = []  # one dict per invocation: memory_mb, run, phase, billed_ms

    for mem in memory_levels:
        function_name = f"{function_prefix}-mem{mem}"
        mem_tag = f"mem{mem}"
        print(f"\n=== {mem} MB ({function_name}) ===")

        cold_billed_runs = []
        cold_init_runs = []
        warm_billed = []

        for run_idx in range(1, runs + 1):
            if runs > 1:
                print(f"-- run {run_idx}/{runs} --")

            print("Cold/first invocation...")
            force_cold_start(lambda_client, function_name)
            event = build_event(mem_tag, path_suffix, method, body)
            status, client_ms, report = invoke_and_get_metrics(lambda_client, function_name, event)
            print(f"  status={status} client_elapsed={client_ms}ms billed={report['billed_ms']}ms "
                  f"init={report['init_ms']}ms max_mem_used={report['max_memory_mb']}MB")
            if report["billed_ms"] is not None:
                cold_billed_runs.append(report["billed_ms"])
                raw_samples.append({"memory_mb": mem, "run": run_idx, "phase": "cold",
                                    "billed_ms": report["billed_ms"]})
            if report["init_ms"] is not None:
                cold_init_runs.append(report["init_ms"])

            print(f"Warm invocations x{warm_requests} (Instantaneous)...")
            for i in range(warm_requests):
                event = build_event(mem_tag, path_suffix, method, body)
                status, client_ms, report = invoke_and_get_metrics(lambda_client, function_name, event)
                if report["billed_ms"] is not None:
                    warm_billed.append(report["billed_ms"])
                    raw_samples.append({"memory_mb": mem, "run": run_idx, "phase": "warm",
                                        "billed_ms": report["billed_ms"]})
                else:
                    print(f"    - Missed log on warm run {i+1}")

        # Cold metrics: median across runs (robust to a single unlucky cold start).
        cold_billed = statistics.median(cold_billed_runs) if cold_billed_runs else None
        cold_init = statistics.median(cold_init_runs) if cold_init_runs else None

        if not warm_billed:
            print("  WARNING: no warm billed-duration samples collected, skipping percentiles")
            p50 = p95 = p99 = avg = med = iqr = cost = None
        else:
            p50, p95, p99 = percentile(warm_billed, 50), percentile(warm_billed, 95), percentile(warm_billed, 99)
            avg = statistics.mean(warm_billed)
            med = statistics.median(warm_billed)
            iqr = percentile(warm_billed, 75) - percentile(warm_billed, 25)
            cost = estimate_cost_per_1k(mem, avg)
            print(f"  warm billed p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} ms "
                  f"avg={avg:.1f}ms median={med:.1f} IQR={iqr:.1f}  est.cost/1k=${cost}  "
                  f"(n={len(warm_billed)} across {runs} run(s))")

        rows.append({
            "memory_mb": mem,
            "cold_billed_ms": round(cold_billed, 2) if cold_billed is not None else None,
            "cold_init_ms": round(cold_init, 2) if cold_init is not None else None,
            "warm_billed_p50_ms": round(p50, 2) if p50 is not None else None,
            "warm_billed_p95_ms": round(p95, 2) if p95 is not None else None,
            "warm_billed_p99_ms": round(p99, 2) if p99 is not None else None,
            "warm_billed_avg_ms": round(avg, 2) if avg is not None else None,
            "warm_billed_median_ms": round(med, 2) if med is not None else None,
            "warm_billed_iqr_ms": round(iqr, 2) if iqr is not None else None,
            "est_cost_per_1k_usd": cost,
            "warm_sample_count": len(warm_billed),
            "runs": runs,
        })

    return rows, raw_samples


def save_csv(rows, path="results.csv"):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved results to {path}")


def save_raw_csv(raw_samples, path):
    if not raw_samples:
        print("No raw samples to save.")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["memory_mb", "run", "phase", "billed_ms"])
        writer.writeheader()
        writer.writerows(raw_samples)
    print(f"Saved {len(raw_samples)} raw samples to {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark Lambda memory configurations via direct invoke + CloudWatch REPORT parsing")
    parser.add_argument("--function-prefix", required=True, help="SAM stack name, e.g. memory-benchmark (functions are named <prefix>-mem128 etc.)")
    parser.add_argument("--memory", nargs="+", type=int, default=[128, 256, 512, 1024, 2048, 3008])
    parser.add_argument("--path", default="/health", help="Path suffix to invoke, e.g. /health, /transform, /items/bench-1")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--body", default=None, help="JSON body string for POST requests, e.g. /transform")
    parser.add_argument("--warm-requests", type=int, default=50)
    parser.add_argument("--runs", type=int, default=1,
                        help="Repeat the full cold+warm sequence N times per config for statistical robustness")
    parser.add_argument("--raw-out", default=None,
                        help="Optional path to write per-invocation raw samples (input for analysis/analyze.py)")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--out", default="results.csv")
    args = parser.parse_args()

    results, raw = run_benchmark(
        function_prefix=args.function_prefix,
        memory_levels=args.memory,
        path_suffix=args.path,
        method=args.method,
        body=args.body,
        warm_requests=args.warm_requests,
        region=args.region,
        runs=args.runs,
    )
    save_csv(results, args.out)
    if args.raw_out:
        save_raw_csv(raw, args.raw_out)