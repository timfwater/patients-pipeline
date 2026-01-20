"""LangChain wedge shim.

Scaffold that re-exports LangChain-related helpers from
`src.patient_risk_pipeline` to enable an incremental refactor while
keeping runtime behavior identical.
"""
from src.patient_risk_pipeline import (
    _risk_rating_via_langchain,
    query_combined_prompt,
)

__all__ = ["_risk_rating_via_langchain", "query_combined_prompt"]
