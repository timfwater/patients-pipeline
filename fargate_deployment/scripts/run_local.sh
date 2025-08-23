#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

export PYTHONPATH="$ROOT_DIR/src:$PYTHONPATH"
# Optional cheap mode
export DRY_RUN_EMAIL="${DRY_RUN_EMAIL:-true}"
export MAX_NOTES="${MAX_NOTES:-5}"

python3 "$ROOT_DIR/src/patient_risk_pipeline.py"
