"""Capture bounded native before-images for the complete reviewed factor scope."""
import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.transformers.factors import preferred_factor_columns
from validate_kr_survivorship_financial_factors import SILVER, KEYS, digest, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, default=SILVER / "kr_full_factor_preparation")
    parser.add_argument("--output", type=Path, default=SILVER / "kr_full_factor_native_before")
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve earlier native snapshots"
    source = args.preparation / "summary.json"
    preparation = json.loads(source.read_text("utf-8"))
    assert len(preparation["results"]) == 36 and preparation["status"] == "prepared_not_independently_validated"
    bounds = {f"SEC_KR_{r['symbol']}": r["start"] for r in preparation["results"]}
    assert len(bounds) == 12
    conditions = []
    parameters = {"ids": preferred_factor_columns()}
    for index, (sid, start) in enumerate(sorted(bounds.items())):
        conditions.append(f"(security_id = {{sid{index}:String}} AND trade_date >= {{first{index}:Date}})")
        parameters[f"sid{index}"] = sid
        parameters[f"first{index}"] = date.fromisoformat(start)
    sql = """SELECT security_id, trade_date, financial_basis, factor_id,
        argMax(factor_value, updated_at) AS factor_value
        FROM fact_daily_factors
        WHERE trade_date >= {start:Date} AND trade_date < {end:Date}
          AND financial_basis IN ('annual', 'quarterly', 'ttm')
          AND factor_id IN {ids:Array(String)} AND (""" + " OR ".join(conditions) + """ )
        GROUP BY security_id, trade_date, financial_basis, factor_id
        SETTINGS max_execution_time=20, max_threads=2"""
    months = pd.period_range(min(bounds.values()), "2026-09", freq="M")
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
        source_sha256=digest(source), query_sha256=sha256(sql.encode()).hexdigest(), bounds=bounds,
        contract=parameters["ids"], months=[])
    save(args.output / "summary.json", report)
    (args.output / "query.sql").write_text(sql, encoding="utf-8")
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
    try:
        for month in months:
            frame = client.query_df(sql, parameters=dict(parameters, start=month.start_time.date(), end=(month+1).start_time.date()))
            if frame.empty and not len(frame.columns):
                frame = pd.DataFrame(columns=KEYS + ["factor_value"])
            path = args.output / f"{month}.parquet"
            frame.to_parquet(path, index=False)
            report["months"].append(dict(month=str(month), rows=len(frame), sha256=digest(path)))
            save(args.output / "summary.json", report)
            if len(report["months"]) % 12 == 1:
                print(month, "months", len(report["months"]), "rows", sum(r["rows"] for r in report["months"]), flush=True)
        report.update(status="finished", rows=sum(r["rows"] for r in report["months"]), finished_at=datetime.now(timezone.utc).isoformat())
        save(args.output / "summary.json", report)
        print(report["status"], "rows", report["rows"], flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
