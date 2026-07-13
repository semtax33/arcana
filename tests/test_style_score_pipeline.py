import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from engine.transformers.style_score_definitions import (
    STYLE_FACTOR_DEFINITIONS,
    STYLE_WEIGHTS,
    canonical_factor_id,
    factor_direction,
)
from engine.workflows.score import (
    FactorTableSchema,
    KR_MIN_UNIVERSE_MARKET_CAP,
    US_MIN_UNIVERSE_MARKET_CAP,
    _build_universe_query,
    build_style_scores,
    build_style_score_range,
    calculate_factor_scores,
    calculate_style_scores,
    load_existing_style_score_dates,
    load_factor_values,
    load_trade_dates,
)


class StyleScorePipelineTest(unittest.TestCase):
    def test_alias_mapping_and_direction(self):
        self.assertEqual(canonical_factor_id("EARNINGS_YIELD"), "epr")
        self.assertEqual(canonical_factor_id("DIVIDEND_YIELD"), "dividend_yield")
        self.assertEqual(canonical_factor_id("MARKET_DIVIDEND_YIELD"), "dividend_yield")
        self.assertEqual(canonical_factor_id("SHAREHOLDER_RETURN"), "sharehold_return")
        self.assertEqual(canonical_factor_id("FCF_DIVIDEND_COVERAGE"), "fcf_dividend_coverage")
        self.assertEqual(canonical_factor_id("SHAREHOLDER_YIELD"), "shareholder_yield")
        self.assertEqual(canonical_factor_id("roe"), "roe")
        self.assertEqual(canonical_factor_id("REAL_EPS_EXPECTED_GROWTH"), "real_eps_expected_growth")
        self.assertEqual(factor_direction("EARNINGS_YIELD"), 1)
        self.assertEqual(factor_direction("DEBT_TO_EQUITY"), -1)
        self.assertEqual(factor_direction("FCF_PAYOUT_RATIO"), -1)
        self.assertEqual(factor_direction("DIVIDEND_CUT"), -1)
        self.assertEqual(factor_direction("REAL_EPS_EXPECTED_GROWTH"), 1)

    def test_universe_query_uses_current_industry_columns(self):
        query = _build_universe_query()

        self.assertIn("i.sector_code", query)
        self.assertIn("i.industry_group_code", query)
        self.assertIn("i.industry_group_name", query)
        self.assertNotIn("i.industry_code", query)

    def test_universe_query_includes_normalized_ordinary_share_class(self):
        query = _build_universe_query()

        self.assertIn("'ORD'", query)

    def test_universe_query_uses_market_specific_market_cap_rules(self):
        query = _build_universe_query()

        self.assertIn("latest_price AS", query)
        self.assertIn("ls.latest_shares * lp.latest_close", query)
        self.assertIn("sm.country = 'US'", query)
        self.assertIn(f">= {US_MIN_UNIVERSE_MARKET_CAP}", query)
        self.assertIn("sm.country = 'KR'", query)
        self.assertIn(f">= {KR_MIN_UNIVERSE_MARKET_CAP}", query)

    def test_asof_factor_query_uses_latest_available_snapshot(self):
        client = _CaptureQueryClient()
        schema = FactorTableSchema(
            table_name="fact_daily_factors",
            value_column="factor_value",
            has_financial_basis=True,
            has_updated_at=True,
            has_source_trade_date=False,
        )

        load_factor_values(
            client,
            date(2026, 5, 24),
            schema=schema,
            factor_ids=["epr"],
            factor_asof_mode="asof",
            financial_basis="annual",
        )

        self.assertIn("FROM fact_daily_factors AS f", client.query)
        self.assertIn("SELECT max(f.trade_date)", client.query)
        self.assertIn("WHERE f.trade_date = source_date", client.query)
        self.assertIn("f.financial_basis = {financial_basis:String}", client.query)

    def test_load_factor_values_uses_ttm_fallback_for_price_style_factors(self):
        client = _BasisFallbackQueryClient()
        schema = FactorTableSchema(
            table_name="fact_daily_factors",
            value_column="factor_value",
            has_financial_basis=True,
            has_updated_at=True,
            has_source_trade_date=False,
        )

        result = load_factor_values(
            client,
            date(2026, 7, 10),
            schema=schema,
            factor_ids=["epr", "tr_12_1"],
            factor_asof_mode="exact",
            financial_basis="annual",
        )

        by_factor = result.set_index("factor_id")
        self.assertEqual(client.parameters["basis_agnostic_factor_ids"], ["tr_12_1"])
        self.assertEqual(by_factor.loc["epr", "_financial_basis"], "annual")
        self.assertEqual(by_factor.loc["tr_12_1", "_financial_basis"], "ttm")

    def test_industry_fallback_uses_factor_level_peer_counts(self):
        universe = _universe(
            [
                *[(f"A{index:02d}", "IG_A", "SEC_A") for index in range(25)],
                *[(f"B{index:02d}", "IG_B", "SEC_A") for index in range(9)],
                *[(f"C{index:02d}", "IG_C", "SEC_C") for index in range(4)],
            ]
        )
        factors = _factor_values(universe["security_id"].tolist(), "epr")

        result = calculate_factor_scores(universe, factors, trade_date="2026-05-24")

        by_security = result.set_index("security_id")
        self.assertEqual(by_security.loc["A00", "industry_level"], "INDUSTRY_GROUP")
        self.assertEqual(by_security.loc["A00", "industry_code"], "IG_A")
        self.assertEqual(by_security.loc["A00", "n_peers"], 25)
        self.assertEqual(by_security.loc["B00", "industry_level"], "SECTOR")
        self.assertEqual(by_security.loc["B00", "industry_code"], "SEC_A")
        self.assertEqual(by_security.loc["B00", "n_peers"], 34)
        self.assertEqual(by_security.loc["C00", "industry_level"], "ALL_NON_FINANCIAL")
        self.assertEqual(by_security.loc["C00", "industry_code"], "ALL_NON_FINANCIAL")

    def test_winsor_percentile_average_rank_and_mad_zero(self):
        securities = [f"S{index}" for index in range(5)]
        universe = _universe([(security_id, "", "") for security_id in securities])
        factors = pd.DataFrame(
            {
                "security_id": securities,
                "trade_date": [date(2026, 5, 24)] * 5,
                "factor_id": ["epr"] * 5,
                "factor_value": [1, 2, 2, 4, 100],
            }
        )

        result = calculate_factor_scores(universe, factors, trade_date="2026-05-24")
        ranked = result.set_index("security_id")

        self.assertTrue(ranked.loc["S4", "is_winsorized"])
        self.assertAlmostEqual(ranked.loc["S1", "percentile_score"], 37.5)
        self.assertAlmostEqual(ranked.loc["S2", "percentile_score"], 37.5)

        flat_factors = factors.copy()
        flat_factors["factor_value"] = 5
        flat_result = calculate_factor_scores(universe, flat_factors, trade_date="2026-05-24")
        self.assertTrue((flat_result["robust_z_score"] == 0).all())

    def test_style_score_renormalizes_available_weights_and_confidence(self):
        factor_scores = pd.DataFrame(
            [
                _factor_score_row("epr", 80.0),
                _factor_score_row("fcfpr", 40.0),
            ]
        )

        result = calculate_style_scores(
            factor_scores,
            trade_date="2026-05-24",
            style_profile="DEFAULT",
        )

        row = result.iloc[0]
        self.assertAlmostEqual(row["value_score"], 60.0)
        self.assertAlmostEqual(row["total_score"], 60.0)
        self.assertAlmostEqual(row["score_confidence"], 0.5)
        self.assertEqual(row["available_factor_count"], 2)
        self.assertIn("roe", row["missing_factor_ids"])

    def test_consensus_style_score_includes_real_consensus_factors(self):
        factor_scores = pd.DataFrame(
            [
                _factor_score_row("real_eps_expected_growth", 90.0, style_group="CONSENSUS"),
                _factor_score_row("real_eps_revision_1m_pct", 70.0, style_group="CONSENSUS"),
                _factor_score_row("real_eps_surprise_pct", 80.0, style_group="CONSENSUS"),
            ]
        )

        result = calculate_style_scores(
            factor_scores,
            trade_date="2026-05-24",
            style_profile="DEFAULT",
        )

        row = result.iloc[0]
        consensus_weights = STYLE_WEIGHTS["CONSENSUS"]
        used_weights = {
            factor_id: consensus_weights[factor_id]
            for factor_id in [
                "real_eps_expected_growth",
                "real_eps_revision_1m_pct",
                "real_eps_surprise_pct",
            ]
        }
        expected_consensus_score = (
            90.0 * used_weights["real_eps_expected_growth"]
            + 70.0 * used_weights["real_eps_revision_1m_pct"]
            + 80.0 * used_weights["real_eps_surprise_pct"]
        ) / sum(used_weights.values())

        self.assertIn("real_eps_expected_growth", STYLE_FACTOR_DEFINITIONS)
        self.assertAlmostEqual(row["consensus_score"], expected_consensus_score)
        self.assertAlmostEqual(row["total_score"], expected_consensus_score)
        self.assertAlmostEqual(row["score_confidence"], sum(used_weights.values()))
        self.assertEqual(row["available_factor_count"], 3)
        self.assertIn("real_revenue_expected_growth", row["missing_factor_ids"])

    def test_style_score_decodes_clickhouse_fixed_string_stock_code(self):
        fixed_stock_code = b"278470" + (b"\x00" * 58)
        factor_scores = pd.DataFrame(
            [
                {
                    **_factor_score_row("epr", 80.0),
                    "stock_code": fixed_stock_code,
                }
            ]
        )

        result = calculate_style_scores(
            factor_scores,
            trade_date="2026-05-24",
            style_profile="DEFAULT",
        )

        self.assertEqual(result.loc[0, "stock_code"], "278470")

    def test_dividend_style_score_uses_shareholder_return_factors(self):
        factor_scores = pd.DataFrame(
            [
                _factor_score_row("dividend_yield", 90.0, style_group="DIVIDEND"),
                _factor_score_row("shareholder_yield", 70.0, style_group="DIVIDEND"),
                _factor_score_row("fcf_dividend_coverage", 80.0, style_group="DIVIDEND"),
                _factor_score_row("shareholder_return_fcf_coverage", 75.0, style_group="DIVIDEND"),
                _factor_score_row("fcfe_dividend_coverage", 65.0, style_group="DIVIDEND"),
                _factor_score_row("fcf_payout_ratio", 60.0, style_group="DIVIDEND"),
                _factor_score_row("payout_ratio", 50.0, style_group="DIVIDEND"),
                _factor_score_row("fcf_yield_dividend_yield_spread", 85.0, style_group="DIVIDEND"),
                _factor_score_row("dps_cagr_5y", 55.0, style_group="DIVIDEND"),
                _factor_score_row("dividend_consistency_streak", 95.0, style_group="DIVIDEND"),
                _factor_score_row("dps_volatility_5y", 40.0, style_group="DIVIDEND"),
                _factor_score_row("dividend_cut", 100.0, style_group="DIVIDEND"),
            ]
        )

        result = calculate_style_scores(
            factor_scores,
            trade_date="2026-05-24",
            style_profile="DIVIDEND_QUALITY",
        )

        row = result.iloc[0]
        self.assertAlmostEqual(row["dividend_score"], 72.8)
        self.assertAlmostEqual(row["total_score"], 72.8)
        self.assertEqual(row["available_factor_count"], 12)

    def test_dividend_style_score_includes_market_dividend_yield(self):
        factor_scores = pd.DataFrame(
            [
                _factor_score_row("dividend_yield", 80.0, style_group="DIVIDEND"),
            ]
        )

        result = calculate_style_scores(
            factor_scores,
            trade_date="2026-05-24",
            style_profile="DIVIDEND_QUALITY",
        )

        row = result.iloc[0]
        self.assertAlmostEqual(row["dividend_score"], 80.0)
        self.assertAlmostEqual(row["total_score"], 80.0)
        self.assertEqual(row["available_factor_count"], 1)
        self.assertIn("fcf_dividend_coverage", row["missing_factor_ids"])

    def test_load_trade_dates_uses_price_calendar(self):
        client = _RangeQueryClient()

        result = load_trade_dates(client, "2026-05-20", "2026-05-22")

        self.assertEqual(result, [date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22)])
        self.assertIn("FROM arcana.price_daily", client.queries[0][0])
        self.assertEqual(client.queries[0][1]["start_date"], "2026-05-20")
        self.assertEqual(client.queries[0][1]["end_date"], "2026-05-22")

    def test_load_existing_style_score_dates_filters_profile(self):
        client = _RangeQueryClient()

        result = load_existing_style_score_dates(
            client,
            "2026-05-20",
            "2026-05-22",
            style_profile="DEFAULT",
        )

        self.assertEqual(result, {date(2026, 5, 21)})
        self.assertIn("FROM arcana.fact_daily_style_score FINAL", client.queries[0][0])
        self.assertEqual(client.queries[0][1]["style_profile"], "DEFAULT")

    def test_build_style_score_range_builds_each_unskipped_trade_date(self):
        client = _RangeQueryClient()

        with (
            patch(
                "engine.workflows.score.build_factor_scores",
                side_effect=lambda *args, **kwargs: (
                    pd.DataFrame([{"row": 1}, {"row": 2}]),
                    pd.DataFrame([{"row": 1}]),
                ),
            ) as factor_build,
            patch(
                "engine.workflows.score.build_style_scores",
                side_effect=lambda *args, **kwargs: pd.DataFrame([{"row": 1}]),
            ) as style_build,
        ):
            result = build_style_score_range(
                "2026-05-20",
                "2026-05-22",
                style_profile="DEFAULT",
                factor_asof_mode="asof",
                skip_existing=True,
                client_factory=lambda: client,
            )

        self.assertEqual(result.trade_dates, [date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22)])
        self.assertEqual(result.processed_dates, [date(2026, 5, 20), date(2026, 5, 22)])
        self.assertEqual(result.skipped_dates, [date(2026, 5, 21)])
        self.assertEqual(result.factor_score_rows, 4)
        self.assertEqual(result.industry_snapshot_rows, 2)
        self.assertEqual(result.style_score_rows, 2)
        self.assertEqual(factor_build.call_count, 2)
        self.assertEqual(style_build.call_count, 2)
        self.assertEqual(factor_build.call_args_list[0].kwargs["factor_asof_mode"], "asof")
        self.assertTrue(client.closed)

    def test_build_style_scores_omits_columns_missing_from_clickhouse_table(self):
        client = _StyleScoreInsertClient()

        build_style_scores(
            "2026-05-24",
            style_profile="DEFAULT",
            client_factory=lambda: client,
        )

        self.assertEqual(client.insert_table, "fact_daily_style_score")
        self.assertNotIn("country", client.insert_columns)
        self.assertNotIn("market_mic", client.insert_columns)
        self.assertIn("security_id", client.insert_columns)
        self.assertEqual(list(client.insert_frame.columns), client.insert_columns)


