from __future__ import annotations

import pandas as pd

from engine.transformers._internal.dart_filings import (
    repair_unit_scale_outliers_from_neighbor_reports,
)


def test_unit_scale_repair_only_changes_supported_rows(tmp_path):
    reference_path = tmp_path / "kr_normalized_000001_2025.06.csv"
    output_path = tmp_path / "kr_normalized_000001_2025.09.csv"
    pd.DataFrame(
        [
            {"canonical_account_id": "REVENUE", "statement_type": "IS", "normalized_amount": 1_000_000},
            {"canonical_account_id": "ASSET_A", "statement_type": "BS", "normalized_amount": 1_000_000_000},
            {"canonical_account_id": "ASSET_B", "statement_type": "BS", "normalized_amount": 2_000_000_000},
            {"canonical_account_id": "ASSET_C", "statement_type": "BS", "normalized_amount": 3_000_000_000},
        ]
    ).to_csv(reference_path, index=False)

    current = pd.DataFrame(
        [
            {
                "canonical_account_id": "REVENUE",
                "statement_type": "IS",
                "amount": "1200000000000",
                "raw_amount": "1200000000000",
                "normalized_amount": "1200000000000",
                "cash_effect_amount": "1200000000000",
                "unit_factor": "1000",
                "reason": "revenue",
            },
            {
                "canonical_account_id": "ASSET_A",
                "statement_type": "BS",
                "amount": "1000000000000000",
                "raw_amount": "1000000000000000",
                "normalized_amount": "1000000000000000",
                "cash_effect_amount": "1000000000000000",
                "unit_factor": "1000000",
                "reason": "asset-a",
            },
            {
                "canonical_account_id": "ASSET_B",
                "statement_type": "BS",
                "amount": "2000000000000000",
                "raw_amount": "2000000000000000",
                "normalized_amount": "2000000000000000",
                "cash_effect_amount": "2000000000000000",
                "unit_factor": "1000000",
                "reason": "asset-b",
            },
            {
                "canonical_account_id": "ASSET_C",
                "statement_type": "BS",
                "amount": "3000000000000000",
                "raw_amount": "3000000000000000",
                "normalized_amount": "3000000000000000",
                "cash_effect_amount": "3000000000000000",
                "unit_factor": "1000000",
                "reason": "asset-c",
            },
        ]
    )

    repaired = repair_unit_scale_outliers_from_neighbor_reports(current, output_path)

    revenue = repaired.loc[repaired["canonical_account_id"].eq("REVENUE")].iloc[0]
    asset_a = repaired.loc[repaired["canonical_account_id"].eq("ASSET_A")].iloc[0]
    assert float(revenue["raw_amount"]) == 1_200_000_000_000
    assert float(revenue["unit_factor"]) == 1_000
    assert "unit-scale repaired" not in revenue["reason"]
    assert float(asset_a["raw_amount"]) == 1_000_000_000
    assert float(asset_a["unit_factor"]) == 1
    assert "row unit-scale repaired" in asset_a["reason"]
