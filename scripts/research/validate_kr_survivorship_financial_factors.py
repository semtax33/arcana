"""Validate daily historical fundamentals against reviewed source amounts.

This read-only calculation does not publish manifests or insert native factors.
It compares the full price-date universe, including expected missing factors.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.loaders.factors import insert_daily_factors

FACTOR_IDS = ["asset_turnover", "gpm", "gross_profitability_pct", "npm", "opm", "roa", "roe"]
KEYS = ["security_id", "trade_date", "financial_basis", "factor_id"]
SILVER = DATA_LAKE.silver("survivorship", "financial_research")


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def options(symbol, financial_dir, start, end):
    return dict(stock_codes=[symbol], market="kr", financial_basis="annual", start_date=start,
                end_date=end, factor_ids=FACTOR_IDS, insert_catalog=False, financial_dir=financial_dir,
                use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False,
                split_insert_by_partition=True)


def expected_at(annual, amounts, day):
    known = [r for r in annual if pd.Timestamp(r["report_date"]) < pd.Timestamp(day)]
    if not known:
        return {}, None, None
    latest = max(known, key=lambda r: (r["period_end_date"], r["report_date"], r["rcept_no"]))
    a = amounts[latest["rcept_no"]]
    sales, income, assets = (a.get(k, np.nan) for k in ("REVENUE", "NET_INCOME", "TOTAL_ASSETS"))
    gross = a.get("GROSS_PROFIT", sales - a.get("COGS", np.nan))
    operating = a.get("OPERATING_INCOME", gross - a.get("OPERATING_EXPENSES_TOTAL", a.get("SGNA", np.nan)))

    def ratio(numerator, denominator, scale=1):
        if denominator == 0 or not np.isfinite(numerator) or not np.isfinite(denominator):
            return np.nan
        return numerator / denominator * scale

    values = {"npm": ratio(income, sales, 100), "opm": ratio(operating, sales, 100),
              "gpm": ratio(gross, sales, 100), "gross_profitability_pct": ratio(gross, assets, 100)}
    candidates = [r for r in known if r["fiscal_year"] == latest["fiscal_year"] - 1]
    previous = max(candidates, key=lambda r: (r["report_date"], r["rcept_no"])) if candidates else None
    if previous and all(previous[k] == latest[k] for k in ("financial_scope", "accounting_regime")):
        b = amounts[previous["rcept_no"]]
        average_assets = (assets + b.get("TOTAL_ASSETS", np.nan)) / 2
        average_equity = (a.get("EAOP", a.get("TOTAL_EQUITY", np.nan))
                          + b.get("EAOP", b.get("TOTAL_EQUITY", np.nan))) / 2
        values.update(asset_turnover=ratio(sales, average_assets), roa=ratio(income, average_assets),
                      roe=ratio(a.get("NET_INCOME_PARENT", income), average_equity, 100))
    return {k: v for k, v in values.items() if np.isfinite(v)}, latest, previous


def canonical(frame):
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date).astype("datetime64[ns]")
    for column in ("security_id", "financial_basis", "factor_id"):
        frame[column] = frame[column].astype("string")
    assert not frame.duplicated(KEYS).any(), "Duplicate factor identity"
    return frame.set_index(KEYS).sort_index()


def validate_symbol(symbol, review_root, staging_root, output, start, end):
    source = review_root / symbol
    review_path, source_review_path = source / "review.json", source / "source_review.json"
    review = json.loads(review_path.read_text("utf-8"))
    evidence = json.loads(source_review_path.read_text("utf-8"))
    assert review["symbol"] == symbol and review["market"] == "kr"
    assert evidence["policy_sha256"] == digest(source / "review_policy.py")
    amounts = {r["receipt"]: {a["canonical_account_id"]: float(a["value"]) for a in r["accepted"]}
               for r in evidence["results"] if r["status"] == "reviewed"}
    financial_dir = staging_root / symbol / "staged_financial"
    manifest_path = financial_dir / "history" / symbol / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert {r["rcept_no"] for r in review["receipts"]} == {r["rcept_no"] for r in manifest["receipts"]}
    dependencies = {str(p): digest(p) for p in (review_path, source_review_path, manifest_path)}
    for record in review["receipts"]:
        for kind in ("source", "normalized"):
            path = (Path(review["source_root"]) / record[kind + "_path"]).resolve()
            assert digest(path) == record[kind + "_sha256"]
        frame = pd.read_csv(record["normalized_path"])
        for key in record["accepted_canonical_account_ids"]:
            values = frame.loc[frame.canonical_account_id.eq(key), "normalized_amount"].astype(float)
            assert len(values) == 1 and float(values.iloc[0]) == amounts[record["rcept_no"]][key]
    panel = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet")
    dependencies[str(panel)] = digest(panel)
    prices = pd.read_parquet(panel)
    prices["trade_date"] = pd.to_datetime(prices.trade_date)
    days = sorted(prices.loc[prices.trade_date.between(start, end), "trade_date"].unique())
    assert len(days) > 0
    annual = [r for r in review["receipts"] if r["financial_basis"] == "annual"]
    rows, lineage = [], []
    for day in days:
        expected, latest, previous = expected_at(annual, amounts, day)
        for factor, value in expected.items():
            rows.append(dict(security_id=f"SEC_KR_{symbol}", trade_date=day, financial_basis="annual",
                             factor_id=factor, factor_value=value))
        lineage.append(dict(trade_date=day, rcept_no=latest["rcept_no"] if latest else None,
                            preceding_rcept_no=previous["rcept_no"] if previous else None,
                            expected_factor_count=len(expected)))
    expected = pd.DataFrame(rows, columns=KEYS + ["factor_value"])
    actual = insert_daily_factors(dry_run=True, **options(symbol, financial_dir, start, end))
    target = output / symbol
    target.mkdir(parents=True, exist_ok=True)
    actual.to_parquet(target / "annual_daily_factors.parquet", index=False)
    expected.to_parquet(target / "independent_expected_factors.parquet", index=False)
    pd.DataFrame(lineage).to_parquet(target / "daily_source_receipts.parquet", index=False)
    actual_keys, expected_keys = canonical(actual), canonical(expected)
    missing = expected_keys.index.difference(actual_keys.index)
    extra = actual_keys.index.difference(expected_keys.index)
    common = actual_keys.index.intersection(expected_keys.index)
    equal = np.isclose(actual_keys.loc[common, "factor_value"].to_numpy(dtype=float),
                       expected_keys.loc[common, "factor_value"].to_numpy(dtype=float),
                       rtol=1e-12, atol=1e-12, equal_nan=False)
    failures = []
    for identity in common[~equal][:20]:
        failures.append(dict(zip(KEYS, identity), actual=float(actual_keys.loc[identity, "factor_value"]),
                             expected=float(expected_keys.loc[identity, "factor_value"])))
    for path, expected_hash in dependencies.items():
        assert digest(path) == expected_hash, "Inputs changed during validation"
    result = dict(symbol=symbol, status="validated" if not len(missing) and not len(extra) and equal.all() else "requires_review",
                  factor_rows=len(actual), price_dates_checked=len(days), dates_without_expected_factors=sum(r["expected_factor_count"] == 0 for r in lineage),
                  annual_receipts=len(annual), missing_rows=len(missing), extra_rows=len(extra), differing_values=int((~equal).sum()),
                  missing_examples=list(missing[:20]), extra_examples=list(extra[:20]), differing_examples=failures,
                  by_factor=actual.groupby("factor_id").size().to_dict(), dependency_sha256=dependencies,
                  factor_artifact_sha256=digest(target / "annual_daily_factors.parquet"),
                  production_published=False, coverage_complete=False)
    save(target / "validation.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, default=SILVER / "kr_receipt_history_v7_review_input/reviewed")
    parser.add_argument("--output", type=Path, default=SILVER / "kr_v7_daily_factor_validation")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--start", default="2017-01-01")
    parser.add_argument("--end", default="2026-09-10")
    args = parser.parse_args()
    assert not (args.output / "summary.json").exists(), "Preserve an existing validation; use another --output"
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    symbols = args.symbols or sorted(p.name for p in args.review_root.iterdir() if (p / "review.json").exists())
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(), validator_sha256=digest(__file__), results=[])
    save(args.output / "summary.json", report)
    for symbol in symbols:
        try:
            result = validate_symbol(symbol, args.review_root, args.staging_root, args.output, args.start, args.end)
        except Exception as exc:
            result = dict(symbol=symbol, status="error", error_type=type(exc).__name__, error=str(exc))
        report["results"].append(result)
        save(args.output / "summary.json", report)
        print(symbol, result["status"], {k: result[k] for k in ("factor_rows", "missing_rows", "extra_rows", "differing_values", "error") if k in result}, flush=True)
    report.update(status="validated" if all(r["status"] == "validated" for r in report["results"]) else "requires_review",
                  finished_at=datetime.now(timezone.utc).isoformat())
    save(args.output / "summary.json", report)
    if report["status"] != "validated":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
