"""Rebuild audited market-factor snapshots and verify their as-of lineage.

Only the published 12-issuer/23-factor scope is changed. Native raw values and
snapshots are backed up monthly in silver; public progress is exported to gold.
Unexplained existing snapshot keys stop publication instead of being deleted.
"""
import argparse
from datetime import date, datetime, timezone
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
from publish_kr_survivorship_market_factors import capture, compare, metadata_difference, next_revision_time
from prepare_kr_survivorship_full_factors import input_inventory
from validate_kr_survivorship_financial_factors import SILVER, KEYS, canonical, digest, save

END = "2026-09-10"
COLUMNS = KEYS + ["factor_value", "source_trade_date", "fiscal_year", "financial_period", "currency", "updated_at"]


def latest_snapshots(frame):
    if frame.empty:
        return frame, pd.DataFrame(columns=KEYS)
    newest = frame.groupby(KEYS, dropna=False).updated_at.transform("max")
    rows = frame.loc[frame.updated_at.eq(newest)]
    fields = ["factor_value", "source_trade_date", "fiscal_year", "financial_period", "currency"]
    ambiguity = rows.groupby(KEYS, dropna=False)[fields].nunique(dropna=False).gt(1).any(axis=1)
    return rows.drop_duplicates(KEYS, keep="last"), ambiguity.loc[ambiguity].reset_index()[KEYS]


def capture_snapshots(client, bounds, ids, folder):
    folder.mkdir(parents=True)
    conditions, parameters = [], {"ids": ids, "last": date.fromisoformat(END)}
    for i, (sid, first) in enumerate(sorted(bounds.items())):
        conditions.append(f"(security_id={{sid{i}:String}} AND trade_date >= {{first{i}:Date}})")
        parameters[f"sid{i}"], parameters[f"first{i}"] = sid, date.fromisoformat(first)
    sql = "SELECT " + ", ".join(COLUMNS) + """ FROM fact_daily_factor_snapshot
        WHERE trade_date >= {start:Date} AND trade_date < {end:Date} AND trade_date <= {last:Date}
        AND financial_basis IN ('annual','quarterly','ttm') AND factor_id IN {ids:Array(String)}
        AND (""" + " OR ".join(conditions) + ") SETTINGS max_execution_time=20,max_threads=2"
    (folder / "query.sql").write_text(sql, "utf-8")
    frames, months = [], []
    for month in pd.period_range(min(bounds.values()), END, freq="M"):
        frame = client.query_df(sql, parameters=dict(parameters, start=month.start_time.date(), end=(month + 1).start_time.date()))
        if frame.empty and not len(frame.columns):
            frame = pd.DataFrame(columns=COLUMNS)
        file = folder / f"{month}.parquet"
        frame.to_parquet(file, index=False)
        frames.append(frame)
        months.append(dict(month=str(month), rows=len(frame), sha256=digest(file)))
        save(folder / "summary.json", dict(status="running", months=months))
        if len(months) % 24 == 1:
            print(folder.name, month, sum(m["rows"] for m in months), flush=True)
    rows = pd.concat(frames, ignore_index=True)
    latest, ambiguous = latest_snapshots(rows)
    save(folder / "summary.json", dict(status="finished", months=months, physical_rows=len(rows), latest_rows=len(latest), ambiguous_keys=len(ambiguous)))
    ambiguous.to_parquet(folder / "ambiguous_keys.parquet", index=False)
    return latest, ambiguous


def market_calendar(client, start, folder):
    sql = """SELECT DISTINCT trade_date FROM price_daily
        WHERE startsWith(security_id,'SEC_KR_') AND trade_date >= {start:Date}
          AND trade_date < {end:Date} AND trade_date <= {last:Date}
        ORDER BY trade_date SETTINGS max_execution_time=20,max_threads=2"""
    folder.mkdir()
    (folder / "query.sql").write_text(sql, "utf-8")
    frames, records = [], []
    for year in range(pd.Timestamp(start).year, 2027):
        frame = client.query_df(sql, parameters=dict(start=max(date(year, 1, 1), date.fromisoformat(start)),
            end=date(year + 1, 1, 1), last=date.fromisoformat(END)))
        path = folder / f"{year}.parquet"
        frame.to_parquet(path, index=False)
        frames.append(frame)
        records.append(dict(year=year, rows=len(frame), sha256=digest(path)))
    save(folder / "summary.json", dict(status="finished", years=records))
    return pd.DatetimeIndex(pd.to_datetime(pd.concat(frames, ignore_index=True).trade_date).unique()).sort_values()


def expected_asof(source, dates):
    frames = []
    source = source.copy()
    source["trade_date"] = pd.to_datetime(source.trade_date).astype("datetime64[ns]")
    dates = dates.astype("datetime64[ns]")
    for key, rows in source.groupby(["security_id", "financial_basis", "factor_id"], sort=False):
        rows = rows.sort_values("trade_date").rename(columns={"trade_date": "source_trade_date"})
        applicable = dates[dates >= rows.source_trade_date.min()]
        joined = pd.merge_asof(pd.DataFrame({"trade_date": applicable}), rows, left_on="trade_date", right_on="source_trade_date")
        assert pd.to_datetime(joined.source_trade_date).le(joined.trade_date).all()
        assert np.isfinite(joined.factor_value).all()
        frames.append(joined[COLUMNS])
    return pd.concat(frames, ignore_index=True)


