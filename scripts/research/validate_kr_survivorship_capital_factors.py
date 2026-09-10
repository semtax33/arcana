"""Independent source arithmetic for operating-capital factors and period units."""
import argparse
import json
from pathlib import Path
from statistics import median
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from validate_kr_survivorship_financial_factors import KEYS, canonical, digest, save
from audit_kr_survivorship_capital_sources import PILOT

FACTORS = ["rect", "ap", "receivables_turnover", "inv_days", "ar_days", "ap_days", "ccc",
    "roic_operational", "roic_operational_growth_1y", "roiic_pct", "incremental_investment_rate_pct"]


def ratio(a, b, scale=1):
    return a / b * scale if np.isfinite(a) and np.isfinite(b) and b != 0 else np.nan


def expected_values(receipts, amounts, day, basis):
    known = sorted([r for r in receipts if pd.Timestamp(r["report_date"]) < day
        and (basis != "annual" or r["financial_basis"] == "annual")], key=lambda r: (r["report_date"], r["rcept_no"]))
    history = sorted({r["period_end_date"]: r for r in known}.values(), key=lambda r: r["period_end_date"])
    if not history:
        return {}
    comparable = []
    scope = (history[-1]["financial_scope"], history[-1]["accounting_regime"])
    for record in reversed(history):
        if (record["financial_scope"], record["accounting_regime"]) != scope:
            break
        comparable.append(record)
    sequence = {r["fiscal_year"] * 4 + r["fiscal_month"] // 3 - 1: r for r in comparable}
    first, last = min(sequence), max(sequence)
    def amount(q, account):
        return amounts[sequence[q]["rcept_no"]].get(account, np.nan) if q in sequence else np.nan
    def quarter(q, account):
        return amount(q, account) if q % 4 == 0 else amount(q, account) - amount(q - 1, account)
    def flow(q, account):
        if basis == "annual":
            return amount(q, account)
        if basis == "quarterly":
            return quarter(q, account)
        return sum(quarter(i, account) for i in range(q - 3, q + 1))
    periods, tax_history = {}, []
    for q in range(first, last + 1, 4 if basis == "annual" else 1):
        sale, cogs = flow(q, "REVENUE"), flow(q, "COGS")
        operating = flow(q, "OPERATING_INCOME")
        if not np.isfinite(operating):
            gross = flow(q, "GROSS_PROFIT")
            if not np.isfinite(gross):
                gross = sale - cogs
            expenses = flow(q, "OPERATING_EXPENSES_TOTAL")
            operating = gross - (expenses if np.isfinite(expenses) else flow(q, "SGNA"))
        observed_tax = ratio(flow(q, "TAX_EXPENSE"), flow(q, "PBT"))
        if np.isfinite(observed_tax) and 0 <= observed_tax <= 1:
            tax = observed_tax
            tax_history.append(tax)
        else:
            tax = median(tax_history) if tax_history else (0.21 if operating >= 0 else 0.)
        capital = sum(amount(q, account) for account in ("TRADE_RECEIVABLES", "INVENTORIES", "PPE", "INTANGIBLE_ASSETS")) - amount(q, "TRADE_PAYABLES")
        periods[q] = dict(sale=sale, cogs=cogs, capital=capital, nopat=operating * (1 - tax))
    def roic(q):
        current = periods.get(q, {})
        capital = current.get("capital", np.nan)
        previous = periods.get(q - 4, {}).get("capital", np.nan)
        denominator = (capital + previous) / 2 if np.isfinite(previous) else capital
        return ratio(current.get("nopat", np.nan), denominator, 100)
    current = periods[last]
    ar = amount(last, "TRADE_RECEIVABLES")
    ap = amount(last, "TRADE_PAYABLES")
    avg_ar = (ar + amount(last - 4, "TRADE_RECEIVABLES")) / 2
    avg_ap = (ap + amount(last - 4, "TRADE_PAYABLES")) / 2
    avg_inventory = (amount(last, "INVENTORIES") + amount(last - 4, "INVENTORIES")) / 2
    days = 365 if basis != "quarterly" else 91.25
    values = dict(rect=ar, ap=ap, receivables_turnover=ratio(current["sale"], avg_ar),
        inv_days=ratio(avg_inventory, current["cogs"], days), ar_days=ratio(avg_ar, current["sale"], days),
        ap_days=ratio(avg_ap, current["cogs"], days), roic_operational=roic(last))
    values["ccc"] = values["inv_days"] + values["ar_days"] - values["ap_days"]
    values["roic_operational_growth_1y"] = ratio(roic(last) - roic(last - 4), abs(roic(last - 4)), 100)
    prior = periods.get(last - 4, {})
    difference = current["capital"] - prior.get("capital", np.nan)
    previous_capital = prior.get("capital", np.nan)
    material = (np.isfinite(difference) and np.isfinite(previous_capital) and abs(previous_capital) > 0
        and abs(difference) >= abs(previous_capital) * .01)
    if material:
        values["roiic_pct"] = ratio(current["nopat"] - prior.get("nopat", np.nan), difference, 100)
        sales_change = current["sale"] - prior.get("sale", np.nan)
        if sales_change > 0:
            values["incremental_investment_rate_pct"] = ratio(difference, sales_change, 100)
    return {key: value for key, value in values.items() if np.isfinite(value)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--capital-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    summary = json.loads((args.preparation / "summary.json").read_text("utf-8"))
    assert summary["status"] == "prepared_not_independently_validated" and len(summary["results"]) == 36
    additions = json.loads(args.capital_review.read_text("utf-8"))
    assert additions["status"] == "reviewed"
    report = dict(status="running", cases=[], preparation_sha256=digest(args.preparation / "summary.json"),
        capital_review_sha256=digest(args.capital_review), validator_sha256=digest(__file__),
        assumptions="365-day year; standalone quarterly flows annualized; existing current/prior-year mean balances; NOPAT observed tax, prior valid median, or declared 21%/loss 0% fallback.",
        coverage_complete=False, native_published=False)
    save(args.output / "summary.json", report)
    for case in summary["results"]:
        symbol, basis = case["symbol"], case["basis"]
        folder = PILOT / ("kr_receipt_history_v5_jtech" if symbol == "035480" else "kr_receipt_history_v7_review_input") / "reviewed" / symbol
        reviewed = json.loads((folder / "source_review.json").read_text("utf-8"))
        amounts = {r["receipt"]: {a["canonical_account_id"]: float(a["value"]) for a in r["accepted"]}
            for r in reviewed["results"] if r["status"] == "reviewed"}
        if symbol == "003410":
            for receipt in additions["results"]:
                if receipt["symbol"] == symbol:
                    amounts[receipt["receipt"]].update({a["canonical_account_id"]: float(a["value"]) for a in receipt["accepted"]})
        manifest_path = Path(case["source_version"]["manifest_path"])
        assert digest(manifest_path) == case["source_version"]["manifest_sha256"]
        receipts = json.loads(manifest_path.read_text("utf-8"))["receipts"]
        for r in receipts:
            assert set(r["accepted_canonical_account_ids"]) == set(amounts[r["rcept_no"]])
            source_root = Path(r.get("evidence_root", manifest_path.parent))
            assert digest(source_root / r["source_path"]) == r["source_sha256"]
        panel = ROOT / f"data-lake/silver/corporate_actions/prices/kr/kr_{symbol}.parquet"
        assert digest(panel) == case["price_sha256"]
        dates = pd.to_datetime(pd.read_parquet(panel, columns=["trade_date"]).trade_date)
        dates = dates.loc[dates.between(case["start"], case["end"])]
        rows, cache = [], {}
        publication_dates = sorted({r["report_date"] for r in receipts})
        for day in dates:
            vintage = max((d for d in publication_dates if pd.Timestamp(d) < day), default=None)
            if vintage not in cache:
                cache[vintage] = expected_values(receipts, amounts, day, basis)
            rows.extend(dict(security_id=f"SEC_KR_{symbol}", trade_date=day, financial_basis=basis,
                factor_id=factor, factor_value=value) for factor, value in cache[vintage].items())
        expected = pd.DataFrame(rows, columns=KEYS + ["factor_value"])
        prepared_path = args.preparation / symbol / basis / "prepared.parquet"
        assert digest(prepared_path) == case["prepared_sha256"]
        actual = pd.read_parquet(prepared_path)
        actual = actual.loc[actual.factor_id.isin(FACTORS)]
        a, e = canonical(actual), canonical(expected)
        common = a.index.intersection(e.index)
        mismatch = ~np.isclose(a.loc[common, "factor_value"].to_numpy(float), e.loc[common, "factor_value"].to_numpy(float), rtol=1e-10, atol=1e-10)
        output = args.output / symbol / basis
        output.mkdir(parents=True)
        expected.to_parquet(output / "expected.parquet", index=False)
        result = dict(symbol=symbol, basis=basis, rows=len(actual), missing=len(e.index.difference(a.index)),
            extra=len(a.index.difference(e.index)), differing=int(mismatch.sum()), expected_sha256=digest(output / "expected.parquet"),
            source_review_sha256=digest(folder / "source_review.json"))
        result["status"] = "validated" if not (result["missing"] or result["extra"] or result["differing"]) else "requires_review"
        if result["status"] != "validated":
            a.join(e, lsuffix="_actual", rsuffix="_expected", how="outer").reset_index().to_parquet(output / "comparison.parquet", index=False)
        save(output / "validation.json", result)
        report["cases"].append(result)
        save(args.output / "summary.json", report)
        print({k:v for k,v in result.items() if not k.endswith("sha256")}, flush=True)
    report.update(status="validated" if all(r["status"] == "validated" for r in report["cases"]) else "requires_review",
        validated_rows=sum(r["rows"] for r in report["cases"] if r["status"] == "validated"))
    save(args.output / "summary.json", report)
    if report["status"] != "validated":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
