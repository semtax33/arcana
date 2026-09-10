"""Publishing reviewed receipts preserves earlier inputs and their availability."""
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from engine.transformers.factors import read_annual_financials


def reviewed_receipt(root, received, sales):
    receipt = received.replace("-", "") + "000001"
    source = root / f"{receipt}.html"
    source.write_text(f"<DOCUMENT><COMPANY-NAME AREGCIK='00255141'>예제</COMPANY-NAME>{sales}</DOCUMENT>", encoding="utf-8")
    output = root / f"{receipt}.csv"
    pd.DataFrame([{"canonical_account_id": "REVENUE", "statement_type": "IS", "original_account_name": "매출액",
                   "period": "2021.12", "normalized_amount": sales}]).to_csv(output, index=False)
    return {"rcept_no": receipt, "fiscal_year": 2021, "fiscal_month": 12,
            "period_end_date": "2021-12-31", "period_start_date": "2021-01-01", "report_date": received,
            "financial_basis": "annual", "financial_scope": "CFS", "accounting_regime": "K_IFRS", "review_status": "verified",
            "review_evidence": "Reviewed issuer identity, current period, scope, account meaning and unit against the retained document.",
            "accepted_canonical_account_ids": ["REVENUE"],
            "normalized_path": output.name, "normalized_sha256": sha256(output.read_bytes()).hexdigest(),
            "source_path": source.name, "source_sha256": sha256(source.read_bytes()).hexdigest(),
            "source_url": f"https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}"}


def review_file(root, receipts, **metadata):
    path = root / "review.json"
    path.write_text(json.dumps({"schema_version": 1, "market": "kr", "symbol": "035480",
                               "corp_code": "00255141", "receipts": receipts, **metadata}), encoding="utf-8")
    return path


def read_financials(root):
    return read_annual_financials("035480", financial_dir=root, market="kr", use_edgartools=False,
                                  require_report_metadata=True)


def test_incremental_publication_keeps_original_and_amendment_without_duplicate_events(tmp_path):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    original = reviewed_receipt(tmp_path, "2022-03-30", 100)
    target = tmp_path / "published"
    publish_reviewed_financial_history(review_file(tmp_path, [original]), financial_dir=target)
    amended = reviewed_receipt(tmp_path, "2022-08-15", 80)
    review = review_file(tmp_path, [original, amended])
    publish_reviewed_financial_history(review, financial_dir=target)
    repeated = publish_reviewed_financial_history(review, financial_dir=target)
    result = read_financials(target)
    assert result.sale.tolist() == [100, 80]
    assert result.report_date.tolist() == [pd.Timestamp("2022-03-30"), pd.Timestamp("2022-08-15")]
    assert repeated["status"] == "unchanged"
    assert repeated["receipts"] == 2


def test_review_extension_adds_a_disclosed_account_without_changing_its_availability(tmp_path):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    receipt = reviewed_receipt(tmp_path, "2022-03-30", 100)
    source = tmp_path / receipt["source_path"]
    source.write_text("<DOCUMENT><COMPANY-NAME AREGCIK='00255141'>예제</COMPANY-NAME>매출액 100; 매출채권 20</DOCUMENT>", "utf-8")
    receipt["source_sha256"] = sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "published"
    original = publish_reviewed_financial_history(review_file(tmp_path, [receipt]), financial_dir=target)
    previous = Path(original["manifest_path"]).read_bytes()
    normalized = tmp_path / receipt["normalized_path"]
    frame = pd.read_csv(normalized)
    frame.loc[len(frame)] = dict(canonical_account_id="TRADE_RECEIVABLES", statement_type="BS",
        original_account_name="매출채권", period="2021.12", normalized_amount=20)
    frame.to_csv(normalized, index=False)
    receipt.update(normalized_sha256=sha256(normalized.read_bytes()).hexdigest(),
        accepted_canonical_account_ids=["REVENUE", "TRADE_RECEIVABLES"],
        review_evidence="Previously accepted sales preserved; pure trade receivables verified against the same retained statement.")
    review = review_file(tmp_path, [receipt], expected_manifest_sha256=sha256(previous).hexdigest(),
        revision_reason="Extend the reviewed operating-capital inputs from the original filing.")
    published = publish_reviewed_financial_history(review, financial_dir=target)
    result = read_financials(target)
    assert result.sale.tolist() == [100]
    assert result.TRADE_RECEIVABLES.tolist() == [20]
    assert result.report_date.tolist() == [pd.Timestamp("2022-03-30")]
    assert Path(published["previous_manifest_path"]).read_bytes() == previous
    assert publish_reviewed_financial_history(review, financial_dir=target)["status"] == "unchanged"