def compare_snapshots(actual, expected):
    compare(actual, expected)
    a, e = canonical(actual), canonical(expected)
    assert pd.to_datetime(a.source_trade_date).eq(pd.to_datetime(e.source_trade_date)).all(), "Snapshot source trade date mismatch"


def snapshot_delta(existing, expected, ambiguous):
    a, e = canonical(existing), canonical(expected)
    extra = a.index.difference(e.index)
    common = a.index.intersection(e.index)
    numeric = ~np.isclose(a.loc[common, "factor_value"].to_numpy(dtype=float, na_value=np.nan),
        e.loc[common, "factor_value"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12)
    metadata = metadata_difference(a.loc[common], e.loc[common]).to_numpy()
    source_date = ~pd.to_datetime(a.loc[common, "source_trade_date"]).eq(pd.to_datetime(e.loc[common, "source_trade_date"])).to_numpy()
    changed = common[numeric | metadata | source_date]
    changed = changed.union(canonical(ambiguous).index.intersection(common))
    delta = e.loc[e.index.difference(a.index).union(changed)].reset_index()[COLUMNS]
    revisions = a.loc[changed].reset_index()
    return delta, revisions, a.loc[extra].reset_index(), dict(new_keys=len(e.index.difference(a.index)),
        numeric_revisions=int(numeric.sum()), metadata_revisions=int(metadata.sum()), source_date_revisions=int(source_date.sum()),
        revised_keys=len(changed), ambiguous_keys=len(ambiguous), unsupported_keys=len(extra))


def clickhouse_rows(frame):
    frame = frame.copy()
    for col in ("trade_date", "source_trade_date", "financial_period"):
        value = pd.to_datetime(frame[col]).dt.date.astype(object)
        frame[col] = value.where(pd.notna(value), None)
    year = pd.to_numeric(frame.fiscal_year).astype("Int64")
    frame["fiscal_year"] = year.astype(object).where(year.notna(), None)
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-publication", type=Path, required=True)
    parser.add_argument("--publish-snapshots", action="store_true")
    parser.add_argument("--output", type=Path, default=SILVER / "kr_market_snapshot_publication")
    args = parser.parse_args()
    pub = json.loads(args.native_publication.read_text("utf-8"))
    assert pub["native_published"] and pub["verified_rows"] == 1366272
    prepared_path = args.native_publication.parent / "prepared.parquet"
    assert digest(prepared_path) == pub["prepared_sha256"]
    prepared = pd.read_parquet(prepared_path)
    inventory = json.loads((Path(pub["preparation"]) / "summary.json").read_text("utf-8"))["input_inventory"]
    dependencies = {str(args.native_publication): digest(args.native_publication), str(prepared_path): digest(prepared_path),
        str(Path(__file__)): digest(__file__), str(ROOT / "engine/loaders/factor_snapshots.py"): digest(ROOT / "engine/loaders/factor_snapshots.py")}
    def verify():
        for p, checksum in dependencies.items():
            assert digest(p) == checksum, f"Snapshot input changed: {p}"
        assert input_inventory(pub["symbols"]) == inventory, "Reviewed source inputs changed"
    attempt = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    report = dict(status="starting", snapshots_published=False, coverage_complete=False, native_publication=str(args.native_publication),
        native_publication_sha256=digest(args.native_publication), dependencies=dependencies, bounds=pub["bounds"], factor_ids=pub["factor_ids"],
        date_policy="Union of native KR price-calendar dates and existing snapshot dates through the cutoff; listing eligibility is applied by consumers.", cutoff=END)
    save(attempt / "publication.json", report)
    implementation = attempt / "implementation"
    implementation.mkdir()
    for p in (Path(__file__), ROOT / "engine/loaders/factor_snapshots.py", ROOT / "scripts/research/publish_kr_survivorship_market_factors.py"):
        shutil.copy2(p, implementation / p.name)
    database = "arcana_snapshot_stage_" + uuid4().hex
    with SourceRefreshLock("kr"):
        verify()
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        created = False
        try:
            source = capture(client, pub["bounds"], pub["factor_ids"], attempt / "native_source")
            compare(source, prepared)
            before, ambiguity = capture_snapshots(client, pub["bounds"], pub["factor_ids"], attempt / "snapshot_before")
            calendar = market_calendar(client, min(pub["bounds"].values()), attempt / "market_calendar")
            existing_dates = pd.DatetimeIndex(pd.to_datetime(before.trade_date).unique())
            dates = calendar.union(existing_dates).sort_values()
            pd.DataFrame({"trade_date": dates, "market_price_date": dates.isin(calendar)}).to_parquet(attempt / "snapshot_dates.parquet", index=False)
            expected = expected_asof(source, dates)
            expected.to_parquet(attempt / "expected_asof.parquet", index=False)
            report.update(status="asof_prepared", expected_rows=len(expected), date_count=len(dates), existing_dates_outside_market_calendar=len(existing_dates.difference(calendar)),
                expected_sha256=digest(attempt / "expected_asof.parquet"))
            save(attempt / "publication.json", report)
            delta, revisions, extra, counts = snapshot_delta(before, expected, ambiguity)
            extra.to_parquet(attempt / "unsupported_existing.parquet", index=False)
            if not extra.empty:
                report.update(status="existing_snapshot_keys_require_review", delta=counts)
                save(attempt / "publication.json", report)
                print(report["status"], counts, flush=True)
                return
            client.command(f"CREATE DATABASE {database}")
            created = True
            client.command(f"""CREATE TABLE {database}.source (
                security_id String,trade_date Date,financial_basis String,factor_id String,factor_value Float64,
                fiscal_year Nullable(UInt16),financial_period Nullable(Date),currency String,updated_at DateTime64(3,'Asia/Seoul'))
                ENGINE=MergeTree ORDER BY (security_id,financial_basis,factor_id,trade_date)""")
            source_insert = source.copy()
            for col in ("trade_date", "financial_period"):
                value = pd.to_datetime(source_insert[col]).dt.date.astype(object)
                source_insert[col] = value.where(pd.notna(value), None)
            year = pd.to_numeric(source_insert.fiscal_year).astype("Int64")
            source_insert["fiscal_year"] = year.astype(object).where(year.notna(), None)
            client.insert_df(f"{database}.source", source_insert)
            ensure_factor_snapshot_table(client, snapshot_table=f"{database}.snapshots")
            validations = []
            for month in pd.period_range(dates.min(), dates.max(), freq="M"):
                days = dates[(dates >= month.start_time) & (dates < (month + 1).start_time)]
                if not len(days):
                    continue
                query, parameters = build_factor_snapshot_insert_query(market="kr", security_ids=sorted(pub["bounds"]),
                    factor_ids=pub["factor_ids"], snapshot_dates=[d.date() for d in days],
                    source_table=f"{database}.source", snapshot_table=f"{database}.snapshots")
                client.command(query, parameters=parameters, settings={"max_threads": 2, "max_execution_time": 25})
                actual = client.query_df(f"SELECT * FROM {database}.snapshots WHERE trade_date >= {{start:Date}} AND trade_date < {{end:Date}}",
                    parameters=dict(start=month.start_time.date(), end=(month + 1).start_time.date()))
                wanted = expected.loc[pd.to_datetime(expected.trade_date).isin(days)]
                compare_snapshots(actual, wanted)
                validations.append(dict(month=str(month), rows=len(actual), status="validated"))
                save(attempt / "sql_validation.json", dict(status="running", months=validations))
                if len(validations) % 24 == 1:
                    print("SQL validated", month, sum(r["rows"] for r in validations), flush=True)
            assert sum(r["rows"] for r in validations) == len(expected)
            save(attempt / "sql_validation.json", dict(status="validated", months=validations, rows=len(expected)))
            wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
            version = next_revision_time(before, revisions, wall_clock)
            delta["updated_at"] = version
            delta = clickhouse_rows(delta)
            delta.to_parquet(attempt / "insert_delta.parquet", index=False)
            revisions.to_parquet(attempt / "revised_before.parquet", index=False)
            verify()
            report.update(status="validated_for_publication", delta=counts, planned_inserts=len(delta),
                revision_version=version.isoformat(), wall_clock=wall_clock.isoformat(), insert_delta_sha256=digest(attempt / "insert_delta.parquet"))
            save(attempt / "publication.json", report)
            if not args.publish_snapshots:
                return
            inserted = 0
            for month, part in delta.groupby(pd.to_datetime(delta.trade_date).dt.to_period("M"), sort=True):
                client.insert_df("fact_daily_factor_snapshot", part, column_names=COLUMNS)
                inserted += len(part)
                if month.month == 1:
                    print("snapshot inserted", month, inserted, flush=True)
            report.update(status="readback_pending", inserted_rows=inserted)
            save(attempt / "publication.json", report)
            after, ambiguous_after = capture_snapshots(client, pub["bounds"], pub["factor_ids"], attempt / "snapshot_after")
            assert ambiguous_after.empty
            compare_snapshots(after, expected)
            verify()
            report.update(status="published_and_verified", snapshots_published=True, verified_rows=len(after),
                finished_at=datetime.now(timezone.utc).isoformat())
            save(attempt / "publication.json", report)
            save(args.output / "latest.json", dict(publication_path=str(attempt / "publication.json"), sha256=digest(attempt / "publication.json")))
            export_json(DATA_LAKE.gold("survivorship", "kr", "market_factors", "20260910", "snapshot_summary.json"), report)
            print(report["status"], "inserted", inserted, "verified", len(after), flush=True)
        finally:
            if created:
                client.command(f"DROP DATABASE {database}")
            client.close()


if __name__ == "__main__":
    main()
