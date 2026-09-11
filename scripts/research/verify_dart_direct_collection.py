"""Verify direct public DART collection and CLI version selection with real sources."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.extractors.stock_splits import download_dart_splits


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-scope", default="direct_validation_20260911")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_]+", args.run_scope):
        raise ValueError("run scope must contain only lowercase letters, digits, and underscores")
    output = DATA_LAKE.silver("survivorship", "financial_research", f"dart_{args.run_scope}")
    target = output / "actual_collection.json"
    if target.exists():
        raise ValueError("Preserve the previous verification; choose a distinct run")
    bronze = DATA_LAKE.bronze("dart", "corporate_actions", args.run_scope)
    fixtures = DATA_LAKE.bronze("fixtures", "dart_public_document")
    expected = {"20180131800068": fixtures / "samsung_original/full.html",
                "20180316800856": fixtures / "html/full.html"}
    original_pins = {str(path): digest(path) for path in expected.values()}
    report = dict(status="collecting", started_at=datetime.now(timezone.utc).isoformat(),
                  original_pins=original_pins, production_registry_changed=False,
                  production_prices_changed=False, cases=[])
    export_json(target, report)
    result = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                  output_dir=bronze)
    report["collection"] = result
    export_json(target, report)
    assert not result["errors"] and result["downloaded_or_cached"] == 2, result
    approved = bronze / "disclosures/005930"
    retained_pins = {}
    for receipt, original in expected.items():
        body = approved / f"{receipt}.html"
        sidecar = body.with_suffix(".html.metadata.json")
        metadata = json.loads(sidecar.read_bytes())
        assert metadata["provider"] == "DART" and metadata["source_validation"] == "verified"
        assert metadata["source_sha256"] == digest(body) == digest(original)
        assert set(metadata["family"]) == set(expected)
        raw = Path(metadata["retained_viewer_path"])
        assert raw.resolve().is_relative_to(bronze.resolve()) and digest(raw) == digest(body)
        for path in (body, sidecar, raw, raw.with_suffix(".html.metadata.json")):
            retained_pins[str(path)] = digest(path)
    repeated = download_dart_splits(symbols=["005930"], start_date="20180316", end_date="20180316",
                                    output_dir=bronze)
    assert not repeated["errors"]
    # A cached index names the latest document; all already acquired family files survive.
    assert all(digest(path) == value for path, value in retained_pins.items())
    report.update(repeated_collection=repeated, retained_pins=retained_pins)
    program = """
import runpy,sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv.pop(1)))
runpy.run_module('engine.workflows.stock_splits',run_name='__main__')
"""
    with tempfile.TemporaryDirectory(prefix="arcana-direct-dart-") as temporary:
        lake = Path(temporary).resolve()
        assert lake.is_relative_to(Path(tempfile.gettempdir()).resolve())
        destination = lake / "bronze/dart/stock_splits/disclosures/005930"
        destination.mkdir(parents=True)
        for receipt in expected:
            for suffix in (".html", ".html.metadata.json"):
                shutil.copy2(approved / f"{receipt}{suffix}", destination / f"{receipt}{suffix}")
        for day, receipt in [("2018-03-15", "20180131800068"), ("2018-03-16", "20180316800856")]:
            completed = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake),
                "--market", "kr", "--symbols", "005930", "--end-date", day,
                "--skip-download", "--skip-prices"], cwd=ROOT, capture_output=True,
                text=True, encoding="utf-8", timeout=60)
            if completed.returncode:
                raise RuntimeError(completed.stdout + completed.stderr)
            gold = json.loads((lake / "gold/corporate_actions/kr/stock_splits.json").read_bytes())
            candidates = [event["source_id"] for item in gold["review"]
                          for event in item.get("candidate_events", [])]
            assert candidates == [receipt], (day, candidates)
            assert gold["events"] == []
            report["cases"].append(dict(as_of=day, selected_source=receipt,
                scope="Real split proposal version selection; execution and prices are not approved"))
            export_json(target, report)
    assert all(digest(path) == value for path, value in {**original_pins, **retained_pins}.items())
    report.update(status="verified", source_files_unchanged=True,
        implementation_pins={str(ROOT / name): digest(ROOT / name) for name in
            ("engine/extractors/stock_splits.py", "engine/workflows/stock_splits.py")},
        verifier_sha256=digest(__file__))
    shutil.copy2(__file__, output / Path(__file__).name)
    export_json(target, report)
    print("Direct DART source bytes, cached preservation, and two CLI date selections verified", flush=True)


if __name__ == "__main__":
    main()
