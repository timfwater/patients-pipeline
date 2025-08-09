#!/usr/bin/env bash
set -euo pipefail

# Load config
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# If IMAGE_URI not set, try using the last built image
if [[ -z "${IMAGE_URI:-}" && -f "$ROOT_DIR/.last_image_uri" ]]; then
  IMAGE_URI="$(cat "$ROOT_DIR/.last_image_uri")"
  echo "ℹ️  Using IMAGE_URI from .last_image_uri -> $IMAGE_URI"
fi

# ---- compat + defaults ----
: "${CLUSTER_NAME:=${ECS_CLUSTER_NAME:-}}"
: "${SUBNET_IDS:=${FARGATE_SUBNET_IDS:-}}"
: "${SECURITY_GROUP_ID:=${FARGATE_SECURITY_GROUP_IDS%%,*}}"
: "${CONTAINER_NAME:=${CONTAINER_NAME:-patient-pipeline}}"

# Resolve execution role ARN (prefer explicit ARN)
if [[ -n "${EXECUTION_ROLE_ARN:-}" ]]; then
  RESOLVED_EXEC_ROLE_ARN="$EXECUTION_ROLE_ARN"
elif [[ -n "${EXECUTION_ROLE_NAME:-}" ]]; then
  RESOLVED_EXEC_ROLE_ARN="$(aws iam get-role --role-name "$EXECUTION_ROLE_NAME" --query 'Role.Arn' --output text)"
else
  echo "❌ Missing EXECUTION_ROLE_ARN or EXECUTION_ROLE_NAME in config.env"; exit 1
fi

# Resolve task role ARN (prefer explicit ARN)
if [[ -n "${TASK_ROLE_ARN:-}" ]]; then
  RESOLVED_TASK_ROLE_ARN="$TASK_ROLE_ARN"
elif [[ -n "${TASK_ROLE_NAME:-}" ]]; then
  RESOLVED_TASK_ROLE_ARN="$(aws iam get-role --role-name "$TASK_ROLE_NAME" --query 'Role.Arn' --output text)"
else
  echo "❌ Missing TASK_ROLE_ARN or TASK_ROLE_NAME in config.env"; exit 1
fi

# Resolve image URI (accept IMAGE_URI or ECR_REPO_URI; add :latest if no tag present)
if [[ -z "${IMAGE_URI:-}" ]]; then
  if [[ -n "${ECR_REPO_URI:-}" ]]; then
    IMAGE_URI="$ECR_REPO_URI"
  else
    echo "❌ Provide IMAGE_URI or ECR_REPO_URI in config.env (or ensure .last_image_uri exists)"; exit 1
  fi
fi
[[ "$IMAGE_URI" == *:* ]] || IMAGE_URI="${IMAGE_URI}:latest"

# Helpers
jq_bin() { command -v jq >/dev/null 2>&1 && echo jq || { echo "jq is required"; exit 1; }; }
JQ=$(jq_bin)

echo "🚀 Deploying task to Fargate in $AWS_REGION"

# -------- Preflight: cluster exists? --------
if ! aws ecs describe-clusters --clusters "$CLUSTER_NAME" --region "$AWS_REGION" --query 'clusters[0].status' --output text >/dev/null 2>&1; then
  echo "ℹ️  ECS cluster '$CLUSTER_NAME' not found; creating..."
  aws ecs create-cluster --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
fi

# -------- Preflight: log group exists? --------
if ! aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$AWS_REGION" --query 'logGroups[?logGroupName==`'"$LOG_GROUP"'`]' --output text | grep -q "$LOG_GROUP"; then
  echo "ℹ️  Creating CloudWatch Logs group: $LOG_GROUP"
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION"
fi

