#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# ---- Config with sensible defaults ----
EXECUTION_ROLE_NAME="${EXECUTION_ROLE_NAME:-PatientPipelineECSExecutionRole}"
TASK_ROLE_NAME="${TASK_ROLE_NAME:-PatientPipelineECSTaskRole}"
POLICY_PREFIX="${POLICY_PREFIX:-PatientPipelinePolicy}"

# Required vars
: "${AWS_REGION:?Missing AWS_REGION in config.env}"
: "${AWS_ACCOUNT_ID:?Missing AWS_ACCOUNT_ID in config.env}"
: "${LOG_GROUP:?Missing LOG_GROUP in config.env}"

# Optional app-level resources (tighten S3/SES if provided)
S3_INPUT_BUCKET="${S3_INPUT_BUCKET:-}"
# If OUTPUT_S3 is an s3://bucket/path, extract bucket name
S3_OUTPUT_BUCKET="$(echo "${OUTPUT_S3:-}" | sed -E 's#^s3://([^/]+)/?.*#\1#' || true)"
SES_IDENTITY="${EMAIL_FROM:-}"
OPENAI_API_KEY_SECRET_NAME="${OPENAI_API_KEY_SECRET_NAME:-}"
# If true, grant the *task role* permission to fetch the secret at runtime.
APP_FETCHES_OPENAI_SECRET_AT_RUNTIME="${APP_FETCHES_OPENAI_SECRET_AT_RUNTIME:-false}"

command -v jq >/dev/null 2>&1 || { echo "jq is required"; exit 1; }

# ---------- helpers ----------
get_or_create_role () {
  local NAME="$1"
  local TRUST="$2"
  if ! aws iam get-role --role-name "$NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws iam create-role \
      --role-name "$NAME" \
      --assume-role-policy-document "$TRUST" \
      --region "$AWS_REGION" >/dev/null
    echo "✅ Created role $NAME"
  else
    echo "ℹ️ Role $NAME exists"
  fi
}

resolve_secret_arn () {
  local sid="$1"
  if [[ -z "$sid" ]]; then echo ""; return; fi
  if [[ "$sid" == arn:* ]]; then echo "$sid"; return; fi
  aws secretsmanager describe-secret \
    --region "$AWS_REGION" \
    --secret-id "$sid" \
    --query 'ARN' --output text 2>/dev/null || echo ""
}

# ---------- Execution role (ECR pulls, logs, secret injection) ----------
EXEC_TRUST_DOC='{
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
}'
get_or_create_role "$EXECUTION_ROLE_NAME" "$EXEC_TRUST_DOC"

# Attach AWS-managed ECS execution policy (ECR + logs)
aws iam attach-role-policy \
  --role-name "$EXECUTION_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy \
  --region "$AWS_REGION" >/dev/null 2>&1 || true
echo "🔗 Ensured AWS-managed AmazonECSTaskExecutionRolePolicy is attached to $EXECUTION_ROLE_NAME"

# Resolve the specific secret ARN (tight scope for secrets + kms)
RESOLVED_OPENAI_SECRET_ARN=""
if [[ -n "$OPENAI_API_KEY_SECRET_NAME" ]]; then
  RESOLVED_OPENAI_SECRET_ARN="$(resolve_secret_arn "$OPENAI_API_KEY_SECRET_NAME")"
  if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "$RESOLVED_OPENAI_SECRET_ARN" != "None" ]]; then
    echo "🔑 Execution will read secret: $RESOLVED_OPENAI_SECRET_ARN"
  else
    echo "⚠️  OPENAI_API_KEY_SECRET_NAME is set ('$OPENAI_API_KEY_SECRET_NAME') but ARN could not be resolved in $AWS_REGION."
  fi
fi

# Inline policy for logs + (tightly scoped) secret access for injection path
EXEC_INLINE_NAME="${POLICY_PREFIX}-ExecutionInline"
EXEC_STATEMENTS='[
  {
    "Sid":"LogsDescribeCreate",
    "Effect":"Allow",
    "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
    "Resource":"*"
  }
]'
if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" ]]; then
  EXEC_STATEMENTS=$(jq -n --arg arn "$RESOLVED_OPENAI_SECRET_ARN" '[
    {
      "Sid":"LogsDescribeCreate",
      "Effect":"Allow",
      "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
      "Resource":"*"
    },
    {
      "Sid":"ReadOpenAISecret",
      "Effect":"Allow",
      "Action":["secretsmanager:GetSecretValue"],
      "Resource": $arn
    },
    {
      "Sid":"DecryptForSecret",
      "Effect":"Allow",
      "Action":["kms:Decrypt"],
      "Resource":"*",
      "Condition":{
        "ForAnyValue:StringEquals":{
          "kms:EncryptionContext:aws:secretsmanager:arn": $arn
        }
      }
    }
  ]')
