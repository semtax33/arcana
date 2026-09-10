"""Close out a verified share-input publication without publishing DB factors."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--red", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    args = parser.parse_args()
    folder = args.publication.resolve()
    if not folder.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Publication evidence must remain in Silver")
    output = folder / "closeout"
    output.mkdir(exist_ok=False)
    evidence = {}

    def verify(path, expected=None):
        path = Path(path).resolve()
        digest = sha256_file(path)
        if expected is not None and digest != expected:
            raise ValueError(f"Evidence changed: {path}")
        evidence[str(path)] = digest
        return digest

    def read(path, expected=None):
        verify(path, expected)
        return json.loads(Path(path).read_text("utf-8"))

    report = read(folder / "publication.json")
    if report["status"] != "default_input_published_and_verified_rebuild_pending":
        raise ValueError("Default input publication is not verified")
    verify(report["default_input_path"], report["default_input_sha256"])
    verify(folder / "candidate.csv", report["candidate_sha256"])
    registry = read(report["registration_path"], report["registration_sha256"])
    for path, digest in read(folder / "input_inventory.json").items():
        verify(path, digest)
    for year in report["years"]:
        verify(year["candidate_path"], year["candidate_sha256"])
        if pq.ParquetFile(year["candidate_path"]).metadata.num_rows != year["verified_rows"]:
            raise ValueError("Year partition row count changed")
    if sum(y["verified_rows"] for y in report["years"]) != report["staged_rows"]:
        raise ValueError("Year partitions do not cover the publication")

    journal_path = Path(report["share_input_change_report"])
    journal = read(journal_path, report["share_input_change_report_sha256"])
    if (journal["status"] != "published" or journal["after_sha256"] != report["default_input_sha256"]
            or journal["historical_sources_sha256"] != report["registration_sha256"]):
        raise ValueError("Change journal does not identify the published inputs")
    verify(folder / "default_before.csv", journal["before_sha256"])
    delta_path = journal_path.parent / "changed_observations.parquet"
    verify(delta_path, journal["changed_observations_sha256"])
    totals = {}
    seen_last = None
    for batch in pq.ParquetFile(delta_path).iter_batches(batch_size=300_000):
        frame = batch.to_pandas()
        keys = pd.MultiIndex.from_frame(frame[["security_id", "trade_date"]])
        if not keys.is_monotonic_increasing or keys.has_duplicates or (seen_last is not None and keys[0] <= seen_last):
            raise ValueError("Change journal contains duplicate or unordered observations")
        seen_last = keys[-1]
        if (not frame["_merge"].eq("right_only").all()
                or frame[["shares_before", "market_cap_before"]].notna().any().any()
                or frame[["shares_after", "market_cap_after"]].isna().any().any()):
            raise ValueError("This publication must contain additions only")
        for sid, rows in frame.groupby("security_id", sort=False):
            dates = rows.trade_date
            old = totals.get(sid)
            totals[sid] = dict(added_rows=len(rows) + (old["added_rows"] if old else 0),
                from_date=min(dates.min().date().isoformat(), old["from_date"] if old else "9999"),
                last_changed_date=max(dates.max().date().isoformat(), old["last_changed_date"] if old else ""),
                changed_rows=0, removed_rows=0)
    if totals != journal["changed_securities"] or sum(r["added_rows"] for r in totals.values()) != report["added_rows"]:
        raise ValueError("Security rebuild scope differs from actual changed observations")

    quarantine = []
    for source in registry["sources"]:
        verify(DATA_LAKE.root / source["path"], source["sha256"])
        entry = source.get("quarantine")
        if entry:
            path = DATA_LAKE.root / entry["path"]
            verify(path, entry["sha256"])
            frame = pd.read_parquet(path)
            frame["source_path"] = source["path"]
            frame["source_sha256"] = source["sha256"]
            frame["source_url"] = source["source_url"]
            frame["review_status"] = "quarantined_invalid_observation"
            frame["reason"] = entry["reason"]
            quarantine.append(frame)
    quarantine = pd.concat(quarantine, ignore_index=True)
    if len(quarantine) != report["quarantined_source_rows"]:
        raise ValueError("Unresolved observation count changed")

    red = read(args.red / "summary.json")
    green = read(folder / "live_calculation_green" / "summary.json")
    if red["status"] == "passed" or green["status"] != "passed" or len(green["results"]) != 6:
        raise ValueError("Actual calculator must demonstrate a failing-to-passing source comparison")
    for result in green["results"]:
        if not result["passed"] or result["expected"] != result["actual"]:
            raise ValueError("Actual calculator result does not match original data")
        verify(result["calculation_path"], result["calculation_sha256"])
    verify(green["source_path"], green["source_sha256"])
    verify(green["original_observation"]["path"], green["original_observation"]["sha256"])
    verify(green["audit_case_path"], green["audit_case_sha256"])
    verify(args.tests)
    suites = ET.parse(args.tests).getroot().iter("testsuite")
    tests = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for key in tests:
            tests[key] += int(suite.get(key, 0))
    if tests != dict(tests=39, failures=0, errors=0, skipped=0):
        raise ValueError("Expected regression run is not complete")
    misplaced = [str(p) for name in ("docs", "tests") for p in (ROOT / name).rglob("*")
        if p.is_file() and p.suffix.lower() in {".html", ".htm", ".json", ".csv"}]
    if misplaced:
        raise ValueError(f"Data files remain outside the data lake: {misplaced}")

    gold = DATA_LAKE.gold("survivorship", "kr", "share_inputs", "1996_2026")
    previous = read(gold / "summary.json")
    shutil.copy2(gold / "summary.json", output / "gold_summary_before.json")
    quarantine_path = gold / "quarantined_observations.parquet"
    if quarantine_path.exists():
        raise ValueError("Refusing to overwrite a previous unresolved-observation export")
    quarantine.to_parquet(quarantine_path, index=False)
    verify(Path(__file__))
    shutil.copy2(__file__, output / Path(__file__).name)
    closeout = dict(status="verified_input_publication_rebuild_pending", checked_at=datetime.now(timezone.utc).isoformat(),
        normalized_observations=report["staged_rows"], added_observations=report["added_rows"],
        preserved_observations=report["preserved_rows"], affected_securities=len(totals),
        registered_sources=len(registry["sources"]), quarantined_observations=len(quarantine),
        tests=tests, actual_calculator_checks=green["results"], misplaced_data_files=misplaced,
        source_cutoff=report["source_cutoff"], last_price_calendar_date=report["last_price_calendar_date"],
        native_restored=False, snapshots_restored=False, coverage_complete=False,
        evidence_sha256=evidence,
        quarantine_export=dict(path=str(quarantine_path.resolve()), sha256=sha256_file(quarantine_path), rows=len(quarantine)))
    export_json(output / "audit.json", closeout)
    previous.update(affected_securities=len(totals), registered_sources=len(registry["sources"]),
        source_cutoff=report["source_cutoff"], last_price_calendar_date=report["last_price_calendar_date"],
        closeout=dict(path=str(output / "audit.json"), sha256=sha256_file(output / "audit.json")),
        actual_calculator_checks_passed=6, regression_tests_passed=39,
        quarantine_export=closeout["quarantine_export"])
    export_json(gold / "summary.json", previous)
    print(json.dumps({key: closeout[key] for key in ("status", "normalized_observations", "added_observations", "affected_securities", "quarantined_observations")}), flush=True)


if __name__ == "__main__":
    main()
