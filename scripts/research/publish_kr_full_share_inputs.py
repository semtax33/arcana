"""Verify all staged dated observations, then publish via regular normalization."""
import argparse
from datetime import datetime, timezone
import gc
from glob import glob
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import SourceRefreshLock, sha256_file
from engine.core.serving_storage import export_json
from engine.transformers.market_data import normalize_shares

KEYS = ["security_id", "trade_date"]
COLUMNS = KEYS + ["shares", "market_cap"]


def partition_csv(path, output):
    output.mkdir()
    writers, counts = {}, {}
    try:
        for chunk in pd.read_csv(path, usecols=COLUMNS, dtype={"security_id":str}, chunksize=300_000):
            chunk["trade_date"] = pd.to_datetime(chunk.trade_date)
            for year, frame in chunk.groupby(chunk.trade_date.dt.year, sort=False):
                table = pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
                if year not in writers:
                    writers[year] = pq.ParquetWriter(output / f"{year}.parquet", table.schema)
                writers[year].write_table(table)
                counts[year] = counts.get(year, 0) + len(frame)
    finally:
        for writer in writers.values():
            writer.close()
    return counts


def keyed(path):
    if not path.exists():
        frame = pd.DataFrame({"security_id":pd.Series(dtype=str), "trade_date":pd.Series(dtype="datetime64[ns]"),
            "shares":pd.Series(dtype=float), "market_cap":pd.Series(dtype=float)})
    else:
        frame = pd.read_parquet(path, columns=COLUMNS)
        frame["trade_date"] = pd.to_datetime(frame.trade_date).astype("datetime64[ns]")
    frame = frame.set_index(KEYS).sort_index()
    if frame.index.has_duplicates:
        raise ValueError("Duplicate dated share input keys")
    return frame


