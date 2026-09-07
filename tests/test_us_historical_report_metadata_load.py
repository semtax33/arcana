from __future__ import annotations


def test_us_metadata_contract_and_scope_are_exact() -> None:
    from scripts.load_us_historical_report_metadata import (
        HistoricalMetadataContract,
        build_scope_filter,
    )

    contract = HistoricalMetadataContract.create(
        ["SEC_US_MSFT", "SEC_US_AAPL", "SEC_US_AAPL"],
        start_year=2006,
        end_year=2016,
    )
    clause, parameters = build_scope_filter(contract)

    assert contract.security_ids == ("SEC_US_AAPL", "SEC_US_MSFT")
    assert clause == (
        "security_id IN {security_ids:Array(String)} "
        "AND fiscal_year >= {start_year:UInt16} "
        "AND fiscal_year <= {end_year:UInt16}"
    )
    assert parameters == {
        "security_ids": ["SEC_US_AAPL", "SEC_US_MSFT"],
        "start_year": 2006,
        "end_year": 2016,
    }
    assert contract.backup_table.startswith(
        "dart_report_metadata_us_hist_backup_2006_2016_"
    )


def test_us_metadata_contract_rejects_non_us_or_reverse_years() -> None:
    import pytest

    from scripts.load_us_historical_report_metadata import HistoricalMetadataContract

    with pytest.raises(ValueError, match="SEC_US_"):
        HistoricalMetadataContract.create(["SEC_KR_005930"], 2006, 2016)
    with pytest.raises(ValueError, match="start_year"):
        HistoricalMetadataContract.create(["SEC_US_AAPL"], 2017, 2016)
