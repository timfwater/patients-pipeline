#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# -------- Required config --------
: "${AWS_REGION:?Missing AWS_REGION in config.env}"
: "${AWS_ACCOUNT_ID:?Missing AWS_ACCOUNT_ID in config.env}"
: "${LOG_GROUP:?Missing LOG_GROUP in config.env}"

# -------- Defaults --------
EXECUTION_ROLE_NAME="${EXECUTION_ROLE_NAME:-PatientPipelineECSExecutionRole}"
TASK_ROLE_NAME="${TASK_ROLE_NAME:-PatientPipelineECSTaskRole}"
POLICY_PREFIX="${POLICY_PREFIX:-PatientPipelinePolicy}"

# Optional app resources (tighten S3/SES if provided)
S3_INPUT_BUCKET="${S3_INPUT_BUCKET:-}"
# If OUTPUT_S3 is an s3://bucket/path, extract bucket name
S3_OUTPUT_BUCKET="$(echo "${OUTPUT_S3:-}" | sed -E 's#^s3://([^/]+)/?.*#\1#' || true)"
SES_IDENTITY="${EMAIL_FROM:-}"
OPENAI_API_KEY_SECRET_NAME="${OPENAI_API_KEY_SECRET_NAME:-}"
APP_FETCHES_OPENAI_SECRET_AT_RUNTIME="${APP_FETCHES_OPENAI_SECRET_AT_RUNTIME:-false}"

command -v jq >/dev/null 2>&1 || { echo "❌ jq is required (brew install jq)."; exit 1; }

# -------- Helpers --------
get_or_create_role () {
  local NAME="$1"
  local TRUST_JSON="$2"

  if ! aws iam get-role --role-name "$NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws iam create-role \
      --role-name "$NAME" \
      --assume-role-policy-document "$TRUST_JSON" \
      --region "$AWS_REGION" >/dev/null
    echo "✅ Created role $NAME"
  else
    echo "ℹ️ Role $NAME exists"
  fi
}

resolve_secret_arn () {
  local sid="$1"
  [[ -z "$sid" ]] && { echo ""; return; }
  [[ "$sid" == arn:* ]] && { echo "$sid"; return; }
  aws secretsmanager describe-secret \
    --region "$AWS_REGION" \
    --secret-id "$sid" \
    --query 'ARN' --output text 2>/dev/null || echo ""
}

ensure_log_group () {
  # CloudWatch Logs are region-scoped; create if absent
  if ! aws logs describe-log-groups \
    --region "$AWS_REGION" \
    --log-group-name-prefix "$LOG_GROUP" \
    --query "logGroups[?logGroupName=='\`${LOG_GROUP}\`'] | length(@)" \
    --output text | grep -q '^1$'; then
    aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION" || true
    echo "🪵 Ensured log group exists: $LOG_GROUP"
  else
    echo "🪵 Log group exists: $LOG_GROUP"
  fi
}

# -------- Start --------
ensure_log_group

EXEC_TRUST_DOC='{
  "Version":"2012-10-17",
  "Statement":[
    {"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}
  ]
}'

# --- Execution role (ECR pulls, logs, optional secret injection) ---
get_or_create_role "$EXECUTION_ROLE_NAME" "$EXEC_TRUST_DOC"

aws iam attach-role-policy \
  --role-name "$EXECUTION_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy \
  --region "$AWS_REGION" >/dev/null 2>&1 || true
echo "🔗 Attached AWS-managed AmazonECSTaskExecutionRolePolicy to $EXECUTION_ROLE_NAME"

RESOLVED_OPENAI_SECRET_ARN=""
if [[ -n "$OPENAI_API_KEY_SECRET_NAME" ]]; then
  RESOLVED_OPENAI_SECRET_ARN="$(resolve_secret_arn "$OPENAI_API_KEY_SECRET_NAME")"
  if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "$RESOLVED_OPENAI_SECRET_ARN" != "None" ]]; then
    echo "🔑 Execution will read secret (for injection): $RESOLVED_OPENAI_SECRET_ARN"
  else
    echo "⚠️ OPENAI_API_KEY_SECRET_NAME is set ('$OPENAI_API_KEY_SECRET_NAME') but ARN could not be resolved in $AWS_REGION."
  fi
fi

