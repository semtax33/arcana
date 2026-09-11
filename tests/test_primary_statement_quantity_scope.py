"""Primary statement totals reach the public factor calculation without overlap."""
import json
import re
from hashlib import sha256
import pandas as pd
import pytest
from lxml import etree

from engine.transformers.factors import create_stock_factor_dataframe
from test_us_filing_account_scope import normalize_retained_bundle
from test_us_complete_account_components import edit_instance, normalize_atvi_companyfacts

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('symbol,accession,year,day,reported_total', [
    ('ALXN', '0001445305-12-000363', 2011, '2012-02-22', 17616000),
    ('TWTR', '0001418091-22-000029', 2021, '2022-02-17', 544848000),
    ('ATVI', '0001047469-11-001413', 2010, '2011-02-28', 198000000),
])
def test_cash_flow_total_is_not_added_to_its_note_components(
    tmp_path, symbol, accession, year, day, reported_total,
):
    # Independent totals from the original statements of cash flows. The
    # examples cover a combined tag, a tag reused for a rounded note component,
    # and a previously correct total. No issuer branch is part of the rule.
    normalize_retained_bundle(tmp_path, symbol, '10-K', accession, year)
    frame = create_stock_factor_dataframe(symbol, market='us', financial_basis='annual',
        start_date=day, end_date=day, financial_dir=tmp_path / 'silver/normalized',
        report_metadata_path=tmp_path / 'silver/reports.csv', use_edgartools=False,
        require_report_metadata=True, financial_availability_delay_days=1,
        requested_factor_ids=['dp'], wacc_online_backfill=False)
    assert len(frame) == 1
    assert frame.dp.iloc[0] == reported_total


def test_missing_statement_total_and_missing_depreciation_do_not_make_amortization_the_total(tmp_path):
    def remove_depreciation(root):
        for fact in list(root):
            if 'Depreciation' in etree.QName(fact).localname:
                root.remove(fact)
    normalize_retained_bundle(tmp_path, 'ATVI', '10-K', '0000718877-20-000003', 2019,
        source_edit=lambda folder: edit_instance(folder, remove_depreciation))
    frame = create_stock_factor_dataframe('ATVI', market='us', financial_basis='annual',
        start_date='2020-02-28', end_date='2020-02-28',
        financial_dir=tmp_path / 'silver/normalized', report_metadata_path=tmp_path / 'silver/reports.csv',
        use_edgartools=False, require_report_metadata=True, financial_availability_delay_days=1,
        requested_factor_ids=['dp'], wacc_online_backfill=False)
    assert len(frame) == 1
    assert pd.isna(frame.dp.iloc[0])


@pytest.mark.parametrize('tag,wrong_account', [
    ('DepreciationDepletionAndAmortization', 'DEPRECIATION_EXPENSE'),
    ('AmortizationOfFinancingCosts', 'AMORTIZATION'),
])
def test_different_quantity_types_are_not_interchangeable_aliases(tmp_path, tag, wrong_account):
    def only_the_other_quantity(payload):
        # A valid, unrelated fact keeps the public output nonempty when the
        # other quantity is correctly withheld.
        assets = payload['facts']['us-gaap']['Assets']
        payload['facts'] = {'us-gaap': {'Assets': assets, tag: dict(label=tag, units={'USD': [dict(
            start='2019-01-01', end='2019-12-31', val=20000000,
            accn='0000718877-20-000003', fy=2019, fp='FY', form='10-K', filed='2020-02-27')]})}}
    frame = normalize_atvi_companyfacts(tmp_path, only_the_other_quantity)
    assert not frame.canonical_account_id.eq(wrong_account).any()


def test_a_combined_tag_without_statement_evidence_is_not_a_verified_total(tmp_path):
    def omit_presentation(folder):
        metadata = folder / 'filing.json'
        manifest = json.loads(metadata.read_bytes())
        omitted = [doc for doc in manifest['xbrl_documents'] if doc['role'] == 'presentation']
        assert omitted
        for document in omitted:
            (folder / document['document_name']).unlink()
        manifest['xbrl_documents'] = [doc for doc in manifest['xbrl_documents'] if doc not in omitted]
        metadata.write_text(json.dumps(manifest), 'utf-8')
    frame = normalize_retained_bundle(tmp_path, 'TWTR', '10-K',
        '0001418091-22-000029', 2021, source_edit=omit_presentation)
    assert not frame.canonical_account_id.eq('DNA_CF').any()


def test_a_statement_line_including_other_expenses_is_not_a_da_total(tmp_path):
    def broaden_disclosed_quantity(folder):
        metadata = folder / 'filing.json'
        manifest = json.loads(metadata.read_bytes())
        changed = 0
        for document in manifest['xbrl_documents']:
            if document['role'] != 'label':
                continue
            path = folder / document['document_name']
            text, count = re.subn('Depreciation and amortization expense',
                'Depreciation and amortization expense and stock-based compensation',
                path.read_text('utf-8'), flags=re.IGNORECASE)
            if count:
                path.write_text(text, 'utf-8')
                document.update(sha256=sha256(path.read_bytes()).hexdigest(), byte_size=path.stat().st_size)
                changed += count
        assert changed
        metadata.write_text(json.dumps(manifest), 'utf-8')
    frame = normalize_retained_bundle(tmp_path, 'TWTR', '10-K',
        '0001418091-22-000029', 2021, source_edit=broaden_disclosed_quantity)
    assert not frame.canonical_account_id.eq('DNA_CF').any()
