"""Restore authoritative bundles across the four reviewed issuers' retained filing spans."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock
from engine.extractors.sec_filings import download_us_filing_htmls


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    scope = DATA_LAKE.silver('survivorship', 'financial_research', 'us_filing_bundle_coverage_20260911')
    inventory = scope / 'retained_accession_inventory.parquet'
    requested = pd.read_parquet(inventory)
    path = scope / 'collection.json'
    if path.exists():
        raise ValueError('This run already has a collection record; inspect its state before resuming')
    root = DATA_LAKE.bronze('sec', 'fillings')
    symbols = ['ALXN', 'ATVI', 'CELG', 'TWTR']
    originals = [p for form in ['10-K', '10-Q', '10-K_A', '10-Q_A'] for symbol in symbols
        for p in (root / form / symbol).rglob('*') if p.is_file()]
    before = {str(p): digest(p) for p in originals}
    export_json(scope / 'inputs_before_collection.json', before)
    report = dict(status='collecting', inventory_sha256=digest(inventory), symbols={},
        coverage_scope='All 10-K/10-Q and amendments within each retained-accession filing-date span',
        whole_market_complete=False, full_price_history_financial_coverage=False,
        production_normalization_changed=False, native_published=False, snapshots_published=False)
    export_json(path, report)
    with SourceRefreshLock('us'):
        for symbol in symbols:
            rows = requested[requested.symbol.eq(symbol)]
            bounds = dict(start_date=rows.filed.min(), end_date=rows.filed.max())
            report['symbols'][symbol] = dict(status='collecting', **bounds)
            export_json(path, report)
            result = download_us_filing_htmls(symbols=[symbol], forms=['10-K', '10-Q'], workers=1,
                output_dir=root, **bounds)
            report['symbols'][symbol].update(status='download_finished', result=json.loads(json.dumps(asdict(result), default=str)))
            export_json(path, report)
            print(json.dumps(dict(symbol=symbol, seen=result.filings_seen, new_bundles=result.filing_bundles_written,
                xbrl_files=result.xbrl_files_written, errors=result.errors)), flush=True)
        observed = {}
        failures = []
        for form in ['10-K', '10-Q', '10-K_A', '10-Q_A']:
            for symbol in symbols:
                for manifest_path in (root / form / symbol).glob('*/filing.json'):
                    try:
                        manifest = json.loads(manifest_path.read_bytes())
                        assert manifest['ticker'] == symbol
                        for document in manifest['xbrl_documents'] + [manifest['primary_document_metadata']]:
                            filename = document['document_name']
                            assert Path(filename).name == filename
                            document_path = manifest_path.parent / filename
                            assert digest(document_path) == document['sha256']
                        key = (symbol, manifest['accession_number'])
                        assert key not in observed
                        observed[key] = dict(path=str(manifest_path), sha256=digest(manifest_path),
                            form=manifest['form'], filed=manifest['filing_date'],
                            xbrl_documents=len(manifest['xbrl_documents']),
                            has_instance=any(item['role']=='instance' for item in manifest['xbrl_documents']))
                    except Exception as exc:
                        failures.append(dict(path=str(manifest_path), error=str(exc)))
        rows = []
        for item in requested.to_dict('records'):
            source = observed.get((item['symbol'], item['accn']))
            rows.append({**item, 'present_after': source is not None,
                'form_and_date_match': bool(source and source['form']==item['form'] and source['filed']==item['filed']),
                'has_instance_after': bool(source and source['has_instance'])})
        final = pd.DataFrame(rows)
        final.to_parquet(scope / 'retained_accession_coverage_after.parquet', index=False)
        changed = [p for p, value in before.items() if digest(p) != value]
        report.update(status='collected_requires_source_scope_review', verified_bundles=len(observed),
            retained_accessions=len(final), retained_present=int(final.present_after.sum()),
            retained_missing=int((~final.present_after).sum()),
            retained_without_instance=int((~final.has_instance_after).sum()),
            form_or_date_mismatches=int((~final.form_and_date_match).sum()),
            changed_existing_files=changed, file_validation_failures=failures,
            bundle_manifests=[dict(symbol=key[0], accession=key[1], **value) for key, value in sorted(observed.items())],
            input_pins=before, runner_sha256=digest(__file__))
        export_json(path, report)
        if changed or failures or report['retained_missing'] or report['form_or_date_mismatches']:
            raise RuntimeError('Collection audit requires review; evidence is preserved')
        print(json.dumps({key: report[key] for key in ['status', 'verified_bundles', 'retained_accessions',
            'retained_present', 'retained_missing', 'retained_without_instance']}), flush=True)


if __name__ == '__main__':
    main()
