# syntax=docker/dockerfile:1.7

# ------------------------------------------------------------
# (Optional) Utilities stage for local debugging
#   Build with:  docker build --target=util -t patient-pipeline:util .
#   (Not used in prod)
# ------------------------------------------------------------
FROM python:3.11-slim AS util
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl awscli jq less vim ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace

# ------------------------------------------------------------
# Runtime image (final)
# ------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/tmp/huggingface \
    TRANSFORMERS_CACHE=/tmp/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# OS deps (HTTPS certs + tini as PID 1)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps (CPU-only torch to avoid huge CUDA wheels)
# NOTE: keep torch out of requirements.txt; install it here from the CPU index.
COPY requirements.txt .

RUN python -m pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
 && python -m pip install --no-cache-dir -r requirements.txt

# OPTIONAL: Pre-cache the embedding model at build time for deterministic first runs.
# You can override with:
#   docker build --build-arg RAG_EMBED_MODEL_ID=sentence-transformers/all-MiniLM-L6-v2 .
ARG RAG_EMBED_MODEL_ID=sentence-transformers/all-MiniLM-L6-v2
RUN python -c "from transformers import AutoTokenizer, AutoModel; \
              AutoTokenizer.from_pretrained('${RAG_EMBED_MODEL_ID}'); \
              AutoModel.from_pretrained('${RAG_EMBED_MODEL_ID}')" || true

# Copy application code
COPY . .

# Create non-root user and ensure write access to /app and /tmp
RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /app \
 && mkdir -p /tmp /tmp/huggingface \
 && chown -R appuser:appuser /tmp
USER appuser

# Use tini for proper signal handling on ECS
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: run your pipeline module
CMD ["python", "-u", "-m", "src.patient_risk_pipeline"]

# Lightweight healthcheck (replace with something app-specific if needed)
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "print('ok')"]
