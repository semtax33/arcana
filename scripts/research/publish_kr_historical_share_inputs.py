"""Restore audited historical share inputs through regular normalization.

Keep bronze immutable, register pinned sources in silver, verify the full staged
input against the old input and independently audited observations, then replace
only the default silver CSV. Native factors and snapshots remain pending.
"""
import argparse
from datetime import datetime, timezone
from glob import glob
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock, sha256_file
from engine.transformers.market_data import normalize_shares

KEYS = ["security_id", "trade_date"]
VALUES = ["shares", "market_cap"]


def require_equal(left, right):
    if not left.index.equals(right.index):
        raise ValueError("Capitalization observation keys differ")
    shares = np.isclose(left.shares.to_numpy(float), right.shares.to_numpy(float), rtol=0, atol=0, equal_nan=True)
    cap = np.isclose(left.market_cap.to_numpy(float), right.market_cap.to_numpy(float), rtol=1e-8, atol=1, equal_nan=True)
    if not (shares & cap).all():
        raise ValueError(f"Capitalization observation values differ: {int((~(shares & cap)).sum())}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--preservation-audit", type=Path, help="Verified raw sources for existing rows absent from symbol caches")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Publication audit and backups must stay in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    report_path = args.output / "publication.json"
    report = dict(status="preparing", default_input_restored=False, native_restored=False,
        snapshots_restored=False, coverage_complete=False, cutoff="2026-09-10",
        source_audit_path=str(args.source_audit.resolve()), source_audit_sha256=sha256_file(args.source_audit))
    export_json(report_path, report)
    audit = json.loads(args.source_audit.read_text("utf-8"))
    if audit["invalid_source_rows"] or audit["differing_existing_rows"] or audit["existing_keys_outside_source"]:
        raise ValueError("Source audit contains unresolved differences")
    reviewed = DATA_LAKE.meta("survivorship", "kr_reviewed.json")
    if sha256_file(reviewed) != audit["reviewed_manifest_sha256"]:
        raise ValueError("Source manifest changed since audit")
    raw_sources = {row["source_id"]: row for row in json.loads(reviewed.read_text("utf-8"))["sources"]}
    prepared = []
    registration = dict(schema_version=1, sources=[], identity_scope="Dated observations only; no new issuer or listing approval.",
        source_audit_path=str(args.source_audit.resolve()), source_audit_sha256=report["source_audit_sha256"])
    for year in audit["years"]:
        source = raw_sources[f"marcap-{year['year']}"]
        raw_path = ROOT / source["path"]
        if sha256_file(raw_path) != source["source_sha256"] or source["source_sha256"] != year["source_sha256"]:
            raise ValueError("Source bytes changed since audit")
        prepared_path = args.source_audit.parent / f"prepared_{year['year']}.parquet"
        if sha256_file(prepared_path) != year["prepared_sha256"]:
            raise ValueError("Audited observation bytes changed")
        prepared.append(pd.read_parquet(prepared_path, columns=KEYS + VALUES))
        registration["sources"].append(dict(year=year["year"], path=raw_path.relative_to(DATA_LAKE.root).as_posix(),
            sha256=source["source_sha256"], source_id=source["source_id"], source_url=source["source_url"],
            observation_date_field="Date", verified_at=source["published_date"]))
    base_registration = json.loads(json.dumps(registration))
    preservation_rows = 0
    if args.preservation_audit is not None:
        preservation = json.loads(args.preservation_audit.read_text("utf-8"))
        if preservation["status"] != "verified_existing_observations" or preservation["default_input_sha256"] != audit["default_share_input_sha256"]:
            raise ValueError("Preservation audit is not verified against the same input")
        for item in preservation["sources"]:
            raw_path = DATA_LAKE.root / item["registration"]["path"]
            if sha256_file(raw_path) != item["registration"]["sha256"]:
                raise ValueError("Preservation source bytes changed")
            observation_path = Path(item["prepared_path"])
            if sha256_file(observation_path) != item["prepared_sha256"]:
                raise ValueError("Preservation observations changed")
            observations = pd.read_parquet(observation_path, columns=KEYS + VALUES)
            preservation_rows += len(observations)
            prepared.append(observations)
            registration["sources"].append(item["registration"])
        registration.update(preservation_audit_path=str(args.preservation_audit.resolve()),
            preservation_audit_sha256=sha256_file(args.preservation_audit))
    historical = pd.concat(prepared, ignore_index=True).set_index(KEYS).sort_index()
    if historical.index.has_duplicates:
        raise ValueError("Audited observations have duplicate keys")
    implementation = args.output / "implementation"
    implementation.mkdir()
    for path in [Path(__file__), ROOT / "engine/transformers/_internal/krx_market_data.py", ROOT / "tests/test_kr_historical_share_normalization.py"]:
        shutil.copy2(path, implementation / path.name)
    report["implementation"] = {path.name: sha256_file(path) for path in implementation.iterdir()}
    current = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    manifest = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    pattern = str(DATA_LAKE.bronze("krx", "shares", "*"))
    published = False
    with SourceRefreshLock("kr"):
        try:
            if sha256_file(current) != audit["default_share_input_sha256"]:
                raise ValueError("Default share input changed since source audit; audit again")
            shutil.copy2(current, args.output / "default_before.csv")
            original = pd.read_csv(current, parse_dates=["trade_date"]).set_index(KEYS).sort_index()
            if original.index.has_duplicates:
                raise ValueError("Default share input has duplicate keys")
            inventory = {str(Path(path).resolve()): sha256_file(path) for path in sorted(glob(pattern))}
            export_json(args.output / "krx_input_inventory.json", inventory)
            if manifest.exists():
                existing = json.loads(manifest.read_text("utf-8"))
                if existing == base_registration and registration != base_registration:
                    shutil.copy2(manifest, args.output / "registration_before.json")
                    export_json(manifest, registration)
                elif existing != registration:
                    raise ValueError("Existing historical source registration differs")
            else:
                export_json(manifest, registration)
            shutil.copy2(manifest, args.output / "historical_sources.json")
            report.update(status="registered_staging", registration_path=str(manifest), registration_sha256=sha256_file(manifest),
                old_input_rows=len(original), source_observations=audit["prepared_source_rows"],
                preserved_recent_observations=preservation_rows, source_years=[r["year"] for r in audit["years"]])
            export_json(report_path, report)
            candidate_path = args.output / "candidate.csv"
            print("Running regular normalize_shares against all bronze inputs", flush=True)
            candidate = normalize_shares(pattern, output_path=candidate_path).set_index(KEYS).sort_index()
            if candidate.index.has_duplicates:
                raise ValueError("Normalized share input has duplicate keys")
            missing = original.index.difference(candidate.index)
            added = candidate.index.difference(original.index)
            expected_added = historical.index.difference(original.index)
            if len(missing) or not added.equals(expected_added):
                missing.to_frame(index=False).to_parquet(args.output / "unexpected_missing.parquet", index=False)
                added.difference(expected_added).to_frame(index=False).to_parquet(args.output / "unexpected_additions.parquet", index=False)
                raise ValueError(f"Unexpected full-input key change: removed={len(missing)}, added={len(added)}, expected_added={len(expected_added)}")
            require_equal(candidate.loc[original.index], original)
            require_equal(candidate.loc[historical.index], historical)
            delta_path = args.output / "added_observations.parquet"
            candidate.loc[added].reset_index().to_parquet(delta_path, index=False)
            report.update(status="staged_all_rows_verified", staged_rows=len(candidate), added_rows=len(added),
                removed_rows=0, differing_existing_rows=0, candidate_sha256=sha256_file(candidate_path),
                added_observations_sha256=sha256_file(delta_path))
            export_json(report_path, report)
            if sha256_file(current) != audit["default_share_input_sha256"]:
                raise ValueError("Default share input changed during staging")
            if {str(Path(path).resolve()): sha256_file(path) for path in sorted(glob(pattern))} != inventory:
                raise ValueError("KRX source inputs changed during staging")
            if sha256_file(manifest) != report["registration_sha256"]:
                raise ValueError("Historical registration changed during staging")
            # Exercise the unchanged default call used by the regular workflow.
            print("Publishing through the regular default normalize_shares call", flush=True)
            del candidate
            normalize_shares(pattern)
            published = True
            if sha256_file(current) != report["candidate_sha256"]:
                raise ValueError("Published input differs from verified staged bytes")
            report.update(status="default_input_published_and_verified_native_pending", default_input_restored=True,
                default_input_path=str(current), default_input_sha256=sha256_file(current),
                published_at=datetime.now(timezone.utc).isoformat(), regular_rerun_matches_staging=True)
            export_json(report_path, report)
        except Exception as error:
            report.update(status="failed_after_input_write" if published else "failed_default_input_preserved", error=str(error))
            export_json(report_path, report)
            raise
    gold = DATA_LAKE.gold("survivorship", "kr", "capitalization_coverage", "20260911", "summary.json")
    if gold.exists():
        shutil.copy2(gold, args.output / "gold_before.json")
        summary = json.loads(gold.read_text("utf-8"))
    else:
        summary = {}
    summary.update(status=report["status"], default_input_restored=True, native_restored=False, snapshots_restored=False,
        default_input_observations=audit["prepared_source_rows"], missing_default_input_observations=0,
        preserved_recent_observations=preservation_rows,
        restored_observations=report["added_rows"], full_default_input_rows=report["staged_rows"],
        publication_path=str(report_path.resolve()), publication_sha256=sha256_file(report_path), coverage_complete=False)
    export_json(gold, summary)
    print({key:report[key] for key in ["status", "old_input_rows", "staged_rows", "added_rows", "default_input_sha256"]}, flush=True)


if __name__ == "__main__":
    main()