fi

EXEC_DOC=$(jq -n --argjson stmts "$EXEC_STATEMENTS" '{ "Version":"2012-10-17", "Statement": $stmts }')
aws iam put-role-policy \
  --role-name "$EXECUTION_ROLE_NAME" \
  --policy-name "$EXEC_INLINE_NAME" \
  --policy-document "$(printf '%s' "$EXEC_DOC")" \
  --region "$AWS_REGION" >/dev/null 2>&1 || \
aws iam put-role-policy \
  --role-name "$EXECUTION_ROLE_NAME" \
  --policy-name "$EXEC_INLINE_NAME" \
  --policy-document "$(printf '%s' "$EXEC_DOC")" \
  --region "$AWS_REGION" >/dev/null
echo "🔗 Applied inline policy to $EXECUTION_ROLE_NAME: $EXEC_INLINE_NAME"

# ---------- Task role (your app: S3 + SES; optional secrets at runtime) ----------
TASK_TRUST_DOC="$EXEC_TRUST_DOC"
get_or_create_role "$TASK_ROLE_NAME" "$TASK_TRUST_DOC"

# S3 access statements (tight to buckets if provided; otherwise none)
S3_STATEMENTS=()
if [[ -n "$S3_INPUT_BUCKET" ]]; then
  S3_STATEMENTS+=("{\"Sid\":\"S3Input\",\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_INPUT_BUCKET\",\"arn:aws:s3:::$S3_INPUT_BUCKET/*\"]}")
fi
if [[ -n "$S3_OUTPUT_BUCKET" && "$S3_OUTPUT_BUCKET" != "s3://" && "$S3_OUTPUT_BUCKET" != "" ]]; then
  S3_STATEMENTS+=("{\"Sid\":\"S3Output\",\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_OUTPUT_BUCKET\",\"arn:aws:s3:::$S3_OUTPUT_BUCKET/*\"]}")
fi

# SES permission (restrict From: when provided)
if [[ -n "$SES_IDENTITY" ]]; then
  SES_STATEMENT=$(jq -n --arg id "$SES_IDENTITY" '{
    "Sid":"SESSendRestricted",
    "Effect":"Allow",
    "Action":["ses:SendEmail","ses:SendRawEmail"],
    "Resource":"*",
    "Condition":{"StringEquals":{"ses:FromAddress":$id}}
  }')
else
  SES_STATEMENT='{"Sid":"SESSend","Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}'
fi

# Secrets Manager permission for the task role (ONLY if your app fetches the secret at runtime)
TASK_SM_STMTS="[]"
if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "$APP_FETCHES_OPENAI_SECRET_AT_RUNTIME" == "true" ]]; then
  TASK_SM_STMTS=$(jq -n --arg arn "$RESOLVED_OPENAI_SECRET_ARN" '[
    {"Sid":"ReadOpenAISecretAtRuntime","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource": $arn},
    {"Sid":"DecryptForSecretAtRuntime","Effect":"Allow","Action":["kms:Decrypt"],"Resource":"*","Condition":{"ForAnyValue:StringEquals":{"kms:EncryptionContext:aws:secretsmanager:arn": $arn}}}
  ]')
fi

TASK_DOC=$(jq -n \
  --argjson s3 "[$(IFS=,; echo "${S3_STATEMENTS[*]-}")]" \
  --argjson ses "$SES_STATEMENT" \
  --argjson sm "$TASK_SM_STMTS" \
  '{
    "Version":"2012-10-17",
    "Statement": ( ($s3 // []) + [$ses] + $sm )
  }')

TASK_INLINE_NAME="${POLICY_PREFIX}-TaskInline"
aws iam put-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-name "$TASK_INLINE_NAME" \
  --policy-document "$(printf '%s' "$TASK_DOC")" \
  --region "$AWS_REGION" >/dev/null 2>&1 || \
aws iam put-role-policy \
  --role-name "$TASK_ROLE_NAME" \
  --policy-name "$TASK_INLINE_NAME" \
  --policy-document "$(printf '%s' "$TASK_DOC")" \
  --region "$AWS_REGION" >/dev/null
echo "🔗 Applied inline policy to $TASK_ROLE_NAME: $TASK_INLINE_NAME"

echo "✅ IAM setup complete:
  Execution Role: $EXECUTION_ROLE_NAME (inline policy + AWS managed ECS execution policy)
  Task Role     : $TASK_ROLE_NAME (inline policy: S3/SES; secrets only if APP_FETCHES_OPENAI_SECRET_AT_RUNTIME=true)"
