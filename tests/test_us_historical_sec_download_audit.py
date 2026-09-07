from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_us_historical_sec_downloads import (
    _safe_bundle_child,
    audit_downloads,
    query_fingerprint,
)


def test_safe_bundle_child_accepts_only_a_direct_filename(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    assert _safe_bundle_child(bundle, "primary.htm") == bundle / "primary.htm"
    assert _safe_bundle_child(bundle, "../outside.htm") is None
    assert _safe_bundle_child(bundle, "nested/inside.htm") is None
    assert _safe_bundle_child(bundle, "") is None


def test_sec_download_audit_accepts_complete_v3_bundle(tmp_path: Path) -> None:
    root = tmp_path / "fillings"
    bundle = root / "10-K" / "AAPL" / "0000320193-16-000001"
    bundle.mkdir(parents=True)
    (bundle / "primary.htm").write_text("<html></html>", encoding="utf-8")
    (bundle / "aapl.xml").write_text("<xbrl></xbrl>", encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "form": "10-K",
        "filing_date": "2016-10-26",
        "source_authority": "SEC_10K_AUDITED",
        "primary_document": "primary.htm",
        "xbrl_documents": [
            {"role": "instance", "document_name": "aapl.xml"}
        ],
    }
    (bundle / "filing.json").write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint_dir = root / "_checkpoints"
    checkpoint_dir.mkdir()
    files = [
        "10-K/AAPL/0000320193-16-000001/primary.htm",
        "10-K/AAPL/0000320193-16-000001/aapl.xml",
        "10-K/AAPL/0000320193-16-000001/filing.json",
    ]
    (checkpoint_dir / "CIK0000320193.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "query_fingerprint": query_fingerprint("2006-01-01", "2016-12-31"),
                "files": files,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    report = audit_downloads(
        ["CIK0000320193"],
        unresolved_symbols=["NOSEC"],
        filings_dir=root,
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert report["passed"] is True
    assert report["complete_cik_count"] == 1
    assert report["manifest_counts"] == {"10-K": 1, "10-Q": 0}
    assert report["explicit_identity_gaps"] == ["NOSEC"]


def test_sec_download_audit_rejects_render_xml_in_manifest(tmp_path: Path) -> None:
    root = tmp_path / "fillings"
    bundle = root / "10-Q" / "AAPL" / "0000320193-16-000002"
    bundle.mkdir(parents=True)
    (bundle / "primary.htm").write_text("<html></html>", encoding="utf-8")
    (bundle / "R1.xml").write_text("<Report></Report>", encoding="utf-8")
    (bundle / "filing.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "form": "10-Q",
                "filing_date": "2016-07-20",
                "source_authority": "SEC_10Q_UNAUDITED",
                "primary_document": "primary.htm",
                "xbrl_documents": [
                    {"role": "instance", "document_name": "R1.xml"}
                ],
            }
        ),
        encoding="utf-8",
    )
    checkpoint_dir = root / "_checkpoints"
    checkpoint_dir.mkdir()
    files = [
        "10-Q/AAPL/0000320193-16-000002/primary.htm",
        "10-Q/AAPL/0000320193-16-000002/R1.xml",
        "10-Q/AAPL/0000320193-16-000002/filing.json",
    ]
    (checkpoint_dir / "CIK0000320193.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "query_fingerprint": query_fingerprint("2006-01-01", "2016-12-31"),
                "files": files,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    report = audit_downloads(
        ["CIK0000320193"],
        unresolved_symbols=[],
        filings_dir=root,
        start_date="2006-01-01",
        end_date="2016-12-31",
    )

    assert report["passed"] is False
    assert any("render_xml_in_manifest" in item for item in report["violations"])
