"""Independently compare prepared anchors with the disclosed accession periods."""
import argparse
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE

FACTORS = {"at": "TOTAL_ASSETS", "seq": "TOTAL_EQUITY", "ni": "NET_INCOME",
           "sale": "REVENUE", "oancf": "CFO", "cogs": "COGS", "xrd": "RND",
           "debt_to_equity": None, "eps": None}


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), "utf-8")


def observations(financial_dir, symbol):
    path = financial_dir / "accessions" / symbol / "manifest.json"
    manifest = json.loads(path.read_bytes())
    source = path.parent / manifest["normalized_path"]
    assert digest(source) == manifest["normalized_sha256"]
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    output = []
    for (accession, published, year, month), group in frame.groupby(["accn", "filed", "fiscal_year", "fiscal_month"]):
        ends = set(group.loc[group.canonical_account_id.ne("COMMON_SHARES_OUTSTANDING"), "period_end"])
        if not ends:
            continue
        assert len(ends) == 1 and not group.canonical_account_id.duplicated().any()
        output.append(dict(accession=accession, published=pd.Timestamp(published),
            end=pd.Timestamp(next(iter(ends))), year=int(year), month=int(month),
            facts={r["canonical_account_id"]: r for r in group.to_dict("records")}))
    return sorted(output, key=lambda r: (r["published"], r["accession"]))


def amount(record, account):
    raw = record["facts"].get(account, {}).get("normalized_amount", "")
    return float(raw) if raw else math.nan


def quarter_flow(known, record, account):
    fact = record["facts"].get(account)
    if fact is None:
        return math.nan, True
    semantic = fact.get("period_semantic")
    value = amount(record, account)
    variants = json.loads(fact.get("reported_durations") or "[]")
    quarters = {(v["period_start"], v["period_end"], v["normalized_amount"]) for v in variants
                if v["period_end"] == str(record["end"].date()) and 60 <= v["duration_days"] <= 120}
    if quarters:
        return (next(iter(quarters))[2] if len(quarters) == 1 else math.nan), True
    if semantic == "QTD":
        return value, True
    if semantic not in {"YTD", "FY"}:
        return math.nan, False
    if record["month"] == 3:
        return value, True
    previous = known.get((record["year"], record["month"] - 3))
    if previous is None:
        return math.nan, True
    prior_fact = previous["facts"].get(account)
    if prior_fact is None:
        return math.nan, True
    if prior_fact.get("period_semantic") == "QTD" and previous["month"] != 3:
        # Reconstruct YTD from disclosed quarters only when every interval is
        # present; an isolated Q2/Q3 is not a prior-year cumulative amount.
        earlier = [known.get((record["year"], month)) for month in range(3, record["month"], 3)]
        if not all(earlier):
            return math.nan, True
        parts = [quarter_flow(known, earlier_record, account) for earlier_record in earlier]
        return value - sum(p[0] for p in parts), all(p[1] for p in parts)
    return value - amount(previous, account), True


def year_flow(record, account):
    fact = record["facts"].get(account)
    if fact is None:
        return math.nan, False
    observations = json.loads(fact.get("reported_durations") or "[]")
    years = {(v["period_start"], v["period_end"], v["normalized_amount"]) for v in observations
             if v["period_end"] == str(record["end"].date()) and 330 <= v["duration_days"] <= 400}
    if years:
        return (next(iter(years))[2] if len(years) == 1 else math.nan), True
    quarters = sorted({(pd.Timestamp(v["period_start"]), pd.Timestamp(v["period_end"]), v["normalized_amount"])
                       for v in observations if 60 <= v["duration_days"] <= 120
                       and pd.Timestamp(v["period_start"]) > record["end"] - pd.Timedelta(days=400)
                       and record["end"] - pd.Timedelta(days=400) < pd.Timestamp(v["period_end"]) <= record["end"]})
    if len(quarters) == 4 and quarters[-1][1] == record["end"]:
        contiguous = all(quarters[n][1] + pd.Timedelta(days=1) == quarters[n+1][0] for n in range(3))
        if contiguous and 330 <= (quarters[-1][1] - quarters[0][0]).days + 1 <= 400:
            return sum(row[2] for row in quarters), True
    return math.nan, False


def reported_eps(record, basis):
    has_durations = any(record["facts"].get(account, {}).get("reported_durations") for account in
                        ["BASIC_EPS", "DILUTED_EPS", "BASIC_SHARES", "DILUTED_SHARES"])
    for account in ["BASIC_EPS", "DILUTED_EPS"]:
        fact = record["facts"].get(account)
        if fact is None:
            continue
        if basis == "annual":
            if fact.get("period_semantic") in {"QTD", "YTD"}:
                continue
            value = amount(record, account)
            if math.isfinite(value):
                return value, True
        elif fact.get("reported_durations"):
            variants = json.loads(fact["reported_durations"])
            lower, upper = (60, 120) if basis == "quarterly" else (330, 400)
            selected = {(v["period_start"], v["period_end"], v["normalized_amount"]) for v in variants
                if lower <= v["duration_days"] <= upper
                and (basis == "quarterly" or fact["form"].startswith("10-K"))}
            if len(selected) == 1:
                return next(iter(selected))[2], True
    return math.nan, has_durations


