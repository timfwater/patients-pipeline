"""S3 and CSV I/O helpers (scaffold).

This module re-exports the chunked CSV reader and audit upload helper
from the existing `src.patient_risk_pipeline` to enable an incremental
refactor without changing behavior.
"""
from src.patient_risk_pipeline import (
    _parse_s3_uri,
    _read_csv_s3_in_chunks,
    log_audit_summary,
)

__all__ = ["_parse_s3_uri", "_read_csv_s3_in_chunks", "log_audit_summary"]