def _universe(items):
    return pd.DataFrame(
        [
            {
                "security_id": security_id,
                "issuer_id": f"ISS_{security_id}",
                "stock_code": security_id[-6:],
                "company_name": security_id,
                "industry_schema": "GICS",
                "industry_group_code": industry_group_code,
                "industry_group_name": industry_group_code,
                "sector_code": sector_code,
                "is_financial": False,
            }
            for security_id, industry_group_code, sector_code in items
        ]
    )


def _factor_values(security_ids, factor_id):
    return pd.DataFrame(
        {
            "security_id": security_ids,
            "trade_date": [date(2026, 5, 24)] * len(security_ids),
            "factor_id": [factor_id] * len(security_ids),
            "factor_value": list(range(1, len(security_ids) + 1)),
        }
    )


def _factor_score_row(factor_id, percentile_score, style_group="VALUE"):
    return {
        "trade_date": date(2026, 5, 24),
        "security_id": "SEC_A",
        "issuer_id": "ISS_A",
        "stock_code": "000001",
        "company_name": "A Corp",
        "industry_schema": "GICS",
        "industry_code": "IG_A",
        "industry_name": "Industry A",
        "factor_id": factor_id,
        "style_group": style_group,
        "factor_direction": 1,
        "raw_factor_value": 1.0,
        "winsorized_value": 1.0,
        "percentile_score": percentile_score,
        "robust_z_score": 0.0,
        "n_peers": 10,
        "score_method": "INDUSTRY_PERCENTILE",
        "fallback_level": "ALL_NON_FINANCIAL",
        "is_valid": True,
        "invalid_reason": "",
        "is_winsorized": False,
        "is_missing": False,
        "score_confidence": 1.0,
        "source_trade_date": date(2026, 5, 24),
    }


