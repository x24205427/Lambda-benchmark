"""
AWS Lambda Memory Benchmark Dashboard
FastAPI + Jinja2 + Vanilla JS with Chart.js
"""

import csv
import io
import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "benchmark_data"

app = FastAPI(title="Lambda Benchmark Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# In-memory store for uploaded / real-benchmark data
_data_store: dict[str, list[dict]] = {}

# ─────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({k: _coerce(v) for k, v in row.items()})
    return rows


def _coerce(v: str):
    # Blank cells (common in partially-populated benchmark CSVs) become None
    # rather than "" so downstream numeric code treats them as missing values.
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        pass
    try:
        return float(v)
    except (ValueError, TypeError):
        pass
    return v


def _num(v, default: float = 0.0) -> float:
    """Coerce a possibly-missing/None/string cell to a float for arithmetic."""
    return v if isinstance(v, (int, float)) else default


def load_default_data() -> dict[str, list[dict]]:
    result = {}
    for name in ("health", "transform", "io"):
        p = DATA_DIR / f"{name}_results.csv"
        if p.exists():
            result[name] = load_csv(p)
    return result


# ─────────────────────────────────────────────
# Analytics helpers
# ─────────────────────────────────────────────

PRICE_PER_GB_SECOND = 0.0000166667
PRICE_PER_REQUEST = 0.0000002
MEMORY_SIZES = [128, 256, 512, 1024, 2048, 3008]


def est_cost_per_million(memory_mb: float, avg_billed_ms: float) -> float:
    gb = _num(memory_mb, 128) / 1024
    seconds = _num(avg_billed_ms) / 1000
    compute = gb * seconds * PRICE_PER_GB_SECOND
    return round((compute + PRICE_PER_REQUEST) * 1_000_000, 4)


def perf_score(row: dict, all_rows: list[dict], endpoint_rows: list[dict]) -> float:
    """Weighted performance score 0-100."""
    def norm(val, vals, invert=True):
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return 100
        n = (val - mn) / (mx - mn)
        return (1 - n) * 100 if invert else n * 100

    warm_vals = [_num(r.get("warm_billed_avg_ms")) for r in endpoint_rows]
    cold_vals = [_num(r.get("cold_init_ms")) for r in endpoint_rows]
    cost_vals = [_num(r.get("est_cost_per_1k_usd")) * 1000 for r in endpoint_rows]

    w = norm(_num(row.get("warm_billed_avg_ms")), warm_vals)
    co = norm(_num(row.get("cold_init_ms")), cold_vals)
    cs = norm(_num(row.get("est_cost_per_1k_usd")) * 1000, cost_vals)

    # memory efficiency: lower allocated but good perf
    mem_eff = norm(_num(row.get("memory_mb")), [_num(r.get("memory_mb")) for r in endpoint_rows])

    return round(0.4 * w + 0.3 * cs + 0.2 * co + 0.1 * mem_eff, 1)


def compute_analytics(data: dict[str, list[dict]]) -> dict:
    health = [dict(r) for r in data.get("health", [])]
    transform = [dict(r) for r in data.get("transform", [])]
    io_data = [dict(r) for r in data.get("io", [])]

    all_rows = []
    for ep, rows in [("health", health), ("transform", transform), ("io", io_data)]:
        for r in rows:
            r["endpoint"] = ep
            r["cost_per_million"] = est_cost_per_million(
                r.get("memory_mb", 128), r.get("warm_billed_avg_ms", 0)
            )
            r["perf_score"] = perf_score(r, all_rows, rows)
            all_rows.append(r)

    all_rows.sort(key=lambda r: r["perf_score"], reverse=True)

    # KPIs — guard against an empty dataset (no CSVs loaded / all rows filtered)
    if all_rows:
        best_warm = min(all_rows, key=lambda r: _num(r.get("warm_billed_avg_ms"), 9999))
        best_cold = min(all_rows, key=lambda r: _num(r.get("cold_init_ms"), 9999))
        best_cost = min(all_rows, key=lambda r: _num(r.get("cost_per_million"), 9999))
        best_config = all_rows[0]
    else:
        best_warm = best_cold = best_cost = best_config = {}

    kpis = {
        "fastest_avg_response": {
            "value": f"{_num(best_warm.get('warm_billed_avg_ms')):.1f}",
            "unit": "ms",
            "desc": f"@ {best_warm.get('memory_mb', 0)} MB",
            "label": "Fastest Avg Response",
            "trend": "Best warm latency",
        },
        "lowest_cold_start": {
            "value": f"{_num(best_cold.get('cold_init_ms')):.0f}",
            "unit": "ms",
            "desc": f"@ {best_cold.get('memory_mb', 0)} MB",
            "label": "Lowest Cold Start",
            "trend": "Quickest init",
        },
        "lowest_cost": {
            "value": f"${_num(best_cost.get('cost_per_million')):.3f}",
            "unit": "",
            "desc": f"per 1M req @ {best_cost.get('memory_mb', 0)} MB",
            "label": "Lowest Est. Cost",
            "trend": "Cheapest config",
        },
        "best_memory_config": {
            "value": str(best_config.get("memory_mb", 0)),
            "unit": "MB",
            "desc": f"Score {best_config.get('perf_score', 0)}/100",
            "label": "Best Memory Config",
            "trend": "Weighted winner",
        },
        "total_runs": {
            "value": str(len(all_rows)),
            "unit": "",
            "desc": "Aggregated configurations",
            "label": "Total Benchmark Runs",
            "trend": "All endpoints",
        },
    }

    # Charts data
    memories = sorted(set(r.get("memory_mb", 0) for r in all_rows))

    def get_by_mem(rows, key):
        return {r.get("memory_mb"): r.get(key) for r in rows}

    health_warm = get_by_mem(health, "warm_billed_avg_ms")
    transform_warm = get_by_mem(transform, "warm_billed_avg_ms")
    io_warm = get_by_mem(io_data, "warm_billed_avg_ms")

    health_cold = get_by_mem(health, "cold_init_ms")
    transform_cold = get_by_mem(transform, "cold_init_ms")
    io_cold = get_by_mem(io_data, "cold_init_ms")

    health_cost = {r.get("memory_mb"): r.get("cost_per_million") for r in health}
    transform_cost = {r.get("memory_mb"): r.get("cost_per_million") for r in transform}
    io_cost = {r.get("memory_mb"): r.get("cost_per_million") for r in io_data}

    # Insights
    insights = generate_insights(health, transform, io_data, memories)

    # Recommendation
    recommendation = generate_recommendation(all_rows)

    return {
        "kpis": kpis,
        "memories": memories,
        "health": health,
        "transform": transform,
        "io": io_data,
        "all_rows": all_rows,
        "charts": {
            "health_warm": [health_warm.get(m) for m in memories],
            "transform_warm": [transform_warm.get(m) for m in memories],
            "io_warm": [io_warm.get(m) for m in memories],
            "health_cold": [health_cold.get(m) for m in memories],
            "transform_cold": [transform_cold.get(m) for m in memories],
            "io_cold": [io_cold.get(m) for m in memories],
            "health_cost": [health_cost.get(m) for m in memories],
            "transform_cost": [transform_cost.get(m) for m in memories],
            "io_cost": [io_cost.get(m) for m in memories],
        },
        "insights": insights,
        "recommendation": recommendation,
        "has_io": len(io_data) > 0,
    }


def generate_insights(health, transform, io_data, memories):
    insights = []
    if health and len(health) >= 2:
        first = next((r for r in health if r["memory_mb"] == min(memories)), None)
        best = min(health, key=lambda r: _num(r.get("warm_billed_avg_ms"), 9999))
        if (first and best and first["memory_mb"] != best["memory_mb"]
                and _num(first.get("warm_billed_avg_ms"))):
            pct = round((1 - _num(best.get("warm_billed_avg_ms")) / _num(first.get("warm_billed_avg_ms"))) * 100)
            insights.append(
                f"Moving from {first['memory_mb']} MB to {best['memory_mb']} MB reduces Health endpoint latency by ~{pct}%."
            )

    # Cost efficiency sweet spot
    if health:
        sorted_h = sorted(health, key=lambda r: _num(r.get("est_cost_per_1k_usd"), 9))
        cheapest = sorted_h[0]
        fastest = min(health, key=lambda r: _num(r.get("warm_billed_avg_ms"), 9999))
        if cheapest["memory_mb"] != fastest["memory_mb"]:
            insights.append(
                f"{cheapest['memory_mb']} MB is the cheapest Health config (${_num(cheapest.get('est_cost_per_1k_usd')):.5f}/1k req) "
                f"while {fastest['memory_mb']} MB is the fastest ({_num(fastest.get('warm_billed_avg_ms')):.1f} ms avg)."
            )

    # Diminishing returns
    if health and len(health) >= 3:
        sorted_h = sorted(health, key=lambda r: r["memory_mb"])
        gains = []
        for i in range(1, len(sorted_h)):
            prev, curr = sorted_h[i - 1], sorted_h[i]
            if prev["warm_billed_avg_ms"] and curr["warm_billed_avg_ms"]:
                gains.append(prev["warm_billed_avg_ms"] - curr["warm_billed_avg_ms"])
        if gains:
            last_gain = gains[-1]
            max_gain = max(gains)
            if last_gain < max_gain * 0.1:
                insights.append(
                    f"Allocating beyond {sorted_h[-2]['memory_mb']} MB yields diminishing returns "
                    f"(<{last_gain:.1f} ms improvement on the Health endpoint)."
                )

    if transform:
        sorted_t = sorted(transform, key=lambda r: r["memory_mb"])
        insights.append(
            f"The Transform endpoint is highly efficient: latency stays below {max(_num(r.get('warm_billed_avg_ms')) for r in transform):.1f} ms "
            f"even at {sorted_t[0]['memory_mb']} MB, demonstrating IO-bound workload characteristics."
        )

    if io_data and health:
        io_best = min(io_data, key=lambda r: _num(r.get("warm_billed_avg_ms"), 9999))
        health_best = min(health, key=lambda r: _num(r.get("warm_billed_avg_ms"), 9999))
        insights.append(
            f"DynamoDB I/O endpoint benefits most from higher memory: {io_best['memory_mb']} MB gives "
            f"{_num(io_best.get('warm_billed_avg_ms')):.1f} ms avg vs Health at {_num(health_best.get('warm_billed_avg_ms')):.1f} ms."
        )

    return insights[:5]


def generate_recommendation(all_rows):
    if not all_rows:
        return {"memory": "N/A", "score": 0, "reason": "No data loaded."}

    # Among the top scorers pick cost-reasonable one
    top = [r for r in all_rows if r["perf_score"] >= all_rows[0]["perf_score"] * 0.85]
    best = min(top, key=lambda r: _num(r.get("cost_per_million"), 9999))
    return {
        "memory": best.get("memory_mb"),
        "endpoint": best.get("endpoint", ""),
        "score": best.get("perf_score"),
        "warm_avg": _num(best.get("warm_billed_avg_ms")),
        "cost_per_million": _num(best.get("cost_per_million")),
        "reason": (
            f"Provides the best balance between response time ({_num(best.get('warm_billed_avg_ms')):.1f} ms avg) "
            f"and AWS Lambda cost (${_num(best.get('cost_per_million')):.3f}/1M requests) "
            f"with a performance score of {best.get('perf_score')}/100."
        ),
    }


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Start empty: only show data after a real benchmark run or CSV upload.
    analytics = compute_analytics(_data_store)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"analytics": analytics},
    )


