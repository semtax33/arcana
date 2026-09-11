"""Replay retained Alpha listing snapshots through the public refresh workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
from engine.core.serving_storage import export_json
from engine.workflows.survivorship import run_survivorship_refresh


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.gold.exists():
        raise ValueError("Use new evidence and Gold directories for this verification run")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    # Verify the independent audit and original sources before replaying.
    for source in baseline["references"].values():
        if digest(source["path"]) != source["sha256"]:
            raise ValueError("Independent original-source hash changed")
    for name, expected in baseline["artifacts"].items():
        if digest(args.baseline.parent / name) != expected:
            raise ValueError("Independent audit artifact changed")

    result = run_survivorship_refresh(
        market="us", end_date=args.as_of, source_dir=args.sources,
        manifest_path=args.output / "not_reviewed.json", output_dir=args.output / "public_refresh",
        gold_dir=args.gold, download=True, load_clickhouse=False,
    )
    quality_path = Path(result["listing_source_quality"]["path"])
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if digest(quality_path) != result["listing_source_quality"]["sha256"]:
        raise AssertionError("Public audit artifact hash mismatch")
    keys = ["symbol", "exchange", "assetType", "ipoDate"]
    comparisons = (
        ("changed_delisting_dates", "moving_delisting_dates.parquet",
         {"delistingDate_previous": "previous_delisting_date", "delistingDate_current": "current_delisting_date"}),
        ("changed_historical_names", "changing_historical_names.parquet",
         {"name_previous": "previous_name", "name_current": "current_name"}),
        ("same_provider_key_in_active_and_delisted", "same_identity_in_current_active_and_delisted.parquet", {}),
    )
    checks = {}
    for category, name, rename in comparisons:
        expected = pd.read_parquet(args.baseline.parent / name).rename(columns=rename)
        columns = keys + list(rename.values())
        expected = expected[columns].sort_values(keys).reset_index(drop=True)
        actual = pd.DataFrame(quality[category], columns=columns).sort_values(keys).reset_index(drop=True)
        pd.testing.assert_frame_equal(actual.astype(str), expected.astype(str), check_dtype=False)
        checks[category] = len(actual)
    if result["status"] != "awaiting_review" or result["coverage_complete"] is not False:
        raise AssertionError("Source-only replay cannot approve lifecycle coverage")
    for name in ("listing_episodes.json", "events.json", "entitlements.json"):
        if (args.gold / name).exists():
            raise AssertionError("Unreviewed lifecycle facts were published")
    implementation = args.output / "implementation"
    implementation.mkdir(parents=True)
    code_hashes = {}
    for relative in (
        "engine/workflows/survivorship.py", "engine/transformers/listing_source_quality.py",
        "engine/extractors/alpha_vantage_prices.py", "scripts/research/verify_us_listing_source_quality_pipeline.py",
        "tests/test_survivorship_workflow.py",
    ):
        source = ROOT / relative
        target = implementation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        code_hashes[relative] = digest(source)
    report = {
        "status": "actual_public_pipeline_matches_independent_source_audit", "as_of": args.as_of,
        "checks": checks, "duplicate_records": len(quality["duplicate_records"]),
        "ambiguous_provider_keys": len(quality["ambiguous_provider_keys"]),
        "baseline": str(args.baseline.resolve()), "baseline_sha256": digest(args.baseline),
        "source_quality": result["listing_source_quality"],
        "silver_audit": result["collection"]["source_quality_audit"], "implementation": code_hashes,
        "database_changed": False, "registered_identities": 0, "coverage_complete": False,
    }
    artifact = export_json(args.output / "verification.json", report)
    export_json(args.gold / "verification.json", {**report, "silver_verification": artifact})
    print(json.dumps({"status": report["status"], "checks": checks,
                      "duplicate_records": report["duplicate_records"], "report": artifact}, ensure_ascii=False))


if __name__ == "__main__":
    main()