# ---- validate core app inputs ----
missing=()
[[ -z "${OUTPUT_S3:-}" ]] && missing+=("OUTPUT_S3")
[[ -z "${THRESHOLD:-}" ]] && missing+=("THRESHOLD")
[[ -z "${START_DATE:-}" ]] && missing+=("START_DATE")
[[ -z "${END_DATE:-}" ]] && missing+=("END_DATE")
[[ -z "${EMAIL_FROM:-}" ]] && missing+=("EMAIL_FROM")
[[ -z "${EMAIL_TO:-}" ]] && missing+=("EMAIL_TO")
if [[ -z "${PHYSICIAN_ID:-}" && -z "${PHYSICIAN_ID_LIST:-}" ]]; then
  missing+=("PHYSICIAN_ID or PHYSICIAN_ID_LIST")
fi
if ((${#missing[@]})); then
  echo "❌ Missing required config: ${missing[*]}"; exit 1
fi

# ---- derive INPUT_S3 if not provided ----
if [[ -z "${INPUT_S3:-}" ]]; then
  if [[ -n "${S3_INPUT_BUCKET:-}" && -n "${INPUT_FILE:-}" ]]; then
    INPUT_S3="s3://${S3_INPUT_BUCKET}/Input/${INPUT_FILE}"
    echo "ℹ️  Derived INPUT_S3=${INPUT_S3}"
  else
    echo "❌ Provide INPUT_S3 or both S3_INPUT_BUCKET and INPUT_FILE"; exit 1
  fi
fi

# ---- container environment to inject ----
ENV_JSON=$(jq -n \
  --arg AWS_REGION        "${AWS_REGION}" \
  --arg INPUT_S3          "${INPUT_S3}" \
  --arg OUTPUT_S3         "${OUTPUT_S3}" \
  --arg PHYSICIAN_ID      "${PHYSICIAN_ID:-}" \
  --arg PHYSICIAN_ID_LIST "${PHYSICIAN_ID_LIST:-}" \
  --arg THRESHOLD         "${THRESHOLD}" \
  --arg START_DATE        "${START_DATE}" \
  --arg END_DATE          "${END_DATE}" \
  --arg EMAIL_FROM        "${EMAIL_FROM}" \
  --arg EMAIL_TO          "${EMAIL_TO}" '
  [
    {name:"AWS_REGION",        value:$AWS_REGION},
    {name:"INPUT_S3",          value:$INPUT_S3},
    {name:"OUTPUT_S3",         value:$OUTPUT_S3},
    {name:"PHYSICIAN_ID",      value:$PHYSICIAN_ID},
    {name:"PHYSICIAN_ID_LIST", value:$PHYSICIAN_ID_LIST},
    {name:"THRESHOLD",         value:$THRESHOLD},
    {name:"START_DATE",        value:$START_DATE},
    {name:"END_DATE",          value:$END_DATE},
    {name:"EMAIL_FROM",        value:$EMAIL_FROM},
    {name:"EMAIL_TO",          value:$EMAIL_TO}
  ]')

# ---- optional: secrets wiring ----
SECRETS_JSON="[]"
if [[ -n "${OPENAI_API_KEY_SECRET_NAME:-}" ]]; then
  if [[ "$OPENAI_API_KEY_SECRET_NAME" == arn:* ]]; then
    SECRET_ARN="$OPENAI_API_KEY_SECRET_NAME"
  else
    SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$OPENAI_API_KEY_SECRET_NAME" \
                 --query 'ARN' --output text --region "$AWS_REGION")
  fi
  SECRETS_JSON=$(jq -n --arg arn "$SECRET_ARN" '[{name:"OPENAI_API_KEY", valueFrom:$arn}]')
fi

# -------- Build task definition from template --------
TEMPLATE="$ROOT_DIR/fargate_deployment/templates/task-def-template.json"
FINAL_TD="$ROOT_DIR/fargate_deployment/final-task-def.json"
[[ -f "$TEMPLATE" ]] || { echo "❌ Missing $TEMPLATE"; exit 1; }

echo "🧩 Rendering task definition..."
cat "$TEMPLATE" | $JQ \
  --arg family "$TASK_FAMILY" \
  --arg image "$IMAGE_URI" \
  --arg execRole "$RESOLVED_EXEC_ROLE_ARN" \
  --arg taskRole "$RESOLVED_TASK_ROLE_ARN" \
  --arg logGroup "$LOG_GROUP" \
  --arg logPrefix "$LOG_STREAM_PREFIX" \
  --arg logRegion "$AWS_REGION" \
  --argjson env "$ENV_JSON" \
  --argjson secrets "$SECRETS_JSON" '
  .family=$family
  | .executionRoleArn=$execRole
  | .taskRoleArn=$taskRole
  | .containerDefinitions[0].image=$image
  | .containerDefinitions[0].name="'$CONTAINER_NAME'"
  | .containerDefinitions[0].environment=$env
  | .containerDefinitions[0].secrets=$secrets
  | .containerDefinitions[0].logConfiguration.options["awslogs-group"]=$logGroup
  | .containerDefinitions[0].logConfiguration.options["awslogs-stream-prefix"]=$logPrefix
  | .containerDefinitions[0].logConfiguration.options["awslogs-region"]=$logRegion
' > "$FINAL_TD"

echo "📦 Registering task definition..."
TD_ARN=$(aws ecs register-task-definition \
  --cli-input-json "file://$FINAL_TD" \
  --region "$AWS_REGION" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)
echo "✅ Registered: $TD_ARN"

# -------- Network config --------
IFS=',' read -r -a SUBNET_ARRAY <<< "$SUBNET_IDS"

# ---- Auto-pick ASSIGN_PUBLIC_IP if not provided (or set to AUTO) ----
AUTO_PUBLIC="DISABLED"
if [[ -z "${ASSIGN_PUBLIC_IP:-}" || "${ASSIGN_PUBLIC_IP}" == "AUTO" ]]; then
  for sn in "${SUBNET_ARRAY[@]}"; do
    VPC_ID=$(aws ec2 describe-subnets --subnet-ids "$sn" --region "$AWS_REGION" --query 'Subnets[0].VpcId' --output text)
    RT_IDS=$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$sn" --region "$AWS_REGION" --query 'RouteTables[].RouteTableId' --output text)
    if [[ -z "$RT_IDS" || "$RT_IDS" == "None" ]]; then
      RT_IDS=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" --region "$AWS_REGION" --query 'RouteTables[].RouteTableId' --output text)
    fi
    IGW=$(aws ec2 describe-route-tables --route-table-ids $RT_IDS --region "$AWS_REGION" \
      --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0` && GatewayId!=null].GatewayId' --output text || true)
    if [[ -n "$IGW" ]]; then
      AUTO_PUBLIC="ENABLED"
      break
    fi
  done
  ASSIGN_PUBLIC_IP="$AUTO_PUBLIC"
  echo "ℹ️  ASSIGN_PUBLIC_IP auto-set to: $ASSIGN_PUBLIC_IP"
fi

# Optional sanity check for internet path when ASSIGN_PUBLIC_IP=ENABLED
if [[ "${ASSIGN_PUBLIC_IP:-DISABLED}" == "ENABLED" ]]; then
  echo "🔎 Quick network sanity check (public internet path)..."
  for sn in "${SUBNET_ARRAY[@]}"; do
    RTB_IDS=$(aws ec2 describe-route-tables --region "$AWS_REGION" \
      --filters "Name=association.subnet-id,Values=$sn" \
      --query 'RouteTables[].RouteTableId' --output text)
    if [[ -z "$RTB_IDS" || "$RTB_IDS" == "None" ]]; then
      VPC_ID=$(aws ec2 describe-subnets --subnet-ids "$sn" --region "$AWS_REGION" --query 'Subnets[0].VpcId' --output text)
      RTB_IDS=$(aws ec2 describe-route-tables --region "$AWS_REGION" \
        --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
        --query 'RouteTables[].RouteTableId' --output text)
    fi
    HAS_IGW=$(aws ec2 describe-route-tables --route-table-ids $RTB_IDS --region "$AWS_REGION" \
      --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0` && GatewayId!=null].GatewayId' --output text || true)
    if [[ -z "$HAS_IGW" ]]; then
      echo "⚠️  Subnet $sn does not appear to route 0.0.0.0/0 to an Internet Gateway. ECR pulls may fail."
    fi
  done
else
  echo "ℹ️  Using private subnets (assignPublicIp=DISABLED). Ensure a NAT + (optional) VPC endpoints exist for:"
  echo "    - com.amazonaws.${AWS_REGION}.ecr.api"
  echo "    - com.amazonaws.${AWS_REGION}.ecr.dkr"
  echo "    - com.amazonaws.${AWS_REGION}.logs"
  echo "    - com.amazonaws.${AWS_REGION}.secretsmanager (if using secrets)"
fi

# -------- Run task --------
echo "🚀 Running task with assignPublicIp=${ASSIGN_PUBLIC_IP:-DISABLED}"
RUN_OUT_JSON=$(aws ecs run-task \
  --cluster "$CLUSTER_NAME" \
  --launch-type FARGATE \
  --task-definition "$TD_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[$(printf '"%s",' "${SUBNET_ARRAY[@]}" | sed 's/,$//')],securityGroups=[\"$SECURITY_GROUP_ID\"],assignPublicIp=${ASSIGN_PUBLIC_IP:-DISABLED}}" \
  --region "$AWS_REGION" \
  --output json)

# Print any immediate API-level failures (wrong SG, bad subnets, etc.)
FAILURES=$(echo "$RUN_OUT_JSON" | $JQ -r '.failures[]? | "\(.arn) \(.reason)"')
if [[ -n "${FAILURES:-}" ]]; then
  echo "❌ run-task failures:"
  echo "$FAILURES"
fi

TASK_ARN=$(echo "$RUN_OUT_JSON" | $JQ -r '.tasks[0].taskArn // empty')
if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "❌ Failed to start task (no taskArn returned). See failures above."
  exit 1
fi
echo "🆔 Task ARN: $TASK_ARN"

echo "⏳ Waiting for task to stop..."
aws ecs wait tasks-stopped --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --region "$AWS_REGION"

# Post-mortem details
DESC_JSON=$(aws ecs describe-tasks --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --region "$AWS_REGION" --output json)
EXIT_CODE=$(echo "$DESC_JSON" | $JQ -r '.tasks[0].containers[0].exitCode // empty')
STOPPED_REASON=$(echo "$DESC_JSON" | $JQ -r '.tasks[0].stoppedReason // empty')
CONTAINER_REASON=$(echo "$DESC_JSON" | $JQ -r '.tasks[0].containers[0].reason // empty')

echo "🧾 Task stopped. stoppedReason: ${STOPPED_REASON:-<none>}"
[[ -n "$CONTAINER_REASON" ]] && echo "🧾 Container reason: $CONTAINER_REASON"
[[ -n "$EXIT_CODE" && "$EXIT_CODE" != "null" ]] && echo "🧪 Exit code: $EXIT_CODE"

# Fetch last 100 log events if possible
echo "📜 Last 100 log events (if available):"
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM_PREFIX/$CONTAINER_NAME/$(echo "$TASK_ARN" | awk -F"/" '{print $NF}')" \
  --limit 100 --region "$AWS_REGION" || true

if [[ -z "$EXIT_CODE" || "$EXIT_CODE" == "null" ]]; then
  echo "⚠️  No container exit code reported. This often indicates the task failed to pull the image (ECR auth/network)."
  echo "   - If using private subnets, ensure NAT + ECR VPC endpoints."
  echo "   - Otherwise set ASSIGN_PUBLIC_IP=ENABLED or keep AUTO with public subnets."
  exit 1
fi

if [[ "$EXIT_CODE" == "0" ]]; then
  echo "✅ Task completed successfully."
else
  echo "❌ Task failed with exit code: $EXIT_CODE"
  echo "👉 Check CloudWatch Logs group: $LOG_GROUP"
  exit "$EXIT_CODE"
fi
