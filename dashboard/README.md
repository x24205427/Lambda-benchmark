# AWS Lambda Memory Benchmark Dashboard

A professional FastAPI + Jinja2 dashboard for analyzing AWS Lambda memory benchmark results.

## Quick Start

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000

## Features

- 📊 **Dashboard** — KPI cards, response time chart, cold start comparison, cost area chart, insights
- 💚 **Health Endpoint** — Dedicated latency + billed duration charts + sortable table
- 🔄 **Transform Endpoint** — Latency + cost charts + sortable table
- 💰 **Cost Analysis** — Area charts + performance heatmap
- 🚀 **Performance** — Radar chart (weighted score) + bubble scatter plot + sortable table
- ❄️ **Cold Start Analysis** — Init time comparison + cold vs warm chart
- 📋 **Raw Data** — Full searchable/sortable data table + CSV download

The dashboard starts empty and only ever displays **real measured data**. Populate
it by either running a live benchmark (button below) or uploading benchmark CSVs.

## Run Benchmark Button (Live AWS)

Click **"⚡ Run Benchmark — Live AWS Lambda"** to:
1. Enter your SAM stack name, region, warm-invocation count and memory tiers
2. Invoke the deployed Lambda functions and stream real execution output
3. Dashboard updates automatically from the freshly measured results

Requires a deployed SAM stack and configured local AWS credentials.

## To Run Real Benchmarks from the CLI

```bash
cd benchmark/
python benchmark.py --function-prefix memory-benchmark \
  --memory 128 256 512 1024 2048 3008 \
  --path /health --warm-requests 50 \
  --region eu-west-1 --out health_results.csv

python benchmark.py --function-prefix memory-benchmark \
  --memory 128 256 512 1024 2048 3008 \
  --path /transform --method POST \
  --body '{"records": [{"a": 1, "b": 2}, {"x": 99, "y": 100}]}' \
  --warm-requests 50 --region eu-west-1 --out transform_results.csv

python benchmark.py --function-prefix memory-benchmark \
  --memory 128 256 512 1024 2048 3008 \
  --path /items/bench-1 --warm-requests 50 \
  --region eu-west-1 --out io_results.csv
```

Then upload the CSVs using the **Upload CSV** button in the dashboard.

## Technology Stack

- **Backend**: Python · FastAPI · Jinja2
- **Frontend**: Vanilla JS · Chart.js · PapaParse
- **Streaming**: Server-Sent Events (SSE)
- **Data**: Real benchmark CSVs, produced by live AWS Lambda runs or uploaded manually
