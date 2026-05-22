import unittest
from datetime import date, timedelta

import pandas as pd

from api.service.chart_service import (
    ChartService,
    StockChartNotFoundError,
    _normalize_stock_code,
)


class FakeClickHouseClient:
    def __init__(self, price_rows, factor_rows=None, metadata_rows=None):
        self.price_rows = price_rows
        self.factor_rows = factor_rows or []
        self.metadata_rows = metadata_rows or []
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        if "FROM price_daily" in query:
            return pd.DataFrame(self.price_rows)
        if "FROM fact_daily_factors" in query:
            return pd.DataFrame(self.factor_rows)
        if "FROM security_master" in query:
            return pd.DataFrame(self.metadata_rows)
        return pd.DataFrame()

    def close(self):
        self.closed = True


class ChartServiceTest(unittest.TestCase):
    def test_get_chart_returns_visible_range_and_recent_indicators(self):
        base_date = date(2026, 4, 1)
        price_rows = [
            {
                "trade_date": base_date + timedelta(days=index),
                "open": 100 + index,
                "high": 105 + index,
                "low": 95 + index,
                "close": 100 + index,
                "volume": 1_000 + index * 10,
                "currency": "KRW",
            }
            for index in range(51)
        ]
        latest_date = date(2026, 5, 21)
        factor_rows = [
            {
                "trade_date": latest_date,
                "factor_id": "ret_1m",
                "factor_value": 0.111,
            },
            {
                "trade_date": latest_date,
                "factor_id": "rsi_14",
                "factor_value": 72,
            },
            {
                "trade_date": latest_date,
                "factor_id": "macd",
                "factor_value": 1.2,
            },
            {
                "trade_date": latest_date,
                "factor_id": "macd_signal",
                "factor_value": 0.9,
            },
        ]
        client = FakeClickHouseClient(
            price_rows,
            factor_rows,
            [{"ticker": "236200", "stock_name": "Suprema", "country": "KR"}],
        )

        result = ChartService(
            client_factory=lambda: client,
            today_factory=lambda: latest_date,
        ).get_chart("236200", "1M")

        self.assertEqual(result.stock.stock_code, "236200")
        self.assertEqual(result.stock.stock_name, "Suprema")
        self.assertEqual(result.range, "1M")
        self.assertEqual(result.from_date, date(2026, 4, 21))
        self.assertEqual(result.to_date, latest_date)
        self.assertEqual(result.chart[0].time, "2026-04-21")
        self.assertEqual(result.recent[0].date, "2026-05-21")
        self.assertAlmostEqual(result.recent[0].monthly_return, 11.1)
        self.assertEqual(result.recent[0].rsi, "Overbought")
        self.assertEqual(result.recent[0].macd["macd"], 1.2)
        self.assertTrue(client.closed)

    def test_get_chart_calculates_technical_signal_fallbacks_from_price_data(self):
        base_date = date(2026, 3, 23)
        price_rows = [
            {
                "trade_date": base_date + timedelta(days=index),
                "open": 100 + index,
                "high": 102 + index,
                "low": 98 + index,
                "close": 100 + index,
                "volume": 1_000 + index,
                "currency": "KRW",
            }
            for index in range(60)
        ]
        latest_date = date(2026, 5, 21)
        client = FakeClickHouseClient(price_rows)

        result = ChartService(
            client_factory=lambda: client,
            today_factory=lambda: latest_date,
        ).get_chart("236200", "1M")

        latest = result.recent[0]
        self.assertEqual(latest.rsi, "Overbought")
        self.assertIn(latest.bollinger_band["signal"], {"Inside_Bands", "Near_Upper_Band"})
        self.assertEqual(latest.trend, "Strong_Uptrend")
        self.assertEqual(latest.macd["macd"] >= latest.macd["signal"], True)
        self.assertTrue(client.closed)

    def test_get_chart_raises_not_found_for_empty_price_data(self):
        client = FakeClickHouseClient([])

        with self.assertRaises(StockChartNotFoundError):
            ChartService(
                client_factory=lambda: client,
                today_factory=lambda: date(2026, 5, 21),
            ).get_chart("236200", "1Y")

    def test_normalize_stock_code_pads_and_rejects_unsafe_values(self):
        self.assertEqual(_normalize_stock_code("5930"), "005930")
        self.assertEqual(_normalize_stock_code("0001a0"), "0001A0")

        with self.assertRaises(ValueError):
            _normalize_stock_code("005930;DROP")


if __name__ == "__main__":
    unittest.main()
