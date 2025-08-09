# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Safer defaults + cleaner logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OS deps (curl for simple checks; awscli inside container if you really need it)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl awscli \
 && rm -rf /var/lib/apt/lists/*

# Leverage layer caching: copy requirements first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the rest of the code
COPY . .

# (Optional but recommended) run as non-root
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Default command (easier to override than ENTRYPOINT)
CMD ["python", "-u", "src/patient_risk_pipeline.py"]
