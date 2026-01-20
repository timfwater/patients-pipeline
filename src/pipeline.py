"""Pipeline entrypoint shim.

This is a small, non-breaking scaffold that exposes the existing
`run_pipeline` (and `main`) from the legacy `src.patient_risk_pipeline`.
It allows other modules to import `from src.pipeline import run_pipeline`
while we incrementally move implementation into dedicated modules.
"""
from __future__ import annotations

from src.patient_risk_pipeline import run_pipeline as run_pipeline
from src.patient_risk_pipeline import main as main

__all__ = ["run_pipeline", "main"]
