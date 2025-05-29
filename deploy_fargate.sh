#!/bin/bash

# -------- SETTINGS --------
CLUSTER_NAME="patient-pipeline-cluster"
TASK_NAME="patient-pipeline-task"
SUBNET_ID="subnet-9b61e5ba"
SECURITY_GROUP_ID="sg-09be1dde75a78c79a"
REGION="us-east-1"
LOG_GROUP="/ecs/patient-pipeline-task"
ENV_FILE="../env.list"
SECRET_NAME="openai-api-key"

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

# -------- ENV PARSING --------
echo "🔍 Reading environment variables from $ENV_FILE..."
ENV_VARS=()

# Inject OPENAI_API_KEY securely
ENV_VARS+=("{\"name\":\"OPENAI_API_KEY\",\"value\":\"$OPENAI_API_KEY\"}")

# Parse remaining env.list (excluding OPENAI_API_KEY)
while IFS='=' read -r key value || [ -n "$key" ]; do
  if [[ "$key" != "OPENAI_API_KEY" && -n "$key" && -n "$value" ]]; then
    ENV_VARS+=("{\"name\":\"$key\",\"value\":\"$value\"}")
  fi
done < "$ENV_FILE"

ENV_JSON=$(IFS=, ; echo "[${ENV_VARS[*]}]")

# -------- RUN TASK --------
echo "⏳ Running ECS Fargate task..."

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER_NAME" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_ID],securityGroups=[$SECURITY_GROUP_ID],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"patient-pipeline\",\"environment\":$ENV_JSON}]}" \
  --task-definition "$TASK_NAME" \
  --region "$REGION" \
  --query "tasks[0].taskArn" \
  --output text)

if [[ -z "$TASK_ARN" ]]; then
  echo "❌ Failed to start ECS task."
  exit 1
fi

echo "✅ Task launched: $TASK_ARN"
TASK_ID=$(basename "$TASK_ARN")
echo "🆔 Task ID: $TASK_ID"

# -------- WAIT FOR LOGS --------
MAX_ATTEMPTS=10
SLEEP_SECONDS=10
ATTEMPT=1
LOG_STREAM=""

echo "⏳ Checking for CloudWatch log stream..."

while [ $ATTEMPT -le $MAX_ATTEMPTS ]; do
  LOG_STREAM=$(aws logs describe-log-streams \
    --log-group-name "$LOG_GROUP" \
    --order-by LastEventTime \
    --descending \
    --limit 1 \
    --query "logStreams[0].logStreamName" \
    --output text 2>/dev/null)

  if [[ "$LOG_STREAM" != "None" && "$LOG_STREAM" != "null" && "$LOG_STREAM" != "" ]]; then
    break
  fi

  echo "🔄 Waiting for logs (Attempt $ATTEMPT/$MAX_ATTEMPTS)..."
  sleep $SLEEP_SECONDS
  ((ATTEMPT++))
done

if [[ "$LOG_STREAM" == "" || "$LOG_STREAM" == "None" || "$LOG_STREAM" == "null" ]]; then
  echo "❌ Log stream not found after $((MAX_ATTEMPTS * SLEEP_SECONDS)) seconds."
  echo "You can manually check logs in CloudWatch under: $LOG_GROUP"
  exit 1
fi

echo "📄 Log stream: $LOG_STREAM"

# -------- PRINT LOGS --------
echo "📥 Fetching logs from CloudWatch..."
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --region "$REGION" \
  --limit 50 \
  --query "events[*].message" \
  --output text