def equal_values(actual, expected, *, preserve_exact=False):
    if not actual.index.equals(expected.index):
        raise ValueError("Share observation identities differ")
    if not np.array_equal(actual.shares.to_numpy(float), expected.shares.to_numpy(float), equal_nan=True):
        raise ValueError("Share counts differ from the original observations")
    a, b = actual.market_cap.to_numpy(float), expected.market_cap.to_numpy(float)
    match = np.array_equal(a, b, equal_nan=True) if preserve_exact else np.isclose(a, b, rtol=1e-8, atol=1, equal_nan=True).all()
    if not match:
        raise ValueError("Capitalization differs from the original observations")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-default", action="store_true")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Staging and backups must remain in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    preparation_path = args.preparation / "summary.json"
    preparation = json.loads(preparation_path.read_text("utf-8"))
    if preparation["status"] != "prepared_registration_public_normalization_pending":
        raise ValueError("A verified full-source registration preparation is required")
    proposal = Path(preparation["registration_path"])
    if sha256_file(proposal) != preparation["registration_sha256"]:
        raise ValueError("Prepared registration changed")
    registered = json.loads(proposal.read_text("utf-8"))
    audited_years = {}
    for item in preparation["audits"]:
        path = Path(item["path"])
        if sha256_file(path) != item["sha256"]:
            raise ValueError("Original observation audit changed")
        audit = json.loads(path.read_text("utf-8"))
        for year in audit["years"]:
            audited_years[year["year"]] = dict(record=year, path=path.parent / str(year["year"]) / "prepared.parquet")
    current = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    registry = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    pattern = str(DATA_LAKE.bronze("krx", "shares", "*"))
    candidate = args.output / "candidate.csv"
    report_path = args.output / "publication.json"
    records = []
    report = dict(status="starting", preparation_path=str(preparation_path.resolve()), preparation_sha256=sha256_file(preparation_path),
        default_input_restored=False, native_restored=False, snapshots_restored=False, coverage_complete=False,
        source_cutoff="2026-09-10", last_price_calendar_date="2026-09-04", years=records,
        expected_added_rows=preparation["expected_added_rows"], expected_total_rows=preparation["expected_total_rows"],
        policy="Preserve every existing input value; add verified dated originals. Exact invalid observations remain quarantined in Silver. No issuer/listing eligibility is approved.")
    export_json(report_path, report)
    implementation = args.output / "implementation"
    implementation.mkdir()
    code = [Path(__file__), ROOT / "engine/transformers/_internal/krx_market_data.py", ROOT / "engine/transformers/share_input_history.py",
        ROOT / "tests/test_kr_historical_share_normalization.py"]
    report["implementation_sha256"] = {str(path.resolve()):sha256_file(path) for path in code}
    for path in code:
        shutil.copy2(path, implementation / path.name)

    def source_inventory():
        result = {str(Path(path).resolve()):sha256_file(path) for path in sorted(glob(pattern))}
        for item in registered["sources"]:
            path = DATA_LAKE.root / item["path"]
            if sha256_file(path) != item["sha256"]:
                raise ValueError("Registered original changed")
            result[str(path.resolve())] = item["sha256"]
            if item.get("quarantine"):
                quarantine = item["quarantine"]
                path = DATA_LAKE.root / quarantine["path"]
                if sha256_file(path) != quarantine["sha256"]:
                    raise ValueError("Quarantine evidence changed")
                result[str(path.resolve())] = quarantine["sha256"]
        return result

    with SourceRefreshLock("kr"):
        if sha256_file(current) != preparation["default_input_sha256"] or sha256_file(registry) != preparation["prior_registration_sha256"]:
            raise ValueError("Production input or source registration changed after preparation")
        shutil.copy2(current, args.output / "default_before.csv")
        shutil.copy2(registry, args.output / "registration_before.json")
        inventory = source_inventory()
        export_json(args.output / "input_inventory.json", inventory)
        registry_written = False
        try:
            export_json(registry, registered)
            registry_written = True
            report.update(status="registered_staging", registration_path=str(registry.resolve()), registration_sha256=sha256_file(registry))
            export_json(report_path, report)
            print("Preparing through regular normalize_shares", flush=True)
            frame = normalize_shares(pattern, output_path=candidate)
            count = len(frame)
            quarantine_count = frame.attrs["quarantined_share_source_rows"]
            del frame
            gc.collect()
            if count != preparation["expected_total_rows"] or quarantine_count != preparation["quarantined_source_rows"]:
                raise ValueError("Staged observations/quarantine counts differ from audited scope")
            before_counts = partition_csv(args.output / "default_before.csv", args.output / "before_by_year")
            after_counts = partition_csv(candidate, args.output / "candidate_by_year")
            if sum(before_counts.values()) != preparation["current_rows"] or set(after_counts) != set(audited_years):
                raise ValueError("Unexpected normalization date scope")
            for year, source in sorted(audited_years.items()):
                if sha256_file(source["path"]) != source["record"]["hashes"]["prepared.parquet"]:
                    raise ValueError("Prepared yearly observations changed")
                old = keyed(args.output / "before_by_year" / f"{year}.parquet")
                new = keyed(args.output / "candidate_by_year" / f"{year}.parquet")
                original = keyed(source["path"])
                expected_index = old.index.union(original.index).sort_values()
                if not new.index.equals(expected_index):
                    raise ValueError(f"Unexpected missing/additional normalized keys in {year}")
                equal_values(new.loc[old.index], old, preserve_exact=True)
                equal_values(new.loc[original.index], original)
                added = new.index.difference(old.index)
                if len(added) != source["record"]["missing_default_rows"]:
                    raise ValueError("Unexpected addition count")
                path = args.output / "candidate_by_year" / f"{year}.parquet"
                record = dict(year=year, verified_rows=len(new), preserved_rows=len(old), added_rows=len(added),
                    removed_rows=0, revised_existing_rows=0, candidate_path=str(path.resolve()), candidate_sha256=sha256_file(path))
                records.append(record)
                export_json(report_path, report)
                print(json.dumps(record), flush=True)
                del old, new, original
                gc.collect()
            if source_inventory() != inventory or sha256_file(current) != preparation["default_input_sha256"]:
                raise ValueError("An input changed during staging")
            if sha256_file(registry) != report["registration_sha256"]:
                raise ValueError("Source registration changed during staging")
            report.update(status="staged_all_observations_verified", staged_rows=count, candidate_sha256=sha256_file(candidate),
                quarantined_source_rows=quarantine_count, added_rows=sum(r["added_rows"] for r in records),
                preserved_rows=sum(r["preserved_rows"] for r in records))
            export_json(report_path, report)
            if args.publish_default:
                print("Publishing through regular normalize_shares with change tracking", flush=True)
                frame = normalize_shares(pattern)
                journal_path = Path(frame.attrs["share_input_change_report"])
                del frame
                gc.collect()
                if sha256_file(current) != report["candidate_sha256"]:
                    raise ValueError("Published default input differs from staged bytes")
                journal = json.loads(journal_path.read_text("utf-8"))
                if journal["status"] != "published":
                    raise ValueError("Input publication journal is incomplete")
                report.update(status="default_input_published_and_verified_rebuild_pending", default_input_restored=True,
                    default_input_path=str(current.resolve()), default_input_sha256=sha256_file(current),
                    share_input_change_report=str(journal_path.resolve()), share_input_change_report_sha256=sha256_file(journal_path),
                    published_at=datetime.now(timezone.utc).isoformat())
            else:
                shutil.copyfile(args.output / "registration_before.json", registry)
                registry_written = False
            export_json(report_path, report)
        except Exception as error:
            unchanged = sha256_file(current) == preparation["default_input_sha256"]
            if unchanged and registry_written:
                shutil.copyfile(args.output / "registration_before.json", registry)
            report.update(status="failed_default_input_preserved" if unchanged else "failed_after_input_write_requires_journal_review", error=str(error))
            export_json(report_path, report)
            raise
    if report["default_input_restored"]:
        gold = DATA_LAKE.gold("survivorship", "kr", "share_inputs", "1996_2026", "summary.json")
        if gold.exists():
            shutil.copy2(gold, args.output / "gold_summary_before.json")
        export_json(gold, dict(status=report["status"], publication_path=str(report_path.resolve()), publication_sha256=sha256_file(report_path),
            default_input_restored=True, native_restored=False, snapshots_restored=False, source_years=sorted(audited_years),
            normalized_observations=report["staged_rows"], added_observations=report["added_rows"], preserved_observations=report["preserved_rows"],
            quarantined_observations=report["quarantined_source_rows"], coverage_complete=False,
            policy=report["policy"]))
    print(json.dumps({k:report[k] for k in ("status", "default_input_restored", "staged_rows", "added_rows", "quarantined_source_rows")}), flush=True)


if __name__ == "__main__":
    main()