@pytest.mark.parametrize("problem", ["stale_manifest", "changed_amount", "removed_account", "changed_scope", "missing_reason"])
def test_review_extension_rejects_changes_to_accepted_history(tmp_path, problem):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    receipt = reviewed_receipt(tmp_path, "2022-03-30", 100)
    target = tmp_path / "published"
    initial = publish_reviewed_financial_history(review_file(tmp_path, [receipt]), financial_dir=target)
    manifest = Path(initial["manifest_path"])
    previous = manifest.read_bytes()
    normalized = tmp_path / receipt["normalized_path"]
    frame = pd.read_csv(normalized)
    frame.loc[len(frame)] = dict(canonical_account_id="TRADE_RECEIVABLES", statement_type="BS",
        original_account_name="매출채권", period="2021.12", normalized_amount=20)
    if problem == "changed_amount":
        frame.loc[0, "normalized_amount"] = 90
    frame.to_csv(normalized, index=False)
    receipt.update(normalized_sha256=sha256(normalized.read_bytes()).hexdigest(),
        accepted_canonical_account_ids=["REVENUE", "TRADE_RECEIVABLES"])
    if problem == "removed_account":
        receipt["accepted_canonical_account_ids"] = ["TRADE_RECEIVABLES"]
    if problem == "changed_scope":
        receipt["financial_scope"] = "OFS"
    review = review_file(tmp_path, [receipt],
        expected_manifest_sha256="0" * 64 if problem == "stale_manifest" else sha256(previous).hexdigest(),
        revision_reason="" if problem == "missing_reason" else "Extend the reviewed operating-capital inputs.")
    with pytest.raises(ValueError):
        publish_reviewed_financial_history(review, financial_dir=target)
    assert manifest.read_bytes() == previous
    assert read_financials(target).sale.tolist() == [100]


@pytest.mark.parametrize("problem", ["modified_source", "unreviewed", "backdated_amendment", "unknown_regime"])
def test_failed_publication_leaves_previously_readable_history_unchanged(tmp_path, problem):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    original = reviewed_receipt(tmp_path, "2022-03-30", 100)
    target = tmp_path / "published"
    publish_reviewed_financial_history(review_file(tmp_path, [original]), financial_dir=target)
    manifest = target / "history/035480/manifest.json"
    previous = manifest.read_bytes()
    amended = reviewed_receipt(tmp_path, "2022-08-15", 80)
    if problem == "modified_source":
        (tmp_path / amended["source_path"]).write_text("different source", encoding="utf-8")
    elif problem == "unreviewed":
        amended["review_status"] = "requires_review"
    elif problem == "backdated_amendment":
        amended["report_date"] = "2022-03-30"
    else:
        amended["accounting_regime"] = "UNKNOWN"
    with pytest.raises(ValueError):
        publish_reviewed_financial_history(review_file(tmp_path, [amended]), financial_dir=target)
    assert manifest.read_bytes() == previous
    assert read_financials(target).sale.tolist() == [100]


