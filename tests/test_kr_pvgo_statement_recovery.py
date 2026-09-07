from __future__ import annotations

from pathlib import Path

import pandas as pd


def test_statement_filename_is_canonical() -> None:
    from scripts.recover_kr_pvgo_statements_2011_2016 import statement_filename

    assert statement_filename(2013, 3) == "finance_statement_(2013.03).html"
    assert statement_filename(2016, 12) == "finance_statement_(2016.12).html"


def test_job_loader_keeps_latest_pit_filing_and_skips_existing(tmp_path: Path) -> None:
    from scripts.recover_kr_pvgo_statements_2011_2016 import (
        load_missing_statement_jobs,
        statement_filename,
    )

    target_path = tmp_path / "targets.csv"
    target_path.write_text("security_id,symbol\nSEC_KR_005930,005930\n", encoding="utf-8")
    part_dir = tmp_path / "parts"
    part_dir.mkdir()
    pd.DataFrame(
        [
            {
                "fiscal_year": "2013",
                "fiscal_month": "3",
                "period_end_date": "2013-03-31",
                "report_date": "2013-05-15",
                "rcept_no": "20130515000001",
                "source_url": "https://dart.fss.or.kr/old",
            },
            {
                "fiscal_year": "2013",
                "fiscal_month": "3",
                "period_end_date": "2013-03-31",
                "report_date": "2013-05-16",
                "rcept_no": "20130516000001",
                "source_url": "https://dart.fss.or.kr/latest",
            },
            {
                "fiscal_year": "2017",
                "fiscal_month": "3",
                "period_end_date": "2017-03-31",
                "report_date": "2017-05-15",
                "rcept_no": "20170515000001",
                "source_url": "https://dart.fss.or.kr/outside",
            },
        ]
    ).to_csv(part_dir / "005930.csv", index=False)
    statement_root = tmp_path / "statements"

    jobs = load_missing_statement_jobs(
        target_path=target_path,
        part_dir=part_dir,
        statement_root=statement_root,
    )

    assert len(jobs) == 1
    assert jobs[0]["rcept_no"] == "20130516000001"
    output_path = statement_root / "005930" / statement_filename(2013, 3)
    output_path.parent.mkdir(parents=True)
    output_path.write_text("x" * 1_001, encoding="utf-8")
    assert load_missing_statement_jobs(
        target_path=target_path,
        part_dir=part_dir,
        statement_root=statement_root,
    ) == []