class _CaptureQueryClient:
    def __init__(self):
        self.query = ""
        self.parameters = {}

    def query_df(self, query, parameters=None):
        self.query = query
        self.parameters = parameters or {}
        return pd.DataFrame()


class _BasisFallbackQueryClient:
    def __init__(self):
        self.query = ""
        self.parameters = {}

    def query_df(self, query, parameters=None):
        self.query = query
        self.parameters = parameters or {}
        return pd.DataFrame(
            [
                {
                    "security_id": "SEC_A",
                    "trade_date": date(2026, 7, 10),
                    "factor_id": "epr",
                    "factor_value": 1.0,
                    "_financial_basis": "annual",
                    "source_trade_date": date(2026, 7, 10),
                    "updated_at": pd.Timestamp("2026-07-10 09:00:00"),
                },
                {
                    "security_id": "SEC_A",
                    "trade_date": date(2026, 7, 10),
                    "factor_id": "epr",
                    "factor_value": 2.0,
                    "_financial_basis": "ttm",
                    "source_trade_date": date(2026, 7, 10),
                    "updated_at": pd.Timestamp("2026-07-10 09:00:00"),
                },
                {
                    "security_id": "SEC_A",
                    "trade_date": date(2026, 7, 10),
                    "factor_id": "tr_12_1",
                    "factor_value": 0.2,
                    "_financial_basis": "ttm",
                    "source_trade_date": date(2026, 7, 10),
                    "updated_at": pd.Timestamp("2026-07-10 09:00:00"),
                },
            ]
        )


