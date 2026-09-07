from __future__ import annotations

import pandas as pd


def test_backfill_contract_freezes_scope_and_snapshot_copy() -> None:
    from scripts.backfill_kr_historical_factors import (
        BackfillContract,
        build_incomplete_batch_cleanup_query,
        build_missing_target_query,
        build_snapshot_copy_query,
        recover_incomplete_batch,
        recover_incomplete_snapshot,
    )

    contract = BackfillContract.create(
        security_ids=["SEC_KR_000075", "SEC_KR_000060"],
        factor_ids=["roe", "ma_50"],
        start_date="2002-01-01",
        end_date="2012-12-31",
        bases=["ttm", "annual", "quarterly"],
    )

    assert contract.security_ids == ("SEC_KR_000060", "SEC_KR_000075")
    assert contract.factor_ids == ("ma_50", "roe")
    assert contract.bases == ("annual", "quarterly", "ttm")
    assert (
        contract.target_sha256
        == "e79f813bc57d1846f99b44ac14c3ef22bbdd488ddaa16fac58a5c52e9f258995"
    )
    assert (
        contract.factor_sha256
        == "bcab6199603a4565f3504ddcd6f97858add568fbd2b794adaed757872ccc876b"
    )

    query, parameters = build_snapshot_copy_query(
        contract,
        year=2002,
        financial_basis="annual",
    )

    assert "INSERT INTO fact_daily_factor_snapshot" in query
    assert "FROM fact_daily_factors FINAL" in query
    assert "source_trade_date" in query
    assert "security_id IN {security_ids:Array(String)}" in query
    assert "isFinite(factor_value)" in query
    assert parameters == {
        "start_date": "2002-01-01",
        "end_date": "2002-12-31",
        "financial_basis": "annual",
        "factor_ids": ["ma_50", "roe"],
        "security_ids": ["SEC_KR_000060", "SEC_KR_000075"],
    }

    target_query, target_parameters = build_missing_target_query(
        start_date="2002-01-01",
        end_date="2012-12-31",
        bases=contract.bases,
    )
    assert "LEFT ANTI JOIN existing" in target_query
    assert "FROM price_daily" in target_query
    assert target_parameters == {
        "start_date": "2002-01-01",
        "end_date": "2012-12-31",
        "bases": ["annual", "quarterly", "ttm"],
    }

    cleanup_query, cleanup_parameters = build_incomplete_batch_cleanup_query(
        contract,
        financial_basis="quarterly",
        security_ids=["SEC_KR_000075"],
    )
    assert cleanup_query.startswith("ALTER TABLE fact_daily_factors DELETE WHERE")
    assert "trade_date >= {start_date:Date}" in cleanup_query
    assert "trade_date <= {end_date:Date}" in cleanup_query
    assert "security_id IN {security_ids:Array(String)}" in cleanup_query
    assert cleanup_parameters["security_ids"] == ["SEC_KR_000075"]
    assert cleanup_parameters["financial_basis"] == "quarterly"

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def command(self, query: str, *, parameters: dict[str, object]) -> None:
            self.calls.append((query, parameters))

    client = RecordingClient()
    status = {
        "in_progress": {
            "financial_basis": "annual",
            "security_ids": ["SEC_KR_000060"],
        }
    }
    assert recover_incomplete_batch(client, contract, status) is True
    assert status["in_progress"] is None
    assert len(client.calls) == 1
    assert client.calls[0][1]["security_ids"] == ["SEC_KR_000060"]
    assert recover_incomplete_batch(client, contract, status) is False

    snapshot_status = {
        "snapshot_in_progress": {"financial_basis": "ttm", "year": 2002}
    }
    assert recover_incomplete_snapshot(client, contract, snapshot_status) is True
    assert snapshot_status["snapshot_in_progress"] is None
    assert len(client.calls) == 2
    assert "fact_daily_factor_snapshot DELETE" in client.calls[1][0]
    assert client.calls[1][1]["financial_basis"] == "ttm"
    assert client.calls[1][1]["start_date"] == "2002-01-01"
    assert client.calls[1][1]["end_date"] == "2002-12-31"
    assert recover_incomplete_snapshot(client, contract, snapshot_status) is False


def test_factor_coverage_uses_price_opportunities_as_the_denominator() -> None:
    from scripts.backfill_kr_historical_factors import calculate_factor_coverage

    rows = calculate_factor_coverage(
        source_rows=[
            {
                "year": 2002,
                "financial_basis": "annual",
                "security_count": 2,
                "factor_count": 3,
                "row_count": 60,
                "checksum": 7,
            }
        ],
        price_rows=[{"year": 2002, "security_count": 4, "price_row_count": 100}],
        years=[2002],
        bases=["annual"],
        contracted_factor_count=5,
    )

    assert rows == [
        {
            "year": 2002,
            "financial_basis": "annual",
            "price_security_count": 4,
            "factor_security_count": 2,
            "security_coverage_pct": 50.0,
            "materialized_factor_count": 3,
            "contracted_factor_count": 5,
            "factor_id_coverage_pct": 60.0,
            "finite_factor_cell_count": 60,
            "possible_factor_cell_count": 500,
            "factor_cell_coverage_pct": 12.0,
            "checksum": 7,
        }
    ]


