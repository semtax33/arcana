"""Normalize the restored filing spans in Silver and enumerate changed accounts."""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock
from engine.transformers.sec_filings import normalize_us_sec_filings


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-scope', default='us_filing_bundle_coverage_20260911')
    parser.add_argument('--baseline', type=Path, default=DATA_LAKE.silver(
        'survivorship', 'financial_research', 'us_accession_full_factor_preparation_rnd_v4_20260911'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9_]+', args.run_scope):
        parser.error('--run-scope must contain only lowercase letters, digits and underscores')
    scope = DATA_LAKE.silver('survivorship', 'financial_research', args.run_scope)
    output = scope / 'full_normalization'
    output.mkdir(parents=True, exist_ok=False)
    baseline = args.baseline.resolve()
    collection_scope = DATA_LAKE.silver('survivorship', 'financial_research', 'us_filing_bundle_coverage_20260911')
    collection = json.loads((collection_scope / 'collection.json').read_bytes())
    sources = [Path(item['path']) for item in collection['bundle_manifests']]
    for path in sources[:]:
        manifest = json.loads(path.read_bytes())
        sources += [path.parent / item['document_name'] for item in
            manifest['xbrl_documents'] + [manifest['primary_document_metadata']]]
    sources += [DATA_LAKE.bronze('sec','companyfacts',f'CIK{cik}.json') for cik in
        ['0000899866','0000718877','0000816284','0001418091']]
    rule_manifest = DATA_LAKE.rules('semantic_us_rule_manifest.json')
    rule_path = rule_manifest.parent / json.loads(rule_manifest.read_bytes())['active_bundle']
    sources += [rule_path, rule_manifest,
        ROOT / 'engine/transformers/_internal/sec_filings.py', Path(__file__)]
    sources += list((baseline / 'normalized').glob('us_normalized_*.csv'))
    pins = {str(p): digest(p) for p in sources}
    report = dict(status='normalizing', baseline=str(baseline), input_pins=pins, production_normalization_changed=False,
        native_published=False, snapshots_published=False, coverage_complete=False)
    export_json(output / 'summary.json', report)
    with SourceRefreshLock('us'):
        normalize_us_sec_filings(symbols=['ALXN','ATVI','CELG','TWTR'], start_year=1999,end_year=2026,
            output_dir=output / 'normalized', report_metadata_path=output / 'metadata.csv',
            use_notes=False,use_edgartools=False,workers=1,save_debug=False,progress_interval=20)
        records=[]
        changes=[]
        for symbol in ['ALXN','ATVI','CELG','TWTR']:
            path=output / 'normalized' / f'us_normalized_{symbol}.csv'
            current=pd.read_csv(path)
            previous=pd.read_csv(baseline / 'normalized' / path.name)
            keys=['canonical_account_id','fiscal_year','fiscal_month']
            merged=previous.merge(current,on=keys,how='outer',suffixes=('_before','_after'),indicator=True)
            changed=merged[(merged.normalized_amount_before.fillna(-1e99)!=merged.normalized_amount_after.fillna(-1e99))
                | merged._merge.ne('both')].copy()
            changed.insert(0,'symbol',symbol)
            changes.append(changed)
            mpath=output / 'normalized/accessions' / symbol / 'manifest.json'
            manifest=json.loads(mpath.read_bytes())
            apath=mpath.parent / manifest['normalized_path']
            assert digest(apath)==manifest['normalized_sha256']
            accessions=pd.read_csv(apath,dtype=str,keep_default_na=False)
            records.append(dict(symbol=symbol,period_rows=len(current),accession_rows=len(accessions),
                accessions=accessions.accn.nunique(),changed_account_cells=len(changed),
                rows_by_source=accessions.groupby('source').size().to_dict()))
        pd.concat(changes,ignore_index=True).to_parquet(output / 'changed_account_cells.parquet',index=False)
        assert all(digest(p)==value for p,value in pins.items())
        report.update(status='normalized_bundles_account_scope_review_required',symbols=records,
            output_pins={str(p):digest(p) for p in output.rglob('*') if p.is_file() and p.name!='summary.json'},
            total_changed_account_cells=sum(item['changed_account_cells'] for item in records))
        export_json(output / 'summary.json',report)
        print(json.dumps({key: report[key] for key in ['status','symbols','total_changed_account_cells']}),flush=True)


if __name__=='__main__':
    main()
