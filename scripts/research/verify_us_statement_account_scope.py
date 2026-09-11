"""Verify staged SEC normalization through all three public factor bases."""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import sys
import warnings

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.source_storage import SourceRefreshLock
from engine.transformers.factors import create_stock_factor_dataframe
from audit_kr_historical_share_dependencies import ReadEvidence


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-scope', default='public_factors')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9_]+', args.run_scope):
        parser.error('--run-scope must contain only lowercase letters, digits and underscores')
    root = ROOT / 'data-lake/silver/survivorship/financial_research'
    staged = root / 'us_statement_account_scope_v5_20260911/full_normalization'
    output = root / 'us_statement_account_scope_20260911' / args.run_scope
    output.mkdir(exist_ok=False)
    normalized = json.loads((staged / 'summary.json').read_bytes())
    assert normalized['status'] == 'normalized_bundles_account_scope_review_required'
    pins = dict(normalized['output_pins'])
    for path in [Path(__file__), *[ROOT / 'engine/transformers/_internal' / name for name in
        ['sec_filings.py', 'sec_accession_history.py', 'financial_history.py', 'filing_periods.py', 'factor_metrics.py']]]:
        pins[str(path)] = digest(path)
    days = dict(ALXN='2015-08-03', ATVI='2011-02-28', CELG='2010-08-05', TWTR='2018-05-04')
    for symbol in days:
        price = ROOT / 'data-lake/silver/corporate_actions/prices/us' / f'us_{symbol}.parquet'
        metadata = price.with_suffix('.metadata.json')
        info = json.loads(metadata.read_bytes())
        assert info['status'] == 'ready' and info['price_provider'] == 'ALPHA_VANTAGE'
        assert info['price_basis'] == 'split_only' and info['symbol'] == symbol
        original = Path(info['source_path']).resolve()
        assert original.is_relative_to((ROOT / 'data-lake/bronze').resolve())
        assert digest(original) == info['source_sha256']
        pins.update({str(p): digest(p) for p in [price, metadata, original]})
    checks = {('ALXN', 'quarterly', 'sale'): 636210000,
        ('ATVI', 'annual', 'sale'): 4447000000, ('ATVI', 'ttm', 'sale'): 4447000000,
        # Annual 10-K's quarterly table reports 1,427m directly. Do not replace
        # it with 1,428m obtained by differencing rounded annual/YTD disclosures.
        ('ATVI', 'quarterly', 'sale'): 1427000000, ('ATVI', 'annual', 'dp'): 198000000,
        ('CELG', 'quarterly', 'xrd'): 342761000, ('TWTR', 'quarterly', 'ni'): 60997000}
    evidence = ReadEvidence(output)
    sys.addaudithook(evidence.hook)
    warnings.filterwarnings('ignore', category=pd.errors.PerformanceWarning)
    warnings.filterwarnings('ignore', category=FutureWarning)
    report = dict(status='running', cases=[], checks=[], input_pins=pins,
        native_published=False, snapshots_published=False, all_account_semantics_verified=False)
    def save():
        (output / 'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False), 'utf-8')
    save()
    with SourceRefreshLock('us'):
        try:
            for symbol, day in days.items():
                for basis in ['annual', 'quarterly', 'ttm']:
                    frame = create_stock_factor_dataframe(symbol, market='us', financial_basis=basis,
                        start_date=day, end_date=day, financial_dir=staged / 'normalized',
                        report_metadata_path=staged / 'metadata.csv', use_edgartools=False,
                        require_report_metadata=True, financial_availability_delay_days=1,
                        requested_factor_ids=['sale', 'dp', 'ni', 'xrd'], wacc_online_backfill=False)
                    assert len(frame) == 1 and str(frame.trade_date.iloc[0].date()) == day
                    path = output / f'{symbol}_{basis}.parquet'
                    frame[['trade_date', 'sale', 'dp', 'ni', 'xrd']].to_parquet(path, index=False)
                    for (expected_symbol, expected_basis, factor), expected in checks.items():
                        if (expected_symbol, expected_basis) == (symbol, basis):
                            actual = float(frame[factor].iloc[0])
                            report['checks'].append(dict(symbol=symbol, basis=basis, factor=factor,
                                expected=expected, actual=actual if pd.notna(actual) else None))
                            assert actual == expected, (symbol, basis, factor, actual, expected)
                    report['cases'].append(dict(symbol=symbol, basis=basis, day=day,
                        path=str(path), sha256=digest(path)))
                    save()
                    print(symbol, basis, 'verified', flush=True)
            for path, stat in evidence.snapshot().items():
                current = Path(path).stat()
                assert dict(size=current.st_size, mtime_ns=current.st_mtime_ns) == stat, path
                pins[path] = digest(path)
            assert all(digest(path) == value for path, value in pins.items())
            report['status'] = 'public_bases_verified_selected_primary_anchors_only'
        except BaseException as error:
            report.update(status='failed', error_type=type(error).__name__, error=str(error))
            raise
        finally:
            evidence.enabled = False
            save()


if __name__ == '__main__':
    main()
