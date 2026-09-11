"""Verify actual DART halt notices through the regular public collection workflow."""
import hashlib
import io
import json
from pathlib import Path
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.workflows.survivorship import run_survivorship_refresh


def main():
    base = DATA_LAKE.silver("survivorship", "financial_research", "kr_trading_halt_source_review_20260911")
    output = base / "regular_collection"
    if output.exists():
        raise ValueError("Use a new verification output")
    previous = []
    for name in ("collection.json", "supplementary_documents.json"):
        previous.extend(json.loads((base / name).read_bytes())["documents"])
    sources = {row["receipt"]: row for row in previous}
    results = []
    for day, receipt in (("2024-02-02", "20240202800801"), ("2025-02-11", "20250211800532")):
        original = sources[receipt]
        if hashlib.sha256(Path(original["source_path"]).read_bytes()).hexdigest() != original["source_sha256"]:
            raise ValueError("The reviewed original changed")
        bronze = DATA_LAKE.bronze("dart", "listings", "regular_halt_collection_20260911", day)
        gold = DATA_LAKE.gold("survivorship", "pipeline_verification", "20260911_trading_halt_collection", day)
        summary = run_survivorship_refresh(market="kr", start_date=day, end_date=day, source_dir=bronze,
            manifest_path=output / "not_reviewed.json", output_dir=output / day, gold_dir=gold,
            download=True, load_clickhouse=False)
        report_path = Path(summary["collection"]["report_path"])
        report = json.loads(report_path.read_bytes())
        candidates = [row for row in report["candidates"] if row["rcept_no"] == receipt]
        if len(candidates) != 1 or candidates[0]["document_status"] != "available":
            raise ValueError("The regular collector did not retain the target halt notice")
        candidate = candidates[0]
        raw = (bronze / candidate["document_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != candidate["document_sha256"]:
            raise ValueError("Collected archive hash mismatch")
        with ZipFile(io.BytesIO(raw)) as archive:
            members = [name for name in archive.namelist() if Path(name).name == receipt + ".xml"]
            if len(members) != 1 or hashlib.sha256(archive.read(members[0])).hexdigest() != original["source_sha256"]:
                raise ValueError("The regular collector's original differs from the independent issuer source")
        if summary["status"] != "awaiting_review" or (gold / "trading_halts.json").exists():
            raise ValueError("Source collection must not approve a halt interval")
        results.append(dict(date=day, receipt=receipt, candidate_count=len(report["candidates"]),
            original_sha256=original["source_sha256"], report=str(report_path),
            report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(), status="verified_pending_review"))
        export_json(output / "verification.json", dict(status="running", dates=results, production_changed=False))
        print(results[-1], flush=True)
    result = dict(status="actual_regular_halt_collection_verified", dates=results,
                  production_changed=False, registered_halts=0, coverage_complete=False)
    artifact = export_json(output / "verification.json", result)
    export_json(DATA_LAKE.gold("survivorship", "pipeline_verification", "20260911_trading_halt_collection", "summary.json"),
                {**result, "silver_verification": artifact})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(dict(status="failed", error_type=type(exc).__name__))
        sys.exit(1)