def test_actual_dart_publication_date_can_follow_the_receipt_identifier_date(tmp_path):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    receipt = reviewed_receipt(tmp_path, "2022-08-15", 80)
    receipt["rcept_no"] = "20220814000001"
    receipt["source_url"] = "https://opendart.fss.or.kr/api/document.xml?rcept_no=20220814000001"
    index = tmp_path / "dart_list.json"
    index.write_text(json.dumps({"status": "000", "list": [{"rcept_no": receipt["rcept_no"],
        "corp_code": "00255141", "rcept_dt": "20220815"}]}), encoding="utf-8")
    receipt.update(publication_source_path=index.name, publication_source_sha256=sha256(index.read_bytes()).hexdigest(),
                   publication_source_url="https://opendart.fss.or.kr/api/list.json?corp_code=00255141")
    target = tmp_path / "published"
    publish_reviewed_financial_history(review_file(tmp_path, [receipt]), financial_dir=target)
    result = read_financials(target)
    assert result.report_date.tolist() == [pd.Timestamp("2022-08-15")]
    assert not list(target.rglob("publication_sources/*.json"))
    manifest = json.loads((target / "history/035480/manifest.json").read_text("utf-8"))
    record = manifest["receipts"][0]
    assert (Path(record["evidence_root"]) / record["publication_source_path"]).resolve() == index.resolve()


def test_publication_references_bronze_sources_without_copying_raw_documents_into_silver(tmp_path):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    bronze = tmp_path / "data-lake/bronze/dart/financial_history"
    bronze.mkdir(parents=True)
    receipt = reviewed_receipt(bronze, "2022-03-30", 100)
    target = tmp_path / "data-lake/silver/dart/normalized"
    review = review_file(bronze, [receipt])
    result = publish_reviewed_financial_history(review, financial_dir=target)
    assert not list(target.rglob("*.html")), "Raw DART documents belong in bronze"
    manifest = json.loads(Path(result["manifest_path"]).read_text("utf-8"))
    record = manifest["receipts"][0]
    source = Path(record["evidence_root"]) / record["source_path"]
    assert source.resolve().is_relative_to(bronze)
    assert sha256(source.read_bytes()).hexdigest() == receipt["source_sha256"]
    previous = Path(result["manifest_path"]).read_bytes()
    source.write_text("changed provider bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        publish_reviewed_financial_history(review, financial_dir=target)
    assert Path(result["manifest_path"]).read_bytes() == previous


def test_incremental_publication_can_keep_a_legacy_embedded_source_record(tmp_path):
    from engine.workflows.financial_history import publish_reviewed_financial_history
    receipt = reviewed_receipt(tmp_path, "2022-03-30", 100)
    target = tmp_path / "published"
    published = publish_reviewed_financial_history(review_file(tmp_path, [receipt]), financial_dir=target)
    manifest_path = Path(published["manifest_path"])
    manifest = json.loads(manifest_path.read_text("utf-8"))
    legacy = manifest["receipts"][0]
    legacy.pop("evidence_root")
    legacy["source_path"] = "legacy_source.html"
    (manifest_path.parent / legacy["source_path"]).write_bytes((tmp_path / receipt["source_path"]).read_bytes())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    amendment = reviewed_receipt(tmp_path, "2022-08-15", 80)
    publish_reviewed_financial_history(review_file(tmp_path, [receipt, amendment]), financial_dir=target)
    updated = json.loads(manifest_path.read_text("utf-8"))
    assert updated["receipts"][0] == legacy
    assert "evidence_root" in updated["receipts"][1]
    assert read_financials(target).sale.tolist() == [100, 80]


@pytest.mark.parametrize("dry_run", [True, False])
def test_refresh_financial_history_target_uses_reviewed_receipts_without_market_downloads(tmp_path, monkeypatch, dry_run):
    from engine.workflows import refresh
    receipt = reviewed_receipt(tmp_path, "2022-03-30", 100)
    review = review_file(tmp_path, [receipt])
    target = tmp_path / "published"
    arguments = ["--market", "kr", "--targets", "financial-history", "--financial-history-review", str(review),
                 "--financial-history-output", str(target), "--skip-clickhouse"]
    if dry_run:
        arguments.append("--dry-run")
    args = refresh.build_arg_parser().parse_args(arguments)
    monkeypatch.setattr(refresh.download_workflow, "_stock_codes", lambda: pytest.fail("No market universe download is needed"))
    refresh.run_refresh(args)
    if dry_run:
        assert not target.exists()
    else:
        assert read_financials(target).sale.tolist() == [100]
