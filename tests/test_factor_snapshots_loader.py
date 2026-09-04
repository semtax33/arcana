import unittest

from engine.loaders.factor_snapshots import (
    build_factor_snapshot_insert_query,
    build_incremental_factor_snapshot_insert_query,
)


class FactorSnapshotsLoaderTest(unittest.TestCase):
    def test_build_factor_snapshot_query_filters_selected_market(self):
        query, params = build_factor_snapshot_insert_query(
            start_date="2026-01-01",
            end_date="2026-01-31",
            market="us",
            financial_basis="annual",
        )

        self.assertEqual(params["security_prefix"], "SEC_US_")
        self.assertIn(
            "startsWith(security_id, {security_prefix:String})",
            query,
        )

    def test_snapshot_queries_can_filter_audited_security_ids(self):
        security_ids = ["SEC_US_CNX", "SEC_US_AAPL"]
        carry_query, carry_params = build_factor_snapshot_insert_query(
            start_date="2026-01-01",
            end_date="2026-01-31",
            market="us",
            security_ids=security_ids,
        )
        incremental_query, incremental_params = build_incremental_factor_snapshot_insert_query(
            snapshot_date="2026-01-06",
            previous_snapshot_date="2026-01-05",
            market="us",
            security_ids=security_ids,
        )

        self.assertEqual(carry_params["security_ids"], sorted(security_ids))
        self.assertIn("security_id IN {security_ids:Array(String)}", carry_query)
        self.assertEqual(incremental_params["security_ids"], sorted(security_ids))
        self.assertIn("f.security_id IN {security_ids:Array(String)}", incremental_query)
        self.assertIn("s.security_id IN {security_ids:Array(String)}", incremental_query)

    def test_build_factor_snapshot_insert_query_builds_carry_forward_snapshot(self):
        query, params = build_factor_snapshot_insert_query(
            start_date="2026-01-01",
            end_date="2026-01-31",
            financial_basis="annual",
            factor_ids=["ROE", "per"],
            source_table="fact_daily_factors",
            snapshot_table="fact_daily_factor_snapshot",
        )

        self.assertEqual(params["start_date"], "2026-01-01")
        self.assertEqual(params["end_date"], "2026-01-31")
        self.assertEqual(params["financial_basis"], "annual")
        self.assertEqual(params["factor_ids"], ["per", "roe"])
        self.assertIn("snapshot_dates", params)
        self.assertIn("INSERT INTO fact_daily_factor_snapshot", query)
        self.assertIn("source_rows AS", query)
        self.assertIn("FROM fact_daily_factors", query)
        self.assertIn("FROM source_rows AS f", query)
        self.assertIn("CROSS JOIN snapshot_dates AS d", query)
        self.assertIn("arrayJoin({snapshot_dates:Array(Date)}) AS snapshot_date", query)
        self.assertIn("argMax(f.factor_value, tuple(f.trade_date, f.updated_at)) AS factor_value", query)
        self.assertIn("max(f.trade_date) AS source_trade_date", query)
        self.assertIn("trade_date <= {end_date:Date}", query)
        self.assertIn("financial_basis = {financial_basis:String}", query)
        self.assertIn("has({factor_ids:Array(String)}, factor_id)", query)

    def test_build_factor_snapshot_insert_query_can_copy_raw_rows_only(self):
        query, _ = build_factor_snapshot_insert_query(
            start_date="2026-01-01",
            end_date="2026-01-31",
            financial_basis="annual",
            factor_ids=["roe"],
            carry_forward=False,
        )

        self.assertIn("trade_date AS source_trade_date", query)
        self.assertNotIn("ASOF LEFT JOIN", query)

    def test_build_incremental_factor_snapshot_insert_query_carries_previous_rows(self):
        query, params = build_incremental_factor_snapshot_insert_query(
            snapshot_date="2026-01-06",
            previous_snapshot_date="2026-01-05",
            financial_basis="annual",
            factor_ids=["ROE", "per"],
            source_table="fact_daily_factors",
            snapshot_table="fact_daily_factor_snapshot",
        )

        self.assertEqual(params["snapshot_date"], "2026-01-06")
        self.assertEqual(params["previous_snapshot_date"], "2026-01-05")
        self.assertEqual(params["financial_basis"], "annual")
        self.assertEqual(params["factor_ids"], ["per", "roe"])
        self.assertIn("current_raw AS", query)
        self.assertIn("previous_snapshot AS", query)
        self.assertIn("FROM fact_daily_factor_snapshot AS s", query)
        self.assertIn("LEFT JOIN current_raw AS r", query)
        self.assertIn("AND r.security_id = ''", query)
        self.assertIn("FROM current_raw", query)
        self.assertIn("UNION ALL", query)
        self.assertIn("f.trade_date = {snapshot_date:Date}", query)
        self.assertIn("s.trade_date = {previous_snapshot_date:Date}", query)

    def test_incremental_query_filters_current_and_previous_market_rows(self):
        query, params = build_incremental_factor_snapshot_insert_query(
            snapshot_date="2026-01-06",
            previous_snapshot_date="2026-01-05",
            market="kr",
            financial_basis="annual",
        )

        self.assertEqual(params["security_prefix"], "SEC_KR_")
        self.assertIn(
            "startsWith(f.security_id, {security_prefix:String})",
            query,
        )
        self.assertIn(
            "startsWith(s.security_id, {security_prefix:String})",
            query,
        )


if __name__ == "__main__":
    unittest.main()
