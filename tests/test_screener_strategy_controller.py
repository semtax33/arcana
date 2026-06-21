from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app


class ScreenerStrategyControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tempdir.name) / "strategies.sqlite3"
        self._previous_db = os.environ.get("ARCANA_SCREENER_STRATEGY_DB")
        os.environ["ARCANA_SCREENER_STRATEGY_DB"] = str(self.db_path)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self._previous_db is None:
            os.environ.pop("ARCANA_SCREENER_STRATEGY_DB", None)
        else:
            os.environ["ARCANA_SCREENER_STRATEGY_DB"] = self._previous_db
        self._tempdir.cleanup()

    def test_save_list_get_overwrite_and_delete_strategy(self) -> None:
        first_payload = {
            "name": "Quality screen",
            "strategy": {
                "market": "KR",
                "industry_group_codes": ["4510"],
                "conditions": [
                    {
                        "factor_id": "roe",
                        "mode": "top_percent",
                        "top_percent": 30,
                        "rank_direction": "catalog",
                        "alias": "roe",
                    }
                ],
                "match_mode": "all",
                "limit": 5000,
            },
        }

        save_response = self.client.post("/api/factor-screen/strategies", json=first_payload)

        self.assertEqual(200, save_response.status_code)
        saved = save_response.json()
        self.assertEqual("Quality screen", saved["name"])
        self.assertEqual("KR", saved["strategy"]["market"])
        strategy_id = saved["id"]

        list_response = self.client.get("/api/factor-screen/strategies")
        self.assertEqual(200, list_response.status_code)
        self.assertEqual(1, len(list_response.json()["strategies"]))

        overwrite_payload = {
            **first_payload,
            "strategy": {
                **first_payload["strategy"],
                "market": "US",
                "industry_group_codes": [],
            },
        }
        overwrite_response = self.client.post(
            "/api/factor-screen/strategies",
            json=overwrite_payload,
        )

        self.assertEqual(200, overwrite_response.status_code)
        overwritten = overwrite_response.json()
        self.assertEqual(strategy_id, overwritten["id"])
        self.assertEqual("US", overwritten["strategy"]["market"])

        list_response = self.client.get("/api/factor-screen/strategies")
        self.assertEqual(1, len(list_response.json()["strategies"]))

        detail_response = self.client.get(f"/api/factor-screen/strategies/{strategy_id}")
        self.assertEqual(200, detail_response.status_code)
        self.assertEqual("US", detail_response.json()["strategy"]["market"])

        delete_response = self.client.delete(f"/api/factor-screen/strategies/{strategy_id}")
        self.assertEqual(200, delete_response.status_code)
        self.assertEqual({"deleted": True}, delete_response.json())

        missing_response = self.client.get(f"/api/factor-screen/strategies/{strategy_id}")
        self.assertEqual(404, missing_response.status_code)

    def test_rejects_blank_strategy_name(self) -> None:
        response = self.client.post(
            "/api/factor-screen/strategies",
            json={
                "name": "   ",
                "strategy": {
                    "market": "KR",
                    "conditions": [
                        {
                            "factor_id": "roe",
                            "mode": "threshold",
                            "operator": ">=",
                            "value": 10,
                        }
                    ],
                },
            },
        )

        self.assertEqual(400, response.status_code)

    def test_rejects_empty_conditions(self) -> None:
        response = self.client.post(
            "/api/factor-screen/strategies",
            json={"name": "Empty", "strategy": {"market": "KR", "conditions": []}},
        )

        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()