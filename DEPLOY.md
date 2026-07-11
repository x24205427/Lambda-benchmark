# Deployment Guide

This project has **two independently deployed pieces**, both on AWS:

| Piece | What it is | How it deploys |
|-------|-----------|----------------|
| `app/` | FastAPI benchmark **target** (6 Lambda functions, one per memory tier) | **AWS SAM** → CloudFormation stack `memory-benchmark` |
| `dashboard/` | FastAPI + Jinja2 web UI (live benchmark, CSV upload, charts) | **Container → AWS App Runner** |

> **Why not GitHub Pages?** The dashboard is a Python server, not a static
> site. It renders templates server-side, streams Server-Sent Events, accepts
> CSV uploads, and shells out to `benchmark/benchmark.py` (which calls AWS with
> `boto3`). GitHub Pages can only serve static files, so it can't host this app.
> `app/` is Lambda-only by nature. Hence: **both pieces run on AWS.**

---

## Prerequisites

- **AWS CLI** configured: `aws configure` (or SSO) with a region, e.g. `eu-west-1`
- **AWS SAM CLI**: `sam --version`
- **Docker**: `docker --version`
- Permissions to deploy CloudFormation, Lambda, DynamoDB, ECR, and App Runner

---

## Part 1 — Deploy the benchmark target (`app/`) with SAM

This is your existing SAM setup (`template.yaml` + `samconfig.toml`, stack
`memory-benchmark`, region `eu-west-1`).

```bash
# from the repo root
sam build
sam deploy            # uses samconfig.toml; add --guided the first time
```

Note the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name memory-benchmark \
  --query "Stacks[0].Outputs" --output table
```

- `FunctionNames` → `memory-benchmark-mem128 … memory-benchmark-mem3008`
  (the dashboard's **SAM Stack Name** field = `memory-benchmark`)
- `ApiBaseUrl` → the HTTP API base URL (handy for a manual smoke test)

Smoke test one function directly:

```bash
aws lambda invoke --function-name memory-benchmark-mem512 \
  --payload '{"version":"2.0","rawPath":"/prod/mem512/health","requestContext":{"http":{"method":"GET","path":"/prod/mem512/health","sourceIp":"1.2.3.4"},"stage":"prod"},"headers":{}}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

Expect `"statusCode": 200`.

---

## Part 2 — Deploy the dashboard (`dashboard/`) to App Runner

The `Dockerfile` and `.dockerignore` live at the repo root. Build context is the
repo root because the dashboard invokes `benchmark/benchmark.py` at runtime, so
both directories are copied into the image.

Set shell variables (adjust region / account):

```bash
export AWS_REGION=eu-west-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REPO=lambda-benchmark-dashboard
export IMAGE_URI=$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest
```

### 2a. Build & push the image to ECR

```bash
# create the repo once (ignore error if it already exists)
aws ecr create-repository --repository-name $ECR_REPO --region $AWS_REGION || true

# log docker in to ECR
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

# build for the amd64 platform App Runner runs (important on Apple Silicon)
docker build --platform linux/amd64 -t $ECR_REPO:latest .
docker tag $ECR_REPO:latest $IMAGE_URI
docker push $IMAGE_URI
```

### 2b. Create the IAM roles App Runner needs

**Access role** — lets App Runner pull the image from ECR:

```bash
aws iam create-role --role-name AppRunnerECRAccessRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'
aws iam attach-role-policy --role-name AppRunnerECRAccessRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
```

**Instance role** — the running container's identity. This is what lets the
dashboard's "Run Benchmark" button invoke and reconfigure the Lambda functions.
The policy (`deploy/apprunner-instance-policy.json`) grants exactly:
`lambda:GetFunctionConfiguration`, `UpdateFunctionConfiguration`, `InvokeFunction`
scoped to `memory-benchmark-mem*`.

```bash
aws iam create-role --role-name DashboardInstanceRole \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"tasks.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'
aws iam put-role-policy --role-name DashboardInstanceRole \
  --policy-name BenchmarkInvoke \
  --policy-document file://deploy/apprunner-instance-policy.json
```

### 2c. Create the App Runner service

```bash
aws apprunner create-service \
  --service-name lambda-benchmark-dashboard \
  --source-configuration '{
    "AuthenticationConfiguration": {
      "AccessRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/AppRunnerECRAccessRole"
    },
    "AutoDeploymentsEnabled": false,
    "ImageRepository": {
      "ImageIdentifier": "'"$IMAGE_URI"'",
      "ImageRepositoryType": "ECR",
      "ImageConfiguration": { "Port": "8080" }
    }
  }' \
  --instance-configuration '{
    "Cpu": "1024",
    "Memory": "2048",
    "InstanceRoleArn": "arn:aws:iam::'"$ACCOUNT_ID"':role/DashboardInstanceRole"
  }' \
  --health-check-configuration '{"Protocol":"HTTP","Path":"/","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":5}' \
  --region $AWS_REGION
```

Get the public URL when the service reaches `RUNNING`:

```bash
aws apprunner list-services --region $AWS_REGION \
  --query "ServiceSummaryList[?ServiceName=='lambda-benchmark-dashboard'].ServiceUrl" \
  --output text
```

Open `https://<that-url>/`. The dashboard starts **empty** (real data only).
Click **⚡ Run Benchmark — Live AWS Lambda**, confirm the SAM stack name
(`memory-benchmark`) and region, and run — results stream in from the live
functions and the charts populate. You can also **Upload CSV** with output from
`benchmark/benchmark.py` run locally.

---

## Updating a deployment

- **Benchmark target (`app/`):** edit code → `sam build && sam deploy`.
- **Dashboard (`dashboard/`):** rebuild & push the image, then trigger a deploy:

  ```bash
  docker build --platform linux/amd64 -t $ECR_REPO:latest .
  docker tag $ECR_REPO:latest $IMAGE_URI
  docker push $IMAGE_URI

  SERVICE_ARN=$(aws apprunner list-services --region $AWS_REGION \
    --query "ServiceSummaryList[?ServiceName=='lambda-benchmark-dashboard'].ServiceArn" --output text)
  aws apprunner start-deployment --service-arn $SERVICE_ARN --region $AWS_REGION
  ```

  (Or set `"AutoDeploymentsEnabled": true` so a fresh `:latest` push redeploys automatically.)

---

## Running the dashboard locally (optional)

```bash
cd dashboard
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

The live-benchmark button works locally too, using whatever AWS credentials are
in your environment (`aws configure`).

---

## Cost / teardown

- **App Runner** bills for provisioned + active container time. To stop:
  `aws apprunner delete-service --service-arn <arn> --region $AWS_REGION`
- **SAM stack:** `sam delete --stack-name memory-benchmark`
- **ECR images:** delete the repo `lambda-benchmark-dashboard` when done.
