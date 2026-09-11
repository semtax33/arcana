"""Reported statement totals survive the public SEC normalization boundary."""
from pathlib import Path
from hashlib import sha256
import json
import os
import shutil

from lxml import etree
import pandas as pd
import pytest

from engine.transformers.sec_filings import normalize_us_sec_filings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
RULE_PATH = Path(os.environ.get('ARCANA_SEC_SCOPE_RULE_UNDER_TEST',
    ROOT / 'data-lake/meta/rules/semantic_us_rule_manifest.json'))


def normalize_retained_bundle(tmp_path, symbol, form, accession, year, source_edit=None):
    """Use an immutable retained official bundle; no internal collaborators mocked."""
    relative = Path(form) / symbol / accession
    raw = tmp_path / 'bronze' / 'sec' / 'fillings'
    shutil.copytree(ROOT / 'data-lake/bronze/sec/fillings' / relative, raw / relative)
    if source_edit is not None:
        source_edit(raw / relative)
    output = tmp_path / 'silver' / 'normalized'
    normalize_us_sec_filings(symbols=[symbol], start_year=year, end_year=year,
        filings_dir=raw, companyfacts_dir=tmp_path / 'bronze' / 'sec' / 'companyfacts',
        output_dir=output, report_metadata_path=tmp_path / 'silver' / 'reports.csv',
        mapping_rule_path=RULE_PATH, use_notes=False, use_edgartools=False,
        save_debug=False, log_progress=False)
    return pd.read_csv(output / f'us_normalized_{symbol}.csv')


@pytest.mark.parametrize('symbol,form,accession,year,total', [
    ('ALXN', '10-Q', '0000899866-15-000135', 2015, 1236543000),
    ('ATVI', '10-K', '0001047469-11-001413', 2010, 4447000000),
    ('CELG', '10-Q', '0001104659-11-060043', 2011, 3558173000),
])
def test_total_revenue_includes_other_revenue_in_the_same_statement(
    tmp_path, symbol, form, accession, year, total,
):
    frame = normalize_retained_bundle(tmp_path, symbol, form, accession, year)
    revenue = frame[frame.canonical_account_id.eq('REVENUE')]
    # ALXN 2015 Q2 primary statement, six months, amounts in thousands:
    # Net product sales 1,236,316; other revenue 227; total revenues 1,236,543.
    # https://www.sec.gov/Archives/edgar/data/899866/000089986615000135/alxn6301510q.htm
    assert len(revenue) == 1
    # ATVI 2010 annual: total net revenues 4,447 (millions), including
    # subscription/licensing/other. CELG 2011 nine months: total 3,558,173
    # (thousands), including collaborative and royalty revenues.
    assert revenue.normalized_amount.item() == total


def test_cash_flow_opening_and_closing_balances_use_the_statement_scope(tmp_path):
    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019).set_index('canonical_account_id')
    # ATVI 2019 statement of cash flows (millions) includes restricted cash:
    # beginning 4,229; ending 5,798. Balance-sheet cash alone is 5,794.
    # https://www.sec.gov/Archives/edgar/data/718877/000071887720000003/atvi-12312019x10xk.htm
    assert frame.loc['CF_CASH_BEGIN', 'normalized_amount'] == 4229000000
    assert frame.loc['CF_CASH_END', 'normalized_amount'] == 5798000000


def test_a_missing_opening_cash_fact_is_not_replaced_with_closing_cash(tmp_path):
    def omit_opening_cash(folder):
        # Explicitly synthetic missing-data variant of the official bundle.
        metadata = folder / 'filing.json'
        manifest = json.loads(metadata.read_bytes())
        document = next(item for item in manifest['xbrl_documents'] if item['role'] == 'instance')
        path = folder / document['document_name']
        tree = etree.parse(str(path))
        for fact in list(tree.getroot()):
            if (etree.QName(fact).localname == 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
                    and fact.get('contextRef') == 'FI2018Q4'):
                tree.getroot().remove(fact)
        tree.write(str(path), encoding='UTF-8', xml_declaration=True)
        document.update(sha256=sha256(path.read_bytes()).hexdigest(), byte_size=path.stat().st_size)
        metadata.write_text(json.dumps(manifest), 'utf-8')

    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019, source_edit=omit_opening_cash)
    assert not frame.canonical_account_id.eq('CF_CASH_BEGIN').any()
    assert frame.loc[frame.canonical_account_id.eq('CF_CASH_END'), 'normalized_amount'].item() == 5798000000


def test_cash_reconciliation_components_do_not_replace_the_rollforward_total(tmp_path):
    frame = normalize_retained_bundle(tmp_path, 'TWTR', '10-Q',
        '0001564590-18-010739', 2018).set_index('canonical_account_id')
    # Q1 cash-flow roll-forward includes restricted cash. The supplementary
    # reconciliation separately shows cash alone 1,601,028 (thousands).
    assert 'CF_CASH_END' in frame.index
    assert frame.loc['CF_CASH_END', 'normalized_amount'] == 1630452000
    assert frame.loc['CF_CASH_BEGIN', 'normalized_amount'] == 1673857000


@pytest.mark.parametrize('symbol,form,accession,year,total', [
    ('ATVI', '10-K', '0001047469-13-001506', 2012, 1195000000),
    ('ALXN', '10-Q', '0000899866-21-000039', 2021, 528600000),
])
def test_other_comprehensive_income_does_not_replace_total_comprehensive_income(
    tmp_path, symbol, form, accession, year, total,
):
    frame = normalize_retained_bundle(tmp_path, symbol, form,
        accession, year).set_index('canonical_account_id')
    # Published consolidated statement: net income 1,149m, OCI 46m,
    # comprehensive income 1,195m. The filer uses different standard concepts.
    assert frame.loc['TOTAL_COMPREHENSIVE_INCOME', 'normalized_amount'] == total
