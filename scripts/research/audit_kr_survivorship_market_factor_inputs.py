"""Independently reconcile reviewed KR market factors with pinned input files.

Read-only against native storage. Reconstruct price units from official events,
weekly OLS from price observations, and effective tax rates from receipt amounts.
All audit products belong in silver; no raw disclosure is relocated or edited.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from validate_kr_survivorship_financial_factors import SILVER, KEYS, canonical, digest, save

TECHNICAL = [f"na_{n}" for n in (5, 20, 50, 150, 200)] + [f"ma_{n}" for n in (50, 120, 150, 200)] + [
    "macd", "macd_signal", "macd_hist", "bb_middle", "bb_upper", "bb_lower", "bb_width_pct", "bb_percent_b",
    "ati", "williams_r_14", "mfi_14"]
FACTORS = TECHNICAL + ["beta", "cost_of_equity", "cost_of_debt_after_tax"]
DEBT_ACCOUNTS = {"LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK", "SHORT_TERM_DEBT"}
INTEREST_ACCOUNTS = {"INTEREST_EXPENSE", "INTEREST_EXPENSE_FALLBACK", "INT_PAID", "INTEREST_PAID_FALLBACK", "FINANCE_COST_FALLBACK"}


def ema(values, span, minimum):
    """Direct recursive definition, independent of production EWM helpers."""
    result, previous, count = [], np.nan, 0
    alpha = 2 / (span + 1)
    for value in values:
        if np.isfinite(value):
            previous = value if not np.isfinite(previous) else alpha * value + (1 - alpha) * previous
            count += 1
        result.append(previous if count >= minimum else np.nan)
    return pd.Series(result, index=values.index)


def price_factors(panel, multiplier):
    close = panel.close * multiplier
    observed = panel.volume.gt(0) & panel.close.gt(0) & close.gt(0)
    close = close.where(observed).ffill()
    high = (panel.high * multiplier).fillna(close).where(observed, close)
    low = (panel.low * multiplier).fillna(close).where(observed, close)
    volume = (panel.volume / multiplier).where(observed, 0)
    values = {}
    for prefix, windows in (("na", (5, 20, 50, 150, 200)), ("ma", (50, 120, 150, 200))):
        values.update({f"{prefix}_{n}": close.rolling(n, min_periods=1).mean() for n in windows})
    values["macd"] = ema(close, 12, 12) - ema(close, 26, 26)
    values["macd_signal"] = ema(values["macd"], 9, 9)
    values["macd_hist"] = values["macd"] - values["macd_signal"]
    middle, std = close.rolling(20).mean(), close.rolling(20).std(ddof=0)
    upper, lower = middle + 2 * std, middle - 2 * std
    values.update(bb_middle=middle, bb_upper=upper, bb_lower=lower,
        bb_width_pct=(upper - lower) / middle * 100, bb_percent_b=(close - lower) / (upper - lower))
    money_location = ((2 * close - low - high) / (high - low).replace(0, np.nan)).fillna(0)
    accumulation = (money_location * volume.fillna(0)).cumsum()
    values["ati"] = ema(accumulation, 3, 3) - ema(accumulation, 10, 10)
    highest, lowest = high.rolling(14).max(), low.rolling(14).min()
    values["williams_r_14"] = -100 * (highest - close) / (highest - lowest).replace(0, np.nan)
    typical = (high + low + close) / 3
    money, change = typical * volume, typical.diff()
    inflow = money.where(change.gt(0), 0).rolling(14).sum()
    outflow = money.where(change.lt(0), 0).rolling(14).sum()
    values["mfi_14"] = (100 * inflow / (inflow + outflow)).where((inflow + outflow).ne(0), 50)
    return pd.DataFrame(values, index=panel.index)


def beta_series(panel, benchmark, *, start=None):
    rows = panel if start is None else panel.loc[panel.trade_date.ge(start)]
    weekly = rows.set_index("trade_date").split_adj_close.resample("W-FRI").last().dropna()
    stock = weekly.pct_change().rename("stock_return").rename_axis("week_end_date").reset_index()
    paired = stock.merge(benchmark[["week_end_date", "weekly_return"]]).dropna().sort_values("week_end_date")
    values = []
    for i in range(len(paired)):
        window = paired.iloc[max(0, i - 103):i + 1]
        if len(window) < 52:
            values.append(np.nan)
            continue
        x, y = window.weekly_return.to_numpy(), window.stock_return.to_numpy()
        x, y = x - x.mean(), y - y.mean()
        values.append(np.clip(.67 * np.dot(x, y) / np.dot(x, x) + .33, .2, 2.5) if np.dot(x, x) else np.nan)
    history = paired[["week_end_date"]].assign(beta=values).dropna()
    return pd.merge_asof(panel[["trade_date"]], history, left_on="trade_date", right_on="week_end_date").beta.fillna(1.)


def tax_rate(receipts, amounts, day, basis):
    known = sorted([r for r in receipts if pd.Timestamp(r["report_date"]) < day
        and (basis != "annual" or r["financial_basis"] == "annual")], key=lambda r: (r["report_date"], r["rcept_no"]))
    history = sorted({r["period_end_date"]: r for r in known}.values(), key=lambda r: r["period_end_date"])
    if not history:
        return .21
    scope = history[-1]["financial_scope"], history[-1]["accounting_regime"]
    sequence = {}
    for record in reversed(history):
        if (record["financial_scope"], record["accounting_regime"]) != scope:
            break
        i = record["fiscal_year"] if basis == "annual" else record["fiscal_year"] * 4 + record["fiscal_month"] // 3 - 1
        sequence[i] = record
    def raw(i, account):
        return amounts[sequence[i]["rcept_no"]].get(account, np.nan) if i in sequence else np.nan
    def quarter(i, account):
        return raw(i, account) if i % 4 == 0 else raw(i, account) - raw(i - 1, account)
    def value(account):
        i = max(sequence)
        if basis == "annual":
            return raw(i, account)
        if basis == "quarterly":
            return quarter(i, account)
        return sum(quarter(j, account) for j in range(i - 3, i + 1))
    tax, income = value("TAX_EXPENSE"), value("PBT")
    rate = tax / income if income else np.nan
    return rate if np.isfinite(rate) and 0 <= rate <= 1 else .21


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_market_input_audit")
    parser.add_argument("--preparation", type=Path, default=SILVER / "kr_full_factor_preparation_final")
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve prior audit evidence"
    args.output.mkdir(parents=True)
    dependencies = {}
    def pin(path):
        path = Path(path)
        dependencies[str(path)] = digest(path)
        return path
    pin(__file__)
    shutil.copy2(__file__, args.output / Path(__file__).name)
    preparation = args.preparation
    summary = json.loads(pin(preparation / "summary.json").read_text("utf-8"))
    differences = pd.read_parquet(pin(SILVER / "kr_full_factor_preparation_missing_fixed/native_reconciliation/differing_native.parquet"))
    ledger = json.loads(pin(ROOT / "data-lake/silver/corporate_actions/kr_stock_splits.json").read_text("utf-8"))
    wacc = ROOT / "data-lake/silver/wacc"
    risk_free = pd.read_csv(pin(wacc / "risk_free_rates.csv"))
    risk_free = risk_free.loc[risk_free.market.eq("kr")].assign(date=lambda d: pd.to_datetime(d.date)).sort_values("date")
    erp = pd.read_csv(pin(wacc / "country_equity_risk_premiums.csv"))
    # Every recorded ERP is outside the accepted range, so the declared model assumption applies.
    assert not erp.loc[erp.country_code.eq("KR"), "equity_risk_premium"].between(0, 25, inclusive="right").any()
    assumptions = pd.read_csv(pin(wacc / "wacc_assumptions.csv"))
    assumption = assumptions.loc[assumptions.market.eq("kr")].iloc[-1]
    assert assumption.default_beta == 1 and assumption.equity_risk_premium == 5 and assumption.credit_spread == 2
    benchmarks = pd.read_csv(pin(wacc / "benchmark_weekly_returns.csv"))
    benchmark = benchmarks.loc[benchmarks.market.eq("kr") & benchmarks.benchmark_id.eq("KOSPI200")].copy()
    benchmark["week_end_date"] = pd.to_datetime(benchmark.week_end_date)
    results, expected_frames, attributions = [], [], []
    for symbol in sorted({r["symbol"] for r in summary["results"]}):
        panel_path = ROOT / f"data-lake/silver/corporate_actions/prices/kr/kr_{symbol}.parquet"
        panel = pd.read_parquet(pin(panel_path)).sort_values("trade_date").reset_index(drop=True)
        pin(panel_path.with_suffix(".metadata.json"))
        panel["trade_date"] = pd.to_datetime(panel.trade_date)
        sid = f"SEC_KR_{symbol}"
        events = [e for e in ledger["events"] if e["security_id"] == sid and e["status"] == "confirmed"
            and pd.Timestamp(e["effective_date"]) <= panel.trade_date.max()]
        multipliers = np.ones(len(panel))
        unique = set()
        for event in events:
            assert event["effective_date_basis"] == "first_split_adjusted_trading_day" and not event["supersedes"]
            key = event["effective_date"], event["share_class"]
            assert key not in unique
            unique.add(key)
            multipliers[panel.trade_date.lt(event["effective_date"])] *= float(event["old_shares"]) / float(event["new_shares"])
            candidates = list((ROOT / f"data-lake/bronze/dart/stock_splits/disclosures/{symbol}").glob(f"*{event['source_id']}*.html"))
            matching = [p for p in candidates if digest(p) == event["source_sha256"]]
            assert len(matching) == 1, (symbol, event["source_id"], "official evidence missing")
            pin(matching[0])
        assert np.allclose(panel.split_adjustment_factor, multipliers, rtol=1e-12, atol=1e-12)
        assert np.allclose(panel.split_adj_close, panel.close * multipliers, rtol=1e-12, atol=1e-12)
        technical = price_factors(panel, multipliers)
        current_beta = beta_series(panel, benchmark)
        old_beta = beta_series(panel, benchmark, start="2010-01-01")
        rf = pd.merge_asof(panel[["trade_date"]], risk_free[["date", "risk_free_rate"]], left_on="trade_date", right_on="date").risk_free_rate.fillna(assumption.risk_free_rate)
        source_root = ROOT / "deliverables/cross_market_top70_20260909/survivorship/financial_normalization_pilot"
        folder = source_root / ("kr_receipt_history_v5_jtech" if symbol == "035480" else "kr_receipt_history_v7_review_input") / "reviewed" / symbol
        review = json.loads(pin(folder / "review.json").read_text("utf-8"))
        evidence = json.loads(pin(folder / "source_review.json").read_text("utf-8"))
        amounts = {r["receipt"]: {a["canonical_account_id"]: float(a["value"]) for a in r["accepted"]}
            for r in evidence["results"] if r["status"] == "reviewed"}
        accounts = set().union(*(set(v) for v in amounts.values()))
        assert not accounts & (DEBT_ACCOUNTS | INTEREST_ACCOUNTS), "Debt cost fallback must be re-reviewed"
        for receipt in review["receipts"]:
            for kind in ("source", "normalized"):
                path = pin(Path(review["source_root"]) / receipt[kind + "_path"])
                assert dependencies[str(path)] == receipt[kind + "_sha256"]
        for case in [r for r in summary["results"] if r["symbol"] == symbol]:
            basis = case["basis"]
            assert digest(panel_path) == case["price_sha256"]
            path = pin(preparation / symbol / basis / "prepared.parquet")
            assert dependencies[str(path)] == case["prepared_sha256"]
            actual = pd.read_parquet(path).loc[lambda d: d.factor_id.isin(FACTORS)]
            values = technical.copy().assign(beta=current_beta, cost_of_equity=rf + 5 * current_beta)
            taxes, cache = [], {}
            report_dates = sorted({r["report_date"] for r in review["receipts"]})
            for day in panel.trade_date:
                vintage = max((d for d in report_dates if pd.Timestamp(d) < day), default=None)
                if vintage not in cache:
                    cache[vintage] = tax_rate(review["receipts"], amounts, day, basis)
                taxes.append(cache[vintage])
            values["cost_of_debt_after_tax"] = (rf + 2) * (1 - np.array(taxes))
            values["trade_date"] = panel.trade_date
            values = values.loc[values.trade_date.between(case["start"], case["end"])]
            expected = values.melt(id_vars="trade_date", var_name="factor_id", value_name="factor_value")
            expected = expected.loc[np.isfinite(expected.factor_value)].assign(security_id=sid, financial_basis=basis)[KEYS + ["factor_value"]]
            a, e = canonical(actual), canonical(expected)
            common = a.index.intersection(e.index)
            bad = common[~np.isclose(a.loc[common, "factor_value"], e.loc[common, "factor_value"], rtol=1e-9, atol=1e-7)]
            output = args.output / symbol / basis
            output.mkdir(parents=True)
            expected.to_parquet(output / "expected.parquet", index=False)
            bad_values = a.loc[bad, ["factor_value"]].rename(columns={"factor_value": "prepared_value"})
            bad_values["independent_value"] = e.loc[bad, "factor_value"]
            bad_values.reset_index().to_parquet(output / "differences.parquet", index=False)
            result = dict(symbol=symbol, basis=basis, rows=len(actual), missing=len(e.index.difference(a.index)), extra=len(a.index.difference(e.index)), differing=len(bad),
                expected_sha256=digest(output / "expected.parquet"), prepared_sha256=digest(path), by_factor=bad_values.reset_index().groupby("factor_id").size().to_dict())
            result["status"] = "validated" if not (result["missing"] or result["extra"] or len(bad)) else "requires_review"
            results.append(result)
            expected_frames.append(expected)
            save(output / "validation.json", result)
        target = differences.loc[differences.security_id.eq(sid)].copy()
        attribution = panel[["trade_date"]].assign(beta_full=current_beta, beta_2010=old_beta,
            equity_full=rf + 5 * current_beta, equity_2010=rf + 5 * old_beta, debt_default_tax=(rf + 2) * .79)
        attributions.append(target.merge(attribution, on="trade_date", how="left"))
        print(symbol, [(r["basis"], r["status"], r["differing"]) for r in results if r["symbol"] == symbol], flush=True)
    expected_all = pd.concat(expected_frames, ignore_index=True)
    attributed = pd.concat(attributions, ignore_index=True).merge(expected_all.rename(columns={"factor_value": "independent_value"}), on=KEYS, how="left")
    attributed.to_parquet(args.output / "native_difference_audit.parquet", index=False)
    for path, checksum in dependencies.items():
        assert digest(path) == checksum, f"Input changed: {path}"
    report = dict(status="validated" if all(r["status"] == "validated" for r in results) else "requires_review",
        results=results, rows=sum(r["rows"] for r in results), dependencies=dependencies, factor_ids=FACTORS,
        tolerance=dict(rtol=1e-9, atol=1e-7), native_mutated=False, coverage_complete=False,
        assumptions="ERP 5%, credit spread 2%, default tax 21%, default beta 1 are declared model assumptions, not observations.",
        limitations="This checks calculations from the pinned reviewed sources; it does not establish completeness of all corporate actions or whole-market data.")
    save(args.output / "summary.json", report)
    print(report["status"], report["rows"], flush=True)


if __name__ == "__main__":
    main()
