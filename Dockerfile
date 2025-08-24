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
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# OS deps (HTTPS certs + tini as PID 1)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user and ensure write access to /app and /tmp
RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /app \
 && mkdir -p /tmp && chown -R appuser:appuser /tmp
USER appuser

# Use tini for proper signal handling on ECS
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: run your pipeline module
CMD ["python", "-u", "-m", "src.patient_risk_pipeline"]

# Lightweight healthcheck (replace with something app-specific if needed)
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "print('ok')"]
