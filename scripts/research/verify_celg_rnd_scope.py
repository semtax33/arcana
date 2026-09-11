"""Compare an actual CELG disclosure with public normalization and daily factors."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers.factors import create_stock_factor_dataframe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--filings-dir', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((DATA_LAKE.root / 'silver').resolve()) or output.exists():
        raise ValueError('Use a new Silver output directory')
    output.mkdir(parents=True)
    kwargs = dict(filings_dir=args.filings_dir) if args.filings_dir else {}
    normalize_us_sec_filings(symbols=['CELG'], start_year=2009, end_year=2010,
        output_dir=output / 'normalized', report_metadata_path=output / 'metadata.csv',
        use_notes=False, use_edgartools=False, workers=1, **kwargs)
    manifest_path = output / 'normalized/accessions/CELG/manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    rows = pd.read_csv(manifest_path.parent / manifest['normalized_path'])
    selected = rows[(rows.accn == '0000950123-10-072016') & (rows.canonical_account_id == 'RND')]
    selected.to_parquet(output / 'selected_rnd.parquet', index=False)
    daily = create_stock_factor_dataframe('CELG', market='us', financial_basis='quarterly',
        start_date='2010-08-03', end_date='2010-08-06', financial_dir=output / 'normalized',
        report_metadata_path=output / 'metadata.csv', use_edgartools=False,
        require_report_metadata=True, financial_availability_delay_days=1,
        requested_factor_ids=['xrd'], wacc_online_backfill=False)
    daily.to_parquet(output / 'daily.parquet', index=False)
    current = daily[pd.to_datetime(daily.trade_date) >= '2010-08-05']
    record = dict(expected_quarter=342761000, expected_ytd=547418000,
        observed_quarter=current.xrd.tolist(), selected=selected.astype(object).where(pd.notna(selected), None).to_dict('records'),
        production_changed=False, runner_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
    record['status'] = 'verified' if len(current) == 2 and current.xrd.eq(342761000).all() else 'source_scope_mismatch'
    (output / 'result.json').write_text(json.dumps(record, indent=2, default=str, allow_nan=False), 'utf-8')
    print(json.dumps({key: record[key] for key in ['status', 'expected_quarter', 'observed_quarter']}), flush=True)
    assert record['status'] == 'verified', 'Quarter R&D must match the primary income statement total'


if __name__ == '__main__':
    main()
