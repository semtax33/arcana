"""Restore the missing CELG 2010 Q2 filing through the regular SEC collector."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.extractors.sec_filings import download_us_filing_htmls


def pins(paths):
    return {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def main():
    silver = DATA_LAKE.silver('survivorship', 'financial_research', 'us_celg_rnd_scope_20260911')
    report_path = silver / 'restoration.json'
    if report_path.exists():
        raise ValueError('Restoration is already recorded; use the normal collector to resume')
    bronze = DATA_LAKE.bronze('sec', 'fillings')
    existing = [p for form in ['10-K', '10-Q'] for p in (bronze / form / 'CELG').rglob('*') if p.is_file()]
    before = pins(existing)
    export_json(silver / 'default_filing_inputs_before.json', before)
    pointer = DATA_LAKE.gold('survivorship', 'us', 'financial_rebuild_preparation', '20260911', 'summary.json')
    old_pointer = pointer.read_bytes()
    (silver / 'previous_gold_preparation.json').write_bytes(old_pointer)
    state = json.loads(old_pointer)
    state.update(status='requires_repreparation_after_celg_filing_recovery',
        prepared_with_current_sources=False, approved_for_publication=False,
        pending_source_recovery='CELG 0000950123-10-072016')
    export_json(pointer, state)
    options = dict(symbols=['CELG'], start_date='2010-08-04', end_date='2010-08-04',
        forms=['10-Q'], output_dir=bronze, workers=1)
    first = download_us_filing_htmls(**options)
    assert first.errors == 0 and first.filings_seen == 1, asdict(first)
    assert pins(existing) == before, 'An earlier filing was changed'
    folder = bronze / '10-Q/CELG/0000950123-10-072016'
    manifest_path = folder / 'filing.json'
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest['accession_number'] == '0000950123-10-072016'
    documents = manifest['xbrl_documents'] + [manifest['primary_document_metadata']]
    for doc in documents:
        assert sha256((folder / doc['document_name']).read_bytes()).hexdigest() == doc['sha256']
    current = pins(folder.iterdir())
    second = download_us_filing_htmls(**options)
    assert second.errors == 0 and second.symbols_resumed == 1, asdict(second)
    assert pins(folder.iterdir()) == current and pins(existing) == before
    result = dict(status='default_bronze_filing_restored_and_cached_rerun_verified',
        first=asdict(first), cached=asdict(second), earlier_file_pins=before,
        restored_file_pins=current, previous_gold_sha256=sha256(old_pointer).hexdigest(),
        production_normalization_changed=False, native_published=False, snapshots_published=False,
        coverage_complete=False, runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
    export_json(report_path, json.loads(json.dumps(result, default=str)))
    print(json.dumps(dict(status=result['status'], earlier_files=len(before), restored_files=len(current))), flush=True)


if __name__ == '__main__':
    main()
