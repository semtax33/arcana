from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from api.main import app
from api.service.consensus_service import ConsensusReportsNotFoundError, ConsensusService


class FakeClickHouseClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.query = ""
        self.parameters: dict[str, object] = {}
        self.closed = False

    def query_df(self, query: str, parameters=None) -> pd.DataFrame:
        self.query = query
        self.parameters = parameters or {}
        return pd.DataFrame(self.rows)

    def close(self) -> None:
        self.closed = True


def _row(
    report_idx: int,
    report_date: str,
    broker_name: str,
    analyst_name: str,
    target_price: float | None,
) -> dict[str, object]:
    return {
        "report_idx": report_idx,
        "report_date": report_date,
        "broker_name": broker_name,
        "analyst_name": analyst_name,
        "report_title": f"report {report_idx}",
        "grade_value": "Buy",
        "old_grade_value": "Hold",
        "target_price": target_price,
        "old_target_price": 80_000,
        "change_price": 10_000,
    }


class ConsensusServiceTest(unittest.TestCase):
    def test_get_kr_reports_averages_each_analysts_latest_valid_target_price(self) -> None:
        client = FakeClickHouseClient(
            [
                _row(5, "2026-07-15", "Broker A", "Kim", 100_000),
                _row(4, "2026-07-14", "Broker B", "Lee", 120_000),
                _row(3, "2026-07-13", "Broker C", "Park", None),
                _row(2, "2026-06-01", "Broker A", "Kim", 90_000),
                _row(1, "2026-05-01", "Broker C", "Park", 80_000),
                _row(0, "2025-01-01", "Broker D", "Choi", 2_000_000),
            ]
        )

        result = ConsensusService(client_factory=lambda: client).get_kr_reports("005930")

        self.assertEqual(result.stock_code, "005930")
        self.assertEqual(result.as_of_date, date(2026, 7, 15))
        self.assertEqual(result.average_target_price, 100_000)
        self.assertEqual(result.target_price_analyst_count, 3)
        self.assertEqual(len(result.reports), 5)
        self.assertEqual(result.reports[0].target_price, 100_000)
        self.assertIn("FROM real_consensus_reports FINAL", client.query)
        self.assertEqual(client.parameters, {"stock_code": "005930"})
        self.assertTrue(client.closed)

    def test_get_kr_reports_raises_when_no_reports_exist(self) -> None:
        client = FakeClickHouseClient([])

        with self.assertRaises(ConsensusReportsNotFoundError):
            ConsensusService(client_factory=lambda: client).get_kr_reports("005930")

        self.assertTrue(client.closed)

    def test_consensus_reports_route_is_registered(self) -> None:
        paths = {route.path for route in app.routes}

        self.assertIn("/api/consensus/kr/{stock_code}/reports", paths)
