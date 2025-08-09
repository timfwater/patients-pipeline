#!/usr/bin/env bash
set -euo pipefail
: "${DEBUG:=false}"; $DEBUG && set -x

# Print failing command + line on any error (for quick diagnosis)
trap 'code=$?; echo -e "\n❌ Failed at ${BASH_SOURCE[0]}:${LINENO} -> ${BASH_COMMAND} (exit $code)"; exit $code' ERR

# -----------------------
# Paths & config loading
# -----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load config.env if present (don’t hard fail; we’ll fill gaps from AWS CLI)
if [[ -f "$ROOT_DIR/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/config.env"
fi

# Pretty banners
cyan()  { echo -e "\033[1;36m$*\033[0m"; }
green() { echo -e "\033[1;32m$*\033[0m"; }
yellow(){ echo -e "\033[1;33m$*\033[0m"; }
red()   { echo -e "\033[1;31m$*\033[0m"; }
blue()  { echo -e "\033[1;34m$*\033[0m"; }

# -----------------------
# Defaults / fallbacks
# -----------------------
# Region/account: fall back to AWS CLI if not exported by shell/config
: "${AWS_REGION:=$(aws configure get region 2>/dev/null || echo us-east-1)}"
if [[ -z "${AWS_ACCOUNT_ID:-}" || "${AWS_ACCOUNT_ID}" == "<empty>" ]]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
fi

# ECR naming defaults
: "${ECR_REPO_NAME:=patient-pipeline}"
: "${ECR_REPO_URI:=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}}"

# Tags / knobs
IMAGE_TAG="${IMAGE_TAG:-}"
FORCE_BUILD="${FORCE_BUILD:-false}"
NO_CACHE="${NO_CACHE:-false}"

# If tag not specified: prefer git short SHA; else timestamp
if [[ -z "$IMAGE_TAG" ]]; then
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    IMAGE_TAG="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  else
    IMAGE_TAG="$(date +%Y%m%d-%H%M%S)"
  fi
fi

# Derive IMAGE_URI if not set (compat with your previous flow)
if [[ -z "${IMAGE_URI:-}" ]]; then
  IMAGE_URI="${ECR_REPO_URI}:${IMAGE_TAG}"
fi

IMAGE_REPO="${IMAGE_URI%:*}"
REPO_NAME="${IMAGE_REPO##*/}"
TAG="${IMAGE_URI##*:}"
ECR_HOST="${IMAGE_REPO%/*}"
EXPECTED_HOST="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Docker timeouts (helpful on slower networks/VPN)
export DOCKER_CLIENT_TIMEOUT="${DOCKER_CLIENT_TIMEOUT:-300}"
export COMPOSE_HTTP_TIMEOUT="${COMPOSE_HTTP_TIMEOUT:-300}"
export DOCKER_BUILDKIT=1

blue "\n=============================="
blue "🐳 Build & Push Docker Image"
blue "=============================="
cyan "Account: $AWS_ACCOUNT_ID"
cyan "Region : $AWS_REGION"
cyan "Repo   : $IMAGE_REPO"
cyan "Tag    : $TAG\n"

# Sanity: ECR host matches account/region
if [[ "$ECR_HOST" != "$EXPECTED_HOST" ]]; then
  yellow "⚠️  IMAGE_REPO host (${ECR_HOST}) != expected ${EXPECTED_HOST}"
  yellow "   Check AWS_ACCOUNT_ID/AWS_REGION/ECR_REPO_URI in config.env"
fi

# -----------------------
# Ensure ECR repo exists
# -----------------------
if ! aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  yellow "📦 ECR repo '$REPO_NAME' not found — creating..."
  aws ecr create-repository --repository-name "$REPO_NAME" --region "$AWS_REGION" >/dev/null
  green "✅ Created ECR repo."
fi

# -----------------------
# Helpers
# -----------------------
docker_login() {
  yellow "🔐 Logging in to ECR..."
  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "${EXPECTED_HOST}"
  green "✅ Authenticated to ECR."
}

tag_exists() {
  aws ecr describe-images \
    --repository-name "$REPO_NAME" \
    --image-ids imageTag="$TAG" \
    --region "$AWS_REGION" >/dev/null 2>&1
}

# -----------------------
# Build (if needed)
# -----------------------
if [[ "$FORCE_BUILD" != "true" ]] && tag_exists; then
  green "✅ Image already exists in ECR: ${IMAGE_REPO}:${TAG} — skipping build (set FORCE_BUILD=true to rebuild)."
else
  command -v docker >/dev/null 2>&1 || { red "❌ Docker CLI not found on PATH."; exit 1; }
  docker info >/dev/null 2>&1 || { red "❌ Docker is not running or not accessible."; exit 1; }

  docker_login

  [[ -f "$ROOT_DIR/.dockerignore" ]] || yellow "ℹ️  Consider adding a .dockerignore to speed up builds."

  yellow "🔨 Building Docker image (platform linux/amd64 for Fargate)..."
  local_tag="${REPO_NAME}:${TAG}"
  NO_CACHE_FLAG=""
  [[ "$NO_CACHE" == "true" ]] && NO_CACHE_FLAG="--no-cache"

  docker build --platform linux/amd64 -t "$local_tag" $NO_CACHE_FLAG "$ROOT_DIR"
  green "✅ Docker build complete."

  yellow "🏷️  Tagging image -> ${IMAGE_REPO}:${TAG}"
  docker tag "$local_tag" "${IMAGE_REPO}:${TAG}"

  # -----------------------
  # Push with retries
  # -----------------------
  MAX_TRIES=4
  DELAY=3
  for attempt in $(seq 1 $MAX_TRIES); do
    yellow "🚀 Pushing image to ECR (attempt $attempt/$MAX_TRIES)..."
    if docker push "${IMAGE_REPO}:${TAG}"; then
      green "✅ Docker push complete."
      break
    fi
    rc=$?
    yellow "⚠️  Push failed (rc=$rc). Re-authenticating and retrying after ${DELAY}s..."
    docker_login || true
    sleep "$DELAY"
    DELAY=$((DELAY*2))
    if [[ $attempt -eq $MAX_TRIES ]]; then
      red "❌ Push failed after $MAX_TRIES attempts."
      exit $rc
    fi
  done
fi

# -----------------------
# Save last image for deploy
# -----------------------
echo "${IMAGE_REPO}:${TAG}" > "$ROOT_DIR/.last_image_uri"
cyan "📄 Saved image URI to $ROOT_DIR/.last_image_uri"
green "🎯 Image ready: ${IMAGE_REPO}:${TAG}\n"
