"""Restore newly source-bound financials using the public normalization path."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from engine.core.serving_storage import export_csv, export_json
from engine.transformers.sec_filings import normalize_us_sec_filings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_bytes())
    for name in ('listing_collection_ledger', 'ticker_map'):
        item = plan[name]
        if sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Financial plan artifact hash mismatch')
    ledger = pd.read_parquet(plan['listing_collection_ledger']['path'])
    bound = ledger.loc[ledger.collection_status.eq('ready') & ledger.mapping_evidence.map(
        lambda value: any(edge['mapping_kind'] == 'sec_submissions_ticker_and_name'
            for edge in json.loads(value)))].copy()
    if bound.empty:
        raise ValueError('No newly reported ticker bindings to normalize')
    symbols = sorted(bound.symbol)
    # Scope the CIK-to-symbol mapping to this run, preserving all selected
    # classes while preventing unrequested aliases from becoming outputs.
    mapping = pd.read_csv(plan['ticker_map']['path'], dtype=str, keep_default_na=False)
    mapping = mapping.loc[mapping.ticker.isin(symbols)]
    args.output.mkdir(parents=True, exist_ok=True)
    selected = export_csv(args.output / 'ticker_map.csv', mapping)
    print(json.dumps(dict(symbols=symbols, listing_states=bound.state.value_counts().to_dict(),
        plan_generation=plan['generation'])), flush=True)
    files = normalize_us_sec_filings(symbols=symbols, start_year=1994,
        end_year=int(plan['as_of'][:4]), ticker_map_path=selected['path'],
        output_dir=args.output / 'normalized', report_metadata_path=args.output / 'report_metadata.csv',
        workers=args.workers, progress_interval=10, use_filings=True, use_notes=True,
        use_edgartools=False, replace_existing=True)
    outputs = []
    for path in files:
        frame = pd.read_csv(path, keep_default_na=False)
        outputs.append(dict(path=str(path.resolve()), sha256=sha256(path.read_bytes()).hexdigest(), rows=len(frame)))
    result = dict(plan_generation=plan['generation'], symbols=symbols, requested_symbols=len(symbols),
        ticker_map=selected, outputs=outputs, normalized_files=len(files),
        native_financial_rows_published=0, financial_history_verified=False,
        status='normalized_pending_source_and_period_validation')
    export_json(args.output / 'normalization_result.json', result)
    export_json(args.gold / 'normalization_result.json', result)
    print(json.dumps(dict(normalized_files=len(files), normalized_rows=sum(item['rows'] for item in outputs))), flush=True)


if __name__ == '__main__':
    main()
