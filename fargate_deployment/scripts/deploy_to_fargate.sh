#!/usr/bin/env bash
set -euo pipefail

# ========= Debug knob (optional) =========
: "${DEBUG:=false}"
$DEBUG && set -x

# ========= Writable FS knob (fixes /tmp writes) =========
# Set to true only if your code does NOT write to local FS.
: "${READONLY_FS:=false}"
# Bash 3.2-compatible truthy check (no ${var,,})
case "${READONLY_FS}" in
  true|TRUE|True|t|T|1|yes|YES|Yes) ROFS_JSON=true ;;
  *) ROFS_JSON=false ;;
esac

# ========= Locate & load config =========
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# Region fallback (from AWS CLI config) if not set
: "${AWS_REGION:=$(aws configure get region 2>/dev/null || echo us-east-1)}"

# ========= Sanity: required CLIs =========
if ! command -v jq >/dev/null 2>&1; then
  echo "❌ 'jq' is required on PATH"; exit 1
fi

# ========= Resolve image =========
if [[ -z "${IMAGE_URI:-}" && -f "$ROOT_DIR/.last_image_uri" ]]; then
  IMAGE_URI="$(cat "$ROOT_DIR/.last_image_uri")"
  echo "ℹ️  Using IMAGE_URI from .last_image_uri -> $IMAGE_URI"
fi
if [[ -z "${IMAGE_URI:-}" ]]; then
  if [[ -n "${ECR_REPO_URI:-}" ]]; then
    IMAGE_URI="$ECR_REPO_URI"
  else
    echo "❌ Provide IMAGE_URI or ECR_REPO_URI in config.env (or ensure .last_image_uri exists)"; exit 1
  fi
fi
[[ "$IMAGE_URI" == *:* ]] || IMAGE_URI="${IMAGE_URI}:latest"

# ========= Resolve cluster / roles =========
: "${CLUSTER_NAME:=${ECS_CLUSTER_NAME:-}}"
: "${TASK_FAMILY:?Missing TASK_FAMILY in config.env}"
: "${LOG_GROUP:?Missing LOG_GROUP in config.env}"
: "${LOG_STREAM_PREFIX:=ecs}"

# Back-compat container name
: "${CONTAINER_NAME:=patient-pipeline}"

# Subnets & SGs
: "${SUBNET_IDS:=${FARGATE_SUBNET_IDS:-}}"
: "${SECURITY_GROUP_ID:=${FARGATE_SECURITY_GROUP_IDS%%,*}}"

# Resolve role ARNs from either direct ARNs or role names
RESOLVED_EXEC_ROLE_ARN="${TASK_EXECUTION_ROLE:-${EXECUTION_ROLE_ARN:-}}"
RESOLVED_TASK_ROLE_ARN="${TASK_ROLE:-${TASK_ROLE_ARN:-}}"
if [[ -z "$RESOLVED_EXEC_ROLE_ARN" && -n "${EXECUTION_ROLE_NAME:-}" ]]; then
  RESOLVED_EXEC_ROLE_ARN="$(aws iam get-role --role-name "$EXECUTION_ROLE_NAME" --query 'Role.Arn' --output text --region "$AWS_REGION")"
fi
if [[ -z "$RESOLVED_TASK_ROLE_ARN" && -n "${TASK_ROLE_NAME:-}" ]]; then
  RESOLVED_TASK_ROLE_ARN="$(aws iam get-role --role-name "$TASK_ROLE_NAME" --query 'Role.Arn' --output text --region "$AWS_REGION")"
fi
: "${RESOLVED_EXEC_ROLE_ARN:?Missing TASK_EXECUTION_ROLE (or EXECUTION_ROLE_ARN/NAME)}"
: "${RESOLVED_TASK_ROLE_ARN:?Missing TASK_ROLE (or TASK_ROLE_ARN/NAME)}"

# ========= Preflight infra =========
echo "🚀 Deploying task to Fargate in $AWS_REGION"
echo "ℹ️  Cluster=$CLUSTER_NAME  Image=$IMAGE_URI"

