"""Materialize audited nullable capital-factor snapshots and verify all dates."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock
from engine.loaders.factor_snapshots import build_factor_snapshot_insert_query, ensure_factor_snapshot_table
from prepare_kr_survivorship_full_factors import input_inventory
from publish_kr_survivorship_capital_factors import capture_rows, compare_nullable, plan_delta, clickhouse_rows, COLUMNS, END
from publish_kr_survivorship_market_factors import next_revision_time
from publish_kr_survivorship_market_snapshots import market_calendar
from validate_kr_survivorship_financial_factors import SILVER, digest, save

SNAPSHOT_COLUMNS = COLUMNS + ["source_trade_date"]


def expected_asof(source, dates):
    frames = []
    source = source.copy()
    source["trade_date"] = pd.to_datetime(source.trade_date).astype("datetime64[ns]")
    dates = dates.astype("datetime64[ns]")
    for _, rows in source.groupby(["security_id", "financial_basis", "factor_id"], sort=False):
        rows = rows.sort_values("trade_date").rename(columns={"trade_date": "source_trade_date"})
        applicable = dates[dates >= rows.source_trade_date.min()]
        joined = pd.merge_asof(pd.DataFrame({"trade_date": applicable}), rows,
            left_on="trade_date", right_on="source_trade_date", direction="backward")
        assert pd.to_datetime(joined.source_trade_date).le(joined.trade_date).all()
        assert np.isfinite(joined.factor_value.dropna()).all()
        frames.append(joined[SNAPSHOT_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_capital_snapshot_publication")
    parser.add_argument("--publish-snapshots", action="store_true")
    args = parser.parse_args()
    native = json.loads(args.native_publication.read_text("utf-8"))
    assert native["native_published"] and native["verified_rows"] == 32635
    prepared_path = args.native_publication.parent / "prepared.parquet"
    assert digest(prepared_path) == native["prepared_sha256"]
    prepared = pd.read_parquet(prepared_path)
    dependencies = dict(native["dependencies"])
    dependencies[str(args.native_publication)] = digest(args.native_publication)
    dependencies[str(prepared_path)] = digest(prepared_path)
    for path in (Path(__file__), ROOT / "engine/loaders/factor_snapshots.py",
                 ROOT / "scripts/research/publish_kr_survivorship_market_snapshots.py"):
        dependencies[str(path)] = digest(path)
    def verify():
        for path, checksum in dependencies.items():
            assert digest(path) == checksum, f"Snapshot dependency changed: {path}"
        assert input_inventory(native["symbols"]) == native["input_inventory"]
    verify()
    attempt = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    report = dict(status="starting", native_publication=str(args.native_publication),
        native_publication_sha256=digest(args.native_publication), dependencies=dependencies,
        bounds=native["bounds"], factor_ids=native["factor_ids"], cutoff=END,
        snapshots_published=False, coverage_complete=False,
        date_policy="KR native price calendar union existing snapshot dates; latest missing source event remains missing until a finite recovery. Listing eligibility is applied by consumers.")
    save(attempt / "publication.json", report)
    shutil.copy2(__file__, attempt / Path(__file__).name)
    database = "arcana_capital_snapshot_stage_" + uuid4().hex
    created = False
    with SourceRefreshLock("kr"):
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        try:
            verify()
            source, source_ambiguity = capture_rows(client, native["bounds"], native["factor_ids"], attempt / "native_source")
            assert source_ambiguity.empty
            compare_nullable(source, prepared)
            before, ambiguity = capture_rows(client, native["bounds"], native["factor_ids"], attempt / "snapshot_before", snapshot=True)
            calendar = market_calendar(client, min(native["bounds"].values()), attempt / "market_calendar")
            existing_dates = pd.DatetimeIndex(pd.to_datetime(before.trade_date).unique())
            dates = calendar.union(existing_dates).sort_values()
            pd.DataFrame({"trade_date": dates, "market_price_date": dates.isin(calendar)}).to_parquet(attempt / "snapshot_dates.parquet", index=False)
            expected = expected_asof(source, dates)
            expected.to_parquet(attempt / "expected_asof.parquet", index=False)
            delta, revisions, extra, counts = plan_delta(before, expected, ambiguity, snapshot=True)
            extra.to_parquet(attempt / "unsupported_existing.parquet", index=False)
            report.update(status="asof_prepared", expected_rows=len(expected), expected_finite_cells=int(expected.factor_value.notna().sum()),
                expected_missing_cells=int(expected.factor_value.isna().sum()), date_count=len(dates), delta=counts,
                existing_dates_outside_market_calendar=len(existing_dates.difference(calendar)),
                expected_sha256=digest(attempt / "expected_asof.parquet"))
            save(attempt / "publication.json", report)
            assert extra.empty, "Existing snapshot keys outside audited as-of scope require review"
            client.command(f"CREATE DATABASE {database}")
            created = True
            client.command(f"""CREATE TABLE {database}.source (
                security_id String, trade_date Date, financial_basis String, factor_id String,
                factor_value Nullable(Float64), fiscal_year Nullable(UInt16), financial_period Nullable(Date),
                currency String, updated_at DateTime64(3,'Asia/Seoul')) ENGINE=Memory""")
            client.insert_df(f"{database}.source", clickhouse_rows(source), column_names=COLUMNS)
            ensure_factor_snapshot_table(client, snapshot_table=f"{database}.snapshots")
            checks = []
            for month in pd.period_range(dates.min(), dates.max(), freq="M"):
                days = dates[(dates >= month.start_time) & (dates < (month+1).start_time)]
                if not len(days):
                    continue
                query, params = build_factor_snapshot_insert_query(market="kr", security_ids=sorted(native["bounds"]),
                    factor_ids=native["factor_ids"], snapshot_dates=[d.date() for d in days],
                    source_table=f"{database}.source", snapshot_table=f"{database}.snapshots")
                client.command(query, parameters=params, settings=dict(max_execution_time=25, max_threads=2))
                actual = client.query_df(f"SELECT * FROM {database}.snapshots WHERE trade_date >= {{start:Date}} AND trade_date < {{end:Date}}",
                    parameters=dict(start=month.start_time.date(), end=(month+1).start_time.date()))
                wanted = expected.loc[pd.to_datetime(expected.trade_date).isin(days)]
                compare_nullable(actual, wanted, snapshot=True)
                checks.append(dict(month=str(month), rows=len(actual), finite_cells=int(actual.factor_value.notna().sum()), status="validated"))
                save(attempt / "sql_validation.json", dict(status="running", months=checks))
                if len(checks) % 24 == 1:
                    print("SQL validated", month, sum(r["rows"] for r in checks), flush=True)
            assert sum(r["rows"] for r in checks) == len(expected)
            save(attempt / "sql_validation.json", dict(status="validated", months=checks, rows=len(expected)))
            wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
            version = next_revision_time(before, revisions, wall_clock)
            delta["updated_at"] = version
            delta = clickhouse_rows(delta)
            delta.to_parquet(attempt / "insert_delta.parquet", index=False)
            revisions.to_parquet(attempt / "revised_before.parquet", index=False)
            verify()
            report.update(status="staged_and_verified", planned_inserts=len(delta),
                revision_version=version.isoformat(), publication_wall_clock=wall_clock.isoformat(),
                insert_delta_sha256=digest(attempt / "insert_delta.parquet"))
            save(attempt / "publication.json", report)
            if not args.publish_snapshots:
                return
            report.update(status="publication_started", inserted_rows=0)
            save(attempt / "publication.json", report)
            for month, part in delta.groupby(pd.to_datetime(delta.trade_date).dt.to_period("M"), sort=True):
                client.insert_df("fact_daily_factor_snapshot", part, column_names=SNAPSHOT_COLUMNS)
                report["inserted_rows"] += len(part)
                save(attempt / "publication.json", report)
            report["status"] = "readback_pending"
            save(attempt / "publication.json", report)
            after, after_ambiguity = capture_rows(client, native["bounds"], native["factor_ids"], attempt / "snapshot_after", snapshot=True)
            assert after_ambiguity.empty
            compare_nullable(after, expected, snapshot=True)
            verify()
            report.update(status="published_and_verified", snapshots_published=True, verified_rows=len(after),
                finished_at=datetime.now(timezone.utc).isoformat())
            save(attempt / "publication.json", report)
            save(args.output / "latest.json", dict(publication_path=str(attempt / "publication.json"), sha256=digest(attempt / "publication.json")))
            export_json(DATA_LAKE.gold("survivorship", "kr", "capital_factors", "20260910", "snapshot_summary.json"), report)
            print(report["status"], "inserted", report["inserted_rows"], "verified", len(after), flush=True)
        finally:
            if created:
                client.command(f"DROP DATABASE {database}")
            client.close()


if __name__ == "__main__":
    main()
