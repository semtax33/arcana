from __future__ import annotations


def test_bridge_contract_is_ttm_and_has_exact_strategy_inputs() -> None:
    from scripts.backfill_kr_pvgo_factors_2012_2016 import (
        END_DATE,
        FACTOR_IDS,
        FINANCIAL_DIR,
        FINANCIAL_BASES,
        REPORT_METADATA_PATH,
        START_DATE,
    )

    assert START_DATE == "2012-01-01"
    assert END_DATE == "2016-12-31"
    assert FINANCIAL_BASES == ("ttm",)
    assert set(FACTOR_IDS) == {
        "pvgo_gap_pct",
        "roiic_wacc_spread",
        "pvgo_compression_pct",
        "pvgo_pct",
    }
    assert FINANCIAL_DIR.name == "pvgo-2012-2016-normalized"
    assert REPORT_METADATA_PATH.name == "kr_pvgo_2012_2016_report_metadata.csv"


def test_target_query_uses_final_kr_price_universe_and_exact_dates() -> None:
    from scripts.backfill_kr_pvgo_factors_2012_2016 import build_target_query

    query = build_target_query()

    assert "FROM price_daily FINAL" in query
    assert "startsWith(security_id, 'SEC_KR_')" in query
    assert "BETWEEN {start_date:Date} AND {end_date:Date}" in query
    assert "GROUP BY security_id" in query
