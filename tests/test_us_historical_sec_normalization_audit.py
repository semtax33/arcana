from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.audit_us_historical_sec_normalization import audit_normalization


def _write_symbol(root: Path, symbol: str, *, amount: float = 100.0) -> None:
    base = pd.DataFrame(
        [
            {
                "fiscal_year": 2016,
                "fiscal_month": 12,
                "canonical_account_id": "TOTAL_ASSETS",
                "normalized_amount": amount,
            }
        ]
    )
    path = root / f"us_normalized_{symbol}.csv"
    base.to_csv(path, index=False)
    base.assign(source="filing_xbrl").to_csv(path.with_suffix(".debug.csv"), index=False)


def _write_metadata(
    path: Path,
    *,
    report_date: str = "2017-02-01",
    report_name: str = "10-K",
) -> None:
    pd.DataFrame(
        [
            {
                "security_id": "SEC_US_AAPL",
                "stock_code": "AAPL",
                "fiscal_year": 2016,
                "fiscal_month": 12,
                "period_end_date": "2016-12-31",
                "report_date": report_date,
                "rcept_no": "0001-16-000001",
                "report_name": report_name,
                "source_type": "statement",
            }
        ]
    ).to_csv(path, index=False)


def test_us_normalization_audit_accepts_filing_rows_and_explicit_gaps(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    _write_symbol(normalized, "AAPL")
    metadata = tmp_path / "metadata.csv"
    _write_metadata(metadata)

    report = audit_normalization(
        ["AAPL", "UNRESOLVED"],
        normalized_dir=normalized,
        metadata_path=metadata,
        start_year=2006,
        end_year=2016,
    )

    assert report["passed"] is True
    assert report["explicit_gap_symbols"] == ["UNRESOLVED"]
    assert report["source_row_counts"] == {"filing_xbrl": 1}


def test_us_normalization_audit_rejects_nonfinite_and_preperiod_disclosure(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    _write_symbol(normalized, "AAPL", amount=float("inf"))
    metadata = tmp_path / "metadata.csv"
    _write_metadata(metadata, report_date="2016-01-01")

    report = audit_normalization(
        ["AAPL"],
        normalized_dir=normalized,
        metadata_path=metadata,
        start_year=2006,
        end_year=2016,
    )

    reasons = {item["reason"] for item in report["violations"]}
    assert report["passed"] is False
    assert "non_finite_normalized_amount" in reasons
    assert "metadata_report_date_before_period_end" in reasons


def test_us_normalization_audit_accepts_sec_amendment_report_names(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    _write_symbol(normalized, "AAPL")
    metadata = tmp_path / "metadata.csv"
    _write_metadata(metadata, report_name="10-K/A")

    report = audit_normalization(
        ["AAPL"],
        normalized_dir=normalized,
        metadata_path=metadata,
        start_year=2006,
        end_year=2016,
    )

    assert report["passed"] is True