STATUS=$(aws ecs describe-clusters --clusters "$CLUSTER_NAME" --region "$AWS_REGION" --query 'clusters[0].status' --output text 2>/dev/null || echo "MISSING")
if [[ "$STATUS" == "INACTIVE" ]]; then
  echo "ℹ️  ECS cluster '$CLUSTER_NAME' is INACTIVE; recreating..."
  aws ecs delete-cluster --cluster "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1 || true
  aws ecs create-cluster --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
elif [[ "$STATUS" != "ACTIVE" ]]; then
  echo "ℹ️  ECS cluster '$CLUSTER_NAME' not found; creating..."
  aws ecs create-cluster --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null
fi

if ! aws logs describe-log-groups --region "$AWS_REGION" --log-group-name-prefix "$LOG_GROUP" \
   --query 'logGroups[?logGroupName==`'"$LOG_GROUP"'`]' --output text | grep -q "$LOG_GROUP"; then
  echo "ℹ️  Creating CloudWatch Logs group: $LOG_GROUP"
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION"
fi

# ========= Validate core app inputs =========
missing=()
[[ -z "${INPUT_S3:-}" && ( -z "${S3_INPUT_BUCKET:-}" || -z "${INPUT_FILE:-}" ) ]] && missing+=("INPUT_S3 or (S3_INPUT_BUCKET+INPUT_FILE)")
[[ -z "${OUTPUT_S3:-}" ]] && missing+=("OUTPUT_S3")
[[ -z "${EMAIL_FROM:-}" ]] && missing+=("EMAIL_FROM")
[[ -z "${EMAIL_TO:-}"   ]] && missing+=("EMAIL_TO")
[[ -z "${THRESHOLD:-}"  ]] && missing+=("THRESHOLD")
if ((${#missing[@]})); then
  echo "❌ Missing required config: ${missing[*]}"; exit 1
fi

# Derive INPUT_S3 if not provided
if [[ -z "${INPUT_S3:-}" ]]; then
  INPUT_S3="s3://${S3_INPUT_BUCKET}/Input/${INPUT_FILE}"
  echo "ℹ️  Derived INPUT_S3=${INPUT_S3}"
fi

# Harmonize physician filters
if [[ -z "${PHYSICIAN_ID_LIST:-}" && -n "${PHYSICIAN_ID:-}" ]]; then
  PHYSICIAN_ID_LIST="$PHYSICIAN_ID"
fi

# ========= Runtime knobs wired for the new Python CLI =========
: "${RUN_ID:=$(date -u +%Y%m%dT%H%M%SZ)}"
: "${LLM_DISABLED:=false}"           # allow fast E2E test (no OpenAI calls)
: "${DRY_RUN_EMAIL:=false}"
: "${MAX_NOTES:=0}"
: "${LOG_FORMAT:=json}"              # "json" (default) or "text"
: "${LOG_LEVEL:=INFO}"
: "${RAG_ENABLED:=false}"
: "${RAG_KB_PATH:=}"
: "${RAG_TOP_K:=4}"
: "${RAG_MAX_CHARS:=2500}"
: "${RAG_AUDIT_MAX_CHARS:=1200}"


# ========= Secrets (OpenAI) handling =========
SECRETS_JSON='[]'
if [[ -n "${OPENAI_API_KEY_SECRET_NAME:-}" ]]; then
  echo "🔐 Will inject OPENAI_API_KEY from Secrets Manager id: $OPENAI_API_KEY_SECRET_NAME"
  if [[ "$OPENAI_API_KEY_SECRET_NAME" == arn:* ]]; then
    SECRET_ARN="$OPENAI_API_KEY_SECRET_NAME"
  else
    SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$OPENAI_API_KEY_SECRET_NAME" --region "$AWS_REGION" --query 'ARN' --output text 2>/dev/null || true)"
  fi
  if [[ -z "${SECRET_ARN:-}" || "$SECRET_ARN" == "None" ]]; then
    echo "❌ Could not resolve secret '$OPENAI_API_KEY_SECRET_NAME' in region $AWS_REGION"; exit 1
  fi
  # Detect JSON vs plaintext secret
  SECRET_STR="$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --region "$AWS_REGION" --query SecretString --output text 2>/dev/null || true)"
  : "${OPENAI_SECRET_JSON_KEY:=OPENAI_API_KEY}"
  if echo "${SECRET_STR:-}" | jq -e --arg k "$OPENAI_SECRET_JSON_KEY" 'try fromjson | has($k)' >/dev/null 2>&1; then
    SECRET_SPEC="${SECRET_ARN}:${OPENAI_SECRET_JSON_KEY}::"
  else
    SECRET_SPEC="${SECRET_ARN}"
  fi
  SECRETS_JSON="$(jq -n --arg spec "$SECRET_SPEC" '[{name:"OPENAI_API_KEY", valueFrom:$spec}]')"
elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "⚠️  Injecting OPENAI_API_KEY from plaintext env (fallback). Consider using Secrets Manager."
else
  echo "❌ Neither OPENAI_API_KEY_SECRET_NAME nor OPENAI_API_KEY present. Cannot proceed."; exit 1
fi

# ========= Build environment block for the container =========
ENV_JSON="$(jq -n \
  --arg AWS_REGION        "$AWS_REGION" \
  --arg INPUT_S3          "$INPUT_S3" \
  --arg OUTPUT_S3         "$OUTPUT_S3" \
  --arg PHYSICIAN_ID_LIST "${PHYSICIAN_ID_LIST:-}" \
  --arg THRESHOLD         "$THRESHOLD" \
  --arg START_DATE        "${START_DATE:-}" \
  --arg END_DATE          "${END_DATE:-}" \
  --arg EMAIL_FROM        "$EMAIL_FROM" \
  --arg EMAIL_TO          "$EMAIL_TO" \
  --arg EMAIL_SUBJECT     "${EMAIL_SUBJECT:-High-Risk Patient Report}" \
  --arg OPENAI_MODEL      "${OPENAI_MODEL:-}" \
  --arg OPENAI_THROTTLE   "${OPENAI_THROTTLE_SEC:-}" \
  --arg AUDIT_BUCKET      "${AUDIT_BUCKET:-}" \
  --arg AUDIT_PREFIX      "${AUDIT_PREFIX:-}" \
  --arg RUN_ID            "$RUN_ID" \
  --arg LLM_DISABLED      "$LLM_DISABLED" \
  --arg DRY_RUN_EMAIL     "$DRY_RUN_EMAIL" \
  --arg MAX_NOTES         "$MAX_NOTES" \
  --arg LOG_FORMAT        "$LOG_FORMAT" \
  --arg LOG_LEVEL         "$LOG_LEVEL" \
  --arg RAG_ENABLED       "$RAG_ENABLED" \
  --arg RAG_KB_PATH       "$RAG_KB_PATH" \
  --arg RAG_TOP_K         "$RAG_TOP_K" \
  --arg RAG_MAX_CHARS     "$RAG_MAX_CHARS" \
  --arg RAG_AUDIT_MAX_CHARS "$RAG_AUDIT_MAX_CHARS" \
  '
  [
    {name:"AWS_REGION",           value:$AWS_REGION},
    {name:"INPUT_S3",             value:$INPUT_S3},
    {name:"OUTPUT_S3",            value:$OUTPUT_S3},
    {name:"PHYSICIAN_ID_LIST",    value:$PHYSICIAN_ID_LIST},
    {name:"THRESHOLD",            value:$THRESHOLD},
    {name:"START_DATE",           value:$START_DATE},
    {name:"END_DATE",             value:$END_DATE},
    {name:"EMAIL_FROM",           value:$EMAIL_FROM},
    {name:"EMAIL_TO",             value:$EMAIL_TO},
    {name:"EMAIL_SUBJECT",        value:$EMAIL_SUBJECT},
    {name:"OPENAI_MODEL",         value:$OPENAI_MODEL},
    {name:"OPENAI_THROTTLE_SEC",  value:$OPENAI_THROTTLE},
    {name:"AUDIT_BUCKET",         value:$AUDIT_BUCKET},
    {name:"AUDIT_PREFIX",         value:$AUDIT_PREFIX},
    {name:"RUN_ID",               value:$RUN_ID},
    {name:"LLM_DISABLED",         value:$LLM_DISABLED},
    {name:"DRY_RUN_EMAIL",        value:$DRY_RUN_EMAIL},
    {name:"MAX_NOTES",            value:$MAX_NOTES},
    {name:"LOG_FORMAT",           value:$LOG_FORMAT},
    {name:"LOG_LEVEL",            value:$LOG_LEVEL},

    {name:"RAG_ENABLED",          value:$RAG_ENABLED},
    {name:"RAG_KB_PATH",          value:$RAG_KB_PATH},
    {name:"RAG_TOP_K",            value:$RAG_TOP_K},
    {name:"RAG_MAX_CHARS",        value:$RAG_MAX_CHARS},
    {name:"RAG_AUDIT_MAX_CHARS",  value:$RAG_AUDIT_MAX_CHARS}
  ]
')"


# If using plaintext API key, append it to env
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  ENV_JSON="$(echo "$ENV_JSON" | jq '. + [{"name":"OPENAI_API_KEY","value":"'"$OPENAI_API_KEY"'"}]')"
fi

# ========= Render task definition from template =========
TEMPLATE="$ROOT_DIR/fargate_deployment/templates/task-def-template.json"
FINAL_TD="$ROOT_DIR/fargate_deployment/final-task-def.json"
[[ -f "$TEMPLATE" ]] || { echo "❌ Missing $TEMPLATE"; exit 1; }

echo "🧩 Rendering task definition..."
jq \
  --arg family   "$TASK_FAMILY" \
  --arg image    "$IMAGE_URI" \
  --arg execRole "$RESOLVED_EXEC_ROLE_ARN" \
  --arg taskRole "$RESOLVED_TASK_ROLE_ARN" \
  --arg logGroup "$LOG_GROUP" \
  --arg logPrefix "$LOG_STREAM_PREFIX" \
  --arg logRegion "$AWS_REGION" \
  --argjson env  "$ENV_JSON" \
  --argjson secrets "$SECRETS_JSON" \
  --argjson rofs "$ROFS_JSON" \
  '
  .family=$family
  | .executionRoleArn=$execRole
  | .taskRoleArn=$taskRole
  | .containerDefinitions[0].image=$image
  | .containerDefinitions[0].name="'"$CONTAINER_NAME"'"
  | .containerDefinitions[0].environment=$env
  | .containerDefinitions[0].secrets=$secrets
  | .containerDefinitions[0].logConfiguration.options["awslogs-group"]=$logGroup
  | .containerDefinitions[0].logConfiguration.options["awslogs-stream-prefix"]=$logPrefix
  | .containerDefinitions[0].logConfiguration.options["awslogs-region"]=$logRegion
  | .containerDefinitions[0].readonlyRootFilesystem=$rofs
  ' \
  "$TEMPLATE" > "$FINAL_TD"

echo "🔍 Rendered secrets block:"
jq '.containerDefinitions[0].secrets' "$FINAL_TD"

# ========= Register task definition =========
echo "📝 Registering task definition..."
TD_ARN="$(aws ecs register-task-definition \
  --cli-input-json file://"$FINAL_TD" \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text \
  --region "$AWS_REGION")"
echo "📦 Registered: $TD_ARN"

# ========= Network configuration =========
IFS=',' read -r -a SUBNET_ARRAY <<< "$SUBNET_IDS"
if ((${#SUBNET_ARRAY[@]} == 0)); then
  echo "❌ FARGATE_SUBNET_IDS/SUBNET_IDS not set"; exit 1
fi
if [[ -z "${ASSIGN_PUBLIC_IP:-}" || "${ASSIGN_PUBLIC_IP}" == "AUTO" ]]; then
  AUTO_PUBLIC="DISABLED"
  for sn in "${SUBNET_ARRAY[@]}"; do
    VPC_ID="$(aws ec2 describe-subnets --subnet-ids "$sn" --region "$AWS_REGION" --query 'Subnets[0].VpcId' --output text)"
    RT_IDS="$(aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$sn" --region "$AWS_REGION" --query 'RouteTables[].RouteTableId' --output text)"
    if [[ -z "$RT_IDS" || "$RT_IDS" == "None" ]]; then
      RT_IDS="$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" --region "$AWS_REGION" --query 'RouteTables[].RouteTableId' --output text)"
    fi
    IGW="$(aws ec2 describe-route-tables --route-table-ids $RT_IDS --region "$AWS_REGION" \
        --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0` && GatewayId!=null].GatewayId' --output text || true)"
    if [[ -n "$IGW" ]]; then AUTO_PUBLIC="ENABLED"; break; fi
  done
  ASSIGN_PUBLIC_IP="$AUTO_PUBLIC"
  echo "ℹ️  ASSIGN_PUBLIC_IP auto-set to: $ASSIGN_PUBLIC_IP"
fi
: "${SECURITY_GROUP_ID:?Missing FARGATE_SECURITY_GROUP_IDS/SECURITY_GROUP_ID}"

echo "NETCFG: subnets=[$SUBNET_IDS] sg=[$SECURITY_GROUP_ID] assignPublicIp=[$ASSIGN_PUBLIC_IP]"
echo "ROLES: exec=$RESOLVED_EXEC_ROLE_ARN task=$RESOLVED_TASK_ROLE_ARN logGroup=$LOG_GROUP"
echo "FS: readonlyRootFilesystem=${ROFS_JSON}"

# ========= Run one-off task =========
echo "🚀 Running task (assignPublicIp=${ASSIGN_PUBLIC_IP})"
RUN_OUT_JSON="$(
  aws ecs run-task \
    --cluster "$CLUSTER_NAME" \
    --launch-type FARGATE \
    --task-definition "$TD_ARN" \
    --count 1 \
    --network-configuration "awsvpcConfiguration={subnets=[$(printf '"%s",' "${SUBNET_ARRAY[@]}" | sed 's/,$//')],securityGroups=[\"$SECURITY_GROUP_ID\"],assignPublicIp=${ASSIGN_PUBLIC_IP}}" \
    --region "$AWS_REGION" \
    --output json
)"

# Print API failures (e.g., ECR auth timeouts)
FAILURES="$(echo "$RUN_OUT_JSON" | jq -r '.failures[]? | "\(.arn) \(.reason)"')"
if [[ -n "${FAILURES:-}" ]]; then
  echo "❌ run-task failures:"; echo "$FAILURES"
fi

TASK_ARN="$(echo "$RUN_OUT_JSON" | jq -r '.tasks[0].taskArn // empty')"
if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "❌ Failed to start task (no taskArn returned). See failures above."; exit 1
fi
echo "🆔 Task ARN: $TASK_ARN"

echo "⏳ Waiting for task to stop..."
aws ecs wait tasks-stopped --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --region "$AWS_REGION"

# ========= Post-mortem & logs =========
DESC_JSON="$(aws ecs describe-tasks --cluster "$CLUSTER_NAME" --tasks "$TASK_ARN" --region "$AWS_REGION" --output json)"
EXIT_CODE="$(echo "$DESC_JSON" | jq -r '.tasks[0].containers[0].exitCode // empty')"
STOPPED_REASON="$(echo "$DESC_JSON" | jq -r '.tasks[0].stoppedReason // empty')"
CONTAINER_REASON="$(echo "$DESC_JSON" | jq -r '.tasks[0].containers[0].reason // empty')"

echo "🧾 Task stopped. stoppedReason: ${STOPPED_REASON:-<none>}"
[[ -n "$CONTAINER_REASON" ]] && echo "🧾 Container reason: $CONTAINER_REASON"
[[ -n "$EXIT_CODE" && "$EXIT_CODE" != "null" ]] && echo "🧪 Exit code: $EXIT_CODE"

# Log stream matches: $LOG_STREAM_PREFIX/$CONTAINER_NAME/<taskId>
TASK_ID="$(echo "$TASK_ARN" | awk -F/ '{print $NF}')"
echo "📜 Last 120 log events (if available):"
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM_PREFIX/$CONTAINER_NAME/$TASK_ID" \
  --limit 120 --region "$AWS_REGION" || true

if [[ -z "$EXIT_CODE" || "$EXIT_CODE" == "null" ]]; then
  echo "⚠️  No container exit code reported. This often means an early image pull/network error."
  exit 1
fi

if [[ "$EXIT_CODE" == "0" ]]; then
  echo "✅ Task completed successfully."
else
  echo "❌ Task failed with exit code: $EXIT_CODE"
  echo "👉 Check CloudWatch Logs group: $LOG_GROUP"
  exit "$EXIT_CODE"
fi
