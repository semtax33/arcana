"""Read bounded monthly native factor snapshots before a reviewed publication."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from validate_kr_survivorship_financial_factors import FACTOR_IDS, save

SQL = """
SELECT trade_date, factor_id, financial_basis, security_id,
       argMax(factor_value, updated_at) AS factor_value
FROM fact_daily_factors
WHERE security_id IN {securities:Array(String)} AND financial_basis = 'annual'
  AND trade_date >= {start:Date} AND trade_date < {end:Date}
  AND factor_id IN {ids:Array(String)}
GROUP BY trade_date, factor_id, financial_basis, security_id
SETTINGS max_execution_time=20, max_threads=2
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-root", type=Path, default=DATA_LAKE.silver("survivorship", "financial_research", "kr_receipt_history_v7_review_input", "reviewed"))
    parser.add_argument("--output", type=Path, default=DATA_LAKE.silver("survivorship", "financial_research", "kr_v7_native_before"))
    args = parser.parse_args()
    assert not (args.output / "summary.json").exists(), "Preserve an existing native snapshot; use another --output"
    symbols = sorted(p.name for p in args.staging_root.iterdir() if (p / "staged_financial/history" / p.name / "manifest.json").exists())
    dates = []
    for symbol in symbols:
        frame = pd.read_parquet(DATA_LAKE.silver("corporate_actions", "prices", "kr", f"kr_{symbol}.parquet"), columns=["trade_date"])
        dates.extend(frame.loc[pd.to_datetime(frame.trade_date).ge("2017-01-01"), "trade_date"].tolist())
    assert symbols and dates
    months = pd.period_range(min(dates), max(dates), freq="M")
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(), symbols=symbols,
                  sql_sha256=sha256(SQL.encode()).hexdigest(), months=[])
    save(args.output / "summary.json", report)
    (args.output / "query.sql").write_text(SQL, encoding="utf-8")
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
    try:
        for month in months:
            parameters = {"start": month.start_time.date(), "end": (month+1).start_time.date(),
                          "securities": [f"SEC_KR_{s}" for s in symbols], "ids": FACTOR_IDS}
            frame = client.query_df(SQL, parameters=parameters)
            path = args.output / f"{month}.parquet"
            frame.to_parquet(path, index=False)
            report["months"].append(dict(month=str(month), rows=len(frame), sha256=sha256(path.read_bytes()).hexdigest(),
                                          captured_at=datetime.now(timezone.utc).isoformat()))
            save(args.output / "summary.json", report)
            if len(report["months"]) % 6 == 0 or len(report["months"]) == 1:
                print(month, "months", len(report["months"]), "rows", sum(r["rows"] for r in report["months"]), flush=True)
        report.update(status="finished", rows=sum(r["rows"] for r in report["months"]), finished_at=datetime.now(timezone.utc).isoformat())
        save(args.output / "summary.json", report)
        print(report["status"], "months", len(report["months"]), "rows", report["rows"], flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
