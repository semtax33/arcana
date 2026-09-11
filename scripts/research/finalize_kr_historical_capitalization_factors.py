"""Verify native publication artifacts and link the completed scope in Gold."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--input-gold-summary", type=Path, required=True)
    args = parser.parse_args()
    output = args.publication.resolve().parent / "closeout"
    if not output.is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Audit output must remain in Silver")
    if not args.input_gold_summary.resolve().is_relative_to((DATA_LAKE.root / "gold").resolve()):
        raise ValueError("User-facing input summary must remain in Gold")
    output.mkdir(exist_ok=False)
    evidence = {}

    def check(path, expected=None):
        path = Path(path).resolve()
        digest = sha256_file(path)
        if expected is not None and digest != expected:
            raise ValueError(f"Artifact changed: {path}")
        evidence[str(path)] = digest
        return digest

    def read(path, expected=None):
        check(path, expected)
        return json.loads(Path(path).read_text("utf-8"))

    publication = read(args.publication)
    verification = read(args.verification)
    if not publication["native_published"] or verification["status"] != "verified":
        raise ValueError("Actual native publication and FactorLab verification must be complete")
    check(args.publication, verification["native_publication_sha256"])
    preparation = read(Path(publication["preparation"]) / "summary.json", publication["preparation_sha256"])
    for path, digest in publication["dependencies"].items():
        if digest is None:
            absent = Path(path).resolve()
            if absent.exists():
                raise ValueError(f"A pinned absent input appeared: {absent}")
            evidence[str(absent)] = None
        else:
            check(path, digest)
    years = sorted(publication["source_years"])
    scope = "_".join(map(str, years))
    if len(years) > 1 and years == list(range(years[0], years[-1] + 1)):
        scope = f"{years[0]}_{years[-1]}"
    gold = DATA_LAKE.gold("survivorship", "kr", "capitalization_factors", scope)
    gold_summary = read(gold / "summary.json")
    check(args.publication, gold_summary["publication_sha256"])
    monthly = {row["month"]: row for row in publication["months"]}
    if set(monthly) != set(gold_summary["artifacts"]) or set(monthly) != {r["month"] for r in verification["months"]}:
        raise ValueError("Gold or actual consumer coverage does not match the published months")
    for record in preparation["months"]:
        for name, digest in record["hashes"].items():
            check(Path(publication["preparation"]) / record["month"] / name, digest)
    for month, record in monthly.items():
        folder = args.publication.parent / month
        if record["status"] != "native_published_and_verified":
            raise ValueError("A native month is incomplete")
        for name, digest in record["hashes"].items():
            check(folder / name, digest)
        artifact = gold_summary["artifacts"][month]
        check(artifact["path"], artifact["sha256"])
        actual = pd.read_parquet(artifact["path"])
        if len(actual) != record["verified_rows"] or len(actual) != artifact["rows"]:
            raise ValueError("Gold row count differs from the verified native scope")
        pd.testing.assert_frame_equal(actual, pd.read_parquet(folder / "expected.parquet"), check_exact=True)
    for record in verification["months"]:
        for name, digest in record["hashes"].items():
            check(args.verification.parent / record["month"] / name, digest)
    check(args.verification.parent / "daily_counts.parquet", verification["daily_counts_sha256"])
    check(ROOT / "scripts/research/verify_kr_historical_capitalization_factorlab.py", verification["implementation_sha256"])
    check(DATA_LAKE.silver("survivorship", "kr", "listing_episodes.json"), verification["listing_episodes_sha256"])
    if sum(row["new_rows"] for row in monthly.values()) != publication["inserted_rows"]:
        raise ValueError("Inserted native counts differ from the monthly checkpoints")
    if "added_rows" in publication:
        if (sum(row["added_rows"] for row in monthly.values()) != publication["added_rows"]
                or sum(row["revised_rows"] for row in monthly.values()) != publication["revised_rows"]
                or publication["added_rows"] + publication["revised_rows"] != publication["inserted_rows"]):
            raise ValueError("Native additions or revisions differ from the monthly checkpoints")
    if sum(row["verified_rows"] for row in monthly.values()) != publication["verified_rows"]:
        raise ValueError("Native readback counts differ from monthly checkpoints")
    check(Path(__file__))
    shutil.copy2(__file__, output / Path(__file__).name)
    shutil.copy2(gold / "summary.json", output / "gold_native_summary_before.json")
    evidence.pop(str((gold / "summary.json").resolve()))
    check(output / "gold_native_summary_before.json")
    input_summary = read(args.input_gold_summary)
    shutil.copy2(args.input_gold_summary, output / "gold_input_summary_before.json")
    evidence.pop(str(args.input_gold_summary.resolve()))
    check(output / "gold_input_summary_before.json")
    result = dict(status="native_and_factorlab_verified_snapshots_pending", checked_at=datetime.now(timezone.utc).isoformat(),
        source_years=years, native_added_rows=publication.get("added_rows", publication["inserted_rows"]),
        native_revised_rows=publication.get("revised_rows", 0), native_inserted_rows=publication["inserted_rows"],
        native_verified_rows=publication["verified_rows"],
        unchanged_existing_rows=publication["unchanged_existing_rows"], factorlab_verified_days=verification["verified_days"],
        factorlab_top70_output_rows=verification["top70_output_rows"], native_restored=True, snapshots_restored=False,
        coverage_complete=False, evidence_sha256=evidence,
        policy="This market-factor scope is complete in the native table. Full share-dependent factor rebuilding and snapshot publication remain pending; no new issuer history or strategy result is approved.")
    export_json(output / "audit.json", result)
    entry = {key:value for key,value in result.items() if key != "evidence_sha256"}
    entry.update(publication_path=str(args.publication.resolve()), publication_sha256=sha256_file(args.publication),
        verification_path=str(args.verification.resolve()), verification_sha256=sha256_file(args.verification),
        closeout_path=str(output / "audit.json"), closeout_sha256=sha256_file(output / "audit.json"))
    gold_summary["consumer_verification"] = entry
    export_json(gold / "summary.json", gold_summary)
    input_summary.setdefault("market_factor_scopes", {})[scope] = entry
    export_json(args.input_gold_summary, input_summary)
    print(json.dumps({k:result[k] for k in ("status", "native_added_rows", "native_verified_rows", "factorlab_verified_days")}), flush=True)


if __name__ == "__main__":
    main()
