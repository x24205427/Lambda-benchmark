# ─────────────────────────────────────────────────────────────
# Dashboard container (AWS App Runner / any container host)
#
# The dashboard shells out to benchmark/benchmark.py at runtime
# (BASE_DIR.parent/benchmark/benchmark.py), so BOTH the dashboard
# and benchmark directories must be present. Build context = repo root.
# ─────────────────────────────────────────────────────────────
FROM python:3.13-slim

# Faster, quieter, no .pyc clutter
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Install dashboard deps (boto3 included → live benchmark works in-container)
COPY dashboard/requirements.txt ./dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt

# App code: dashboard (served) + benchmark (invoked as a subprocess)
COPY dashboard/ ./dashboard/
COPY benchmark/ ./benchmark/

# App Runner sends traffic to $PORT (defaults to 8080)
ENV PORT=8080
EXPOSE 8080

WORKDIR /srv/dashboard
# Shell form so $PORT is expanded at runtime
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
