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

command -v jq >/dev/null 2>&1 || { echo "jq is required"; exit 1; }

# ---------- helpers ----------
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

# Prune non-default versions so we never hit the 5-version cap
prune_policy_versions () {
  local ARN="$1"
  local NON_DEFAULT COUNT
  NON_DEFAULT=$(aws iam list-policy-versions --policy-arn "$ARN" \
                 --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text)
  COUNT=$(wc -w <<< "$NON_DEFAULT" | awk '{print $1}')
  if [[ -n "$NON_DEFAULT" && $COUNT -ge 4 ]]; then
    local TO_DELETE NEEDED i=0
    TO_DELETE=$(aws iam list-policy-versions --policy-arn "$ARN" \
                 --query 'Versions[?IsDefaultVersion==`false`].[VersionId,CreateDate]' --output text \
                 | sort -k2 | awk '{print $1}')
    NEEDED=$((COUNT - 3))
    for vid in $TO_DELETE; do
      aws iam delete-policy-version --policy-arn "$ARN" --version-id "$vid" >/dev/null 2>&1 || true
      i=$((i+1))
      [[ $i -ge $NEEDED ]] && break
    done
    echo "🧹 Pruned $i old non-default version(s) for $ARN"
  fi
}

create_or_update_managed_policy () {
  local NAME="$1"
  local DOC="$2"

  local ARN
  ARN=$(aws iam list-policies --scope Local --query 'Policies[?PolicyName==`'"$NAME"'`].Arn' --output text)
  if [[ -z "$ARN" || "$ARN" == "None" ]]; then
    ARN=$(aws iam create-policy --policy-name "$NAME" \
      --policy-document "$DOC" \
      --query 'Policy.Arn' --output text)
    echo "$ARN"
  else
    prune_policy_versions "$ARN"
    aws iam create-policy-version --policy-arn "$ARN" --policy-document "$DOC" --set-as-default >/dev/null
    echo "$ARN"
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

# ---------- Execution role (pull image, logs, secrets for env-injection) ----------
EXEC_TRUST_DOC='{
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
}'
get_or_create_role "$EXECUTION_ROLE_NAME" "$EXEC_TRUST_DOC"

# Standard ECS execution policy (pull from ECR, write logs, etc.)
attach_policy_if_missing "$EXECUTION_ROLE_NAME" "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"

# Resolve the specific secret ARN (to scope permissions tightly)
RESOLVED_OPENAI_SECRET_ARN=""
if [[ -n "$OPENAI_API_KEY_SECRET_NAME" ]]; then
  RESOLVED_OPENAI_SECRET_ARN="$(resolve_secret_arn "$OPENAI_API_KEY_SECRET_NAME")"
  if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "$RESOLVED_OPENAI_SECRET_ARN" != "None" ]]; then
    echo "🔑 Execution will read secret: $RESOLVED_OPENAI_SECRET_ARN"
  else
    echo "⚠️  OPENAI_API_KEY_SECRET_NAME is set ('$OPENAI_API_KEY_SECRET_NAME') but ARN could not be resolved in $AWS_REGION."
    echo "    Execution role secret access will be skipped."
  fi
fi

# Add logs + (tightly scoped) secret read for env injection.
# Use proper KMS decrypt condition keyed on Secrets Manager encryption context.
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
EXEC_POLICY_ARN=$(create_or_update_managed_policy "${POLICY_PREFIX}-Execution" "$EXEC_DOC")
attach_policy_if_missing "$EXECUTION_ROLE_NAME" "$EXEC_POLICY_ARN"

# ---------- Task role (your app: S3 + SES; secrets optional if app fetches at runtime) ----------
TASK_TRUST_DOC="$EXEC_TRUST_DOC"
get_or_create_role "$TASK_ROLE_NAME" "$TASK_TRUST_DOC"

# S3 access statements (tight to buckets if provided; otherwise none)
S3_STATEMENTS=()
if [[ -n "$S3_INPUT_BUCKET" ]]; then
  S3_STATEMENTS+=("{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_INPUT_BUCKET\",\"arn:aws:s3:::$S3_INPUT_BUCKET/*\"]}")
fi
if [[ -n "$S3_OUTPUT_BUCKET" && "$S3_OUTPUT_BUCKET" != "s3://" && "$S3_OUTPUT_BUCKET" != "" ]]; then
  S3_STATEMENTS+=("{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::$S3_OUTPUT_BUCKET\",\"arn:aws:s3:::$S3_OUTPUT_BUCKET/*\"]}")
fi

# SES permission (restrict From: when provided)
if [[ -n "$SES_IDENTITY" ]]; then
  SES_STATEMENT=$(jq -n --arg id "$SES_IDENTITY" '{
    "Effect":"Allow",
    "Action":["ses:SendEmail","ses:SendRawEmail"],
    "Resource":"*",
    "Condition":{"StringEquals":{"ses:FromAddress":$id}}
  }')
else
  SES_STATEMENT='{"Effect":"Allow","Action":["ses:SendEmail","ses:SendRawEmail"],"Resource":"*"}'
fi

# Secrets Manager permission for the task role (ONLY if your app fetches the secret at runtime)
OPENAI_TASK_SM_STMTS="[]"
if [[ -n "$RESOLVED_OPENAI_SECRET_ARN" && "${APP_FETCHES_OPENAI_SECRET_AT_RUNTIME:-false}" == "true" ]]; then
  OPENAI_TASK_SM_STMTS=$(jq -n --arg arn "$RESOLVED_OPENAI_SECRET_ARN" '[
    {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource": $arn},
    {"Effect":"Allow","Action":["kms:Decrypt"],"Resource":"*","Condition":{"ForAnyValue:StringEquals":{"kms:EncryptionContext:aws:secretsmanager:arn": $arn}}}
  ]')
fi

# Build the task policy document
TASK_DOC=$(jq -n \
  --argjson s3 "[$(IFS=,; echo "${S3_STATEMENTS[*]}")]" \
  --argjson ses "$SES_STATEMENT" \
  --argjson sm "$OPENAI_TASK_SM_STMTS" \
  '{
    "Version":"2012-10-17",
    "Statement": ($s3 + [$ses] + $sm)
  }')

TASK_POLICY_ARN=$(create_or_update_managed_policy "${POLICY_PREFIX}-Task" "$TASK_DOC")
attach_policy_if_missing "$TASK_ROLE_NAME" "$TASK_POLICY_ARN"

echo "✅ IAM setup complete:
  Execution Role: $EXECUTION_ROLE_NAME
  Task Role     : $TASK_ROLE_NAME
  Notes         : Exec role is scoped to the exact secret ARN; task role has S3/SES only (no secret needed unless APP_FETCHES_OPENAI_SECRET_AT_RUNTIME=true)"