@app.get("/api/data")
async def get_data():
    # Start empty: only show data after a real benchmark run or CSV upload.
    analytics = compute_analytics(_data_store)
    return JSONResponse(analytics)


@app.post("/api/upload")
async def upload_csv(files: list[UploadFile] = File(...)):
    global _data_store
    _data_store = {}
    for file in files:
        content = await file.read()
        text = content.decode("utf-8")
        name = file.filename or ""
        if "health" in name.lower():
            key = "health"
        elif "transform" in name.lower():
            key = "transform"
        elif "io" in name.lower():
            key = "io"
        else:
            key = name.replace(".csv", "")
        rows = []
        for row in csv.DictReader(io.StringIO(text)):
            rows.append({k: _coerce(v) for k, v in row.items()})
        _data_store[key] = rows

    analytics = compute_analytics(_data_store)
    return JSONResponse({"status": "ok", "analytics": analytics})


@app.post("/api/reset")
async def reset():
    global _data_store
    _data_store = {}
    return JSONResponse({"status": "ok"})


@app.get("/api/real-benchmark/stream")
async def real_benchmark_stream(
    function_prefix: str = "memory-benchmark",
    region: str = "eu-west-1",
    warm_requests: int = 50,
    memories: str = "128,256,512,1024,2048,3008",
):
    """
    Stream real benchmark execution output as Server-Sent Events.
    Invokes the python benchmark script in lambda-memory-app/benchmark/benchmark.py.
    """
    import sys
    import asyncio

    async def event_generator():
        # Clean/parse memories
        mem_list = []
        for m in memories.split(","):
            m = m.strip()
            if m.isdigit():
                mem_list.append(int(m))
        if not mem_list:
            mem_list = [128, 256, 512, 1024, 2048, 3008]

        script_path = BASE_DIR.parent / "benchmark" / "benchmark.py"
        if not script_path.exists():
            yield f"data: {json.dumps({'type': 'error', 'text': f'Benchmark script not found at {script_path}'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'info', 'text': f'Found benchmark script: {script_path}'})}\n\n"
        yield f"data: {json.dumps({'type': 'info', 'text': f'Target AWS Region: {region}'})}\n\n"
        yield f"data: {json.dumps({'type': 'info', 'text': f'Function Prefix: {function_prefix}'})}\n\n"
        yield f"data: {json.dumps({'type': 'info', 'text': f'Memory configurations: {mem_list}'})}\n\n"

        steps = [
            ("health", "/health", "GET", None),
            ("transform", "/transform", "POST", '{"records": [{"a": 1, "b": 2}]}'),
            ("io", "/items/bench-1", "GET", None),
        ]

        # Reset global data store so new files will be loaded
        global _data_store
        _data_store = {}

        # Ensure DATA_DIR exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        success = True
        for ep_name, path, method, body in steps:
            out_file = DATA_DIR / f"{ep_name}_results.csv"
            
            cmd = [
                sys.executable,
                "-u",
                str(script_path),
                "--function-prefix",
                function_prefix,
                "--path",
                path,
                "--warm-requests",
                str(warm_requests),
                "--region",
                region,
                "--out",
                str(out_file),
            ]
            if method == "POST":
                cmd.extend(["--method", "POST"])
                if body:
                    cmd.extend(["--body", body])
            
            cmd.append("--memory")
            cmd.extend([str(m) for m in mem_list])

            yield f"data: {json.dumps({'type': 'cmd', 'text': '$ ' + ' '.join(cmd)})}\n\n"
            yield f"data: {json.dumps({'type': 'info', 'text': f'Running real benchmark for {ep_name.upper()} endpoint...'})}\n\n"

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'text': f'Failed to start benchmark subprocess: {str(e)}'})}\n\n"
                success = False
                break

            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').rstrip()
                if not text.strip():
                    continue

                msg_type = "log"
                if text.startswith("==="):
                    msg_type = "progress"
                elif "Saved results to" in text:
                    msg_type = "success"
                elif "WARNING:" in text:
                    msg_type = "error"
                
                yield f"data: {json.dumps({'type': msg_type, 'text': text})}\n\n"

            await process.wait()
            if process.returncode != 0:
                yield f"data: {json.dumps({'type': 'error', 'text': f'Benchmark process exited with code {process.returncode}'})}\n\n"
                success = False
                break
            else:
                yield f"data: {json.dumps({'type': 'success', 'text': f'Successfully generated and saved {ep_name}_results.csv'})}\n\n"

        if not success:
            yield f"data: {json.dumps({'type': 'error', 'text': 'Real benchmark run failed. The dashboard was not updated because new metrics could not be generated. Please deploy your SAM stack using sam deploy --guided first.'})}\n\n"
            yield f"data: {json.dumps({'type': 'failed'})}\n\n"
            return

        # After all steps, load the freshly-measured CSVs into the store so the
        # real data persists across page refreshes, then recalculate analytics.
        try:
            _data_store = load_default_data()
            analytics = compute_analytics(_data_store)
            yield f"data: {json.dumps({'type': 'done', 'analytics': analytics})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Failed to load/compute updated analytics: {str(e)}'})}\n\n"
            yield f"data: {json.dumps({'type': 'failed'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{endpoint}")
async def download_csv(endpoint: str):
    rows = _data_store.get(endpoint, [])
    if not rows:
        raise HTTPException(status_code=404, detail="No data for endpoint")

    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output = io.BytesIO(si.getvalue().encode())

    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={endpoint}_results.csv"},
    )
