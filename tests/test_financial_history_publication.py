"""Publishing reviewed receipts preserves earlier inputs and their availability."""
from hashlib import sha256
import json

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


def review_file(root, receipts):
    path = root / "review.json"
    path.write_text(json.dumps({"schema_version": 1, "market": "kr", "symbol": "035480",
                               "corp_code": "00255141", "receipts": receipts}), encoding="utf-8")
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
