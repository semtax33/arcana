from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from engine.extractors._internal.hankyung_consensus import download_hankyung_consensus_reports
from engine.loaders.consensus import load_hankyung_consensus
from engine.loaders.factors import create_factor_catalog_dataframe
from engine.transformers.consensus import normalize_hankyung_consensus
from engine.transformers.factors import add_real_consensus_factors
from engine.workflows._internal import download_workflow, normalize_workflow


class RecordingClient:
    def __init__(self):
        self.inserts = []
        self.closed = False

    def insert_df(self, table_name, frame, column_names):
        self.inserts.append((table_name, frame.copy(), list(column_names)))

    def close(self):
        self.closed = True


def test_download_hankyung_consensus_paginates_writes_and_sleeps(tmp_path: Path):
    calls = []
    sleeps = []

    def fake_get_json(url, headers):
        calls.append((url, headers.copy()))
        page = int(parse_qs(urlparse(url).query)["page"][0])
        if page == 1:
            return {
                "last_page": 2,
                "total": 1,
                "data": [
                    {
                        "BUSINESS_CODE": "5930",
                        "REGISTER_DATE": "20260630094807",
                        "REPORT_IDX": 1,
                    }
                ],
            }
        return {"last_page": 2, "total": 1, "data": []}

    counts = download_hankyung_consensus_reports(
        start_date="2026-01-01",
        end_date="2026-12-31",
        output_dir=tmp_path,
        token="test-token",
        http_get_json=fake_get_json,
        sleeper=sleeps.append,
        uniform=lambda low, high: (low + high) / 2,
    )

    assert counts["pages"] == 2
    assert counts["rows"] == 1
    assert counts["written"] == 1
    assert len(calls) == 2
    assert all(headers["Authorization"] == "Bearer test-token" for _, headers in calls)
    assert sleeps == [2.5, 2.5]
    assert (tmp_path / "005930_20260630094807.json").exists()

    repeated = download_hankyung_consensus_reports(
        start_date="2026-01-01",
        end_date="2026-12-31",
        output_dir=tmp_path,
        token="test-token",
        http_get_json=fake_get_json,
        sleeper=lambda _: None,
        uniform=lambda low, high: low,
    )
    assert repeated["skipped"] == 1


def test_normalize_loader_and_real_factors_use_silver_csv(tmp_path: Path):
    bronze_dir = tmp_path / "bronze"
    silver_dir = tmp_path / "silver"
    bronze_dir.mkdir()
    _write_report(
        bronze_dir / "005930_20260530000000.json",
        business_code="005930",
        register_date="20260530000000",
        report_date="2026-05-30",
        report_idx=10,
        eps=100,
    )
    _write_report(
        bronze_dir / "005930_20260630000000.json",
        business_code="999999",
        register_date="20260629000000",
        report_date="2026-06-30",
        report_idx=11,
        eps=120,
    )

    result = normalize_hankyung_consensus(
        bronze_dir=bronze_dir,
        output_dir=silver_dir,
        stale_days=180,
    )

    reports = pd.read_csv(result["reports_path"], dtype={"stock_code": str})
    estimates = pd.read_csv(result["estimates_path"], dtype={"stock_code": str})
    daily = pd.read_csv(result["daily_path"], dtype={"stock_code": str})

    assert len(reports) == 2
    assert set(reports["stock_code"]) == {"005930"}
    assert set(reports["file_year"]) == {2026}
    assert "filename_business_code_mismatch" in reports["quality_flags"].fillna("").iat[1]
    assert "filename_register_date_mismatch" in reports["quality_flags"].fillna("").iat[1]
    assert {"basic_eps", "revenue", "operating_income", "net_income"}.issubset(set(estimates["metric_id"]))
    assert not daily.empty
    assert daily.loc[daily["metric_id"] == "basic_eps", "consensus_mean"].iloc[-1] == 120

    client = RecordingClient()
    counts = load_hankyung_consensus(silver_dir=silver_dir, client=client)

    assert counts["real_consensus_reports"] == len(reports)
    assert [table for table, _, _ in client.inserts] == [
        "real_consensus_reports",
        "real_consensus_estimates",
        "real_consensus_daily",
    ]

    daily_prices = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2026-05-30", "2026-06-30", "2027-03-15"]),
            "close": [10, 10, 10],
        }
    )
    financials = pd.DataFrame(
        [
            {
                "fiscal_year": 2025,
                "fiscal_month": 12,
                "report_date": "2026-03-15",
                "BASIC_EPS": 100,
            },
            {
                "fiscal_year": 2026,
                "fiscal_month": 12,
                "report_date": "2027-03-15",
                "BASIC_EPS": 130,
            },
        ]
    )
    factors = add_real_consensus_factors(
        daily_prices,
        financials,
        "005930",
        real_consensus_daily_path=result["daily_path"],
    )

    assert factors.loc[1, "real_eps_expected_growth"] == 20
    assert factors.loc[1, "real_eps_revision_1m_pct"] == 20
    assert round(factors.loc[2, "real_eps_surprise_pct"], 6) == round((130 - 120) / 120 * 100, 6)


def test_real_consensus_factor_catalog_and_cli_dispatch():
    factor_ids = [
        "real_eps_revision_1m_pct",
        "real_eps_expected_growth",
        "real_eps_surprise_pct",
    ]

    catalog = create_factor_catalog_dataframe(factor_ids).set_index("factor_id")

    assert set(catalog.index) == set(factor_ids)
    assert (catalog["factor_type"] == "growth").all()
    assert (catalog["unit"] == "percent").all()
    assert (catalog["value_direction"] == "HIGHER_BETTER").all()
    assert "consensus" in download_workflow.DOWNLOAD_ACTIONS
    assert "consensus" in download_workflow.US_DOWNLOAD_ACTIONS
    assert callable(normalize_workflow.normalize_consensus)


def _write_report(
    path: Path,
    *,
    business_code: str,
    register_date: str,
    report_date: str,
    report_idx: int,
    eps: int,
) -> None:
    row = {
        "REPORT_IDX": report_idx,
        "PUBLISH_CODE": "0001",
        "OFFICE_NAME": "테스트증권",
        "BUSINESS_CODE": business_code,
        "BUSINESS_NAME": "테스트",
        "REPORT_TYPE": "CO",
        "REPORT_TITLE": "테스트 리포트",
        "REPORT_WRITER": "애널리스트",
        "REPORT_DATE": report_date,
        "REGISTER_DATE": register_date,
        "UPDATE_DATE": register_date,
        "STOCK_SETTLEMENT_DAY": "202612",
        "STOCK_PRE_EPS": str(eps),
        "STOCK_EXPECTED_SALES": "1000",
        "STOCK_PRE_OPERATING_PROFIT": "200",
        "STOCK_PRE_NET_INCOME": "130",
        "TARGET_STOCK_PRICES": "90000",
    }
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
