from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


from api.main import app
from api.mcp import McpServer, _iter_api_routes, build_tools
from api.model.screening import FactorScreenResult


class McpServerTest(unittest.TestCase):
    def test_build_tools_exposes_all_fastapi_routes(self) -> None:
        tools = build_tools()
        route_names = {route.name for route in _iter_api_routes(app.routes)}

        self.assertEqual(route_names, set(tools))

    def test_tools_list_returns_mcp_tool_metadata(self) -> None:
        server = McpServer(build_tools())
        response = server._dispatch("tools/list", {})

        tool_names = {tool["name"] for tool in response["tools"]}
        self.assertIn("health_check", tool_names)
        self.assertIn("get_multiple_valuation_bands", tool_names)
        self.assertIn("run_factor_backtest", tool_names)
        self.assertIn("get_operating_metrics", tool_names)
        self.assertIn("get_unit_economics", tool_names)
        self.assertIn("get_operating_metric_drivers", tool_names)
        self.assertIn("get_estimates", tool_names)
        self.assertIn("get_estimate_consensus", tool_names)
        self.assertIn("get_estimate_consensus_history", tool_names)
        self.assertIn("get_estimate_drivers", tool_names)
        self.assertIn("list_screener_strategies", tool_names)
        self.assertIn("save_screener_strategy", tool_names)
        self.assertIn("get_screener_strategy", tool_names)
        self.assertIn("list_strategies", tool_names)
        self.assertIn("screen_strategy", tool_names)

    def test_call_tool_returns_json_text_content(self) -> None:
        server = McpServer(build_tools())
        response = server._dispatch(
            "tools/call",
            {"name": "health_check", "arguments": {}},
        )

        self.assertFalse(response["isError"])
        self.assertEqual(
            {"status": "ok"},
            json.loads(response["content"][0]["text"]),
        )

    def test_call_tool_can_save_and_load_screener_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            previous_db = os.environ.get("ARCANA_SCREENER_STRATEGY_DB")
            os.environ["ARCANA_SCREENER_STRATEGY_DB"] = str(
                Path(tempdir) / "strategies.sqlite3"
            )
            try:
                server = McpServer(build_tools())
                save_response = server._dispatch(
                    "tools/call",
                    {
                        "name": "save_screener_strategy",
                        "arguments": {
                            "name": "Quality screen",
                            "strategy": {
                                "market": "KR",
                                "conditions": [
                                    {
                                        "factor_id": "roe",
                                        "mode": "top_percent",
                                        "top_percent": 30,
                                        "rank_direction": "catalog",
                                    }
                                ],
                            },
                        },
                    },
                )

                self.assertFalse(save_response["isError"])
                saved = json.loads(save_response["content"][0]["text"])

                load_response = server._dispatch(
                    "tools/call",
                    {
                        "name": "get_screener_strategy",
                        "arguments": {"strategy_id": saved["id"]},
                    },
                )

                self.assertFalse(load_response["isError"])
                loaded = json.loads(load_response["content"][0]["text"])
                self.assertEqual(saved["id"], loaded["id"])
                self.assertEqual("Quality screen", loaded["name"])
                self.assertEqual("KR", loaded["strategy"]["market"])

                list_response = server._dispatch(
                    "tools/call",
                    {"name": "list_strategies", "arguments": {}},
                )
                self.assertFalse(list_response["isError"])
                listed = json.loads(list_response["content"][0]["text"])
                self.assertEqual("Quality screen", listed["strategies"][0]["name"])

                with patch(
                    "api.controller.factor_screen_controller.FactorScreenService.screen_stocks",
                    return_value=FactorScreenResult(
                        total_count=0,
                        fixed_columns=[],
                        factor_columns=[],
                        rows=[],
                    ),
                ):
                    screen_response = server._dispatch(
                        "tools/call",
                        {
                            "name": "screen_strategy",
                            "arguments": {"strategy_id": saved["id"]},
                        },
                    )

                self.assertFalse(screen_response["isError"])
                screened = json.loads(screen_response["content"][0]["text"])
                self.assertEqual("EMPTY", screened["summary"]["screening_result"])
            finally:
                if previous_db is None:
                    os.environ.pop("ARCANA_SCREENER_STRATEGY_DB", None)
                else:
                    os.environ["ARCANA_SCREENER_STRATEGY_DB"] = previous_db


    def test_http_root_accepts_mcp_json_rpc(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("2.0", payload["jsonrpc"])
        self.assertEqual(1, payload["id"])
        tool_names = {tool["name"] for tool in payload["result"]["tools"]}
        self.assertIn("health_check", tool_names)

    def test_http_root_returns_info_for_get(self) -> None:
        client = TestClient(app)
        response = client.get("/")

        self.assertEqual(200, response.status_code)
        self.assertEqual("mcp", response.json()["protocol"])

if __name__ == "__main__":
    unittest.main()




