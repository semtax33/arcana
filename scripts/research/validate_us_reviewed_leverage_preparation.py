"""Check daily leverage availability against retained canonical balances and dates.

This validates one dependency of the full prepared contract. It does not approve
complete borrowing/lease semantics or publish any native/PIT rows.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    assert output.is_relative_to((DATA_LAKE.root / "silver").resolve())
    output.mkdir(parents=True, exist_ok=False)
    source = args.preparation / "summary.json"
    prep = json.loads(source.read_bytes())
    assert prep["status"] == "prepared_not_independently_validated" and len(prep["cases"]) == 12
    pins = dict(prep["input_pins"])
    pins[str(source.resolve())] = sha256_file(source)
    pins[str(Path(__file__).resolve())] = sha256_file(__file__)
    metadata_path = DATA_LAKE.silver("sec", "us_report_metadata.csv")
    metadata = pd.read_csv(metadata_path, dtype={"stock_code": str})
    pins[str(metadata_path.resolve())] = sha256_file(metadata_path)
    report = dict(status="running", factor_id="debt_to_equity", cases=[], input_pins=pins,
        native_published=False, snapshots_published=False, complete_debt_source_semantics_approved=False)
    export_json(output / "summary.json", report)
    for case in prep["cases"]:
        symbol, basis = case["symbol"], case["basis"]
        facts_path = DATA_LAKE.silver("sec", "normalized", f"us_normalized_{symbol}.csv")
        pins[str(facts_path.resolve())] = sha256_file(facts_path)
        facts = pd.read_csv(facts_path)
        assert not facts.duplicated(["fiscal_year", "fiscal_month", "canonical_account_id"]).any()
        periods = {(int(y), int(m)): dict(zip(g.canonical_account_id, g.normalized_amount))
                   for (y, m), g in facts.groupby(["fiscal_year", "fiscal_month"])}
        records = metadata.loc[metadata.stock_code.eq(symbol) & metadata.source_type.eq("statement")].copy()
        if basis == "annual":
            records = records.loc[records.fiscal_month.eq(12)]
        assert not records.duplicated(["fiscal_year", "fiscal_month"]).any()
        records["report_date"] = pd.to_datetime(records.report_date)
        records["period_end_date"] = pd.to_datetime(records.period_end_date)
        panel = DATA_LAKE.silver("corporate_actions", "prices", "us", f"us_{symbol}.parquet")
        pins[str(panel.resolve())] = sha256_file(panel)
        days = pd.to_datetime(pd.read_parquet(panel, columns=["trade_date"]).trade_date)
        days = sorted(days.loc[days.between(case["start"], case["end"])])
        expected = []
        for day in days:
            known = records.loc[records.report_date.lt(day)]
            receipt, period, value = None, None, math.nan
            if not known.empty:
                current = known.sort_values(["period_end_date", "report_date", "rcept_no"]).iloc[-1]
                balances = periods.get((int(current.fiscal_year), int(current.fiscal_month)), {})
                long = balances.get("LONG_TERM_DEBT", math.nan)
                short = balances.get("SHORT_TERM_DEBT", math.nan)
                equity = balances.get("TOTAL_EQUITY", math.nan)
                if all(math.isfinite(v) for v in (long, short, equity)) and equity != 0:
                    value = (long + short) / equity
                receipt, period = current.rcept_no, current.period_end_date
            expected.append(dict(trade_date=day, expected=value, rcept_no=receipt, financial_period=period))
        wanted = pd.DataFrame(expected)
        prepared_path = Path(case["path"])
        assert sha256_file(prepared_path) == case["sha256"]
        pins[str(prepared_path)] = case["sha256"]
        frame = pd.read_parquet(prepared_path)
        actual = frame.loc[frame.factor_id.eq("debt_to_equity"), ["trade_date", "factor_value"]].copy()
        actual.trade_date = pd.to_datetime(actual.trade_date)
        assert not actual.trade_date.duplicated().any()
        observed = pd.merge_asof(wanted, actual.sort_values("trade_date"), on="trade_date", direction="backward")
        equal = np.isclose(observed.expected.to_numpy(float), observed.factor_value.to_numpy(float),
                           rtol=1e-12, atol=1e-12, equal_nan=True)
        folder = output / symbol / basis
        folder.mkdir(parents=True)
        observed.to_parquet(folder / "comparison.parquet", index=False)
        missing_dates, previous = [], None
        for row in wanted.itertuples():
            available = math.isfinite(row.expected)
            if not available and previous is not False:
                missing_dates.append(row.trade_date)
            previous = available
        expected_dates = set(wanted.loc[wanted.expected.notna(), "trade_date"]) | set(missing_dates)
        record = dict(symbol=symbol, basis=basis, price_dates=len(wanted), differing_values=int((~equal).sum()),
            expected_finite_days=int(wanted.expected.notna().sum()),
            missing_native_events=len(expected_dates - set(actual.trade_date)),
            extra_native_events=len(set(actual.trade_date) - expected_dates),
            comparison_sha256=sha256_file(folder / "comparison.parquet"))
        record["status"] = "validated" if not any(record[k] for k in
            ("differing_values", "missing_native_events", "extra_native_events")) else "requires_review"
        report["cases"].append(record)
        export_json(output / "summary.json", report)
        print(json.dumps(record), flush=True)
    for path, digest in pins.items():
        assert sha256_file(path) == digest, path
    report.update(status="validated_leverage_preparation_only" if all(r["status"] == "validated" for r in report["cases"])
                  else "requires_review", finished_at=datetime.now(timezone.utc).isoformat(),
                  scope="Full retained price days for four US securities and three bases; independent canonical balance arithmetic and next-day availability, one factor only.")
    shutil.copy2(__file__, output / Path(__file__).name)
    export_json(output / "summary.json", report)
    if report["status"] == "requires_review":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
