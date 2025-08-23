#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

: "${OUTPUT_S3:?Missing OUTPUT_S3}"
AUDIT_BUCKET="${AUDIT_BUCKET:-$(echo "$OUTPUT_S3" | sed -E 's#^s3://([^/]+)/.*#\1#')}"
AUDIT_PREFIX="${AUDIT_PREFIX:-audit_logs}"

OUT_DIR="${1:-/tmp/patient-pipeline-artifacts}"
mkdir -p "$OUT_DIR"

echo "📥 Downloading latest output & audit to $OUT_DIR"

# Output CSV
OUT_BUCKET="$(echo "$OUTPUT_S3" | sed -E 's#^s3://([^/]+)/.*#\1#')"
OUT_KEY="$(echo "$OUTPUT_S3" | sed -E 's#^s3://[^/]+/(.*)#\1#')"
aws s3 cp "s3://$OUT_BUCKET/$OUT_KEY" "$OUT_DIR/output.csv" --region "${AWS_REGION:-us-east-1}"

# Latest audit JSON
LATEST_AUDIT_KEY="$(aws s3 ls "s3://$AUDIT_BUCKET/$AUDIT_PREFIX/" --region "${AWS_REGION:-us-east-1}" \
  | awk '{print $4}' | sort | tail -n1)"
if [[ -n "$LATEST_AUDIT_KEY" ]]; then
  aws s3 cp "s3://$AUDIT_BUCKET/$AUDIT_PREFIX/$LATEST_AUDIT_KEY" "$OUT_DIR/$LATEST_AUDIT_KEY" --region "${AWS_REGION:-us-east-1}"
  echo "✅ Downloaded: $OUT_DIR/$LATEST_AUDIT_KEY"
else
  echo "⚠️  No audit logs found under s3://$AUDIT_BUCKET/$AUDIT_PREFIX/"
fi

echo "✅ Output CSV: $OUT_DIR/output.csv"
