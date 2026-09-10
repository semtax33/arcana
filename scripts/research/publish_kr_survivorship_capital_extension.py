"""Stage and publish an additive, source-checked financial review extension."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import SourceRefreshLock
from engine.core.serving_storage import export_json
from engine.transformers.factors import read_annual_financials, read_quarterly_financials, read_ttm_financials
from engine.workflows.financial_history import publish_reviewed_financial_history
from audit_kr_survivorship_capital_sources import digest, save

READERS = dict(annual=read_annual_financials, quarterly=read_quarterly_financials, ttm=read_ttm_financials)
UNCHANGED = ["asset_turnover", "gpm", "gross_profitability_pct", "npm", "opm", "roa", "roe"]
IDENTITY = ["report_date", "rcept_no", "financial_period", "financial_scope", "accounting_regime"]
ADDED = ["TRADE_RECEIVABLES", "TRADE_PAYABLES"]
CAPITAL = ["rect", "ap", "invested_capital_operational", "roiic_pct", "receivables_turnover", "ar_days", "ap_days"]


def read(reader, directory, symbol):
    result = reader(symbol, financial_dir=directory, market="kr", use_edgartools=False, require_report_metadata=True)
    return result.reindex(columns=IDENTITY + UNCHANGED + ADDED + CAPITAL).reset_index(drop=True)


def verify_amounts(frame, checked):
    for account in ADDED:
        wanted = [float(checked.get(str(receipt), {}).get(account, np.nan)) for receipt in frame.rcept_no]
        np.testing.assert_allclose(pd.to_numeric(frame[account]).to_numpy(dtype=float), wanted,
            rtol=1e-12, atol=1e-12, equal_nan=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--symbol", default="003410")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-financial-history", action="store_true")
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    assert not args.output.exists(), "Preserve the earlier extension attempt"
    audit = json.loads(args.source_audit.read_text("utf-8"))
    assert audit["status"] == "reviewed"
    for path, checksum in audit["implementations"].items():
        assert digest(path) == checksum
    original = audit["source_reviews"][args.symbol]
    assert digest(original["path"]) == original["sha256"]
    review = deepcopy(json.loads(Path(original["path"]).read_text("utf-8")))
    checked = {r["receipt"]: {a["canonical_account_id"]: a["value"] for a in r["accepted"]}
        for r in audit["results"] if r["symbol"] == args.symbol}
    assert len(checked) == len(review["receipts"])
    checksums = {str(args.source_audit): digest(args.source_audit), original["path"]: original["sha256"],
        str(Path(__file__)): digest(__file__), str(ROOT / "engine/workflows/financial_history.py"): digest(ROOT / "engine/workflows/financial_history.py")}
    for receipt in review["receipts"]:
        for kind in ("source", "normalized", "debug", "publication_source"):
            checksums[receipt[kind + "_path"]] = receipt[kind + "_sha256"]
    for result in audit["results"]:
        if result["symbol"] == args.symbol:
            path = args.source_audit.parent / args.symbol / result["receipt"] / "primary_rows.parquet"
            checksums[str(path)] = result["primary_rows_sha256"]
    def verify():
        for path, checksum in checksums.items():
            assert digest(path) == checksum, f"Extension evidence changed: {path}"
    verify()
    production = DATA_LAKE.silver("dart", "normalized")
    manifest = production / "history" / args.symbol / "manifest.json"
    previous = manifest.read_bytes()
    previous_sha = digest(manifest)
    review.update(expected_manifest_sha256=previous_sha,
        revision_reason="Add pure trade receivables/payables verified against the same retained DART primary statements; preserve previous values, scope and financial availability.")
    added = 0
    for receipt in review["receipts"]:
        additions = set(checked[receipt["rcept_no"]]) - set(receipt["accepted_canonical_account_ids"])
        if additions:
            receipt["accepted_canonical_account_ids"] = sorted(set(receipt["accepted_canonical_account_ids"]) | additions)
            receipt["review_evidence"] += f" Additional pure trade balances verified by source audit SHA256 {digest(args.source_audit)}; combined other balances are excluded."
            added += len(additions)
    assert added > 0
    save(args.output / "review.json", review)
    (args.output / "manifest_before.json").write_bytes(previous)
    report = dict(status="prepared", financial_history_published=False, native_factors_published=False,
        symbol=args.symbol, added_facts=added, receipts=len(review["receipts"]), source_audit_sha256=digest(args.source_audit),
        previous_manifest_sha256=previous_sha, evidence=checksums, coverage_complete=False)
    save(args.output / "publication.json", report)
    staging = args.output / "staging"
    shutil.copytree(manifest.parent, staging / "history" / args.symbol)
    with SourceRefreshLock("kr"):
        assert digest(manifest) == previous_sha
        stage = publish_reviewed_financial_history(args.output / "review.json", financial_dir=staging)
        stage_manifest = Path(stage["manifest_path"])
        comparisons, after_frames = [], {}
        for basis, reader in READERS.items():
            before = read(reader, production, args.symbol)
            after = read(reader, staging, args.symbol)
            pd.testing.assert_frame_equal(before[IDENTITY + UNCHANGED], after[IDENTITY + UNCHANGED],
                check_dtype=False, check_exact=False, rtol=1e-12, atol=1e-12)
            verify_amounts(after, checked)
            before.to_parquet(args.output / f"before_{basis}.parquet", index=False)
            after.to_parquet(args.output / f"staged_{basis}.parquet", index=False)
            after_frames[basis] = after
            comparisons.append(dict(basis=basis, events=len(after), source_amounts_verified=True,
                prior_seven_factors_unchanged=True, pure_receivables=int(after.TRADE_RECEIVABLES.notna().sum()),
                pure_payables=int(after.TRADE_PAYABLES.notna().sum())))
        verify()
        report.update(status="staged_and_verified", comparisons=comparisons, staged_manifest_sha256=digest(stage_manifest))
        save(args.output / "publication.json", report)
        if not args.publish_financial_history:
            print(report["status"], comparisons, flush=True)
            return
        published = publish_reviewed_financial_history(args.output / "review.json", financial_dir=production)
        assert digest(manifest) == digest(stage_manifest)
        for basis, reader in READERS.items():
            current = read(reader, production, args.symbol)
            pd.testing.assert_frame_equal(current, after_frames[basis])
            current.to_parquet(args.output / f"published_{basis}.parquet", index=False)
        verify()
        report.update(status="published_and_verified", financial_history_published=True,
            manifest_path=str(manifest), manifest_sha256=digest(manifest),
            previous_manifest_path=published["previous_manifest_path"],
            finished_at=datetime.now(timezone.utc).isoformat())
        save(args.output / "publication.json", report)
        export_json(DATA_LAKE.gold("survivorship", "kr", "financial_factors", "20260910", "capital_source_extension_summary.json"), report)
        print(report["status"], "added_facts", added, "receipts", len(review["receipts"]), comparisons, flush=True)


if __name__ == "__main__":
    main()
