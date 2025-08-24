# FILE: tests/test_extract_risk.py
import pytest
from src.patient_risk_pipeline import extract_risk_score

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Risk Score: 0", 0.0),
        ("Risk Score: 1", 0.01),          # supports raw integers 0–100
        ("Risk Score: 87", 0.87),
        ("Risk Score: 100", 1.0),
        (" Risk Score :  42 ", 0.42),     # extra spaces
        ("Risk Score: 55.0", 0.55),       # float-like number
    ],
)
def test_extract_risk_score_valid(text, expected):
    assert extract_risk_score(text) == expected

@pytest.mark.parametrize(
    "text",
    [
        "Risk Score: -1",      # below range
        "Risk Score: 101",     # above range
        "Risk Score: abc",     # non-numeric
        "no score here",       # missing
        None,                  # null-safe
        "",                    # empty string
        "Risk: 10",            # wrong label
    ],
)
def test_extract_risk_score_out_of_bounds_and_noise(text):
    assert extract_risk_score(text) is None
