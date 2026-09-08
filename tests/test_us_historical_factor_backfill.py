from __future__ import annotations


def test_us_backfill_contract_freezes_exact_scope() -> None:
    from scripts.backfill_us_historical_factors import HistoricalBackfillContract

    contract = HistoricalBackfillContract.create(
        security_ids=["SEC_US_MSFT", "SEC_US_AAPL", "SEC_US_AAPL"],
        factor_ids=["roe", "ma_50"],
        start_date="2006-01-01",
        end_date="2016-12-31",
        bases=["ttm", "annual", "quarterly"],
    )

    assert contract.security_ids == ("SEC_US_AAPL", "SEC_US_MSFT")
    assert contract.factor_ids == ("ma_50", "roe")
    assert contract.bases == ("annual", "quarterly", "ttm")
    assert contract.years == tuple(range(2006, 2017))
    assert contract.raw_backup_table.startswith("fact_daily_factors_us_hist_backup_")
    assert contract.snapshot_backup_table.startswith(
        "fact_daily_factor_snapshot_us_hist_backup_"
    )


def test_us_backfill_contract_rejects_unsafe_scope() -> None:
    import pytest

    from scripts.backfill_us_historical_factors import HistoricalBackfillContract

    with pytest.raises(ValueError, match="SEC_US_"):
        HistoricalBackfillContract.create(
            security_ids=["SEC_KR_005930"],
            factor_ids=["roe"],
            start_date="2006-01-01",
            end_date="2016-12-31",
        )

    with pytest.raises(ValueError, match="start_date"):
        HistoricalBackfillContract.create(
            security_ids=["SEC_US_AAPL"],
            factor_ids=["roe"],
            start_date="2017-01-01",
            end_date="2016-12-31",
        )


