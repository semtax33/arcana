from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.loaders._internal.clickhouse_filings import insert_report_metadata


class RecordingClient:
    def __init__(self):
        self.inserts = []
        self.closed = False

    def insert_df(self, table_name, frame, column_names):
        self.inserts.append((table_name, frame.copy(), list(column_names)))

    def close(self):
        self.closed = True


def test_insert_report_metadata_inserts_by_report_month(tmp_path: Path):
    path = tmp_path / "kr_report_metadata.csv"
    pd.DataFrame(
        [
            {
                "security_id": "SEC_KR_005930",
                "stock_code": "005930",
                "fiscal_year": 2025,
                "fiscal_month": 12,
                "period_end_date": "2025-12-31",
                "report_date": "2026-03-10",
                "rcept_no": "20260310000001",
                "report_name": "사업보고서",
                "source_type": "statement",
                "source_url": "https://dart.example/1",
                "updated_at": "2026-06-28 00:00:00",
            },
            {
                "security_id": "SEC_KR_005930",
                "stock_code": "005930",
                "fiscal_year": 2026,
                "fiscal_month": 3,
                "period_end_date": "2026-03-31",
                "report_date": "2026-05-15",
                "rcept_no": "20260515000001",
                "report_name": "분기보고서",
                "source_type": "statement",
                "source_url": "https://dart.example/2",
                "updated_at": "2026-06-28 00:00:00",
            },
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")

    client = RecordingClient()
    count = insert_report_metadata(path=path, client=client)

    assert count == 2
    assert len(client.inserts) == 2
    assert [len(frame) for _, frame, _ in client.inserts] == [1, 1]
    assert all(table_name == "dart_report_metadata" for table_name, _, _ in client.inserts)
    assert all("_partition" not in frame.columns for _, frame, _ in client.inserts)
