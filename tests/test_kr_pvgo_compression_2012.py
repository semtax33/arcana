from __future__ import annotations


def test_compression_backfill_is_exactly_scoped() -> None:
    from scripts.backfill_kr_pvgo_compression_2012 import (
        END_DATE,
        FACTOR_IDS,
        FINANCIAL_BASES,
        MARCAP_SHARES_PATH,
        START_DATE,
        START_WARMUP_DAYS,
    )

    assert START_DATE == "2012-01-01"
    assert END_DATE == "2012-12-31"
    assert FACTOR_IDS == ("pvgo_compression_pct",)
    assert FINANCIAL_BASES == ("ttm",)
    assert START_WARMUP_DAYS >= 104 * 7
    assert MARCAP_SHARES_PATH.name == "kr_pvgo_2011_2016_marcap_shares.csv"
