#!/usr/bin/env bash
set -euo pipefail

# Print failing command + line on any error (so we can diagnose precisely)
trap 'code=$?; echo -e "\n❌ Failed at ${BASH_SOURCE[0]}:${LINENO} -> ${BASH_COMMAND} (exit $code)"; exit $code' ERR

# ------------- paths & config -------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Robust config.env discovery
CANDIDATES=(
  "$SCRIPT_DIR/../../config.env"   # repo root (expected)
  "$SCRIPT_DIR/../config.env"      # fargate_deployment/
  "$SCRIPT_DIR/config.env"         # scripts/ (last resort)
  "$(pwd)/config.env"              # current working dir
)

CONFIG_PATH=""
for p in "${CANDIDATES[@]}"; do
  if [[ -f "$p" ]]; then CONFIG_PATH="$p"; break; fi
done

if [[ -z "$CONFIG_PATH" ]]; then
  echo "❌ Could not find config.env. Searched:"
  printf '   - %s\n' "${CANDIDATES[@]}"
  exit 1
fi

echo "📄 Using config: $CONFIG_PATH"
# shellcheck disable=SC1090
source "$CONFIG_PATH"

ROOT_DIR="$(cd "$(dirname "$CONFIG_PATH")" && pwd)"

SETUP_IAM="$SCRIPT_DIR/setup_iam.sh"
BUILD_PUSH="$SCRIPT_DIR/build_and_push.sh"
DEPLOY="$SCRIPT_DIR/deploy_to_fargate.sh"

# Ensure helper scripts exist + are executable
for f in "$SETUP_IAM" "$BUILD_PUSH" "$DEPLOY"; do
  [[ -f "$f" ]] || { echo "❌ Missing script: $f"; exit 1; }
done
chmod +x "$SETUP_IAM" "$BUILD_PUSH" "$DEPLOY" || true

# ------------- pretty printing -------------
cyan()  { echo -e "\033[1;36m$*\033[0m"; }
green() { echo -e "\033[1;32m$*\033[0m"; }
yellow(){ echo -e "\033[1;33m$*\033[0m"; }
blue()  { echo -e "\033[1;34m$*\033[0m"; }
red()   { echo -e "\033[1;31m$*\033[0m"; }

blue "\n====================================="
blue "🚀 Running FULL patient-pipeline flow"
blue "=====================================\n"

# ------------- knobs (simple, memorable) -------------
SKIP_IAM="${SKIP_IAM:-false}"
SKIP_BUILD="${SKIP_BUILD:-false}"
SKIP_DEPLOY="${SKIP_DEPLOY:-false}"
FAST="${FAST:-false}"
NO_CACHE="${NO_CACHE:-false}"

# Assign public IP auto by default (deploy script will also auto-detect)
export ASSIGN_PUBLIC_IP="${ASSIGN_PUBLIC_IP:-AUTO}"

# ------------- quick preflight -------------
yellow "🧪 Preflight checks..."
command -v aws >/dev/null 2>&1 || { red "AWS CLI not found"; exit 1; }
command -v jq  >/dev/null 2>&1 || { red "jq not found"; exit 1; }
aws sts get-caller-identity --query Account --output text >/dev/null 2>&1 \
  || { red "Unable to call STS. Check AWS creds/profile/region."; exit 1; }
if ! docker info >/dev/null 2>&1; then
  red "Docker not running or not accessible."
  exit 1
fi
# Hard config sanity (fail early with clear messages)
: "${AWS_REGION:?Missing AWS_REGION in config.env}"
: "${AWS_ACCOUNT_ID:?Missing AWS_ACCOUNT_ID in config.env}"
: "${ECR_REPO_URI:?Missing ECR_REPO_URI in config.env}"
: "${TASK_FAMILY:?Missing TASK_FAMILY in config.env}"
: "${ECS_CLUSTER_NAME:?Missing ECS_CLUSTER_NAME in config.env}"
: "${LOG_GROUP:?Missing LOG_GROUP in config.env}"
: "${FARGATE_SUBNET_IDS:?Missing FARGATE_SUBNET_IDS in config.env}"
: "${FARGATE_SECURITY_GROUP_IDS:?Missing FARGATE_SECURITY_GROUP_IDS in config.env}"
green "✅ Preflight OK."

