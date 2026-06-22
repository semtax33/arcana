import unittest
from datetime import date

import pandas as pd

from api.service.backtest_service import BacktestService, _rebalance_dates
from api.service.dto import FactorBacktestRequestDto, FactorConditionDto


class FakeClickHouseClient:
    def __init__(self):
        self.closed = False
        self.queries = []

    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "SELECT DISTINCT trade_date" in query:
            return pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(
                        ["2026-01-01", "2026-01-02", "2026-01-03"]
                    )
                }
            )
        if "latest_factor_values AS" in query and "style_total_score" in query:
            return pd.DataFrame(
                [
                    {
                        "security_id": "SEC_KR_A",
                        "ticker": "A",
                        "stock_name": "A Corp",
                        "factor_id": "style_total_score",
                        "value_direction": "HIGHER_BETTER",
                        "factor_value": 80,
                        "rank_high": 1,
                        "rank_low": 1,
                        "factor_count": 1,
                        "percentile_score": 100,
                    },
                ]
            )
        if "latest_factor_values AS" in query:
            return pd.DataFrame(
                [
                    {
                        "security_id": "SEC_KR_A",
                        "ticker": "A",
                        "stock_name": "A Corp",
                        "factor_id": "roe",
                        "value_direction": "HIGHER_BETTER",
                        "factor_value": 20,
                        "rank_high": 1,
                        "rank_low": 2,
                        "factor_count": 2,
                        "percentile_score": 100,
                    },
                    {
                        "security_id": "SEC_KR_A",
                        "ticker": "A",
                        "stock_name": "A Corp",
                        "factor_id": "per",
                        "value_direction": "LOWER_BETTER",
                        "factor_value": 5,
                        "rank_high": 2,
                        "rank_low": 1,
                        "factor_count": 2,
                        "percentile_score": 100,
                    },
                    {
                        "security_id": "SEC_KR_B",
                        "ticker": "B",
                        "stock_name": "B Corp",
                        "factor_id": "roe",
                        "value_direction": "HIGHER_BETTER",
                        "factor_value": 10,
                        "rank_high": 2,
                        "rank_low": 1,
                        "factor_count": 2,
                        "percentile_score": 0,
                    },
                    {
                        "security_id": "SEC_KR_B",
                        "ticker": "B",
                        "stock_name": "B Corp",
                        "factor_id": "per",
                        "value_direction": "LOWER_BETTER",
                        "factor_value": 12,
                        "rank_high": 1,
                        "rank_low": 2,
                        "factor_count": 2,
                        "percentile_score": 0,
                    },
                ]
            )
        if "FROM price_daily" in query and "security_ids" in parameters:
            return pd.DataFrame(
                {
                    "security_id": ["SEC_KR_A", "SEC_KR_A"],
                    "trade_date": pd.to_datetime(["2026-01-02", "2026-01-03"]),
                    "close": [100, 110],
                }
            )
        if "FROM benchmark_price_daily" in query:
            return pd.DataFrame()
        return pd.DataFrame()

    def close(self):
        self.closed = True


class MultiRebalanceFakeClickHouseClient(FakeClickHouseClient):
    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        self.queries.append((query, parameters))
        if "SELECT DISTINCT trade_date" in query:
            return pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(
                        [
                            "2026-01-01",
                            "2026-01-02",
                            "2026-01-03",
                            "2026-03-31",
                            "2026-04-01",
                            "2026-04-02",
                            "2026-04-03",
                        ]
                    )
                }
            )
        if "latest_factor_values AS" in query:
            return super().query_df(query, parameters)
        if "FROM price_daily" in query and "security_ids" in parameters:
            return pd.DataFrame(
                {
                    "security_id": ["SEC_KR_A"] * 4,
                    "trade_date": pd.to_datetime(
                        ["2026-01-02", "2026-01-03", "2026-04-01", "2026-04-02"]
                    ),
                    "close": [100, 110, 110, 121],
                }
            )
        if "FROM benchmark_price_daily" in query:
            return pd.DataFrame()
        return pd.DataFrame()



