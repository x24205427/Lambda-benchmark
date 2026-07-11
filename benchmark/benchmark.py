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


def run_benchmark(function_prefix, memory_levels, path_suffix, method, body, warm_requests, region):
    lambda_client = boto3.client("lambda", region_name=region)
    rows = []

    for mem in memory_levels:
        function_name = f"{function_prefix}-mem{mem}"
        mem_tag = f"mem{mem}"
        print(f"\n=== {mem} MB ({function_name}) ===")

        print("Cold/first invocation...")
        
        force_cold_start(lambda_client, function_name)
        
        event = build_event(mem_tag, path_suffix, method, body)
        status, client_ms, report = invoke_and_get_metrics(lambda_client, function_name, event)
        
        print(f"  status={status} client_elapsed={client_ms}ms billed={report['billed_ms']}ms "
              f"init={report['init_ms']}ms max_mem_used={report['max_memory_mb']}MB")
              
        cold_billed = report["billed_ms"]
        cold_init = report["init_ms"]

        print(f"Warm invocations x{warm_requests} (Instantaneous)...")
        warm_billed = []
        
        for i in range(warm_requests):
            event = build_event(mem_tag, path_suffix, method, body)
            status, client_ms, report = invoke_and_get_metrics(lambda_client, function_name, event)
            
            if report["billed_ms"] is not None:
                warm_billed.append(report["billed_ms"])
            else:
                print(f"    - Missed log on warm run {i+1}")

        if not warm_billed:
            print("  WARNING: no warm billed-duration samples collected, skipping percentiles")
            p50 = p95 = p99 = avg = None
            cost = None
        else:
            p50, p95, p99 = percentile(warm_billed, 50), percentile(warm_billed, 95), percentile(warm_billed, 99)
            avg = statistics.mean(warm_billed)
            cost = estimate_cost_per_1k(mem, avg)
            print(f"  warm billed p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} ms "
                  f"avg={avg:.1f}ms  est.cost/1k=${cost}  (n={len(warm_billed)})")

        rows.append({
            "memory_mb": mem,
            "cold_billed_ms": cold_billed,
            "cold_init_ms": cold_init,
            "warm_billed_p50_ms": round(p50, 2) if p50 is not None else None,
            "warm_billed_p95_ms": round(p95, 2) if p95 is not None else None,
            "warm_billed_p99_ms": round(p99, 2) if p99 is not None else None,
            "warm_billed_avg_ms": round(avg, 2) if avg is not None else None,
            "est_cost_per_1k_usd": cost,
            "warm_sample_count": len(warm_billed),
        })

    return rows


def save_csv(rows, path="results.csv"):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved results to {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark Lambda memory configurations via direct invoke + CloudWatch REPORT parsing")
    parser.add_argument("--function-prefix", required=True, help="SAM stack name, e.g. memory-benchmark (functions are named <prefix>-mem128 etc.)")
    parser.add_argument("--memory", nargs="+", type=int, default=[128, 256, 512, 1024, 2048, 3008])
    parser.add_argument("--path", default="/health", help="Path suffix to invoke, e.g. /health, /transform, /items/bench-1")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--body", default=None, help="JSON body string for POST requests, e.g. /transform")
    parser.add_argument("--warm-requests", type=int, default=50)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--out", default="results.csv")
    args = parser.parse_args()

    results = run_benchmark(
        function_prefix=args.function_prefix,
        memory_levels=args.memory,
        path_suffix=args.path,
        method=args.method,
        body=args.body,
        warm_requests=args.warm_requests,
        region=args.region,
    )
    save_csv(results, args.out)