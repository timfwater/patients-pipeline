"""LLM helper shim.

This lightweight module re-exports LLM-related helpers from
`src.patient_risk_pipeline` so other modules can import `src.llm`
during an incremental refactor without changing runtime behavior.
Do not add heavy logic here — it's a temporary compatibility layer.
"""
from src.patient_risk_pipeline import (
    _extract_openai_key_from_secret_string,
    _get_openai_key_from_secrets,
    get_openai_key,
    normalize_hf_parameters,
    _invoke_sagemaker_textgen,
    get_chat_response,
    OPENAI_CLIENT,
)

__all__ = [
    "_extract_openai_key_from_secret_string",
    "_get_openai_key_from_secrets",
    "get_openai_key",
    "normalize_hf_parameters",
    "_invoke_sagemaker_textgen",
    "get_chat_response",
    "OPENAI_CLIENT",
]
