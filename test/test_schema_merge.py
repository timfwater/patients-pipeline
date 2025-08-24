# FILE: test/test_schema_merge.py
import pandas as pd
from pandas.api.types import DatetimeTZDtype

def test_merge_preserves_row_count_and_columns():
    # Original input rows
    df_original = pd.DataFrame({
        "idx": [1, 2, 3],
        "visit_date": pd.to_datetime(["2024-05-01", "2024-05-02", "2024-05-03"], utc=True),
        "full_note": ["a", "b", "c"],
        "physician_id": [1, 1, 2],
    })

    # Simulated processed/filtered output
    df_filtered = pd.DataFrame({
        "idx": [1, 3],
        "visit_date": pd.to_datetime(["2024-05-01", "2024-05-03"], utc=True),
        "risk_rating": ["Risk Score: 55", "Risk Score: 90"],
        "risk_score": [0.55, 0.90],
        "combined_response": ["...", "..."],
        "follow_up_1mo": ["No", "Yes"],
        "follow_up_6mo": ["No", "No"],
        "oncology_rec": ["No", "No"],
        "cardiology_rec": ["No", "Yes"],
        "top_concerns": ["HTN", "Chest pain\nHTN"],
    })

    columns_to_merge = [
        "idx", "visit_date", "risk_rating", "risk_score", "combined_response",
        "follow_up_1mo", "follow_up_6mo", "oncology_rec", "cardiology_rec", "top_concerns"
    ]

    df_final = df_original.merge(df_filtered[columns_to_merge], on=["idx", "visit_date"], how="left")

    # Row count preserved
    assert len(df_final) == len(df_original)

    # Expected columns exist
    for c in columns_to_merge:
        assert c in df_final.columns

    # Date dtype remains timezone-aware datetime
    assert isinstance(df_final["visit_date"].dtype, DatetimeTZDtype)

    # Rows without matches should have NaNs in merged columns (e.g., idx=2)
    row2 = df_final[df_final["idx"] == 2].iloc[0]
    assert pd.isna(row2["risk_score"])
    assert pd.isna(row2["combined_response"])
