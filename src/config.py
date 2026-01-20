"""Centralized configuration module for patient-pipeline.

This module reads runtime environment variables and exposes them as
Python constants for use by other modules. It mirrors the env-derived
knobs that were previously defined at the top of `src.patient_risk_pipeline`.
Creating this file is a non-breaking change — it does not modify existing
files; it simply provides a single import target for future refactors.
"""
from __future__ import annotations

import os

# LLM / runtime knobs (env-override)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
GLOBAL_THROTTLE = float(os.getenv("OPENAI_THROTTLE_SEC", "0") or 0)
LLM_DISABLED = os.getenv("LLM_DISABLED", "false").lower() == "true"
CSV_CHUNK_ROWS = int(os.getenv("CSV_CHUNK_ROWS", "5000"))
OUTPUT_TMP = os.getenv("OUTPUT_TMP", "/tmp/output.csv")

# If true, prefer pandas+s3fs path reading; otherwise stream via boto3 (safer in containers)
USE_S3FS = os.getenv("USE_S3FS", "false").lower() == "true"

# LangChain wedge toggle (safe default: off)
USE_LANGCHAIN = os.getenv("USE_LANGCHAIN", "false").lower() == "true"

# Shared OpenAI runtime knobs (used by both direct OpenAI + LangChain wedge)
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0") or 0)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "800") or 800)
OPENAI_TIMEOUT_SEC = int(os.getenv("OPENAI_TIMEOUT_SEC", "60") or 60)

# LLM backend selection: openai (default) | sagemaker
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()

# SageMaker (used when LLM_PROVIDER=sagemaker)
SAGEMAKER_ENDPOINT_NAME = os.getenv("SAGEMAKER_ENDPOINT_NAME", "").strip()
SAGEMAKER_REGION = os.getenv("SAGEMAKER_REGION", os.getenv("AWS_REGION", "us-east-1")).strip()
SAGEMAKER_CONTENT_TYPE = os.getenv("SAGEMAKER_CONTENT_TYPE", "application/json").strip()

# Generation knobs for SageMaker path (and optional shared defaults)
GEN_TEMPERATURE = float(os.getenv("GEN_TEMPERATURE", str(OPENAI_TEMPERATURE)) or 0)
GEN_MAX_NEW_TOKENS = int(os.getenv("GEN_MAX_NEW_TOKENS", str(OPENAI_MAX_TOKENS)) or 800)

__all__ = [
    "OPENAI_MODEL",
    "GLOBAL_THROTTLE",
    "LLM_DISABLED",
    "CSV_CHUNK_ROWS",
    "OUTPUT_TMP",
    "USE_S3FS",
    "USE_LANGCHAIN",
    "OPENAI_TEMPERATURE",
    "OPENAI_MAX_TOKENS",
    "OPENAI_TIMEOUT_SEC",
    "LLM_PROVIDER",
    "SAGEMAKER_ENDPOINT_NAME",
    "SAGEMAKER_REGION",
    "SAGEMAKER_CONTENT_TYPE",
    "GEN_TEMPERATURE",
    "GEN_MAX_NEW_TOKENS",
]