class _RangeQueryClient:
    def __init__(self):
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "FROM arcana.price_daily" in query:
            return pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(
                        ["2026-05-20", "2026-05-21", "2026-05-22"]
                    )
                }
            )
        if "FROM arcana.fact_daily_style_score FINAL" in query:
            return pd.DataFrame({"trade_date": pd.to_datetime(["2026-05-21"])})
        return pd.DataFrame()

    def close(self):
        self.closed = True


class _StyleScoreInsertClient:
    def __init__(self):
        self.insert_table = None
        self.insert_frame = None
        self.insert_columns = None
        self.closed = False

    def query_df(self, query, parameters=None):
        if query.startswith("DESCRIBE TABLE"):
            columns = [
                column
                for column in calculate_style_scores(
                    pd.DataFrame([_factor_score_row("epr", 80.0)]),
                    trade_date="2026-05-24",
                ).columns
                if column not in {"country", "market_mic"}
            ]
            return pd.DataFrame({"name": columns})
        if "FROM arcana.fact_daily_factor_score FINAL" in query:
            return pd.DataFrame([_factor_score_row("epr", 80.0)])
        return pd.DataFrame()

    def insert_df(self, table, df, column_names=None):
        self.insert_table = table
        self.insert_frame = df.copy()
        self.insert_columns = list(column_names or [])

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
