#!/bin/bash
set -euo pipefail

# -------- DEFAULT SETTINGS --------
CLUSTER_NAME="patient-pipeline-cluster"
TASK_NAME="patient-pipeline-task"
SUBNET_ID="subnet-9b61e5ba"
SECURITY_GROUP_ID="sg-09be1dde75a78c79a"
REGION="us-east-1"
LOG_GROUP="/ecs/patient-pipeline"
ENV_FILE="../ec2_deployment/env.list"
SECRET_NAME="openai/api-key"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_OUTPUT_FILE="logs/ecs_task_${TIMESTAMP}.txt"
mkdir -p logs

# -------- PARSE ARGS --------
while [[ $# -gt 0 ]]; do
  case $1 in
    --subnet-id) SUBNET_ID="$2"; shift 2;;
    --security-group-id) SECURITY_GROUP_ID="$2"; shift 2;;
    --env-file) ENV_FILE="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --task-name) TASK_NAME="$2"; shift 2;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2;;
    *) echo "❌ Unknown option: $1"; exit 1;;
  esac
done

# -------- FETCH SECRET --------
echo "🔐 Retrieving OPENAI_API_KEY from Secrets Manager..."
OPENAI_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_NAME" \
  --query SecretString \
  --output text)

if [[ -z "$OPENAI_API_KEY" ]]; then
  echo "❌ Failed to retrieve OPENAI_API_KEY from Secrets Manager."
  exit 1
fi

# -------- ENVIRONMENT VARIABLES --------
echo "📦 Reading environment variables from $ENV_FILE..."
ENV_VARS=()
ENV_VARS+=("{\"name\":\"OPENAI_API_KEY\",\"value\":\"$OPENAI_API_KEY\"}")

while IFS='=' read -r key value || [ -n "$key" ]; do
  [[ "$key" =~ ^#.*$ || -z "$key" || "$key" == "OPENAI_API_KEY" ]] && continue
  ENV_VARS+=("{\"name\":\"$key\",\"value\":\"$value\"}")
done < "$ENV_FILE"

ENV_JSON=$(IFS=, ; echo "[${ENV_VARS[*]}]")

# -------- RUN TASK --------
echo "🚀 Launching ECS Fargate task..."

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER_NAME" \
  --launch-type FARGATE \
  --task-definition "$TASK_NAME" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SECURITY_GROUP_ID],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"patient-pipeline\",\"environment\":$ENV_JSON}]}" \
  --region "$REGION" \
  --query "tasks[0].taskArn" \
  --output text)

if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "❌ Failed to launch ECS Fargate task."
  exit 1
fi

TASK_ID=$(basename "$TASK_ARN")
echo "✅ Task launched: $TASK_ARN"
echo "🆔 Task ID: $TASK_ID"

# -------- WAIT FOR LOG STREAM --------
MAX_ATTEMPTS=12
SLEEP_SECONDS=10
ATTEMPT=1
LOG_STREAM=""

echo "⏳ Waiting for CloudWatch logs..."

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
  LOG_STREAM=$(aws logs describe-log-streams \
    --log-group-name "$LOG_GROUP" \
    --order-by LastEventTime \
    --descending \
    --limit 1 \
    --region "$REGION" \
    --query "logStreams[0].logStreamName" \
    --output text 2>/dev/null)

  if [[ "$LOG_STREAM" != "None" && "$LOG_STREAM" != "null" && -n "$LOG_STREAM" ]]; then
    break
  fi

  echo "🔄 Attempt $ATTEMPT/$MAX_ATTEMPTS: Waiting for logs..."
  sleep $SLEEP_SECONDS
  ((ATTEMPT++))
done

if [[ "$LOG_STREAM" == "None" || -z "$LOG_STREAM" ]]; then
  echo "❌ Log stream not found. Please check CloudWatch manually."
  exit 1
fi

echo "📄 Log stream ready: $LOG_STREAM"

# -------- FETCH LOG EVENTS --------
echo "📥 Fetching latest CloudWatch logs..."

aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --region "$REGION" \
  --limit 100 \
  --query "events[*].message" \
  --output text > "$LOG_OUTPUT_FILE"

cat "$LOG_OUTPUT_FILE"
echo "✅ Logs saved to $LOG_OUTPUT_FILE"
