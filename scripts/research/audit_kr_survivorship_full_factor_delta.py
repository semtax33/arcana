"""Account for every changed candidate cell after the missing-input fixes."""
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from validate_kr_survivorship_financial_factors import SILVER, FACTOR_IDS, canonical, digest, save
from publish_kr_survivorship_financial_factors import compare


def main():
    before = SILVER / "kr_full_factor_preparation"
    after = SILVER / "kr_full_factor_preparation_missing_fixed"
    target = after / "delta_audit"
    assert not target.exists(), "Preserve prior evidence"
    left = json.loads((before / "summary.json").read_text("utf-8"))
    right = json.loads((after / "summary.json").read_text("utf-8"))
    assert len(left["results"]) == len(right["results"]) == 36
    expected_status = "prepared_not_independently_validated"
    assert left["status"] == right["status"] == expected_status
    prior = {(r["symbol"], r["basis"]): r for r in left["results"]}
    eligible_removals = {"roic_operational", "roic_operational_growth_1y", "incremental_investment_rate_pct",
        "roiic_pct", "roiic_wacc_spread", "fcf_negative_freq_5y_pct", "fcf_negative_freq_10y_pct"}
    # All reviewed receipts in this scope lack both reviewed trade receivables
    # and payables, and reviewed capex. Their absence cannot prove a zero amount.
    missing_evidence = {}
    for symbol in sorted({r["symbol"] for r in right["results"]}):
        manifest = ROOT / "data-lake/silver/dart/normalized/history" / symbol / "manifest.json"
        records = json.loads(manifest.read_text("utf-8"))["receipts"]
        accounts = set()
        for record in records:
            path = manifest.parent / record["normalized_path"]
            assert digest(path) == record["normalized_sha256"]
            accounts.update(pd.read_csv(path, usecols=["canonical_account_id"]).canonical_account_id)
        unreviewed = {"TRADE_RECEIVABLES", "TRADE_AND_OTHER_RECEIVABLES", "OTHER_RECEIVABLES",
            "TRADE_PAYABLES", "TRADE_AND_OTHER_PAYABLES", "OTHER_PAYABLES", "CAPEX_PPE", "CAPEX_INTANG"}
        assert not (accounts & unreviewed)
        missing_evidence[symbol] = dict(receipts=len(records), manifest_sha256=digest(manifest),
            accepted_accounts=sorted(accounts), unreviewed_required_accounts=sorted(unreviewed))
    removed, cases, preserved_seven = [], [], []
    for item in right["results"]:
        symbol, basis = item["symbol"], item["basis"]
        old_item = prior[(symbol, basis)]
        assert old_item["source_version"]["manifest_sha256"] == item["source_version"]["manifest_sha256"]
        assert old_item["price_sha256"] == item["price_sha256"]
        old_path, new_path = (p / symbol / basis / "prepared.parquet" for p in (before, after))
        assert digest(old_path) == old_item["prepared_sha256"] and digest(new_path) == item["prepared_sha256"]
        a, b = canonical(pd.read_parquet(old_path)), canonical(pd.read_parquet(new_path))
        extra, dropped = b.index.difference(a.index), a.index.difference(b.index)
        assert not len(extra), "Unexpected new cells need another review"
        common = a.index.intersection(b.index)
        np.testing.assert_allclose(a.loc[common, "factor_value"].to_numpy(dtype=float),
            b.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)
        delta = a.loc[dropped, ["factor_value"]].reset_index()
        assert set(delta.factor_id) <= eligible_removals
        delta["reason"] = np.where(delta.factor_id.str.startswith("fcf_negative_freq"),
            "no_reviewed_capex_no_observable_fcf", "unreviewed_operating_capital_components")
        removed.append(delta)
        recent = b.reset_index()
        preserved_seven.append(recent.loc[recent.factor_id.isin(FACTOR_IDS) & recent.trade_date.ge("2017-01-01")])
        cases.append(dict(symbol=symbol, basis=basis, before_rows=len(a), after_rows=len(b),
                          removed_rows=len(delta), changed_values=0, added_rows=0))
    discarded = pd.concat(removed, ignore_index=True)
    preserved = pd.concat(preserved_seven, ignore_index=True)
    latest = json.loads((SILVER / "kr_v7_period_publication/latest.json").read_text("utf-8"))
    publication = Path(latest["publication_path"])
    assert digest(publication) == latest["sha256"]
    published = pd.read_parquet(publication.parent / "prepared.parquet")
    compare(preserved, published)
    target.mkdir()
    discarded.to_parquet(target / "withdrawn_candidates.parquet", index=False)
    report = dict(status="delta_verified", native_published=False, coverage_complete=False,
        before_rows=sum(c["before_rows"] for c in cases), after_rows=sum(c["after_rows"] for c in cases),
        withdrawn_rows=len(discarded), withdrawn_by_factor=dict(Counter(discarded.factor_id)),
        existing_published_seven_rows_unchanged=len(preserved), cases=cases, source_evidence=missing_evidence,
        remaining="Full candidate validation and native reconciliation remain; this is not a full publication approval.")
    save(target / "summary.json", report)
    print({k: report[k] for k in ("status", "before_rows", "after_rows", "withdrawn_rows", "withdrawn_by_factor", "existing_published_seven_rows_unchanged")})


if __name__ == "__main__":
    main()
