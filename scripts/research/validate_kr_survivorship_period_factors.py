"""Independent annual/quarterly/TTM source arithmetic for reviewed historical issuers."""
import argparse
from datetime import datetime, timezone
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
from validate_kr_survivorship_financial_factors import FACTOR_IDS, KEYS, SILVER, canonical, digest, save

FLOWS = {"REVENUE", "NET_INCOME", "NET_INCOME_PARENT", "COGS", "GROSS_PROFIT", "OPERATING_INCOME", "SGNA", "OPERATING_EXPENSES_TOTAL"}
BALANCES = {"TOTAL_ASSETS", "TOTAL_EQUITY", "EAOP"}


def source_values(receipts, amounts, day, basis):
    if basis == "annual":
        receipts = [r for r in receipts if r["financial_basis"] == "annual"]
    known = sorted((r for r in receipts if pd.Timestamp(r["report_date"]) < pd.Timestamp(day)),
                   key=lambda r: (r["report_date"], r["rcept_no"]))
    by_period = {r["period_end_date"]: r for r in known}
    history = sorted(by_period.values(), key=lambda r: r["period_end_date"])
    if not history:
        return {}, None, []
    current = history[-1]
    current_scope = (current["financial_scope"], current["accounting_regime"])
    comparable = []
    for r in reversed(history):
        if (r["financial_scope"], r["accounting_regime"]) != current_scope:
            break
        comparable.append(r)
    sequence = {r["fiscal_year"] * 4 + r["fiscal_month"] // 3 - 1: r for r in comparable}
    q = current["fiscal_year"] * 4 + current["fiscal_month"] // 3 - 1

    def amount(index, account):
        record = sequence.get(index)
        return amounts[record["rcept_no"]].get(account, np.nan) if record else np.nan

    def quarter(index, account):
        if account in BALANCES:
            return amount(index, account)
        # Quarter one starts a new YTD series. Other quarters require exactly
        # the preceding fiscal quarter; gaps and missing accounts stay missing.
        return amount(index, account) if index % 4 == 0 else amount(index, account) - amount(index - 1, account)

    def value(account):
        if basis == "annual":
            return amount(q, account)
        if basis == "quarterly" or account in BALANCES:
            return quarter(q, account)
        return sum(quarter(index, account) for index in range(q - 3, q + 1))

    a = {key: value(key) for key in FLOWS | BALANCES}
    b = {key: amount(q - 4, key) for key in BALANCES}
    gross = a["GROSS_PROFIT"] if np.isfinite(a["GROSS_PROFIT"]) else a["REVENUE"] - a["COGS"]
    operating = a["OPERATING_INCOME"]
    if not np.isfinite(operating):
        cost = a["OPERATING_EXPENSES_TOTAL"] if np.isfinite(a["OPERATING_EXPENSES_TOTAL"]) else a["SGNA"]
        operating = gross - cost

    def ratio(numerator, denominator, scale=1):
        return numerator / denominator * scale if np.isfinite(numerator) and np.isfinite(denominator) and denominator != 0 else np.nan

    average_assets = (a["TOTAL_ASSETS"] + b["TOTAL_ASSETS"]) / 2
    expected = {"gpm": ratio(gross, a["REVENUE"], 100), "opm": ratio(operating, a["REVENUE"], 100),
                "npm": ratio(a["NET_INCOME"], a["REVENUE"], 100),
                "gross_profitability_pct": ratio(gross, a["TOTAL_ASSETS"], 100),
                "asset_turnover": ratio(a["REVENUE"], average_assets), "roa": ratio(a["NET_INCOME"], average_assets)}
    # Prefer a complete parent basis; otherwise use complete group income and
    # group equity together. Partial ownership fields are not independent fills.
    parent_fields = [a["NET_INCOME_PARENT"], a["EAOP"], b["EAOP"]]
    if all(np.isfinite(v) for v in parent_fields) and (a["EAOP"] + b["EAOP"]) != 0:
        expected["roe"] = ratio(a["NET_INCOME_PARENT"], (a["EAOP"] + b["EAOP"]) / 2, 100)
        ownership_basis = "parent"
    else:
        expected["roe"] = ratio(a["NET_INCOME"], (a["TOTAL_EQUITY"] + b["TOTAL_EQUITY"]) / 2, 100)
        ownership_basis = "group"
    lineage = {"rcept_no": current["rcept_no"], "ownership_basis": ownership_basis,
               "source_receipts": [sequence[index]["rcept_no"] for index in range(q - 4, q + 1) if index in sequence]}
    return {key: value for key, value in expected.items() if np.isfinite(value)}, lineage, a


def validate(source, output, basis, start, end):
    review_path, evidence_path = source / "review.json", source / "source_review.json"
    review = json.loads(review_path.read_text("utf-8"))
    evidence = json.loads(evidence_path.read_text("utf-8"))
    symbol = review["symbol"]
    policy = source / "review_policy.py"
    if not policy.exists() and symbol == "035480":
        policy = ROOT / "deliverables/cross_market_top70_20260909/survivorship/review_jtech_financial_history.py"
    assert evidence["policy_sha256"] == digest(policy)
    amounts = {r["receipt"]: {v["canonical_account_id"]: float(v["value"]) for v in r["accepted"]}
               for r in evidence["results"] if r["status"] == "reviewed"}
    financial_dir = DATA_LAKE.silver("dart", "normalized")
    manifest_path = financial_dir / "history" / symbol / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert {r["rcept_no"] for r in manifest["receipts"]} == {r["rcept_no"] for r in review["receipts"]}
    for record in review["receipts"]:
        for kind in ("source", "normalized"):
            assert digest(Path(review["source_root"]) / record[kind + "_path"]) == record[kind + "_sha256"]
    panel_path = DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet")
    frame = pd.read_parquet(panel_path)
    days = sorted(pd.to_datetime(frame.trade_date).loc[lambda s: s.between(start, end)].unique())
    rows, lineage, cache = [], [], {}
    # Only publication vintages change source arithmetic; keep the price-date
    # enumeration independent of the factor loader's emitted rows.
    dates = sorted({r["report_date"] for r in review["receipts"]})
    for day in days:
        vintage = max((d for d in dates if pd.Timestamp(d) < day), default=None)
        if vintage not in cache:
            cache[vintage] = source_values(review["receipts"], amounts, day, basis)
        values, provenance, _ = cache[vintage]
        for factor, value in values.items():
            rows.append(dict(security_id=f"SEC_KR_{symbol}", trade_date=day, financial_basis=basis,
                             factor_id=factor, factor_value=value))
        lineage.append(dict(trade_date=day, expected_factor_count=len(values), **(provenance or {})))
    expected = pd.DataFrame(rows, columns=KEYS + ["factor_value"])
    actual = insert_daily_factors(stock_codes=[symbol], financial_basis=basis, start_date=start, end_date=end,
        factor_ids=FACTOR_IDS, market="kr", financial_dir=financial_dir, dry_run=True, insert_catalog=False,
        use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
    target = output / symbol / basis
    target.mkdir(parents=True, exist_ok=True)
    actual.to_parquet(target / "actual.parquet", index=False)
    expected.to_parquet(target / "expected.parquet", index=False)
    pd.DataFrame(lineage).to_parquet(target / "source_lineage.parquet", index=False)
    a, e = canonical(actual), canonical(expected)
    missing, extra = e.index.difference(a.index), a.index.difference(e.index)
    common = a.index.intersection(e.index)
    differs = ~np.isclose(a.loc[common, "factor_value"].to_numpy(dtype=float), e.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)
    errors = [dict(zip(KEYS, key), actual=float(a.loc[key, "factor_value"]), expected=float(e.loc[key, "factor_value"])) for key in common[differs][:20]]
    result = dict(symbol=symbol, basis=basis, status="validated" if not len(missing) and not len(extra) and not differs.any() else "requires_review",
                  rows=len(actual), dates_checked=len(days), missing=len(missing), extra=len(extra), differing=int(differs.sum()),
                  missing_examples=list(missing[:20]), extra_examples=list(extra[:20]), differing_examples=errors,
                  source_hashes={str(p): digest(p) for p in (review_path, evidence_path, manifest_path, panel_path)},
                  actual_sha256=digest(target / "actual.parquet"), production_published=False)
    save(target / "validation.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--additional-review", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, default=SILVER / "kr_v7_period_factor_validation")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--bases", nargs="+", choices=["annual", "quarterly", "ttm"], default=["quarterly", "ttm"])
    args = parser.parse_args()
    assert not (args.output / "summary.json").exists(), "Preserve earlier evidence; use another --output"
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    sources = sorted(p for p in args.review_root.iterdir() if (p / "review.json").exists()) + args.additional_review
    sources = [p for p in sources if not args.symbols or p.name in args.symbols]
    report = dict(status="running", validator_sha256=digest(__file__), started_at=datetime.now(timezone.utc).isoformat(), results=[])
    save(args.output / "summary.json", report)
    for source in sources:
        for basis in args.bases:
            try:
                result = validate(source, args.output, basis, "2017-01-01", "2026-09-10")
            except Exception as exc:
                result = dict(symbol=source.name, basis=basis, status="error", error_type=type(exc).__name__, error=str(exc))
            report["results"].append(result)
            save(args.output / "summary.json", report)
            print({k: result[k] for k in ("symbol", "basis", "status", "rows", "missing", "extra", "differing", "error") if k in result}, flush=True)
    report.update(status="validated" if all(r["status"] == "validated" for r in report["results"]) else "requires_review", finished_at=datetime.now(timezone.utc).isoformat())
    save(args.output / "summary.json", report)
    if report["status"] != "validated":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
