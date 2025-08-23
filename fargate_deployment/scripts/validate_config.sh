#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

missing=()
for v in AWS_REGION AWS_ACCOUNT_ID ECR_REPO_URI TASK_FAMILY ECS_CLUSTER_NAME LOG_GROUP FARGATE_SUBNET_IDS FARGATE_SECURITY_GROUP_IDS OUTPUT_S3 EMAIL_FROM EMAIL_TO; do
  [[ -z "${!v:-}" ]] && missing+=("$v")
done
if ((${#missing[@]})); then
  echo "❌ Missing in config.env: ${missing[*]}"; exit 1
fi

# INPUT_S3 can be derived from bucket+file, so warn if absent
if [[ -z "${INPUT_S3:-}" && -z "${S3_INPUT_BUCKET:-}" ]]; then
  echo "⚠️  INPUT_S3 not set and S3_INPUT_BUCKET empty; deploy script can derive if INPUT_FILE is provided."
fi

echo "✅ config.env looks sane."