# Build execution inline policy safely with jq
exec_policy=$(jq -n '
  {
    "Version":"2012-10-17",
    "Statement":[
      {
        "Sid":"LogsRW",
        "Effect":"Allow",
        "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
        "Resource":"*"
      }
    ]
  }')

if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" ]]; then
  exec_policy=$(jq \
    --arg arn "$RESOLVED_OPENAI_SECRET_ARN" \
    '.Statement += [
      {"Sid":"ReadOpenAISecret","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":$arn},
      {"Sid":"DecryptForSecret","Effect":"Allow","Action":["kms:Decrypt"],"Resource":"*",
       "Condition":{"ForAnyValue:StringEquals":{"kms:EncryptionContext:aws:secretsmanager:arn":$arn}}}
    ]' <<<"$exec_policy")
  # NOTE: If you ever see KMS decrypt errors for Secrets Manager, as a fallback you can
  # allow decrypt on alias/aws/secretsmanager instead of (or in addition to) the condition:
  #   {"Action":"kms:Decrypt","Resource":"arn:aws:kms:'"$AWS_REGION"':'"$AWS_ACCOUNT_ID"':alias/aws/secretsmanager","Effect":"Allow"}
fi

aws iam put-role-policy \
  --role-name "$EXECUTION_ROLE_NAME" \
  --policy-name "${POLICY_PREFIX}-ExecutionInline" \
  --policy-document "$exec_policy" \
  --region "$AWS_REGION" >/dev/null
echo "🔗 Applied inline exec policy: ${POLICY_PREFIX}-ExecutionInline"

# --- Task role (app runtime: S3/SES; optional secret fetch at runtime) ---
get_or_create_role "$TASK_ROLE_NAME" "$EXEC_TRUST_DOC"

task_policy=$(jq -n '{ "Version":"2012-10-17", "Statement":[] }')

# S3 input (read)
if [[ -n "$S3_INPUT_BUCKET" ]]; then
  task_policy=$(jq \
    --arg b "arn:aws:s3:::$S3_INPUT_BUCKET" \
    --arg o "arn:aws:s3:::$S3_INPUT_BUCKET/*" \
    '.Statement += [
      {"Sid":"S3InputList","Effect":"Allow","Action":["s3:ListBucket"],"Resource":$b},
      {"Sid":"S3InputGet","Effect":"Allow","Action":["s3:GetObject"],"Resource":$o}
    ]' <<<"$task_policy")
fi

# S3 output (write)
if [[ -n "$S3_OUTPUT_BUCKET" && "$S3_OUTPUT_BUCKET" != "s3://" ]]; then
  task_policy=$(jq \
    --arg b "arn:aws:s3:::$S3_OUTPUT_BUCKET" \
    --arg o "arn:aws:s3:::$S3_OUTPUT_BUCKET/*" \
    '.Statement += [
      {"Sid":"S3OutputList","Effect":"Allow","Action":["s3:ListBucket"],"Resource":$b},
      {"Sid":"S3OutputPut","Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":$o}
    ]' <<<"$task_policy")
fi

# SES send (restricted From: if provided)
if [[ -n "$SES_IDENTITY" ]]; then
  task_policy=$(jq \
    --arg from "$SES_IDENTITY" \
    '.Statement += [
      {"Sid":"SESSendRestricted","Effect":"Allow",
       "Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*",
       "Condition":{"StringEquals":{"ses:FromAddress":$from}}}
    ]' <<<"$task_policy")
else
  task_policy=$(jq \
    '.Statement += [
      {"Sid":"SESSend","Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}
    ]' <<<"$task_policy")
fi

# Secrets Manager at runtime (only if the APP fetches it)
if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "$APP_FETCHES_OPENAI_SECRET_AT_RUNTIME" == "true" ]]; then
  task_policy=$(jq \
    --arg arn "$RESOLVED_OPENAI_SECRET_ARN" \
    '.Statement += [
      {"Sid":"ReadOpenAISecretAtRuntime","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":$arn},
      {"Sid":"DecryptForSecretAtRuntime","Effect":"Allow","Action":["kms:Decrypt"],"Resource":"*",
       "Condition":{"ForAnyValue:StringEquals":{"kms:EncryptionContext:aws:secretsmanager:arn":$arn}}}
    ]' <<<"$task_policy")
fi

aws iam put-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-name "${POLICY_PREFIX}-TaskInline" \
  --policy-document "$task_policy" \
  --region "$AWS_REGION" >/dev/null
echo "🔗 Applied inline task policy: ${POLICY_PREFIX}-TaskInline"

# -------- Summary --------
echo
echo "✅ IAM setup complete"
echo "   • Execution Role : $EXECUTION_ROLE_NAME"
echo "   • Task Role      : $TASK_ROLE_NAME"
echo "   • Log Group      : $LOG_GROUP"
[[ -n "$S3_INPUT_BUCKET"  ]]  && echo "   • S3 Input       : arn:aws:s3:::$S3_INPUT_BUCKET (ro)"
[[ -n "$S3_OUTPUT_BUCKET" ]] && echo "   • S3 Output      : arn:aws:s3:::$S3_OUTPUT_BUCKET (rw)"
[[ -n "$SES_IDENTITY"     ]] && echo "   • SES From       : $SES_IDENTITY (restricted)"
[[ -n "$RESOLVED_OPENAI_SECRET_ARN" ]] && echo "   • Secret ARN     : $RESOLVED_OPENAI_SECRET_ARN (exec inject$( [[ "$APP_FETCHES_OPENAI_SECRET_AT_RUNTIME" == "true" ]] && echo ", task runtime" ))"
