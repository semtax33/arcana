from __future__ import annotations

from pathlib import Path

import pytest


DATA_HEAVY = {
    "test_benchmark_download.py",
    "test_factor_lab_snapshot_coverage.py",
    "test_refresh_workflow.py",
    "test_sec_filings_download.py",
    "test_yfinance_price_elt.py",
}
INTEGRATION_TOKENS = (
    "workflow",
    "pipeline",
    "loader",
    "service",
    "query",
    "mcp_server",
    "source_storage",
)
SEMANTIC_TOKENS = (
    "semantic",
    "mapping",
    "normalizer",
    "statement_period",
    "business_info",
    "comment_extraction",
    "unit_scale",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every test one primary execution tier without altering test behavior."""

    for item in items:
        filename = Path(str(item.fspath)).name
        stem = Path(filename).stem
        if any(item.get_closest_marker(name) for name in ("unit", "semantic", "integration", "data_heavy")):
            continue
        if filename in DATA_HEAVY:
            item.add_marker(pytest.mark.data_heavy)
        elif any(token in stem for token in SEMANTIC_TOKENS):
            item.add_marker(pytest.mark.semantic)
        elif any(token in stem for token in INTEGRATION_TOKENS):
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)
