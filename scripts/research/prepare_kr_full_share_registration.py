"""Freeze the audited originals and prepare a full share-source registration."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file, write_source_bytes
from engine.core.serving_storage import export_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audits", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Registration preparation must be in Silver")
    args.output.mkdir(parents=True, exist_ok=False)
    current_path = DATA_LAKE.silver("krx", "shares", "kr_normalized_shares.csv")
    registry_path = DATA_LAKE.silver("krx", "shares", "historical_sources.json")
    before_hash = sha256_file(current_path)
    registry_hash = sha256_file(registry_path)
    current_registry = json.loads(registry_path.read_text("utf-8"))
    sources, quarantines, audit_refs = [], [], []
    added, current_rows, source_rows, rejected_rows, outside_rows = 0, 0, 0, 0, 0
    seen = set()
    for audit_path in args.audits:
        audit = json.loads(audit_path.read_text("utf-8"))
        if audit["status"] not in {"audited_conflicts_require_review", "source_audited_restoration_pending"}:
            raise ValueError("Source audit is incomplete")
        if audit["pinned_inputs"][str(current_path)] != before_hash or audit["totals"]["differing_existing_rows"]:
            raise ValueError("Source audit does not match the current input without value conflicts")
        audit_refs.append(dict(path=str(audit_path.resolve()), sha256=sha256_file(audit_path)))
        for year in audit["years"]:
            if year["year"] in seen:
                raise ValueError("Source years overlap across audits")
            seen.add(year["year"])
            raw_path = Path(year["source_path"])
            if sha256_file(raw_path) != year["source_sha256"]:
                raise ValueError("Original source changed after auditing")
            frozen = DATA_LAKE.bronze("registered-market-sources", f"sha256={year['source_sha256']}", raw_path.name)
            if not frozen.exists():
                write_source_bytes(frozen, raw_path.read_bytes(), source="verified-Marcap-original-archive")
            if sha256_file(frozen) != year["source_sha256"]:
                raise ValueError("Frozen original differs from audited bytes")
            entry = dict(year=year["year"], format="marcap_parquet", path=frozen.relative_to(DATA_LAKE.root).as_posix(),
                original_path=raw_path.relative_to(DATA_LAKE.root).as_posix(), sha256=year["source_sha256"],
                source_id=f"marcap-{year['year']}", source_url=year["source_url"], observation_date_field="Date",
                start_date=f"{year['year']}-01-01", end_date=min(f"{year['year']}-12-31", audit["end_date"]),
                source_audit_path=str(audit_path.resolve()), source_audit_sha256=sha256_file(audit_path))
            folder = audit_path.parent / str(year["year"])
            for name, expected in year["hashes"].items():
                if sha256_file(folder / name) != expected:
                    raise ValueError("Yearly audit evidence changed")
            if year["invalid_rows"]:
                rejected = folder / "invalid.parquet"
                target = DATA_LAKE.silver("krx", "shares", "source_quarantine", year["source_sha256"], "observations.parquet")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(rejected, target)
                if sha256_file(target) != year["hashes"]["invalid.parquet"]:
                    raise ValueError("Existing quarantine differs from the audited invalid observations")
                entry["quarantine"] = dict(path=target.relative_to(DATA_LAKE.root).as_posix(), sha256=sha256_file(target), rows=year["invalid_rows"],
                    reason="Missing/nonpositive/nonintegral values or failed price-times-shares identity; retained for review, never converted to zero.")
                quarantines.append(dict(source_id=entry["source_id"], original_sha256=year["source_sha256"], **entry["quarantine"]))
            sources.append(entry)
        totals = audit["totals"]
        added += totals["missing_default_rows"]
        current_rows += totals["default_rows"]
        source_rows += totals["prepared_rows"]
        rejected_rows += totals["invalid_rows"]
        outside_rows += totals["existing_without_source"]
    # The dated September 4 listing snapshot is outside the Marcap cache's last
    # date and already supported all 2,873 preserved regular observations.
    preserved = [source for source in current_registry["sources"] if source.get("format") == "fdr_listing_csv"]
    if len(preserved) != 1 or preserved[0]["observation_date"] != "2026-09-04" or outside_rows != 2873:
        raise ValueError("Unexpected observations outside annual Marcap sources require a preservation audit")
    for source in preserved:
        if sha256_file(DATA_LAKE.root / source["path"]) != source["sha256"]:
            raise ValueError("The preserved dated listing source changed")
    registration = dict(schema_version=1, sources=sorted(sources, key=lambda r:r["year"]) + preserved,
        identity_scope="Dated observations only; no new issuer or listing approval.", audits=audit_refs,
        quarantined_source_rows=rejected_rows, quarantine_policy="Exact original invalid keys and values must match each pinned Silver quarantine; every other invalid row remains fatal.")
    export_json(args.output / "historical_sources.json", registration)
    shutil.copy2(registry_path, args.output / "registration_before.json")
    shutil.copy2(__file__, args.output / Path(__file__).name)
    if sha256_file(current_path) != before_hash or sha256_file(registry_path) != registry_hash:
        raise ValueError("Production input or registration changed during preparation")
    report = dict(status="prepared_registration_public_normalization_pending", created_at=datetime.now(timezone.utc).isoformat(),
        source_years=sorted(seen), original_source_count=len(sources), source_count=len(registration["sources"]),
        default_input_sha256=before_hash, prior_registration_sha256=registry_hash,
        registration_path=str((args.output / "historical_sources.json").resolve()), registration_sha256=sha256_file(args.output / "historical_sources.json"),
        current_rows=current_rows, expected_added_rows=added, expected_total_rows=current_rows+added,
        audited_source_rows=source_rows, quarantined_source_rows=rejected_rows, preserved_snapshot_rows=outside_rows,
        quarantines=quarantines, audits=audit_refs, default_input_changed=False, native_published=False, snapshots_published=False,
        coverage_complete=False, implementation_sha256=sha256_file(__file__))
    export_json(args.output / "summary.json", report)
    print(json.dumps({k:report[k] for k in ("status", "source_count", "expected_added_rows", "expected_total_rows", "quarantined_source_rows")}), flush=True)


if __name__ == "__main__":
    main()
