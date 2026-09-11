"""Rebuild snapshots influenced by an audited native-observation scope.

Select added keys, preserved existing keys, or the complete audited scope. Each observation applies
until the next native event, including a NULL
event. Events in intervening unaudited months and after the final audited date
close the preceding influence interval without being republished themselves.
Calendar caches do not grant listing eligibility or imply tradable prices.
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
from engine.core.source_storage import SourceRefreshLock, sha256_file
from engine.loaders.factor_snapshots import build_factor_snapshot_insert_query, ensure_factor_snapshot_table
from publish_kr_historical_capitalization_factors import SQL as NATIVE_SQL, capture, compare_complete
from publish_kr_survivorship_capital_factors import COLUMNS, KEYS, latest_nullable, compare_nullable, plan_delta, clickhouse_rows
from publish_kr_survivorship_market_factors import next_revision_time
from publish_kr_survivorship_market_snapshots import market_calendar
from validate_kr_survivorship_financial_factors import canonical

END = "2026-09-10"
GROUPS = ["security_id", "financial_basis", "factor_id"]
SNAPSHOT_COLUMNS = COLUMNS + ["source_trade_date"]
BOUNDARY_SQL = """SELECT security_id, financial_basis, factor_id, min(trade_date) AS stop_date
FROM fact_daily_factors
WHERE trade_date >= {start:Date} AND trade_date < {end:Date} AND trade_date <= {cutoff:Date}
AND startsWith(security_id,'SEC_KR_')
AND factor_id IN ('mcap_mil','csho') AND financial_basis IN ('annual','quarterly','ttm')
GROUP BY security_id, financial_basis, factor_id
SETTINGS max_execution_time=30,max_threads=2"""
SNAPSHOT_SQL = "SELECT " + ",".join(SNAPSHOT_COLUMNS) + """ FROM fact_daily_factor_snapshot
WHERE trade_date >= {start:Date} AND trade_date < {end:Date} AND trade_date <= {cutoff:Date}
AND security_id IN {sids:Array(String)}
AND factor_id IN ('mcap_mil','csho') AND financial_basis IN ('annual','quarterly','ttm')
SETTINGS max_execution_time=30,max_threads=2"""


def normalize(frame):
    frame = frame.copy()
    for field in ("trade_date", "source_trade_date", "stop_date"):
        if field in frame:
            frame[field] = pd.to_datetime(frame[field]).astype("datetime64[ns]")
    for field in GROUPS:
        if field in frame:
            frame[field] = frame[field].astype(object)
    return frame


def first_subsequent_events(client, folder, security_ids=None, *, start="2016-01"):
    folder.mkdir()
    query = BOUNDARY_SQL
    (folder / "query.sql").write_text(query, "utf-8")
    frames, records = [], []
    for month in pd.period_range(start, END, freq="M"):
        parameters = dict(start=max(month.start_time.date(), pd.Timestamp(start).date()),
            end=(month + 1).start_time.date(), cutoff=date.fromisoformat(END))
        rows = client.query_df(query, parameters=parameters)
        if rows.empty and not len(rows.columns):
            rows = pd.DataFrame(columns=GROUPS + ["stop_date"])
        rows = normalize(rows)
        market_rows = len(rows)
        if security_ids is not None:
            rows.to_parquet(folder / f"{month}_market.parquet", index=False)
            rows = rows.loc[rows.security_id.isin(security_ids)].copy()
        path = folder / f"{month}.parquet"
        rows.to_parquet(path, index=False)
        frames.append(rows)
        records.append(dict(month=str(month), market_rows=market_rows, rows=len(rows), sha256=sha256_file(path)))
        export_json(folder / "summary.json", dict(status="running", months=records))
        if month.month in (1, 7):
            print("subsequent event boundaries", str(month), len(rows), flush=True)
    result = pd.concat(frames, ignore_index=True).groupby(GROUPS, as_index=False).stop_date.min() if frames else normalize(
        pd.DataFrame({**{key:pd.Series(dtype=object) for key in GROUPS}, "stop_date":pd.Series(dtype="datetime64[ns]")}))
    result.to_parquet(folder / "first_subsequent.parquet", index=False)
    export_json(folder / "summary.json", dict(status="verified", months=records,
        query_period="month", security_ids=security_ids, start=start,
        policy="First observed native event closes influence, even when its value is NULL. No settlement or quote is inferred.",
        first_subsequent_sha256=sha256_file(folder / "first_subsequent.parquet")))
    return result


def affected_asof(source, dates, boundaries):
    """Independent calendar join restricted to the selected source-key scope."""
    source = normalize(source)
    entities = source[GROUPS].drop_duplicates()
    grid = pd.DataFrame({"trade_date": dates}).merge(entities, how="cross")
    right = source.rename(columns={"trade_date": "source_trade_date"})
    expected = pd.merge_asof(grid.sort_values("trade_date"), right.sort_values("source_trade_date"),
        left_on="trade_date", right_on="source_trade_date", by=GROUPS, direction="backward")
    expected = expected.loc[expected.selected_source.eq(True)].copy()
    expected = expected.merge(boundaries, on=GROUPS, how="left", validate="many_to_one")
    expected = expected.loc[expected.stop_date.isna() | expected.trade_date.lt(expected.stop_date)]
    assert expected.source_trade_date.le(expected.trade_date).all()
    assert np.isfinite(expected.factor_value).all()
    return expected[SNAPSHOT_COLUMNS]


def native_capture(client, month, path, security_ids):
    if security_ids is None:
        return normalize(capture(client, month, path, end_date=END))
    query = NATIVE_SQL.replace("AND startsWith(security_id,'SEC_KR_')", "AND security_id IN {sids:Array(String)}")
    rows = client.query_df(query, parameters=dict(start=month.start_time.date(),
        end=min((month + 1).start_time.date(), (pd.Timestamp(END) + pd.Timedelta(days=1)).date()), sids=security_ids))
    if rows.empty and not len(rows.columns):
        rows = pd.DataFrame(columns=COLUMNS)
    rows.to_parquet(path, index=False)
    path.with_suffix(".sql").write_text(query, "utf-8")
    latest, ambiguous = latest_nullable(rows)
    if not ambiguous.empty:
        raise ValueError("Native latest version is ambiguous")
    return normalize(latest)


def snapshot_capture(client, month, sids, path):
    rows = client.query_df(SNAPSHOT_SQL, parameters=dict(start=month.start_time.date(),
        end=(month + 1).start_time.date(), cutoff=date.fromisoformat(END), sids=sids))
    if rows.empty and not len(rows.columns):
        rows = pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    rows = normalize(rows)
    rows.to_parquet(path, index=False)
    latest, ambiguity = latest_nullable(rows, snapshot=True)
    if not ambiguity.empty:
        ambiguity.to_parquet(path.with_name(path.stem + "_ambiguities.parquet"), index=False)
        raise ValueError("Existing latest snapshot has ambiguous values or lineage")
    return latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-snapshots", action="store_true")
    parser.add_argument("--source-scope", choices=("added", "existing", "all"), default="added")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Audits and backups must stay in silver")
    native = json.loads(args.native_publication.read_text("utf-8"))
    assert native["native_published"] and native["factor_ids"] == ["mcap_mil", "csho"]
    years = sorted(native["source_years"])
    if not years or len(years) != len(set(years)):
        raise ValueError("Snapshot preparation requires unique audited source years")
    source_cutoff = pd.Timestamp(native.get("end_date") or f"{years[-1]}-12-31")
    if source_cutoff.year != years[-1] or source_cutoff > pd.Timestamp(END):
        raise ValueError("Native source cutoff is outside the audited scope")
    if [row["month"] for row in native["months"]] != [str(month) for year in years
            for month in pd.period_range(f"{year}-01", f"{year}-12", freq="M") if month.start_time <= source_cutoff]:
        raise ValueError("Complete monthly native publication is required")
    source_start = f"{years[0]}-01-01"
    subsequent_start = (source_cutoff + pd.Timedelta(days=1)).date().isoformat()
    scope_name = "_".join(map(str, years))
    if len(years) > 1 and years == list(range(years[0], years[-1] + 1)):
        scope_name = f"{years[0]}_{years[-1]}"
    args.output.mkdir(parents=True, exist_ok=False)
    scoped_ids = None
    if args.source_scope in {"existing", "all"}:
        identities = set()
        for record in native["months"]:
            path = args.native_publication.parent / record["month"] / ("native_before.parquet" if args.source_scope == "existing" else "expected.parquet")
            if sha256_file(path) != record["hashes"][path.name]:
                raise ValueError("Existing native evidence changed")
            identities.update(pd.read_parquet(path, columns=["security_id"]).security_id)
        scoped_ids = sorted(identities)
        assert scoped_ids, "No audited native source keys to repair"
    dependencies = dict(native["dependencies"])
    dependencies[str(args.native_publication.resolve())] = sha256_file(args.native_publication)
    for path in (Path(__file__), ROOT / "engine/loaders/factor_snapshots.py",
                 ROOT / "scripts/research/publish_kr_survivorship_market_snapshots.py",
                 ROOT / "scripts/research/publish_kr_survivorship_market_factors.py"):
        dependencies[str(path)] = sha256_file(path)
    def verify_dependencies():
        for path, checksum in dependencies.items():
            dependency = ROOT / path
            if (None if not dependency.exists() else sha256_file(dependency)) != checksum:
                raise ValueError(f"Snapshot dependency changed: {Path(path).name}")
    verify_dependencies()
    implementation = args.output / "implementation"
    implementation.mkdir()
    for path in dependencies:
        if Path(path).suffix == ".py":
            shutil.copy2(ROOT / path, implementation / Path(path).name)
    records = []
    report = dict(status="starting", snapshots_published=False, coverage_complete=False,
        native_publication_path=str(args.native_publication.resolve()),
        native_publication_sha256=sha256_file(args.native_publication), dependencies=dependencies,
        cutoff=END, months=records, inserted_rows=0, verified_rows=0, new_keys=0, revised_keys=0,
        source_scope=args.source_scope, scoped_security_ids=scoped_ids,
        source_years=years, source_start=source_start, source_cutoff=source_cutoff.date().isoformat(), subsequent_start=subsequent_start,
        scope=f"Only calendar snapshots whose latest native event belongs to the {args.source_scope} {scope_name} native-key scope. Stop at the next native event, including NULL, or the frozen cutoff. Other snapshots are preserved.")
    report_path = args.output / "publication.json"
    export_json(report_path, report)
    (args.output / "snapshot_capture.sql").write_text(SNAPSHOT_SQL, "utf-8")
    database = "arcana_historical_cap_snapshots_" + uuid4().hex
    created = False
    with SourceRefreshLock("kr"):
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
        try:
            boundaries = first_subsequent_events(client, args.output / "subsequent_events", scoped_ids, start=subsequent_start)
            calendar = market_calendar(client, source_start, args.output / "market_calendar")
            calendar = calendar.astype("datetime64[ns]")
            client.command(f"CREATE DATABASE {database}")
            created = True
            client.command(f"""CREATE TABLE {database}.source (
                security_id String, trade_date Date, financial_basis String, factor_id String,
                factor_value Nullable(Float64), fiscal_year Nullable(UInt16), financial_period Nullable(Date),
                currency String, updated_at DateTime64(3,'Asia/Seoul')) ENGINE=Memory""")
            ensure_factor_snapshot_table(client, snapshot_table=f"{database}.snapshots")
            state = pd.DataFrame(columns=COLUMNS + ["selected_source"])
            by_month = {r["month"]: r for r in native["months"]}
            for month in pd.period_range(source_start, END, freq="M"):
                folder = args.output / str(month)
                folder.mkdir()
                observed = native_capture(client, month, folder / "native_current.parquet", scoped_ids)
                if str(month) in by_month:
                    record = by_month[str(month)]
                    original_folder = args.native_publication.parent / str(month)
                    for name, expected_hash in record["hashes"].items():
                        if sha256_file(original_folder / name) != expected_hash:
                            raise ValueError("Native publication evidence changed")
                    original = pd.read_parquet(original_folder / "expected.parquet")
                    if scoped_ids is not None:
                        original = original.loc[original.security_id.isin(scoped_ids)]
                    audited = observed.trade_date.le(source_cutoff)
                    compare_complete(observed.loc[audited], original)
                    added = normalize(pd.read_parquet(original_folder / "insert_delta.parquet"))
                    added_keys = pd.MultiIndex.from_frame(observed[KEYS]).isin(pd.MultiIndex.from_frame(added[KEYS]))
                    observed["selected_source"] = audited & (True if args.source_scope == "all" else (added_keys if args.source_scope == "added" else ~added_keys))
                else:
                    observed["selected_source"] = False
                source = pd.concat([state, observed], ignore_index=True) if not state.empty else observed
                source = normalize(source)
                state = source.sort_values("trade_date").drop_duplicates(GROUPS, keep="last")
                days = calendar[(calendar >= month.start_time) & (calendar < (month + 1).start_time)]
                expected = affected_asof(source, days, boundaries)
                if expected.empty:
                    records.append(dict(month=str(month), status="no_affected_calendar_dates", rows=0))
                    export_json(report_path, report)
                    continue
                sids = sorted(expected.security_id.unique())
                before = snapshot_capture(client, month, sids, folder / "snapshot_before.parquet")
                extra_dates = pd.DatetimeIndex(before.trade_date.unique()).difference(days)
                if len(extra_dates):
                    expected = affected_asof(source, days.union(extra_dates).sort_values(), boundaries)
                expected.to_parquet(folder / "expected_asof.parquet", index=False)
                source.to_parquet(folder / "source_with_previous_state.parquet", index=False)
                current, wanted = canonical(before), canonical(expected)
                relevant = current.loc[current.index.intersection(wanted.index)].reset_index()
                untouched = current.loc[current.index.difference(wanted.index)].reset_index()
                delta, revisions, extra, counts = plan_delta(relevant, expected, pd.DataFrame(columns=KEYS), snapshot=True)
                assert extra.empty
                client.command(f"TRUNCATE TABLE {database}.source")
                client.command(f"TRUNCATE TABLE {database}.snapshots")
                client.insert_df(f"{database}.source", clickhouse_rows(source[COLUMNS]), column_names=COLUMNS)
                all_days = sorted(expected.trade_date.dt.date.unique())
                query, parameters = build_factor_snapshot_insert_query(market="kr", factor_ids=native["factor_ids"],
                    security_ids=sids, snapshot_dates=all_days, source_table=f"{database}.source",
                    snapshot_table=f"{database}.snapshots")
                (folder / "asof.sql").write_text(query, "utf-8")
                client.command(query, parameters=parameters, settings=dict(max_execution_time=30, max_threads=2))
                staged = normalize(client.query_df(f"SELECT * FROM {database}.snapshots"))
                staged_wanted = canonical(staged).loc[wanted.index].reset_index()
                compare_nullable(staged_wanted, expected, snapshot=True)
                staged_wanted.to_parquet(folder / "sql_asof_verified.parquet", index=False)
                wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
                version = next_revision_time(before, revisions, wall_clock)
                delta["updated_at"] = version
                delta = clickhouse_rows(delta)
                delta.to_parquet(folder / "insert_delta.parquet", index=False)
                revisions.to_parquet(folder / "revised_before.parquet", index=False)
                record = dict(month=str(month), status="sql_verified", rows=len(expected),
                    finite_rows=int(expected.factor_value.notna().sum()), affected_securities=len(sids),
                    existing_untouched_rows=len(untouched), existing_dates_outside_calendar=len(extra_dates),
                    delta=counts, publication_wall_clock=wall_clock.isoformat(), revision_version=version.isoformat())
                export_json(folder / "checkpoint.json", record)
                if args.publish_snapshots:
                    if not delta.empty:
                        client.insert_df("fact_daily_factor_snapshot", delta[SNAPSHOT_COLUMNS], column_names=SNAPSHOT_COLUMNS)
                        report["inserted_rows"] += len(delta)
                        export_json(report_path, report)
                    after = snapshot_capture(client, month, sids, folder / "snapshot_after.parquet")
                    actual = canonical(after)
                    compare_nullable(actual.loc[wanted.index].reset_index(), expected, snapshot=True)
                    if len(actual.index.difference(current.index.union(wanted.index))):
                        raise ValueError("Unexpected snapshot keys appeared during publication")
                    if not untouched.empty:
                        preserved = actual.loc[canonical(untouched).index].reset_index()
                        compare_nullable(preserved, untouched, snapshot=True)
                        compare_complete(preserved, untouched)
                    if not delta.empty:
                        changed = actual.loc[canonical(delta).index].reset_index()
                        compare_complete(changed, delta)
                    record["status"] = "published_and_verified"
                record["hashes"] = {p.name: sha256_file(p) for p in folder.glob("*.parquet")}
                records.append(record)
                report.update(status="publishing" if args.publish_snapshots else "preparing")
                report["verified_rows"] += len(expected)
                report["new_keys"] += counts["new_keys"]
                report["revised_keys"] += counts["revised_keys"]
                export_json(folder / "checkpoint.json", record)
                export_json(report_path, report)
                print(str(month), record["status"], len(expected), counts, flush=True)
            verify_dependencies()
            later = first_subsequent_events(client, args.output / "subsequent_events_after", scoped_ids, start=subsequent_start)
            pd.testing.assert_frame_equal(boundaries.sort_values(GROUPS).reset_index(drop=True), later.sort_values(GROUPS).reset_index(drop=True))
            later_calendar = market_calendar(client, source_start, args.output / "market_calendar_after").astype("datetime64[ns]")
            pd.testing.assert_index_equal(calendar, later_calendar)
            report.update(status="published_and_verified" if args.publish_snapshots else "sql_verified_not_published",
                snapshots_published=args.publish_snapshots, finished_at=datetime.now(timezone.utc).isoformat())
            export_json(report_path, report)
        except BaseException as error:
            report.update(status="failed_check_month_checkpoints", error_type=type(error).__name__, error=str(error))
            export_json(report_path, report)
            raise
        finally:
            if created:
                client.command(f"DROP DATABASE {database}")
            client.close()
    if args.publish_snapshots:
        gold_name = "snapshot_existing_keys_summary.json" if args.source_scope == "existing" else "snapshot_summary.json"
        gold_path = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", scope_name, gold_name)
        if gold_path.exists():
            shutil.copy2(gold_path, args.output / "gold_snapshot_summary_before.json")
        export_json(gold_path,
            dict(status=report["status"], snapshots_published=True, coverage_complete=False,
                publication_path=str(report_path.resolve()), publication_sha256=sha256_file(report_path),
                inserted_rows=report["inserted_rows"], verified_rows=report["verified_rows"],
                new_keys=report["new_keys"], revised_keys=report["revised_keys"], scope=report["scope"], cutoff=END,
                source_years=years, source_scope=args.source_scope))
    print(report["status"], report["verified_rows"], report["inserted_rows"], flush=True)


if __name__ == "__main__":
    main()
