from __future__ import annotations

import json
from datetime import date

import pandas as pd

from api.service.estimate_service import EstimateService
from api.service.operating_metrics_service import OperatingMetricsService
from engine.loaders.operating_metrics import load_operating_metrics
from engine.transformers.operating_metrics import (
    create_operating_metric_gold,
    create_operating_metric_gold_for_stocks,
)


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


class QueryClient:
    def __init__(self, frame):
        self.frame = frame
        self.queries = []
        self.closed = False

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        return self.frame.copy()

    def close(self):
        self.closed = True


def test_create_operating_metric_gold_for_stocks_skips_missing_business_info_when_fail_fast(
    tmp_path,
    capsys,
):
    results = create_operating_metric_gold_for_stocks(
        ["088980"],
        silver_root=tmp_path / "silver" / "business-info",
        progress=True,
        continue_on_error=False,
    )

    assert results == []
    output = capsys.readouterr().out
    assert "[SKIP] operating metrics 1/1 stock=088980" in output
    assert "processed=0/1, skipped=1, failed=0" in output


def test_create_operating_metric_gold_builds_pqc_outputs(tmp_path):
    silver_root = tmp_path / "silver" / "business-info"
    normalized_statement_dir = tmp_path / "silver" / "dart" / "normalized"
    report_metadata_path = tmp_path / "silver" / "dart" / "kr_report_metadata.csv"
    operating_root = tmp_path / "gold" / "operating-metrics"
    estimate_root = tmp_path / "gold" / "estimates"
    stock_dir = silver_root / "123456"
    stock_dir.mkdir(parents=True)
    normalized_statement_dir.mkdir(parents=True)

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
    pd.DataFrame(
        [
            {
                "canonical_account_id": account_id,
                "canonical_account_name": account_id,
                "statement_type": "IS",
                "period": f"{year}.12",
                "normalized_amount": value,
                "fiscal_year": year,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            }
            for year, values in [
                (
                    2025,
                    {
                        "REVENUE": 100_000_000,
                        "OPERATING_INCOME": 20_000_000,
                        "NET_INCOME": 12_000_000,
                        "NET_INCOME_PARENT": 11_000_000,
                        "BASIC_EPS": 1100,
                        "DILUTED_EPS": 1090,
                    },
                ),
                (
                    2026,
                    {
                        "REVENUE": 120_000_000,
                        "OPERATING_INCOME": 24_000_000,
                        "NET_INCOME": 15_000_000,
                        "NET_INCOME_PARENT": 14_000_000,
                        "BASIC_EPS": 1400,
                        "DILUTED_EPS": 1380,
                    },
                ),
            ]
            for account_id, value in values.items()
        ]
    ).to_csv(normalized_statement_dir / "kr_normalized_123456.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(
        [
            {
                "security_id": "SEC_KR_123456",
                "stock_code": "123456",
                "fiscal_year": 2025,
                "fiscal_month": 12,
                "period_end_date": "2025-12-31",
                "report_date": "2026-03-15",
                "rcept_no": "20260315000001",
                "report_name": "사업보고서",
                "source_type": "statement",
                "source_url": "https://dart.example/2025",
                "updated_at": "2026-06-28 00:00:00",
            },
            {
                "security_id": "SEC_KR_123456",
                "stock_code": "123456",
                "fiscal_year": 2026,
                "fiscal_month": 12,
                "period_end_date": "2026-12-31",
                "report_date": "2027-03-15",
                "rcept_no": "20270315000001",
                "report_name": "사업보고서",
                "source_type": "statement",
                "source_url": "https://dart.example/2026",
                "updated_at": "2027-03-15 00:00:00",
            },
        ]
    ).to_csv(report_metadata_path, index=False, encoding="utf-8-sig")

    result = create_operating_metric_gold(
        "123456",
        silver_root=silver_root,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        normalized_statement_dir=normalized_statement_dir,
        report_metadata_path=report_metadata_path,
        as_of_date="2026-06-01",
        write_history=True,
    )

    assert result.raw_rows == 8
    assert result.metric_rows == 8
    assert result.unit_rows == 2
    assert result.driver_rows == 2
    assert result.estimate_component_rows >= 4
    assert result.estimate_consensus_history_path is not None
    assert result.estimate_consensus_history_path.exists()
    history_frame = pd.read_csv(result.estimate_consensus_history_path, dtype={"stock_code": str})
    assert len(history_frame) == result.estimate_consensus_rows
    assert set(history_frame["as_of_date"]) == {"2027-03-15"}

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
    assert any(row.metric_id == "operating_income" for row in consensus.rows)
    assert any(row.metric_id == "net_income" for row in consensus.rows)
    assert any(row.metric_id == "basic_eps" for row in consensus.rows)

    history = estimate_service.get_consensus_history(
        "123456",
        start_date=pd.Timestamp("2027-03-15").date(),
        end_date=pd.Timestamp("2027-03-15").date(),
        metric_id="revenue",
    )
    assert history.source == "gold_csv_history"
    assert history.rows
    assert {row.metric_id for row in history.rows} == {"revenue"}

    clickhouse_history = EstimateService(
        client_factory=lambda: QueryClient(history_frame),
        gold_root=estimate_root,
    ).get_consensus_history("123456", metric_id="revenue")
    assert clickhouse_history.source == "arcana_estimate_consensus_history"
    assert clickhouse_history.rows

    repeated_result = create_operating_metric_gold(
        "123456",
        silver_root=silver_root,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        normalized_statement_dir=normalized_statement_dir,
        report_metadata_path=report_metadata_path,
        as_of_date="2026-06-01",
        write_history=True,
    )
    repeated_history = pd.read_csv(repeated_result.estimate_consensus_history_path, dtype={"stock_code": str})
    assert len(repeated_history) == len(history_frame)

    historical_result = create_operating_metric_gold(
        "123456",
        silver_root=silver_root,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        normalized_statement_dir=normalized_statement_dir,
        report_metadata_path=report_metadata_path,
        estimate_all_periods=True,
    )
    historical_consensus = pd.read_csv(historical_result.estimate_consensus_path, dtype={"target_period": str})
    assert {"2026.12", "2027.12"}.issubset(set(historical_consensus["target_period"]))
    assert {"2026-03-15", "2027-03-15"}.issubset(set(historical_consensus["as_of_date"]))
    assert historical_consensus["model_count"].min() >= 3

    client = RecordingClient()
    counts = load_operating_metrics(
        ["123456"],
        client=client,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        load_history=True,
    )
    assert counts["business_operating_metric"] == 8
    assert counts["arcana_estimate_consensus"] >= 4
    assert counts["arcana_estimate_consensus_history"] >= 4
    assert any(table_name == "arcana_estimate_consensus_history" for table_name, _, _ in client.inserts)
    history_insert = next(frame for table_name, frame, _ in client.inserts if table_name == "arcana_estimate_consensus_history")
    assert set(history_insert["as_of_date"]) == {date(2027, 3, 15)}
    assert client.inserts
    for _, frame, _ in client.inserts:
        if "stock_code" in frame.columns and not frame.empty:
            assert isinstance(frame["stock_code"].iloc[0], str)
            assert frame["stock_code"].iloc[0] == "123456"

    override_client = RecordingClient()
    load_operating_metrics(
        ["123456"],
        client=override_client,
        operating_gold_root=operating_root,
        estimate_gold_root=estimate_root,
        load_history=True,
        as_of_date="2026-06-28",
    )
    override_history_insert = next(
        frame for table_name, frame, _ in override_client.inserts if table_name == "arcana_estimate_consensus_history"
    )
    assert set(override_history_insert["as_of_date"]) == {date(2026, 6, 28)}
