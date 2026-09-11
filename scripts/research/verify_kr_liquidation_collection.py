"""Verify actual public collection of four observed DART liquidation documents."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.extractors.survivorship import download_survivorship_sources


def main():
    output = DATA_LAKE.silver("survivorship", "financial_research", "kr_liquidation_collection_20260911")
    report_path = output / "actual_collection.json"
    if report_path.exists():
        raise ValueError("Keep the previous verification report immutable")
    inventory = dict(status="running", dates=[], production_changed=False, coverage_complete=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    for day, receipt in [("2026-05-27", "20260527900424"), ("2026-06-08", "20260608900348"),
                         ("2026-06-09", "20260609000084"), ("2026-06-18", "20260618000252")]:
        bronze = DATA_LAKE.bronze("dart", "listings", "liquidation_collection_20260911", day)
        result = download_survivorship_sources(market="kr", start_date=day, end_date=day, output_dir=bronze)
        source_report = Path(result["report_path"])
        candidates = json.loads(source_report.read_bytes())["candidates"]
        found = [r for r in candidates if r["source_id"] == receipt]
        if len(found) != 1:
            raise ValueError(f"Observed liquidation document was not collected: {receipt}")
        candidate = found[0]
        if candidate["document_status"] != "available" or candidate["review_status"] != "pending":
            raise ValueError("Expected an available original with unapproved lifecycle status")
        raw_path = bronze / candidate["document_path"]
        if sha256_file(raw_path) != candidate["document_sha256"]:
            raise ValueError("Collected original bytes differ from provenance")
        inventory["dates"].append(dict(date=day, target_receipt=receipt, target=candidate,
            collection_report=str(source_report), collection_report_sha256=sha256_file(source_report),
            other_pending_candidates=len(candidates)-1))
        export_json(report_path, inventory)
        print(dict(date=day, receipt=receipt, status="original_collected_pending_review"), flush=True)
    implementation = output / "implementation"
    implementation.mkdir(exist_ok=True)
    archived = {}
    for name in [__file__, "engine/extractors/survivorship.py", "tests/test_survivorship_workflow.py"]:
        source = Path(name).resolve()
        target = implementation / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        archived[str(source)] = dict(path=str(target), sha256=sha256_file(target))
    inventory.update(status="four_original_documents_collected_pending_review", implementation=archived)
    export_json(report_path, inventory)


if __name__ == "__main__":
    main()
