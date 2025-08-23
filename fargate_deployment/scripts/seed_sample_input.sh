#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

: "${S3_INPUT_BUCKET:?Missing S3_INPUT_BUCKET in config.env}"
DEST="s3://${S3_INPUT_BUCKET}/Input/may_01_14.csv"

TMP="/tmp/patient_sample.csv"
cat > "$TMP" <<'CSV'
idx,visit_date,full_note,physician_id
101,2024-05-02,"Patient with HTN, DM2; missed follow-up.",1
102,2024-05-03,"New chest pain on exertion; EKG borderline.",1
103,2024-05-05,"Stable asthma; well-controlled; no ED visits.",2
CSV

aws s3 cp "$TMP" "$DEST" --region "${AWS_REGION:-us-east-1}"
echo "✅ Uploaded sample to $DEST"
