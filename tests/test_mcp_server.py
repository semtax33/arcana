from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient


from api.main import app
from api.mcp import McpServer, _iter_api_routes, build_tools


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
        self.assertIn("get_estimate_drivers", tool_names)

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




