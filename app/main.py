import os
import time
import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from mangum import Mangum

# OPTIMIZATION: Disabled OpenAPI docs generation to reduce cold start latency
app = FastAPI(
    title="Memory Allocation Benchmark API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

TABLE_NAME = os.environ.get("DDB_TABLE_NAME", "")
_dynamodb_resource = None


def get_table():
    """Lazily create the DynamoDB resource/table handle (kept warm across invocations)."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        # OPTIMIZATION: Lazy import of boto3 to eliminate heavy import overhead 
        # for non-database endpoints (e.g., /health and /transform)
        import boto3
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource.Table(TABLE_NAME)


# ---------------------------------------------------------------------------
# (i) Health endpoint - CPU bound
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Lightweight CPU-bound endpoint: no I/O, minimal work, fast to return."""
    start = time.perf_counter()
    # tiny CPU-bound workload so memory/CPU allocation has something to measure
    total = sum(i * i for i in range(500000))
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    return {
        "status": "ok",
        "checksum": total,
        "compute_time_ms": elapsed_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# (ii) JSON transformation endpoint - parse and serialise
# ---------------------------------------------------------------------------
class TransformRequest(BaseModel):
    records: list[dict] = Field(..., description="List of arbitrary JSON records to transform")


@app.post("/transform")
def transform(payload: TransformRequest):
    """Parses, validates (Pydantic), and reserialises a batch of JSON records."""
    start = time.perf_counter()
    transformed = []
    for record in payload.records:
        transformed.append(
            {
                "id": str(uuid.uuid4()),
                "original_keys": sorted(record.keys()),
                "field_count": len(record),
                "data": record,
            }
        )
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    return {
        "count": len(transformed),
        "results": transformed,
        "compute_time_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# (iii) DynamoDB read endpoint - I/O bound
# ---------------------------------------------------------------------------
@app.get("/items/{item_id}")
def read_item(item_id: str):
    """I/O-bound endpoint: reads a single item from DynamoDB."""
    if not TABLE_NAME:
        raise HTTPException(status_code=500, detail="DDB_TABLE_NAME not configured")

    start = time.perf_counter()
    table = get_table()
    response = table.get_item(Key={"item_id": item_id})
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)

    item = response.get("Item")
    if item is None:
        # seed a deterministic synthetic item on first read so benchmarks are repeatable
        item = {
            "item_id": item_id,
            "payload": "synthetic-benchmark-data",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        table.put_item(Item=item)

    return {
        "item": item,
        "io_time_ms": elapsed_ms,
    }


@app.get("/")
def root():
    return {"message": "Memory Allocation Benchmark API", "endpoints": ["/health", "/transform", "/items/{item_id}"]}

MEM_TAG = os.environ.get("MEM_TAG", "")
_mangum_handler = Mangum(app)


def handler(event, context):
    print(json.dumps({"debug": "incoming_event", "MEM_TAG": MEM_TAG, "event": event}))

    if MEM_TAG:
        prefix = f"/{MEM_TAG}"
        http = event.get("requestContext", {}).get("http")
        if http is not None:
            stage = event.get("requestContext", {}).get("stage", "")
            full_prefix = f"/{stage}{prefix}" if stage and stage != "$default" else prefix
            path = http.get("path", "")
            new_path = path[len(full_prefix):] if path.startswith(full_prefix) else path
            http["path"] = new_path or "/"
        else:
            path = event.get("path", "")
            new_path = path[len(prefix):] if path.startswith(prefix) else path
            event["path"] = new_path or "/"
            if event.get("resource", "").startswith(prefix):
                event["resource"] = event["resource"][len(prefix):] or "/"

    response = _mangum_handler(event, context)
    print(json.dumps({"debug": "response", "statusCode": response.get("statusCode")}))
    return response