class ChangingRebalanceFakeClickHouseClient(MultiRebalanceFakeClickHouseClient):
    def query_df(self, query, parameters=None):
        parameters = parameters or {}
        if "latest_factor_values AS" in query:
            self.queries.append((query, parameters))
            leading_security = "SEC_KR_B" if parameters.get("signal_date") == "2026-03-31" else "SEC_KR_A"
            trailing_security = "SEC_KR_A" if leading_security == "SEC_KR_B" else "SEC_KR_B"
            leading_ticker = "B" if leading_security == "SEC_KR_B" else "A"
            trailing_ticker = "A" if trailing_security == "SEC_KR_A" else "B"
            leading_name = "B Corp" if leading_security == "SEC_KR_B" else "A Corp"
            trailing_name = "A Corp" if trailing_security == "SEC_KR_A" else "B Corp"
            return pd.DataFrame(
                [
                    {
                        "security_id": leading_security,
                        "ticker": leading_ticker,
                        "stock_name": leading_name,
                        "factor_id": "roe",
                        "value_direction": "HIGHER_BETTER",
                        "factor_value": 20,
                        "rank_high": 1,
                        "rank_low": 2,
                        "factor_count": 2,
                        "percentile_score": 100,
                    },
                    {
                        "security_id": leading_security,
                        "ticker": leading_ticker,
                        "stock_name": leading_name,
                        "factor_id": "per",
                        "value_direction": "LOWER_BETTER",
                        "factor_value": 5,
                        "rank_high": 2,
                        "rank_low": 1,
                        "factor_count": 2,
                        "percentile_score": 100,
                    },
                    {
                        "security_id": trailing_security,
                        "ticker": trailing_ticker,
                        "stock_name": trailing_name,
                        "factor_id": "roe",
                        "value_direction": "HIGHER_BETTER",
                        "factor_value": 10,
                        "rank_high": 2,
                        "rank_low": 1,
                        "factor_count": 2,
                        "percentile_score": 0,
                    },
                    {
                        "security_id": trailing_security,
                        "ticker": trailing_ticker,
                        "stock_name": trailing_name,
                        "factor_id": "per",
                        "value_direction": "LOWER_BETTER",
                        "factor_value": 12,
                        "rank_high": 1,
                        "rank_low": 2,
                        "factor_count": 2,
                        "percentile_score": 0,
                    },
                ]
            )
        if "FROM price_daily" in query and "security_ids" in parameters:
            self.queries.append((query, parameters))
            return pd.DataFrame(
                {
                    "security_id": ["SEC_KR_A", "SEC_KR_A", "SEC_KR_B", "SEC_KR_B"],
                    "trade_date": pd.to_datetime(
                        ["2026-01-02", "2026-01-03", "2026-04-01", "2026-04-02"]
                    ),
                    "close": [100, 110, 100, 105],
                }
            )
        return super().query_df(query, parameters)
