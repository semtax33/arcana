from __future__ import annotations

import json

import pandas as pd

from api.service.estimate_service import EstimateService
from api.service.operating_metrics_service import OperatingMetricsService
from engine.loaders.operating_metrics import load_operating_metrics
from engine.transformers.operating_metrics import create_operating_metric_gold


class FailingClientFactory:
    def __call__(self):
        raise RuntimeError("clickhouse unavailable")


class RecordingClient:
    def __init__(self):
        self.inserts = []
        self.closed = False

    def insert_df(self, table_name, frame, column_names):
        self.inserts.append((table_name, frame.copy(), list(column_names)))

    def close(self):
        self.closed = True


def test_create_operating_metric_gold_builds_pqc_outputs(tmp_path):
    silver_root = tmp_path / "silver" / "business-info"
    operating_root = tmp_path / "gold" / "operating-metrics"
    estimate_root = tmp_path / "gold" / "estimates"
    stock_dir = silver_root / "123456"
    stock_dir.mkdir(parents=True)

    table_rows = []
    metric_rows = []
    for year, revenue, quantity, asp, unit_cost in [
        (2025, "100", "10", "10000000", "6000000"),
        (2026, "120", "12", "10000000", "6000000"),
    ]:
        table_id = f"KR_123456_{year}12_products_services_001"
        table_rows.append(
            {
                "stock_code": "123456",
                "period": f"{year}.12",
                "rcept_no": f"{year}0001",
                "source_uri": f"source/{year}",
                "section_key": "products_services",
                "section_title": "2. 주요 제품 및 서비스",
                "table_id": table_id,
                "table_kind": "data_table",
                "table_title": "주요 제품 매출 및 판매량",
                "caption_or_context": "주요 제품 매출 판매량 평균판매가격 단위당원가",
                "unit_text": "(단위 : 백만원, 개, 원)",
            }
        )
        metric_rows.append(
            {
                "table_id": table_id,
                "row_idx": 0,
                "row_json": json.dumps(["제품A", "제품A"], ensure_ascii=False),
                "row_text": f"제품A | 제품A | {revenue} | {quantity} | {asp} | {unit_cost}",
                "header_value_map_json": json.dumps(
                    {
                        "구분": "제품A",
                        "매출액": revenue,
                        "판매량": quantity,
                        "평균판매가격": asp,
                        "단위당원가": unit_cost,
                    },
                    ensure_ascii=False,
                ),
            }
        )

    pd.DataFrame(table_rows).to_csv(stock_dir / "kr_business_info_tables.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(metric_rows).to_csv(stock_dir / "kr_business_info_rows.csv", index=False, encoding="utf-8-sig")

    result = create_operating_metric_gold(
        "123456",
        silver_root=silver_root,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
    )

    assert result.raw_rows == 8
    assert result.metric_rows == 8
    assert result.unit_rows == 2
    assert result.driver_rows == 2
    assert result.estimate_component_rows >= 4

    unit_df = pd.read_csv(result.unit_economics_path)
    latest = unit_df.sort_values(["fiscal_year", "fiscal_month"]).iloc[-1]
    assert latest["revenue"] == 120_000_000
    assert latest["quantity"] == 12
    assert latest["asp"] == 10_000_000
    assert latest["c"] == 6_000_000
    assert latest["gross_profit"] == 48_000_000

    driver_df = pd.read_csv(result.driver_path)
    latest_driver = driver_df.sort_values(["fiscal_year", "fiscal_month"]).iloc[-1]
    assert abs(latest_driver["revenue_yoy_pct"] - 20.0) < 1e-9
    assert abs(latest_driver["q_yoy_pct"] - 20.0) < 1e-9
    assert abs(latest_driver["asp_yoy_pct"] - 0.0) < 1e-9

    operating_service = OperatingMetricsService(
        client_factory=FailingClientFactory(),
        gold_root=operating_root,
    )
    unit_response = operating_service.get_unit_economics("123456")
    assert unit_response.source == "gold_csv"
    assert len(unit_response.rows) == 2

    estimate_service = EstimateService(
        client_factory=FailingClientFactory(),
        gold_root=estimate_root,
    )
    consensus = estimate_service.get_consensus("123456")
    assert consensus.source == "gold_csv"
    assert consensus.target_period == "2027.12"
    assert any(row.metric_id == "revenue" for row in consensus.rows)

    historical_result = create_operating_metric_gold(
        "123456",
        silver_root=silver_root,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        estimate_all_periods=True,
    )
    historical_consensus = pd.read_csv(historical_result.estimate_consensus_path, dtype={"target_period": str})
    assert {"2026.12", "2027.12"}.issubset(set(historical_consensus["target_period"]))
    assert historical_consensus["model_count"].min() >= 3

    client = RecordingClient()
    counts = load_operating_metrics(
        ["123456"],
        client=client,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
    )
    assert counts["business_operating_metric"] == 8
    assert counts["arcana_estimate_consensus"] >= 4
    assert client.inserts
    for _, frame, _ in client.inserts:
        if "stock_code" in frame.columns and not frame.empty:
            assert isinstance(frame["stock_code"].iloc[0], str)
            assert frame["stock_code"].iloc[0] == "123456"
