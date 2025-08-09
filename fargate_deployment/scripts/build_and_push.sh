#!/usr/bin/env bash
set -euo pipefail

# -----------------------
# Load configuration
# -----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/config.env"

# Pretty banners
cyan()  { echo -e "\033[1;36m$*\033[0m"; }
green() { echo -e "\033[1;32m$*\033[0m"; }
yellow(){ echo -e "\033[1;33m$*\033[0m"; }
red()   { echo -e "\033[1;31m$*\033[0m"; }
blue()  { echo -e "\033[1;34m$*\033[0m"; }

# -----------------------
# Configurable knobs
# -----------------------
IMAGE_TAG="${IMAGE_TAG:-latest}"
FORCE_BUILD="${FORCE_BUILD:-false}"
NO_CACHE="${NO_CACHE:-false}"

# -----------------------
# Derive IMAGE_URI if not set
# -----------------------
if [[ -z "${IMAGE_URI:-}" ]]; then
  if [[ -n "${ECR_REPO_URI:-}" ]]; then
    IMAGE_URI="${ECR_REPO_URI}:${IMAGE_TAG}"
  else
    red "❌ Provide ECR_REPO_URI in config.env (or set IMAGE_URI)"; exit 1
  fi
fi

IMAGE_REPO="${IMAGE_URI%:*}"
REPO_NAME="${IMAGE_REPO##*/}"
TAG="${IMAGE_URI##*:}"
ECR_HOST="${IMAGE_REPO%/*}"
EXPECTED_HOST="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

blue "\n=============================="
blue "🐳 Build & Push Docker Image"
blue "=============================="
cyan "Repo: $IMAGE_REPO"
cyan "Tag : $TAG\n"

# Sanity: ECR host matches account/region
if [[ "$ECR_HOST" != "$EXPECTED_HOST" ]]; then
  yellow "⚠️  IMAGE_REPO host (${ECR_HOST}) != expected ${EXPECTED_HOST}"
  yellow "   Check AWS_ACCOUNT_ID/AWS_REGION/ECR_REPO_URI in config.env"
fi

# Ensure ECR repo exists
if ! aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  yellow "📦 ECR repo '$REPO_NAME' not found — creating..."
  aws ecr create-repository --repository-name "$REPO_NAME" --region "$AWS_REGION" >/dev/null
  green "✅ Created ECR repo."
fi

# Helper: does tag already exist?
tag_exists() {
  aws ecr describe-images \
    --repository-name "$REPO_NAME" \
    --image-ids imageTag="$TAG" \
    --region "$AWS_REGION" >/dev/null 2>&1
}

# Build + push (or skip)
if [[ "$FORCE_BUILD" != "true" ]] && tag_exists; then
  green "✅ Image already exists in ECR: ${IMAGE_REPO}:${TAG} — skipping build (set FORCE_BUILD=true to rebuild)."
else
  # Docker availability (only needed if we build)
  command -v docker >/dev/null 2>&1 || { red "❌ Docker CLI not found on PATH."; exit 1; }
  docker info >/dev/null 2>&1 || { red "❌ Docker is not running or not accessible."; exit 1; }

  yellow "🔐 Logging in to ECR..."
  aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "${EXPECTED_HOST}"
  green "✅ Authenticated to ECR."

  [[ -f "$ROOT_DIR/.dockerignore" ]] || yellow "ℹ️  Consider adding a .dockerignore to speed up builds."

  yellow "🔨 Building Docker image (platform linux/amd64 for Fargate)..."
  # Use a scalar flag to avoid array expansion issues under 'set -u'
  NO_CACHE_FLAG=""
  [[ "${NO_CACHE}" == "true" ]] && NO_CACHE_FLAG="--no-cache"

  export DOCKER_BUILDKIT=1
  docker build --platform linux/amd64 -t "${REPO_NAME}:${TAG}" ${NO_CACHE_FLAG} "$ROOT_DIR"
  green "✅ Docker build complete."

  yellow "🏷️  Tagging image -> ${IMAGE_REPO}:${TAG}"
  docker tag "${REPO_NAME}:${TAG}" "${IMAGE_REPO}:${TAG}"

  yellow "🚀 Pushing image to ECR..."
  docker push "${IMAGE_REPO}:${TAG}"
  green "✅ Docker push complete."
fi

# Save last image URI for deploy (always)
echo "${IMAGE_REPO}:${TAG}" > "$ROOT_DIR/.last_image_uri"
cyan "📄 Saved image URI to $ROOT_DIR/.last_image_uri"
green "🎯 Image ready: ${IMAGE_REPO}:${TAG}\n"
