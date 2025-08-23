# FILE: tests/test_extract_risk.py
from src.patient_risk_pipeline import extract_risk_score

def test_extract_risk_score_basic():
    assert extract_risk_score("Risk Score: 0") == 0.0
    assert extract_risk_score("Risk Score: 87") == 0.87
    assert extract_risk_score("Risk Score: 100") == 1.0

def test_extract_risk_score_out_of_bounds_and_noise():
    assert extract_risk_score("Risk Score: -1") is None
    assert extract_risk_score("Risk Score: 101") is None
    assert extract_risk_score("no score here") is None
    assert extract_risk_score(None) is None