class BacktestServiceTest(unittest.TestCase):
    def test_rebalance_dates_include_start_and_period_boundary_execution_dates(self):
        trading_days = [
            date(2026, 1, 2),
            date(2026, 3, 31),
            date(2026, 4, 1),
            date(2026, 6, 30),
            date(2026, 7, 1),
        ]

        result = _rebalance_dates(
            trading_days,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 7, 1),
            frequency="quarterly",
        )

        self.assertEqual(
            result,
            [date(2026, 1, 2), date(2026, 4, 1), date(2026, 7, 1)],
        )

    def test_factor_backtest_sorts_by_average_score_and_limits_positions(self):
        client = FakeClickHouseClient()
        request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100),
                FactorConditionDto(factor_id="per", mode="top_percent", top_percent=100),
            ],
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
            rebalance_frequency="quarterly",
            max_positions=1,
        )

        result = BacktestService(client_factory=lambda: client).run_factor_backtest(request)

        self.assertTrue(client.closed)
        self.assertAlmostEqual(result.summary.cumulative_return, 0.1)
        self.assertEqual(result.summary.rebalance_count, 1)
        self.assertEqual(result.rebalance_history[0].signal_date, date(2026, 1, 1))
        self.assertEqual(len(result.rebalance_history[0].positions), 1)
        self.assertEqual(result.rebalance_history[0].positions[0].security_id, "SEC_KR_A")
        self.assertEqual(result.rebalance_history[0].positions[0].weight, 1.0)
        self.assertEqual(result.rebalance_history[0].positions[0].score, 100.0)
        self.assertTrue(any("Survivor bias" in warning for warning in result.warnings))

    def test_factor_backtest_loads_price_history_once_for_multiple_rebalances(self):
        client = MultiRebalanceFakeClickHouseClient()
        request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100),
                FactorConditionDto(factor_id="per", mode="top_percent", top_percent=100),
            ],
            start_date=date(2026, 1, 2),
            end_date=date(2026, 4, 3),
            rebalance_frequency="quarterly",
            max_positions=1,
        )

        result = BacktestService(client_factory=lambda: client).run_factor_backtest(request)

        price_history_queries = [
            query
            for query, params in client.queries
            if "FROM price_daily" in query and "security_ids" in params
        ]
        self.assertEqual(len(result.rebalance_history), 2)
        self.assertEqual(len(price_history_queries), 1)
        self.assertAlmostEqual(result.summary.cumulative_return, 0.21)

    def test_factor_backtest_marks_entered_and_exited_positions(self):
        client = ChangingRebalanceFakeClickHouseClient()
        request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100),
                FactorConditionDto(factor_id="per", mode="top_percent", top_percent=100),
            ],
            start_date=date(2026, 1, 2),
            end_date=date(2026, 4, 3),
            rebalance_frequency="quarterly",
            max_positions=1,
        )

        result = BacktestService(client_factory=lambda: client).run_factor_backtest(request)

        self.assertEqual(len(result.rebalance_history), 2)
        first_rebalance = result.rebalance_history[0]
        second_rebalance = result.rebalance_history[1]
        self.assertEqual([position.security_id for position in first_rebalance.positions], ["SEC_KR_A"])
        self.assertEqual([position.security_id for position in first_rebalance.entered_positions], ["SEC_KR_A"])
        self.assertEqual(first_rebalance.exited_positions, [])
        self.assertEqual([position.security_id for position in second_rebalance.positions], ["SEC_KR_B"])
        self.assertEqual([position.security_id for position in second_rebalance.entered_positions], ["SEC_KR_B"])
        self.assertEqual([position.security_id for position in second_rebalance.exited_positions], ["SEC_KR_A"])

    def test_factor_backtest_resolves_default_style_profile_for_style_score_conditions(self):
        client = FakeClickHouseClient()
        request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="style_total_score",
                    mode="top_percent",
                    top_percent=100,
                ),
            ],
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
            rebalance_frequency="quarterly",
            max_positions=1,
        )

        BacktestService(client_factory=lambda: client).run_factor_backtest(request)

        snapshot_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(snapshot_queries), 1)
        self.assertEqual(snapshot_queries[0][1]["style_profile"], "MINERVINI_ZWEIG")

    def test_factor_backtest_passes_style_profile_to_snapshot_query(self):
        client = FakeClickHouseClient()
        request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id="style_total_score",
                    mode="top_percent",
                    top_percent=100,
                ),
            ],
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 3),
            rebalance_frequency="quarterly",
            market="KR",
            style_profile="MINERVINI_ZWEIG",
            max_positions=1,
        )

        BacktestService(client_factory=lambda: client).run_factor_backtest(request)

        snapshot_queries = [
            (query, params)
            for query, params in client.queries
            if "latest_factor_values AS" in query
        ]
        self.assertEqual(len(snapshot_queries), 1)
        query, params = snapshot_queries[0]
        self.assertIn("FROM arcana.fact_daily_style_score AS s", query)
        self.assertEqual(params["style_profile"], "MINERVINI_ZWEIG")
        self.assertEqual(params["market_country"], "KR")


if __name__ == "__main__":
    unittest.main()