def expected_values(known, record, basis):
    if record is None:
        return {factor: (math.nan, True) for factor in FACTORS}
    assets, equity = amount(record, "TOTAL_ASSETS"), amount(record, "TOTAL_EQUITY")
    if math.isfinite(assets) and assets != 0 and abs(equity) > abs(assets) * 2:
        equity = math.nan
    long, short = amount(record, "LONG_TERM_DEBT"), amount(record, "SHORT_TERM_DEBT")
    debt_ratio = (long + short) / equity if equity != 0 else math.nan
    values = dict(at=(assets, True), seq=(equity, True), debt_to_equity=(debt_ratio, True),
                  eps=reported_eps(record, basis))
    for factor in ["ni", "sale", "oancf", "cogs", "xrd"]:
        account = FACTORS[factor]
        if basis == "annual":
            fact = record["facts"].get(account, {})
            if fact.get("period_semantic") in {"QTD", "YTD"}:
                values[factor] = (year_flow(record, account)[0], True)
            else:
                values[factor] = (amount(record, account), True)
        elif basis == "quarterly":
            values[factor] = quarter_flow(known, record, account)
        else:
            full_year, reported = year_flow(record, account)
            if reported:
                values[factor] = (full_year, True)
                continue
            sequence = record["year"] * 4 + record["month"] // 3 - 1
            periods = [known.get((n // 4, (n % 4 + 1) * 3)) for n in range(sequence - 3, sequence + 1)]
            if not all(periods):
                values[factor] = (math.nan, True)
            else:
                parts = [quarter_flow(known, period, account) for period in periods]
                values[factor] = (sum(p[0] for p in parts), all(p[1] for p in parts))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    prep_path = args.preparation / "summary.json"
    prep = json.loads(prep_path.read_bytes())
    assert prep["status"] == "prepared_not_independently_validated" and len(prep["cases"]) == 12
    pins = {**prep["input_pins"], **prep["normalized_input_pins"], str(prep_path.resolve()): digest(prep_path),
            str(Path(__file__).resolve()): digest(__file__)}
    for path, sha in pins.items():
        assert digest(path) == sha, path
    report = dict(status="running", factors=list(FACTORS), cases=[], input_pins=pins,
                  native_published=False, snapshots_published=False, all_factor_semantics_approved=False)
    save(args.output / "summary.json", report)
    for case in prep["cases"]:
        symbol, basis = case["symbol"], case["basis"]
        records = observations(Path(prep["financial_dir"]), symbol)
        if basis == "annual":
            records = [r for r in records if r["month"] == 12]
        panel = DATA_LAKE.silver("corporate_actions", "prices", "us", f"us_{symbol}.parquet")
        days = sorted(pd.to_datetime(pd.read_parquet(panel, columns=["trade_date"]).trade_date))
        days = [day for day in days if pd.Timestamp(case["start"]) <= day <= pd.Timestamp(case["end"])]
        known, index, expected = {}, 0, []
        for day in days:
            while index < len(records) and records[index]["published"] < day:
                row = records[index]
                known[(row["year"], row["month"])] = row
                index += 1
            record = max(known.values(), key=lambda r: r["end"]) if known else None
            for factor, (value, testable) in expected_values(known, record, basis).items():
                expected.append(dict(trade_date=day, factor_id=factor, expected=value, testable=testable,
                    accession=record["accession"] if record else None, financial_period=record["end"] if record else None))
        prepared = Path(case["path"])
        assert digest(prepared) == case["sha256"]
        actual = pd.read_parquet(prepared, columns=["trade_date", "factor_id", "factor_value"])
        actual.trade_date = pd.to_datetime(actual.trade_date)
        compared = []
        expected = pd.DataFrame(expected)
        for factor in FACTORS:
            wanted = expected[expected.factor_id.eq(factor)].copy()
            observed = actual[actual.factor_id.eq(factor)].drop(columns="factor_id").sort_values("trade_date")
            assert not observed.trade_date.duplicated().any()
            joined = pd.merge_asof(wanted, observed, on="trade_date", direction="backward")
            equal = np.isclose(joined.expected.to_numpy(float), joined.factor_value.to_numpy(float),
                               rtol=1e-12, atol=1e-12, equal_nan=True)
            joined["differs"] = ~equal & joined.testable
            compared.append(joined)
        comparison = pd.concat(compared, ignore_index=True)
        folder = args.output / symbol / basis
        folder.mkdir(parents=True)
        comparison.to_parquet(folder / "comparison.parquet", index=False)
        comparison[comparison.differs].to_parquet(folder / "differences.parquet", index=False)
        record = dict(symbol=symbol, basis=basis, price_dates=len(days),
            testable_cells=int(comparison.testable.sum()), untestable_cells=int((~comparison.testable).sum()),
            differing_cells=int(comparison.differs.sum()),
            factors_with_differences=comparison[comparison.differs].groupby("factor_id").size().to_dict(),
            comparison_sha256=digest(folder / "comparison.parquet"))
        report["cases"].append(record)
        save(args.output / "summary.json", report)
        print(json.dumps(record), flush=True)
    for path, sha in pins.items():
        assert digest(path) == sha, path
    report["status"] = "requires_review" if any(r["differing_cells"] for r in report["cases"]) else "validated_reported_anchors_only"
    save(args.output / "summary.json", report)
    if report["status"] == "requires_review":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
