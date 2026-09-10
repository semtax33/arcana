"""Publish independently audited KR market factors without deleting other data.

Monthly full-row before-images and readback are retained in silver. This is a
partial source restoration: full-factor and snapshot rebuild markers stay dirty.
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import SourceRefreshLock
from engine.loaders.factors import _insert_daily_factor_rows_by_partition
from validate_kr_survivorship_financial_factors import SILVER, KEYS, canonical, digest, save
from prepare_kr_survivorship_full_factors import input_inventory

NATIVE_COLUMNS = KEYS + ["factor_value", "fiscal_year", "financial_period", "currency", "updated_at"]


def metadata_difference(old, new):
    changed = pd.Series(False, index=old.index)
    for column in ("fiscal_year", "financial_period", "currency"):
        if column not in old or column not in new:
            continue
        a, b = old[column], new[column]
        if column == "financial_period":
            a, b = pd.to_datetime(a), pd.to_datetime(b)
        equal = a.eq(b).fillna(False) | (a.isna() & b.isna())
        changed |= ~equal
    return changed


def plan_delta(existing, prepared):
    old, new = canonical(existing), canonical(prepared)
    assert not len(old.index.difference(new.index)), "Native extras require an explicit missing-value revision"
    common = old.index.intersection(new.index)
    changed = common[~np.isclose(old.loc[common, "factor_value"], new.loc[common, "factor_value"], rtol=1e-12, atol=1e-12)
        | metadata_difference(old.loc[common], new.loc[common]).to_numpy()]
    keys = new.index.difference(old.index).union(changed)
    corrections = old.loc[changed, ["factor_value"]].rename(columns={"factor_value": "previous_value"})
    corrections["corrected_value"] = new.loc[changed, "factor_value"]
    return new.loc[keys].reset_index()[prepared.columns], corrections.reset_index()


def latest_rows(frame):
    if frame.empty:
        return frame
    newest = frame.groupby(KEYS, dropna=False).updated_at.transform("max")
    current = frame.loc[frame.updated_at.eq(newest)].copy()
    assert not current.groupby(KEYS, dropna=False).factor_value.nunique().gt(1).any(), "Ambiguous native latest version"
    return current.drop_duplicates(KEYS, keep="last")


def verify_correction_versions(existing, corrections, timestamp):
    assert timestamp.tzinfo is not None, "Publication timestamp requires an explicit timezone"
    if corrections.empty:
        return
    before = canonical(existing)
    keys = canonical(corrections).index
    previous = pd.to_datetime(before.loc[keys, "updated_at"])
    if previous.dt.tz is None:
        previous = previous.dt.tz_localize("Asia/Seoul")
    assert previous.lt(timestamp).all(), "Correction version would not supersede the native value"


def next_revision_time(existing, revisions, wall_clock):
    """Order cache revisions; record the actual wall clock separately.

    Legacy rows can have future versions from earlier writers. Preserve those
    rows and supersede their version without changing financial availability.
    """
    assert wall_clock.tzinfo is not None, "Revision wall clock requires an explicit timezone"
    version = wall_clock.ceil("ms")
    if not revisions.empty:
        before = canonical(existing)
        previous = pd.to_datetime(before.loc[canonical(revisions).index, "updated_at"])
        if previous.dt.tz is None:
            previous = previous.dt.tz_localize("Asia/Seoul")
        version = max(version, previous.max().ceil("ms") + pd.Timedelta(milliseconds=1))
    return version


def capture(client, bounds, ids, folder):
    folder.mkdir(parents=True)
    conditions, params = [], {"ids": ids}
    for i, (sid, start) in enumerate(sorted(bounds.items())):
        conditions.append(f"(security_id={{sid{i}:String}} AND trade_date >= {{first{i}:Date}})")
        params[f"sid{i}"], params[f"first{i}"] = sid, date.fromisoformat(start)
    sql = "SELECT " + ", ".join(NATIVE_COLUMNS) + """ FROM fact_daily_factors
        WHERE trade_date >= {start:Date} AND trade_date < {end:Date}
        AND financial_basis IN ('annual','quarterly','ttm')
        AND factor_id IN {ids:Array(String)} AND (""" + " OR ".join(conditions) + """ )
        SETTINGS max_execution_time=20,max_threads=2"""
    (folder / "query.sql").write_text(sql, "utf-8")
    frames, records = [], []
    for month in pd.period_range(min(bounds.values()), "2026-09", freq="M"):
        frame = client.query_df(sql, parameters=dict(params, start=month.start_time.date(), end=(month + 1).start_time.date()))
        if frame.empty and not len(frame.columns):
            frame = pd.DataFrame(columns=NATIVE_COLUMNS)
        path = folder / f"{month}.parquet"
        frame.to_parquet(path, index=False)
        records.append(dict(month=str(month), rows=len(frame), sha256=digest(path)))
        frames.append(frame)
        save(folder / "summary.json", dict(status="running", months=records))
        if len(records) % 24 == 1:
            print(folder.name, month, "rows", sum(r["rows"] for r in records), flush=True)
    result = pd.concat(frames, ignore_index=True)
    save(folder / "summary.json", dict(status="finished", months=records, physical_rows=len(result)))
    return latest_rows(result)


def compare(actual, expected, *, rtol=1e-12, atol=1e-12):
    a, e = canonical(actual), canonical(expected)
    assert a.index.equals(e.index), "Factor identity/availability mismatch"
    assert np.isclose(a.factor_value, e.factor_value, rtol=rtol, atol=atol).all(), "Factor value mismatch"
    assert not metadata_difference(a, e).any(), "Financial metadata mismatch"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=SILVER / "kr_market_factor_publication")
    parser.add_argument("--publish-native", action="store_true")
    args = parser.parse_args()
    attempt = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    dependencies = {}
    def pin(path, checksum=None):
        current = digest(path)
        assert checksum is None or current == checksum, f"Validated file changed: {path}"
        dependencies[str(path)] = current
    def verify():
        for path, checksum in dependencies.items():
            assert digest(path) == checksum, f"Publication dependency changed: {path}"
        assert input_inventory(symbols) == preparation["input_inventory"], "Calculation input changed"
    preparation = json.loads((args.preparation / "summary.json").read_text("utf-8"))
    validation = json.loads((args.validation / "summary.json").read_text("utf-8"))
    assert preparation["status"] == "prepared_not_independently_validated" and validation["status"] == "validated"
    assert len(preparation["results"]) == len(validation["results"]) == 36
    symbols = sorted({r["symbol"] for r in preparation["results"]})
    ids = validation["factor_ids"]
    assert len(symbols) == 12 and len(ids) == 23 and not any(f.startswith("lab_") for f in ids)
    for path in (args.preparation / "summary.json", args.validation / "summary.json", Path(__file__)):
        pin(path)
    for mapping in (validation["dependencies"], preparation["implementation_sha256"]):
        for path, checksum in mapping.items():
            pin(path, checksum)
    validated = {(r["symbol"], r["basis"]): r for r in validation["results"]}
    assert len(validated) == 36
    frames = []
    for case in preparation["results"]:
        symbol, basis = case["symbol"], case["basis"]
        check = validated[(symbol, basis)]
        assert check["status"] == "validated"
        actual_path = args.preparation / symbol / basis / "prepared.parquet"
        expected_path = args.validation / symbol / basis / "expected.parquet"
        pin(actual_path, case["prepared_sha256"])
        pin(expected_path, check["expected_sha256"])
        assert digest(actual_path) == check["prepared_sha256"]
        actual = pd.read_parquet(actual_path).loc[lambda d: d.factor_id.isin(ids)]
        compare(actual, pd.read_parquet(expected_path), **validation["tolerance"])
        frames.append(actual)
    prepared = pd.concat(frames, ignore_index=True)
    bounds = {f"SEC_KR_{r['symbol']}": r["start"] for r in preparation["results"]}
    report = dict(status="validated", native_published=False, snapshots_published=False, coverage_complete=False,
        rows=len(prepared), symbols=symbols, factor_ids=ids, bounds=bounds, dependencies=dependencies,
        preparation=str(args.preparation), validation=str(args.validation), attempt=str(attempt))
    save(attempt / "publication.json", report)
    shutil.copy2(__file__, attempt / Path(__file__).name)
    with SourceRefreshLock("kr"):
        verify()
        if not args.publish_native:
            return
        client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=30)
        try:
            before = capture(client, bounds, ids, attempt / "native_before")
            delta, corrections = plan_delta(before, prepared)
            wall_clock = pd.Timestamp.now(tz="Asia/Seoul")
            now = next_revision_time(before, corrections, wall_clock)
            verify_correction_versions(before, corrections, now)
            prepared["updated_at"] = now
            delta["updated_at"] = now
            report["publication_timestamp"] = now.isoformat()
            report["publication_wall_clock"] = wall_clock.isoformat()
            report["legacy_clock_adjustment"] = bool(now > wall_clock.ceil("ms"))
            old, candidate = canonical(before), canonical(prepared)
            common = old.index.intersection(candidate.index)
            report["numeric_corrections"] = int((~np.isclose(old.loc[common, "factor_value"], candidate.loc[common, "factor_value"], rtol=1e-12, atol=1e-12)).sum())
            report["metadata_corrections"] = int(metadata_difference(old.loc[common], candidate.loc[common]).sum())
            for name, frame in (("prepared", prepared), ("insert_delta", delta), ("corrections", corrections)):
                frame.to_parquet(attempt / f"{name}.parquet", index=False)
                report[name + "_sha256"] = digest(attempt / f"{name}.parquet")
            verify()
            report.update(status="publication_started", previous_rows=len(before), planned_inserts=len(delta), corrected_rows=len(corrections))
            save(attempt / "publication.json", report)
            inserted = _insert_daily_factor_rows_by_partition(client, delta, split_by_partition=True)
            report.update(status="readback_pending", inserted_rows=inserted)
            save(attempt / "publication.json", report)
            assert inserted == len(delta)
            after = capture(client, bounds, ids, attempt / "native_after")
            compare(after, prepared)
            verify()
            gold = DATA_LAKE.gold("survivorship", "kr", "market_factors", "20260910", attempt.name)
            artifacts = {basis: export_frame(gold / f"{basis}.parquet", prepared.loc[prepared.financial_basis.eq(basis)].reset_index(drop=True)) for basis in ("annual", "quarterly", "ttm")}
            report.update(status="native_published_and_verified_snapshots_pending", native_published=True,
                verified_rows=len(after), gold_artifacts=artifacts, finished_at=datetime.now(timezone.utc).isoformat())
            save(attempt / "publication.json", report)
            save(args.output / "latest.json", dict(publication_path=str(attempt / "publication.json"), sha256=digest(attempt / "publication.json")))
            export_json(gold / "summary.json", report)
            print(report["status"], "inserted", inserted, "corrected", len(corrections), "verified", len(after), flush=True)
        finally:
            client.close()


if __name__ == "__main__":
    main()