def test_basis_invariant_copy_excludes_any_security_with_usable_report_metadata() -> None:
    from scripts.backfill_kr_historical_factors import (
        BackfillContract,
        basis_invariant_security_ids,
        build_basis_invariant_copy_query,
    )

    contract = BackfillContract.create(
        security_ids=["SEC_KR_000060", "SEC_KR_000075", "SEC_KR_000149"],
        factor_ids=["ma_50", "roe"],
        start_date="2002-01-01",
        end_date="2012-12-31",
    )
    metadata = pd.DataFrame(
        [
            {"stock_code": "000060", "report_date": "2010-03-31"},
            {"stock_code": "000075", "report_date": "2013-03-31"},
            {"stock_code": "000149", "report_date": "2001-03-31"},
            {"stock_code": "999999", "report_date": "2010-03-31"},
        ]
    )

    invariant_ids = basis_invariant_security_ids(contract, metadata)

    assert invariant_ids == ("SEC_KR_000075",)
    query, parameters = build_basis_invariant_copy_query(
        contract,
        financial_basis="quarterly",
        security_ids=invariant_ids,
    )
    assert "INSERT INTO fact_daily_factors" in query
    assert f"FROM {contract.annual_stage_table} AS source_rows FINAL" in query
    assert "'annual'" not in query
    assert "financial_basis = {source_basis:String}" in query
    assert "source_rows.financial_basis = {source_basis:String}" in query
    assert "{target_basis:String} AS financial_basis" in query
    assert "security_id IN {security_ids:Array(String)}" in query
    assert "isFinite(source_rows.factor_value)" in query
    assert parameters["source_basis"] == "annual"
    assert parameters["target_basis"] == "quarterly"
    assert parameters["security_ids"] == ["SEC_KR_000075"]


def test_aggregate_factor_coverage_uses_factor_id_union_and_weighted_cells() -> None:
    from scripts.backfill_kr_historical_factors import aggregate_factor_coverage

    rows = aggregate_factor_coverage(
        source_rows=[
            {
                "year": 2002,
                "financial_basis": "annual",
                "security_count": 2,
                "factor_ids": ["a", "b"],
                "row_count": 60,
            },
            {
                "year": 2003,
                "financial_basis": "annual",
                "security_count": 3,
                "factor_ids": ["b", "c"],
                "row_count": 90,
            },
        ],
        price_rows=[
            {"year": 2002, "security_count": 4, "price_row_count": 100},
            {"year": 2003, "security_count": 5, "price_row_count": 200},
        ],
        bases=["annual"],
        contracted_factor_count=5,
    )

    assert rows == [
        {
            "financial_basis": "annual",
            "price_security_year_count": 9,
            "factor_security_year_count": 5,
            "security_year_coverage_pct": 55.555556,
            "materialized_factor_count": 3,
            "contracted_factor_count": 5,
            "factor_id_coverage_pct": 60.0,
            "finite_factor_cell_count": 150,
            "possible_factor_cell_count": 1500,
            "factor_cell_coverage_pct": 10.0,
        }
    ]


def test_coverage_query_can_be_bounded_to_one_year_and_basis() -> None:
    from scripts.backfill_kr_historical_factors import _coverage_query

    query = _coverage_query("fact_daily_factors", one_basis=True)
    snapshot_query = _coverage_query(
        "fact_daily_factor_snapshot", one_basis=True
    )

    assert "trade_date >= {start_date:Date}" in query
    assert "trade_date <= {end_date:Date}" in query
    assert "financial_basis = {financial_basis:String}" in query
    assert "GROUP BY year, financial_basis" in query
    assert "cityHash64(toString(tuple(" in query
    assert "factor_value, trade_date, fiscal_year" in query
    assert "factor_value, source_trade_date, fiscal_year" in snapshot_query


def test_market_applicable_coverage_rebases_only_explicitly_excluded_denominator() -> None:
    from scripts.backfill_kr_historical_factors import (
        calculate_market_applicable_factor_coverage,
    )

    rows = calculate_market_applicable_factor_coverage(
        source_rows=[
            {
                "year": 2002,
                "financial_basis": "annual",
                "security_count": 2,
                "factor_count": 2,
                "factor_ids": ["ma_50", "roe"],
                "row_count": 60,
                "checksum": 7,
            }
        ],
        price_rows=[{"year": 2002, "security_count": 4, "price_row_count": 100}],
        years=[2002],
        bases=["annual"],
        global_factor_ids=["ma_50", "roe", "us_eps_consensus"],
        applicable_factor_ids=["ma_50", "roe"],
    )

    assert rows[0]["materialized_factor_count"] == 2
    assert rows[0]["contracted_factor_count"] == 2
    assert rows[0]["factor_id_coverage_pct"] == 100.0
    assert rows[0]["possible_factor_cell_count"] == 200
    assert rows[0]["factor_cell_coverage_pct"] == 30.0


def test_market_applicable_coverage_rejects_excluded_factors_in_numerator() -> None:
    from scripts.backfill_kr_historical_factors import (
        calculate_market_applicable_factor_coverage,
    )

    with __import__("pytest").raises(ValueError, match="excluded factor"):
        calculate_market_applicable_factor_coverage(
            source_rows=[
                {
                    "year": 2002,
                    "financial_basis": "annual",
                    "security_count": 1,
                    "factor_count": 1,
                    "factor_ids": ["us_eps_consensus"],
                    "row_count": 10,
                }
            ],
            price_rows=[{"year": 2002, "security_count": 1, "price_row_count": 10}],
            years=[2002],
            bases=["annual"],
            global_factor_ids=["ma_50", "us_eps_consensus"],
            applicable_factor_ids=["ma_50"],
        )
