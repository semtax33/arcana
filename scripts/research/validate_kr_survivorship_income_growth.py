"""Validate all three income-growth horizons directly from reviewed receipt amounts."""
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from validate_kr_survivorship_financial_factors import SILVER, KEYS, canonical, digest, save

FACTORS = [f"net_income_growth_{n}y" for n in (1, 3, 5)]


def expected_growth(receipts, amounts, day, basis):
    known = sorted([r for r in receipts if pd.Timestamp(r["report_date"]) < day
        and (basis != "annual" or r["financial_basis"] == "annual")], key=lambda r: (r["report_date"], r["rcept_no"]))
    history = sorted({r["period_end_date"]: r for r in known}.values(), key=lambda r: r["period_end_date"])
    if not history:
        return {}
    scope = (history[-1]["financial_scope"], history[-1]["accounting_regime"])
    comparable = []
    for r in reversed(history):
        if (r["financial_scope"], r["accounting_regime"]) != scope:
            break
        comparable.append(r)
    def index(record):
        return record["fiscal_year"] if basis == "annual" else record["fiscal_year"] * 4 + record["fiscal_month"] // 3 - 1
    sequence = {index(r): r for r in comparable}
    latest = max(sequence)
    def raw(i, account):
        return amounts[sequence[i]["rcept_no"]].get(account, np.nan) if i in sequence else np.nan
    def quarter(i, account):
        return raw(i, account) if i % 4 == 0 else raw(i, account) - raw(i - 1, account)
    def value(i, account):
        if basis == "annual":
            return raw(i, account)
        if basis == "quarterly":
            return quarter(i, account)
        return sum(quarter(j, account) for j in range(i - 3, i + 1))
    result = {}
    for years in (1, 3, 5):
        previous = latest - years * (1 if basis == "annual" else 4)
        a, b = value(latest, "NET_INCOME_PARENT"), value(previous, "NET_INCOME_PARENT")
        if not (np.isfinite(a) and np.isfinite(b)):
            a, b = value(latest, "NET_INCOME"), value(previous, "NET_INCOME")
        if np.isfinite(a) and np.isfinite(b) and ((a > 0 and b > 0) or (a < 0 and b < 0)):
            result[f"net_income_growth_{years}y"] = (a - b) / abs(b) * 100
    return result


def main():
    preparation = SILVER / "kr_full_factor_preparation_final"
    prepared_summary = json.loads((preparation / "summary.json").read_text("utf-8"))
    assert len(prepared_summary["results"]) == 36 and prepared_summary["status"] == "prepared_not_independently_validated"
    target = preparation / "income_growth_validation"
    assert not target.exists(), "Preserve earlier validation"
    results = []
    for case in prepared_summary["results"]:
        symbol, basis = case["symbol"], case["basis"]
        folder = ROOT / "deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot"
        source = folder / ("kr_receipt_history_v5_jtech" if symbol == "035480" else "kr_receipt_history_v7_review_input") / "reviewed" / symbol
        review = json.loads((source / "review.json").read_text("utf-8"))
        evidence = json.loads((source / "source_review.json").read_text("utf-8"))
        amounts = {r["receipt"]: {v["canonical_account_id"]: float(v["value"]) for v in r["accepted"]}
            for r in evidence["results"] if r["status"] == "reviewed"}
        for record in review["receipts"]:
            for kind in ("source", "normalized"):
                assert digest(Path(review["source_root"]) / record[kind + "_path"]) == record[kind + "_sha256"]
        prices = pd.read_parquet(ROOT / f"data-lake/silver/corporate_actions/prices/kr/kr_{symbol}.parquet", columns=["trade_date"])
        days = pd.to_datetime(prices.trade_date).loc[lambda s: s.between(case["start"], case["end"])]
        dates = sorted({r["report_date"] for r in review["receipts"]})
        rows, cache = [], {}
        for day in days:
            vintage = max((d for d in dates if pd.Timestamp(d) < day), default=None)
            if vintage not in cache:
                cache[vintage] = expected_growth(review["receipts"], amounts, day, basis)
            rows.extend(dict(security_id=f"SEC_KR_{symbol}", trade_date=day, financial_basis=basis,
                factor_id=f, factor_value=v) for f, v in cache[vintage].items())
        expected = pd.DataFrame(rows, columns=KEYS + ["factor_value"])
        path = preparation / symbol / basis / "prepared.parquet"
        assert digest(path) == case["prepared_sha256"]
        actual = pd.read_parquet(path)
        actual = actual.loc[actual.factor_id.isin(FACTORS)]
        a, b = canonical(actual), canonical(expected)
        common = a.index.intersection(b.index)
        missing, extra = b.index.difference(a.index), a.index.difference(b.index)
        mismatch = ~np.isclose(a.loc[common, "factor_value"].to_numpy(dtype=float),
            b.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)
        output = target / symbol / basis
        output.mkdir(parents=True)
        expected.to_parquet(output / "expected.parquet", index=False)
        result = dict(symbol=symbol, basis=basis, rows=len(actual), missing=len(missing), extra=len(extra),
            differing=int(mismatch.sum()), days_checked=len(days), review_sha256=digest(source / "review.json"),
            evidence_sha256=digest(source / "source_review.json"), preparation_sha256=digest(path))
        result["status"] = "validated" if not len(missing) and not len(extra) and not mismatch.any() else "requires_review"
        results.append(result)
        save(output / "validation.json", result)
    status = "validated" if all(r["status"] == "validated" for r in results) else "requires_review"
    save(target / "summary.json", dict(status=status, validator_sha256=digest(__file__), results=results,
        validated_rows=sum(r["rows"] for r in results), coverage_complete=False))
    print(status, "rows", sum(r["rows"] for r in results), "cases", len(results))
    if status != "validated":
        print([r for r in results if r["status"] != "validated"])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
