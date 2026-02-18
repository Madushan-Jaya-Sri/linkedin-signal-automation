#!/bin/bash
set -e

# ─── Configuration ───────────────────────────────────────────
REGION="us-east-2"
FUNCTION_NAME="linkedin-lead-intel"
ECR_REPO_NAME="linkedin-lead-intel"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO_NAME}"
ROLE_NAME="lambda-linkedin-lead-intel-role"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Load env vars from .env file
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

echo "══════════════════════════════════════════════════════════"
echo "  Deploying LinkedIn Lead Intelligence to AWS Lambda"
echo "  Region: ${REGION}  Account: ${ACCOUNT_ID}"
echo "══════════════════════════════════════════════════════════"

# ─── Step 1: Create ECR Repository ──────────────────────────
echo ""
echo "▶ Step 1: Creating ECR repository..."
aws ecr create-repository \
  --repository-name "${ECR_REPO_NAME}" \
  --region "${REGION}" \
  --image-scanning-configuration scanOnPush=false \
  2>/dev/null || echo "  (repository already exists)"

# ─── Step 2: Build & Push Docker Image ──────────────────────
echo ""
echo "▶ Step 2: Building Docker image..."
docker build --platform linux/amd64 -t "${ECR_REPO_NAME}" .

echo "  Logging into ECR..."
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "  Pushing image to ECR..."
docker tag "${ECR_REPO_NAME}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

# ─── Step 3: Create IAM Role ────────────────────────────────
echo ""
echo "▶ Step 3: Creating Lambda execution role..."
TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

aws iam create-role \
  --role-name "${ROLE_NAME}" \
  --assume-role-policy-document "${TRUST_POLICY}" \
  2>/dev/null || echo "  (role already exists)"

aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" \
  2>/dev/null || true

# Wait for role to propagate
echo "  Waiting for role to propagate..."
sleep 10

# ─── Step 4: Create or Update Lambda Function ───────────────
echo ""
echo "▶ Step 4: Creating Lambda function..."

# Check if function exists
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" 2>/dev/null; then
  echo "  Updating existing function..."
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --image-uri "${ECR_URI}:latest" \
    --region "${REGION}" > /dev/null

  # Wait for update to complete
  echo "  Waiting for update..."
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${REGION}"

  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --timeout 900 \
    --memory-size 512 \
    --environment "Variables={APIFY_API_TOKEN=${APIFY_API_TOKEN},OPENAI_API_KEY=${OPENAI_API_KEY},AWS_LWA_INVOKE_MODE=response_stream}" \
    --region "${REGION}" > /dev/null
else
  echo "  Creating new function..."
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --package-type Image \
    --code "ImageUri=${ECR_URI}:latest" \
    --role "${ROLE_ARN}" \
    --timeout 900 \
    --memory-size 512 \
    --environment "Variables={APIFY_API_TOKEN=${APIFY_API_TOKEN},OPENAI_API_KEY=${OPENAI_API_KEY},AWS_LWA_INVOKE_MODE=response_stream}" \
    --region "${REGION}" > /dev/null

  # Wait for function to be active
  echo "  Waiting for function to become active..."
  aws lambda wait function-active-v2 --function-name "${FUNCTION_NAME}" --region "${REGION}"
fi

# ─── Step 5: Create Function URL with Streaming ─────────────
echo ""
echo "▶ Step 5: Setting up Function URL with streaming..."

aws lambda create-function-url-config \
  --function-name "${FUNCTION_NAME}" \
  --auth-type NONE \
  --invoke-mode RESPONSE_STREAM \
  --region "${REGION}" \
  2>/dev/null || echo "  (function URL already exists)"

# Add public access permission
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "FunctionURLAllowPublicAccess" \
  --action "lambda:InvokeFunctionUrl" \
  --principal "*" \
  --function-url-auth-type NONE \
  --region "${REGION}" \
  2>/dev/null || true

# ─── Done! ──────────────────────────────────────────────────
FUNCTION_URL=$(aws lambda get-function-url-config \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query 'FunctionUrl' --output text)

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo ""
echo "  Live URL: ${FUNCTION_URL}"
echo "══════════════════════════════════════════════════════════"
