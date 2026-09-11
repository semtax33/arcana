"""The restored issuer's real SEC bundle must reach the public daily calculation."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pandas as pd
import pytest

pytestmark = pytest.mark.integration
RULE_PATH = Path(os.environ.get('ARCANA_SEC_SCOPE_RULE_UNDER_TEST',
    'data-lake/meta/rules/semantic_us_rule_manifest.json')).resolve()


def test_celg_rnd_uses_the_statement_total_after_official_source_recovery(tmp_path):
    output = tmp_path / 'silver' / 'celg_rnd'
    output.mkdir(parents=True)
    program = '''
import sys
from pathlib import Path
from engine.transformers.sec_filings import normalize_us_sec_filings
from engine.transformers.factors import create_stock_factor_dataframe
out=Path(sys.argv[1])
normalize_us_sec_filings(symbols=['CELG'], start_year=2009, end_year=2010,
    output_dir=out/'normalized', report_metadata_path=out/'metadata.csv',
    mapping_rule_path=Path(sys.argv[2]),
    use_notes=False, use_edgartools=False, workers=1, save_debug=False)
frame=create_stock_factor_dataframe('CELG', market='us', financial_basis='quarterly',
    start_date='2010-08-03', end_date='2010-08-06', financial_dir=out/'normalized',
    report_metadata_path=out/'metadata.csv', use_edgartools=False,
    require_report_metadata=True, financial_availability_delay_days=1,
    requested_factor_ids=['xrd'], wacc_online_backfill=False)
frame[['trade_date','xrd']].to_parquet(out/'daily.parquet', index=False)
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-W', 'ignore', '-c', program, str(output), str(RULE_PATH)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    frame = pd.read_parquet(output / 'daily.parquet').set_index('trade_date')
    # Independent primary statement amounts: Q1 204,657,000; Q2 342,761,000.
    # August 4 publication becomes usable on August 5 under the declared delay.
    assert len(frame) == 4
    assert frame.loc[pd.Timestamp('2010-08-04'), 'xrd'] == 204657000
    assert frame.loc[pd.Timestamp('2010-08-05'), 'xrd'] == 342761000
    assert frame.loc[pd.Timestamp('2010-08-06'), 'xrd'] == 342761000


def test_celg_other_current_liabilities_preserve_the_reported_balance_sheet_line(tmp_path):
    output = tmp_path / 'silver' / 'celg_liabilities'
    program = '''
import sys
from pathlib import Path
from engine.transformers.sec_filings import normalize_us_sec_filings
out=Path(sys.argv[1])
normalize_us_sec_filings(symbols=['CELG'], start_year=2010, end_year=2010,
    output_dir=out/'normalized', report_metadata_path=out/'metadata.csv',
    mapping_rule_path=Path(sys.argv[2]),
    use_notes=False, use_edgartools=False, workers=1, save_debug=False)
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-W', 'ignore', '-c', program, str(output), str(RULE_PATH)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    frame = pd.read_csv(output / 'normalized/us_normalized_CELG.csv')
    row = frame[(frame.canonical_account_id == 'OTHER_CURRENT_LIABILITIES') & (frame.fiscal_month == 6)]
    # June 30 balance sheet reports these as three separate lines, in USD:
    # other current liabilities 86,564,000; accrued expenses 307,965,000;
    # income taxes payable 9,013,000.
    assert len(row) == 1
    assert row.normalized_amount.item() == 86564000


@pytest.mark.parametrize('reported_tag', ['AccruedLiabilitiesCurrent', 'AccruedIncomeTaxesCurrent'])
def test_an_accrual_or_tax_balance_does_not_stand_in_for_other_current_liabilities(tmp_path, reported_tag):
    from engine.transformers.sec_filings import normalize_us_sec_filings
    source = tmp_path / 'bronze' / 'sec' / 'companyfacts'
    source.mkdir(parents=True)
    facts = {tag: dict(label=tag, units=dict(USD=[dict(end='2020-06-30', val=amount,
        accn='0000999990-20-000001', fy=2020, fp='Q2', form='10-Q', filed='2020-08-01')]))
        for tag, amount in [('Assets', 100), (reported_tag, 10)]}
    (source / 'CIK0000999990.json').write_text(json.dumps(dict(cik=999990,
        entityName='Synthetic liability scope', facts={'us-gaap': facts})), 'utf-8')
    metadata = tmp_path / 'silver' / 'tickers.csv'
    metadata.parent.mkdir(parents=True)
    pd.DataFrame([dict(cik=999990, ticker='999990', title='Synthetic liability scope')]).to_csv(metadata, index=False)
    output = tmp_path / 'silver' / 'normalized'
    normalize_us_sec_filings(symbols=['999990'], start_year=2020, end_year=2020,
        companyfacts_dir=source, ticker_map_path=metadata, output_dir=output,
        report_metadata_path=tmp_path / 'silver' / 'reports.csv', mapping_rule_path=RULE_PATH,
        use_filings=False, use_notes=False, use_edgartools=False, save_debug=False, log_progress=False)
    rows = pd.read_csv(output / 'us_normalized_999990.csv')
    assert not rows.canonical_account_id.eq('OTHER_CURRENT_LIABILITIES').any()
