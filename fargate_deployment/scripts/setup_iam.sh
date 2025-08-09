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
S3_OUTPUT_BUCKET="$(echo "${OUTPUT_S3:-}" | sed -E 's#^s3://([^/]+)/.*#\1#' || true)"
SES_IDENTITY="${EMAIL_FROM:-}"

get_or_create_role () {
  local NAME="$1"
  local TRUST="$2"
  if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    aws iam create-role \
      --role-name "$NAME" \
      --assume-role-policy-document "$TRUST" \
      >/dev/null
    echo "✅ Created role $NAME"
  else
    echo "ℹ️ Role $NAME exists"
  fi
}

attach_policy_if_missing () {
  local ROLE="$1"
  local ARN="$2"
  if ! aws iam list-attached-role-policies --role-name "$ROLE" \
      --query 'AttachedPolicies[].PolicyArn' --output text | grep -q "$ARN"; then
    aws iam attach-role-policy --role-name "$ROLE" --policy-arn "$ARN" >/dev/null
    echo "🔗 Attached $ARN to $ROLE"
  fi
}

create_or_update_inline_policy () {
  local NAME="$1"
  local DOC="$2"

  local EXISTS
  EXISTS=$(aws iam list-policies --scope Local --query 'Policies[?PolicyName==`'"$NAME"'`].Arn' --output text)
  if [[ -z "$EXISTS" ]]; then
    ARN=$(aws iam create-policy --policy-name "$NAME" \
      --policy-document "$DOC" \
      --query 'Policy.Arn' --output text)
    echo "$ARN"
  else
    # Put new version and set as default
    ARN="$EXISTS"
    aws iam create-policy-version --policy-arn "$ARN" --policy-document "$DOC" --set-as-default >/dev/null
    echo "$ARN"
  fi
}

# ---------- Execution role (pull image, logs, secrets) ----------
EXEC_TRUST_DOC='{
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
}'
get_or_create_role "$EXECUTION_ROLE_NAME" "$EXEC_TRUST_DOC"

# Standard ECS execution policy (pull from ECR, write logs, etc.)
attach_policy_if_missing "$EXECUTION_ROLE_NAME" "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"

# Allow reading OpenAI API key from Secrets Manager (if you configured one)
EXEC_INLINE_DOC=$(jq -n --arg lg "$LOG_GROUP" '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid":"LogsDescribeCreate",
      "Effect":"Allow",
      "Action":["logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams","logs:CreateLogGroup"],
      "Resource":"*"
    },
    {
      "Sid":"SecretsRead",
      "Effect":"Allow",
      "Action":["secretsmanager:GetSecretValue","kms:Decrypt"],
      "Resource":"*"
    }
  ]
}')
EXEC_POLICY_ARN=$(create_or_update_inline_policy "${POLICY_PREFIX}-Execution" "$EXEC_INLINE_DOC")
attach_policy_if_missing "$EXECUTION_ROLE_NAME" "$EXEC_POLICY_ARN"

# ---------- Task role (app needs S3 + SES) ----------
TASK_TRUST_DOC="$EXEC_TRUST_DOC"
get_or_create_role "$TASK_ROLE_NAME" "$TASK_TRUST_DOC"

S3_STATEMENTS=()
if [[ -n "$S3_INPUT_BUCKET" ]]; then
  S3_STATEMENTS+=("{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_INPUT_BUCKET\",\"arn:aws:s3:::$S3_INPUT_BUCKET/*\"]}")
fi
if [[ -n "$S3_OUTPUT_BUCKET" && "$S3_OUTPUT_BUCKET" != "s3://" && "$S3_OUTPUT_BUCKET" != "" ]]; then
  S3_STATEMENTS+=("{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_OUTPUT_BUCKET\",\"arn:aws:s3:::$S3_OUTPUT_BUCKET/*\"]}")
fi

SES_STATEMENT='{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}'
if [[ -n "$SES_IDENTITY" ]]; then
  SES_STATEMENT=$(jq -n --arg id "$SES_IDENTITY" '{
    "Effect":"Allow",
    "Action":["ses:SendEmail","ses:SendRawEmail"],
    "Resource":"*",
    "Condition":{"StringLike":{"ses:FromAddress":[$id]}}
  }')
fi

TASK_DOC=$(jq -n --argjson s3 "[$(IFS=,; echo "${S3_STATEMENTS[*]}")]" --argjson ses "$SES_STATEMENT" '{
  "Version":"2012-10-17",
  "Statement": ($s3 + [$ses])
}')
TASK_POLICY_ARN=$(create_or_update_inline_policy "${POLICY_PREFIX}-Task" "$TASK_DOC")
attach_policy_if_missing "$TASK_ROLE_NAME" "$TASK_POLICY_ARN"

echo "✅ IAM setup complete:
  Execution Role: $EXECUTION_ROLE_NAME
  Task Role     : $TASK_ROLE_NAME"
