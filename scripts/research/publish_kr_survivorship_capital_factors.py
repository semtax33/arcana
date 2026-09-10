"""Publish source-verified capital factors, including explicit abstentions.

Back up complete native rows, stage nullable values in ClickHouse, upsert only
audited keys, and read back the complete scope. Full rebuild markers stay dirty.
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
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import SourceRefreshLock
from prepare_kr_survivorship_full_factors import input_inventory
from publish_kr_survivorship_financial_factors import verify_existing_publication
from publish_kr_survivorship_market_factors import metadata_difference, next_revision_time
from validate_kr_survivorship_financial_factors import SILVER, KEYS, canonical, digest, save

END = "2026-09-10"
COLUMNS = KEYS + ["factor_value", "fiscal_year", "financial_period", "currency", "updated_at"]


def latest_nullable(rows, *, snapshot=False):
    if rows.empty:
        return rows, pd.DataFrame(columns=KEYS)
    newest = rows.groupby(KEYS, dropna=False).updated_at.transform("max")
    selected = rows.loc[rows.updated_at.eq(newest)]
    fields = ["factor_value", "fiscal_year", "financial_period", "currency"]
    if snapshot:
        fields.append("source_trade_date")
    ambiguous = selected.groupby(KEYS, dropna=False)[fields].nunique(dropna=False).gt(1).any(axis=1)
    return selected.drop_duplicates(KEYS, keep="last"), ambiguous.loc[ambiguous].reset_index()[KEYS]


def capture_rows(client, bounds, ids, folder, *, snapshot=False):
    folder.mkdir(parents=True)
    columns = COLUMNS + (["source_trade_date"] if snapshot else [])
    table = "fact_daily_factor_snapshot" if snapshot else "fact_daily_factors"
    parameters = dict(ids=ids, cutoff=date.fromisoformat(END))
    conditions = []
    for i, (sid, first) in enumerate(sorted(bounds.items())):
        conditions.append(f"(security_id={{sid{i}:String}} AND trade_date >= {{first{i}:Date}})")
        parameters[f"sid{i}"], parameters[f"first{i}"] = sid, date.fromisoformat(first)
    query = "SELECT " + ", ".join(columns) + f""" FROM {table}
        WHERE trade_date >= {{start:Date}} AND trade_date < {{end:Date}} AND trade_date <= {{cutoff:Date}}
          AND financial_basis IN ('annual','quarterly','ttm') AND factor_id IN {{ids:Array(String)}}
          AND (""" + " OR ".join(conditions) + ") SETTINGS max_execution_time=20,max_threads=2"
    (folder / "query.sql").write_text(query, "utf-8")
    frames, months = [], []
    for month in pd.period_range(min(bounds.values()), END, freq="M"):
        frame = client.query_df(query, parameters=dict(parameters, start=month.start_time.date(), end=(month+1).start_time.date()))
        if frame.empty and not len(frame.columns):
            frame = pd.DataFrame(columns=columns)
        path = folder / f"{month}.parquet"
        frame.to_parquet(path, index=False)
        frames.append(frame)
        months.append(dict(month=str(month), rows=len(frame), sha256=digest(path)))
        save(folder / "summary.json", dict(status="running", months=months))
        if len(months) % 24 == 1:
            print(folder.name, month, sum(r["rows"] for r in months), flush=True)
    rows = pd.concat(frames, ignore_index=True)
    latest, ambiguous = latest_nullable(rows, snapshot=snapshot)
    ambiguous.to_parquet(folder / "ambiguous_keys.parquet", index=False)
    save(folder / "summary.json", dict(status="finished", months=months, physical_rows=len(rows),
        latest_rows=len(latest), ambiguous_keys=len(ambiguous)))
    return latest, ambiguous


def numeric_equal(a, b):
    return np.isclose(a.to_numpy(dtype=float, na_value=np.nan), b.to_numpy(dtype=float, na_value=np.nan),
        rtol=1e-12, atol=1e-12, equal_nan=True)


def compare_nullable(actual, expected, *, snapshot=False):
    a, e = canonical(actual), canonical(expected)
    assert a.index.equals(e.index), "Published identity/availability differs"
    assert numeric_equal(a.factor_value, e.factor_value).all(), "Published nullable values differ"
    assert not metadata_difference(a, e).any(), "Published financial metadata differs"
    if snapshot:
        assert pd.to_datetime(a.source_trade_date).eq(pd.to_datetime(e.source_trade_date)).all()


def plan_delta(before, prepared, ambiguous, *, snapshot=False):
    a, e = canonical(before), canonical(prepared)
    extra = a.index.difference(e.index)
    common = a.index.intersection(e.index)
    numeric = ~numeric_equal(a.loc[common, "factor_value"], e.loc[common, "factor_value"])
    metadata = metadata_difference(a.loc[common], e.loc[common]).to_numpy()
    dates = np.zeros(len(common), dtype=bool)
    if snapshot:
        dates = ~pd.to_datetime(a.loc[common, "source_trade_date"]).eq(pd.to_datetime(e.loc[common, "source_trade_date"])).to_numpy()
    changed = common[numeric | metadata | dates].union(canonical(ambiguous).index.intersection(common))
    keys = e.index.difference(a.index).union(changed)
    return e.loc[keys].reset_index()[prepared.columns], a.loc[changed].reset_index(), a.loc[extra].reset_index(), dict(
        new_keys=len(e.index.difference(a.index)), revised_keys=len(changed), numeric_revisions=int(numeric.sum()),
        metadata_revisions=int(metadata.sum()), source_date_revisions=int(dates.sum()), unsupported_keys=len(extra))


def clickhouse_rows(frame):
    frame = frame.copy()
    for col in ("trade_date", "financial_period", "source_trade_date"):
        if col in frame:
            dates = pd.to_datetime(frame[col]).dt.date.astype(object)
            frame[col] = dates.where(pd.notna(dates), None)
    year = pd.to_numeric(frame.fiscal_year).astype("Int64")
    frame["fiscal_year"] = year.astype(object).where(year.notna(), None)
    return frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_capital_factor_publication")
    parser.add_argument("--publish-native", action="store_true")
    args = parser.parse_args()
    preparation = json.loads((args.preparation / "summary.json").read_text("utf-8"))
    assert preparation["status"] == "validated_not_published"
    symbols = sorted({r["symbol"] for r in preparation["cases"]})
    ids = preparation["factor_ids"]
    assert symbols == ["003410", "035480"] and len(ids) == 11 and not any(f.startswith("lab_") for f in ids)
    dependencies = dict(preparation["implementation_sha256"])
    dependencies[str(args.preparation / "summary.json")] = digest(args.preparation / "summary.json")
    for path in (Path(__file__), ROOT / "scripts/research/publish_kr_survivorship_market_factors.py",
                 ROOT / "scripts/research/publish_kr_survivorship_financial_factors.py",
                 ROOT / "scripts/research/validate_kr_survivorship_financial_factors.py",
                 ROOT / "scripts/research/prepare_kr_survivorship_full_factors.py"):
        dependencies[str(path)] = digest(path)
    frames = []
    for case in preparation["cases"]:
        path = args.preparation / case["symbol"] / case["basis"] / "prepared.parquet"
        dependencies[str(path)] = case["prepared_sha256"]
        frames.append(pd.read_parquet(path))
    prepared = pd.concat(frames, ignore_index=True)[COLUMNS]
    assert len(prepared) == 32635 and int(prepared.factor_value.notna().sum()) == 32541
    assert np.isfinite(prepared.factor_value.dropna()).all()
    canonical(prepared)
    bounds = {f"SEC_KR_{r['symbol']}": r["source_version"]["from_date"] for r in preparation["cases"]}
    def verify():
        for path, checksum in dependencies.items():
            assert digest(path) == checksum, f"Publication dependency changed: {path}"
        assert input_inventory(symbols) == preparation["input_inventory"], "Reviewed input changed"
        for symbol in symbols:
            case = next(r for r in preparation["cases"] if r["symbol"] == symbol)
            source = case["source_version"]
            verify_existing_publication(dict(manifest_path=source["manifest_path"], sha256=source["manifest_sha256"]))
    verify()
    attempt = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    report = dict(status="starting", native_published=False, snapshots_published=False, coverage_complete=False,
        symbols=symbols, factor_ids=ids, bounds=bounds, cutoff=END, preparation=str(args.preparation),
        dependencies=dependencies, input_inventory=preparation["input_inventory"], finite_cells=32541, abstention_events=94)
    save(attempt / "publication.json", report)
    shutil.copy2(__file__, attempt / Path(__file__).name)
    database = "arcana_capital_stage_" + uuid4().hex
    created = False
    with SourceRefreshLock("kr"):
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        try:
            verify()
            before, ambiguous = capture_rows(client, bounds, ids, attempt / "native_before")
            delta, revisions, extra, counts = plan_delta(before, prepared, ambiguous)
            extra.to_parquet(attempt / "unsupported_existing.parquet", index=False)
            report.update(status="reconciled", previous_rows=len(before), delta=counts)
            save(attempt / "publication.json", report)
            assert extra.empty, "Existing keys outside the source event preparation require explicit review"
            wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
            version = next_revision_time(before, revisions, wall_clock)
            prepared["updated_at"] = version
            delta["updated_at"] = version
            prepared, delta = clickhouse_rows(prepared), clickhouse_rows(delta)
            for name, frame in (("prepared", prepared), ("insert_delta", delta), ("revised_before", revisions)):
                frame.to_parquet(attempt / f"{name}.parquet", index=False)
                report[name + "_sha256"] = digest(attempt / f"{name}.parquet")
            client.command(f"CREATE DATABASE {database}")
            created = True
            client.command(f"""CREATE TABLE {database}.source (
                security_id String, trade_date Date, financial_basis String, factor_id String,
                factor_value Nullable(Float64), fiscal_year Nullable(UInt16), financial_period Nullable(Date),
                currency String, updated_at DateTime64(3,'Asia/Seoul')) ENGINE=Memory""")
            if not before.empty:
                client.insert_df(f"{database}.source", clickhouse_rows(before), column_names=COLUMNS)
            if not delta.empty:
                client.insert_df(f"{database}.source", delta, column_names=COLUMNS)
            staged, stage_ambiguity = latest_nullable(client.query_df(f"SELECT * FROM {database}.source"))
            assert stage_ambiguity.empty
            compare_nullable(staged, prepared)
            verify()
            report.update(status="staged_and_verified", stage_verified_rows=len(staged),
                revision_version=version.isoformat(), publication_wall_clock=wall_clock.isoformat(), planned_inserts=len(delta))
            save(attempt / "publication.json", report)
            if not args.publish_native:
                return
            report.update(status="publication_started", inserted_rows=0)
            save(attempt / "publication.json", report)
            for month, part in delta.groupby(pd.to_datetime(delta.trade_date).dt.to_period("M"), sort=True):
                client.insert_df("fact_daily_factors", part, column_names=COLUMNS)
                report["inserted_rows"] += len(part)
                save(attempt / "publication.json", report)
            report["status"] = "readback_pending"
            save(attempt / "publication.json", report)
            after, after_ambiguity = capture_rows(client, bounds, ids, attempt / "native_after")
            assert after_ambiguity.empty
            compare_nullable(after, prepared)
            verify()
            gold = DATA_LAKE.gold("survivorship", "kr", "capital_factors", "20260910", attempt.name)
            artifacts = {basis: export_frame(gold / f"{basis}.parquet", prepared.loc[prepared.financial_basis.eq(basis)].reset_index(drop=True))
                for basis in ("annual", "quarterly", "ttm")}
            report.update(status="native_published_and_verified_snapshots_pending", native_published=True,
                verified_rows=len(after), gold_artifacts=artifacts, finished_at=datetime.now(timezone.utc).isoformat())
            save(attempt / "publication.json", report)
            save(args.output / "latest.json", dict(publication_path=str(attempt / "publication.json"), sha256=digest(attempt / "publication.json")))
            export_json(gold / "summary.json", report)
            print(report["status"], "inserted", report["inserted_rows"], "verified", len(after), flush=True)
        finally:
            if created:
                client.command(f"DROP DATABASE {database}")
            client.close()


if __name__ == "__main__":
    main()