# ------------- smart IMAGE_TAG -------------
if [[ -z "${IMAGE_TAG:-}" ]]; then
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    IMAGE_TAG="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  else
    HASH=$( (cd "$ROOT_DIR" && { cat Dockerfile 2>/dev/null || true; cat requirements.txt 2>/dev/null || true; find src -type f -print0 2>/dev/null | sort -z | xargs -0 cat 2>/dev/null || true; } | shasum | awk '{print $1}') )
    TS=$(date +%Y%m%d-%H%M%S)
    IMAGE_TAG="${TS}-${HASH:0:8}"
  fi
fi
export IMAGE_TAG

cyan "🧾 IMAGE_TAG: $IMAGE_TAG"
cyan "⚙️  Mode: SKIP_IAM=$SKIP_IAM  SKIP_BUILD=$SKIP_BUILD  SKIP_DEPLOY=$SKIP_DEPLOY  FAST=$FAST  NO_CACHE=$NO_CACHE  ASSIGN_PUBLIC_IP=$ASSIGN_PUBLIC_IP"

START_ALL=$(date +%s)

# ------------- Step 1: IAM (idempotent) -------------
run_iam=true
if [[ "$SKIP_IAM" == "true" ]]; then
  run_iam=false
elif [[ "$FAST" == "true" ]]; then
  if aws iam get-role --role-name "${EXECUTION_ROLE_NAME:-PatientPipelineECSExecutionRole}" >/dev/null 2>&1 \
     && aws iam get-role --role-name "${TASK_ROLE_NAME:-PatientPipelineECSTaskRole}" >/dev/null 2>&1; then
    run_iam=false
  fi
fi

if $run_iam; then
  STEP_START=$(date +%s)
  yellow "🔐 Step 1: Setting up IAM roles & policies..."
  "$SETUP_IAM"
  green "✅ IAM setup complete. ($(($(date +%s)-STEP_START))s)"
else
  green "✅ Step 1: IAM setup skipped."
fi

# ------------- Step 2: Build & Push -------------
run_build=true
if [[ "$SKIP_BUILD" == "true" ]]; then
  run_build=false
elif [[ "$FAST" == "true" ]]; then
  REPO="${ECR_REPO_URI##*/}"
  if aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$IMAGE_TAG" --region "$AWS_REGION" >/dev/null 2>&1; then
    run_build=false
  fi
fi

if $run_build; then
  STEP_START=$(date +%s)
  yellow "🐳 Step 2: Building & pushing Docker image..."
  # FORCE_BUILD=true so we don’t silently reuse a stale local build; tag still deduped by ECR
  FORCE_BUILD=true NO_CACHE="$NO_CACHE" "$BUILD_PUSH"
  green "✅ Build & push complete. ($(($(date +%s)-STEP_START))s)"
else
  green "✅ Step 2: Build skipped (image tag exists)."
  # Ensure deploy has an image URI even if we skipped the build script
  echo "${ECR_REPO_URI}:${IMAGE_TAG}" > "$ROOT_DIR/.last_image_uri"
fi

# Always ensure .last_image_uri points to what we intend to deploy
if [[ ! -s "$ROOT_DIR/.last_image_uri" ]]; then
  echo "${ECR_REPO_URI}:${IMAGE_TAG}" > "$ROOT_DIR/.last_image_uri"
fi
DEP_IMG="$(cat "$ROOT_DIR/.last_image_uri")"

# ------------- Step 3: Deploy -------------
if [[ "$SKIP_DEPLOY" == "true" ]]; then
  yellow "⏭️  Step 3: Deploy skipped (SKIP_DEPLOY=true)."
else
  yellow "🚢 Step 3: Deploying to Fargate..."
  cyan "📦 Deploying image: $DEP_IMG"
  # Pass IMAGE_URI explicitly (deploy script can still auto-resolve if unset)
  export IMAGE_URI="$DEP_IMG"

  set +e
  STEP_START=$(date +%s)
  "$DEPLOY"
  DEPLOY_RC=$?
  set -e

  if [[ $DEPLOY_RC -ne 0 ]]; then
    red "❌ Deployment failed (exit $DEPLOY_RC). See logs above for stoppedReason/container reason."
    exit $DEPLOY_RC
  fi
  green "✅ Deployment complete. ($(($(date +%s)-STEP_START))s)"
fi

ELAPSED_ALL=$(($(date +%s)-START_ALL))
blue "\n🎉 All steps finished successfully in ${ELAPSED_ALL}s.\n"
