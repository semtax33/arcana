"""Replay retained API/XML and public/HTML versions through the real refresh CLI."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json


def main():
    output = DATA_LAKE.silver("survivorship", "financial_research", "split_correction_family_20260911")
    target = output / "actual_mixed_sources.json"
    if target.exists():
        raise ValueError("Preserve the previous verification; choose a new reviewed run")
    sources = [
        DATA_LAKE.bronze("dart", "corporate_actions", "response_retention_20260911",
                         "disclosures", "204210", "20170515001024.html"),
        DATA_LAKE.bronze("dart", "corporate_actions", "public_document_pipeline_20260911",
                         "disclosures", "204210", "20170518000045.html"),
    ]
    pins = {}
    for source in sources:
        sidecar = source.with_suffix(".html.metadata.json")
        metadata = json.loads(sidecar.read_bytes())
        assert metadata["security_id"] == "SEC_KR_204210"
        assert metadata["source_sha256"] == sha256(source.read_bytes()).hexdigest()
        for path in (source, sidecar):
            pins[str(path)] = sha256(path.read_bytes()).hexdigest()
    report = dict(status="verifying", started_at=datetime.now(timezone.utc).isoformat(),
        source_pins=pins, cases=[], production_registry_changed=False,
        financial_values_published=False)
    with tempfile.TemporaryDirectory(prefix="arcana-dart-correction-") as temporary:
        lake = Path(temporary).resolve()
        assert lake.is_relative_to(Path(tempfile.gettempdir()).resolve())
        folder = lake / "bronze/dart/stock_splits/disclosures/204210"
        folder.mkdir(parents=True)
        for source in sources:
            (folder / source.name).write_bytes(source.read_bytes())
            sidecar = source.with_suffix(".html.metadata.json")
            (folder / sidecar.name).write_bytes(sidecar.read_bytes())
        program = """
import runpy,sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv.pop(1)))
runpy.run_module('engine.workflows.stock_splits',run_name='__main__')
"""
        for day, expected in [("2017-05-17", "20170515001024"),
                              ("2017-05-18", "20170518000045"),
                              ("2017-05-18", "20170518000045")]:
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake),
                "--market", "kr", "--symbols", "204210", "--end-date", day,
                "--skip-download", "--skip-prices"], cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8", timeout=60)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            gold = json.loads((lake / "gold/corporate_actions/kr/stock_splits.json").read_bytes())
            selected = [item["source_id"] for item in gold["review"]
                        if item.get("source_id") in {"20170515001024", "20170518000045"}]
            assert selected == [expected], (day, selected)
            assert gold["events"] == []
            report["cases"].append(dict(as_of=day, selected_source=selected[0],
                events=0, scope="Source-version selection only; these are financial reports, not split decisions"))
            export_json(target, report)
            print(day, selected[0], "verified", flush=True)
    assert all(sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in pins.items())
    report.update(status="verified", source_files_unchanged=True,
        implementation_sha256=sha256((ROOT / "engine/workflows/stock_splits.py").read_bytes()).hexdigest(),
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
    (output / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    export_json(target, report)


if __name__ == "__main__":
    main()