def test_us_universe_query_is_price_and_date_scoped() -> None:
    from scripts.backfill_us_historical_factors import build_universe_query

    query, parameters = build_universe_query(
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert "FROM price_daily" in query
    assert "startsWith(security_id, {security_prefix:String})" in query
    assert "trade_date >= {start_date:Date}" in query
    assert "trade_date <= {end_date:Date}" in query
    assert "price_date_sum" in query
    assert "price_date_square_sum" in query
    assert parameters == {
        "security_prefix": "SEC_US_",
        "start_date": "2006-01-01",
        "end_date": "2016-12-31",
    }


def test_prepare_targets_recreates_missing_csv_without_resetting_status(
    tmp_path, monkeypatch
) -> None:
    import json

    import scripts.backfill_us_historical_factors as sut

    contract = sut.HistoricalBackfillContract.create(
        security_ids=["SEC_US_AAPL"],
        factor_ids=["roe"],
        start_date="2006-01-01",
        end_date="2016-12-31",
    )
    status = sut._initial_status(contract)
    status["factor_completed"]["annual"] = ["SEC_US_AAPL"]
    status["factor_inserted_rows"]["annual"] = 123
    status_path = tmp_path / "status.json"
    sut._write_json(status_path, status)
    target_path = tmp_path / "targets.csv"

    class _UniverseResult:
        column_names = [
            "security_id",
            "symbol",
            "min_price_date",
            "max_price_date",
            "price_row_count",
            "price_date_sum",
            "price_date_square_sum",
        ]
        result_rows = [
            (
                "SEC_US_AAPL",
                "AAPL",
                "2006-01-03",
                "2016-12-30",
                2,
                1,
                1,
            )
        ]

    class _UniverseClient:
        def query(self, query, parameters=None):
            return _UniverseResult()

    monkeypatch.setattr(sut, "_load_contract", lambda *args, **kwargs: contract)

    assert sut.prepare_targets(
        _UniverseClient(),
        target_path=target_path,
        status_path=status_path,
        start_date="2006-01-01",
        end_date="2016-12-31",
    ) == contract

    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert target_path.exists()
    assert persisted["factor_completed"]["annual"] == ["SEC_US_AAPL"]
    assert persisted["factor_inserted_rows"]["annual"] == 123


def test_price_input_coverage_requires_silver_and_clickhouse_to_match() -> None:
    import pandas as pd
    import pytest

    from scripts.backfill_us_historical_factors import compare_price_coverage

    expected = pd.DataFrame(
        [
            {
                "security_id": "SEC_US_AAPL",
                "min_price_date": "2016-01-04",
                "max_price_date": "2016-01-05",
                "price_row_count": 2,
                "price_date_sum": 33605,
                "price_date_square_sum": 564648013,
            }
        ]
    )
    actual = expected.copy()

    report = compare_price_coverage(expected, actual)

    assert report == {"price_security_count": 1, "price_row_count": 2}

    actual.loc[0, "price_date_square_sum"] += 1
    with pytest.raises(RuntimeError, match="Silver price input differs"):
        compare_price_coverage(expected, actual)


def test_us_scope_queries_include_every_contract_dimension() -> None:
    from scripts.backfill_us_historical_factors import (
        HistoricalBackfillContract,
        build_delete_query,
        build_snapshot_copy_query,
    )

    contract = HistoricalBackfillContract.create(
        security_ids=["SEC_US_AAPL"],
        factor_ids=["ma_50", "roe"],
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    delete_query, delete_parameters = build_delete_query(
        contract,
        table="fact_daily_factors",
        start_date="2008-01-01",
        end_date="2008-12-31",
        financial_basis="quarterly",
    )
    assert delete_query.startswith("ALTER TABLE fact_daily_factors DELETE WHERE")
    assert "security_id IN {security_ids:Array(String)}" in delete_query
    assert "trade_date >= {start_date:Date}" in delete_query
    assert "trade_date <= {end_date:Date}" in delete_query
    assert "financial_basis = {financial_basis:String}" in delete_query
    assert "has({factor_ids:Array(String)}, factor_id)" in delete_query
    assert delete_parameters["security_ids"] == ["SEC_US_AAPL"]
    assert delete_parameters["factor_ids"] == ["ma_50", "roe"]

    snapshot_query, snapshot_parameters = build_snapshot_copy_query(
        contract,
        year=2008,
        financial_basis="quarterly",
    )
    assert "INSERT INTO fact_daily_factor_snapshot" in snapshot_query
    assert "FROM fact_daily_factors FINAL" in snapshot_query
    assert "trade_date AS source_trade_date" in snapshot_query
    assert "isFinite(factor_value)" in snapshot_query
    assert snapshot_parameters["start_date"] == "2008-01-01"
    assert snapshot_parameters["end_date"] == "2008-12-31"
    assert snapshot_parameters["financial_basis"] == "quarterly"


class _QueryResult:
    def __init__(self, count: int):
        self.result_rows = [(count,)]


class _BackupClient:
    def __init__(self, counts: list[int]):
        self.counts = iter(counts)
        self.commands: list[str] = []

    def query(self, query, parameters=None):
        return _QueryResult(next(self.counts))

    def command(self, query, parameters=None, settings=None):
        self.commands.append(query)


def test_backup_mismatch_blocks_live_factor_delete(tmp_path) -> None:
    import pytest

    from scripts.backfill_us_historical_factors import (
        HistoricalBackfillContract,
        _initial_status,
        backup_and_delete_scope,
    )

    contract = HistoricalBackfillContract.create(
        security_ids=["SEC_US_AAPL"],
        factor_ids=["roe"],
        start_date="2016-01-01",
        end_date="2016-12-31",
    )
    client = _BackupClient([10, 9])

    with pytest.raises(RuntimeError, match="backup mismatch"):
        backup_and_delete_scope(
            client,
            contract,
            _initial_status(contract),
            table="fact_daily_factors",
            status_path=tmp_path / "status.json",
        )

    assert not any(
        command.startswith("ALTER TABLE fact_daily_factors DELETE")
        for command in client.commands
    )
