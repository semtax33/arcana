"""Complete disclosed components produce totals at the public normalization seam."""
from copy import deepcopy
from hashlib import sha256
import json

from lxml import etree
import pandas as pd
import pytest

from engine.transformers.sec_filings import normalize_us_sec_filings
from test_us_filing_account_scope import ROOT, RULE_PATH, normalize_retained_bundle

pytestmark = pytest.mark.integration


def test_sgna_includes_both_selling_and_administrative_expenses(tmp_path):
    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019).set_index('canonical_account_id')
    # Official 2019 statement: selling/marketing 926m, G&A 732m.
    # SG&A comprises both, independently worked total 1,658m.
    assert frame.loc['SGNA', 'normalized_amount'] == 1658000000


def normalize_atvi_companyfacts(tmp_path, source_edit=None):
    payload = json.loads((ROOT / 'data-lake/bronze/sec/companyfacts/CIK0000718877.json').read_bytes())
    # Isolate the original accession, including all its comparative facts.
    for namespace in payload['facts'].values():
        for fact in namespace.values():
            for unit, rows in fact['units'].items():
                fact['units'][unit] = [row for row in rows if row.get('accn') == '0000718877-20-000003']
    if source_edit:
        source_edit(payload)
    raw = tmp_path / 'bronze/sec/companyfacts'
    raw.mkdir(parents=True)
    (raw / 'CIK0000718877.json').write_text(json.dumps(payload), 'utf-8')
    output = tmp_path / 'silver/normalized'
    normalize_us_sec_filings(symbols=['ATVI'], start_year=2019, end_year=2019,
        filings_dir=tmp_path / 'bronze/sec/fillings', companyfacts_dir=raw,
        output_dir=output, report_metadata_path=tmp_path / 'silver/reports.csv',
        mapping_rule_path=RULE_PATH, use_notes=False, use_edgartools=False,
        save_debug=False, log_progress=False)
    return pd.read_csv(output / 'us_normalized_ATVI.csv')


def test_companyfacts_preserves_the_complete_sgna_total_and_component_evidence(tmp_path):
    frame = normalize_atvi_companyfacts(tmp_path).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == 1658000000
    folder = tmp_path / 'silver/normalized/accessions/ATVI'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    accessions = pd.read_csv(folder / manifest['normalized_path']).set_index('canonical_account_id')
    sgna = accessions.loc['SGNA']
    assert sgna.source == 'companyfacts_components'
    assert sgna.accn == '0000718877-20-000003'
    components = json.loads(sgna.component_facts)
    assert {part['concept']: part['raw_amount'] for part in components} == {
        'us-gaap:SellingAndMarketingExpense': 926000000,
        'us-gaap:GeneralAndAdministrativeExpense': 732000000,
    }


@pytest.mark.parametrize('change', ['missing', 'period', 'accession', 'unit', 'conflict'])
def test_incomplete_or_incompatible_components_do_not_become_sgna(tmp_path, change):
    def alter(payload):
        fact = payload['facts']['us-gaap']['GeneralAndAdministrativeExpense']
        rows = fact['units']['USD']
        row = next(item for item in rows if item['end'] == '2019-12-31')
        if change == 'missing':
            rows.remove(row)
        elif change == 'period':
            row['start'] = '2019-04-01'
        elif change == 'accession':
            row['accn'] = '0000718877-20-999999'
        elif change == 'unit':
            fact['units'] = {'CAD': rows}
        else:
            rows.append(dict(row, val=733000000))
    frame = normalize_atvi_companyfacts(tmp_path, alter)
    assert not frame.canonical_account_id.eq('SGNA').any()


@pytest.mark.parametrize('administrative,total', [(0, 926000000), (-26000000, 900000000)])
def test_disclosed_zero_and_expense_credits_are_kept_in_component_totals(tmp_path, administrative, total):
    def alter(payload):
        for row in payload['facts']['us-gaap']['GeneralAndAdministrativeExpense']['units']['USD']:
            if row['end'] == '2019-12-31':
                row['val'] = administrative
    frame = normalize_atvi_companyfacts(tmp_path, alter).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == total


def test_a_reported_sgna_total_precedes_a_component_sum(tmp_path):
    def alter(payload):
        total = deepcopy(payload['facts']['us-gaap']['SellingAndMarketingExpense'])
        total['label'] = 'Selling, general and administrative expense'
        for row in total['units']['USD']:
            if row['end'] == '2019-12-31':
                row['val'] = 1660000000
        payload['facts']['us-gaap']['SellingGeneralAndAdministrativeExpense'] = total
    frame = normalize_atvi_companyfacts(tmp_path, alter).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == 1660000000


def test_reported_quarter_total_does_not_hide_complete_annual_components(tmp_path):
    def alter(payload):
        # Synthetic mixed presentation of the original annual components:
        # a separate directly reported Q4 total, with no directly reported FY total.
        fact = deepcopy(payload['facts']['us-gaap']['SellingAndMarketingExpense'])
        annual = next(row for row in fact['units']['USD'] if row['end'] == '2019-12-31')
        fact['units']['USD'] = [dict(annual, start='2019-10-01', val=400000000)]
        fact['label'] = 'Selling, general and administrative expense'
        payload['facts']['us-gaap']['SellingGeneralAndAdministrativeExpense'] = fact
    frame = normalize_atvi_companyfacts(tmp_path, alter).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == 1658000000
    folder = tmp_path / 'silver/normalized/accessions/ATVI'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    accessions = pd.read_csv(folder / manifest['normalized_path']).set_index('canonical_account_id')
    sgna = accessions.loc['SGNA']
    assert sgna.period_semantic == 'FY'
    observations = json.loads(sgna.reported_durations)
    quarter = [row for row in observations if row['period_start'] == '2019-10-01'
               and row['period_end'] == '2019-12-31']
    assert len(quarter) == 1 and quarter[0]['normalized_amount'] == 400000000


def test_reported_annual_total_keeps_quarters_derived_from_complete_components(tmp_path):
    def alter(payload):
        for tag, quarter_amount in [('SellingAndMarketingExpense', 300000000),
                                    ('GeneralAndAdministrativeExpense', 99000000)]:
            rows = payload['facts']['us-gaap'][tag]['units']['USD']
            annual = next(row for row in rows if row['end'] == '2019-12-31')
            rows.append(dict(annual, start='2019-10-01', val=quarter_amount))
        total = deepcopy(payload['facts']['us-gaap']['SellingAndMarketingExpense'])
        annual = next(row for row in total['units']['USD'] if row['start'] == '2019-01-01')
        total['units']['USD'] = [dict(annual, val=1660000000)]
        payload['facts']['us-gaap']['SellingGeneralAndAdministrativeExpense'] = total
    frame = normalize_atvi_companyfacts(tmp_path, alter).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == 1660000000
    folder = tmp_path / 'silver/normalized/accessions/ATVI'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    sgna = pd.read_csv(folder / manifest['normalized_path']).set_index('canonical_account_id').loc['SGNA']
    assert sgna.source == 'companyfacts_primary'
    quarter = [row for row in json.loads(sgna.reported_durations) if row['period_start'] == '2019-10-01']
    assert len(quarter) == 1 and quarter[0]['normalized_amount'] == 399000000
    assert quarter[0]['derivation'] == 'sum_complete_components'


def test_filing_preserves_both_a_reported_quarter_and_complete_annual_components(tmp_path):
    def with_quarter(root):
        context = deepcopy(next(el for el in root if el.get('id') == 'FD2019Q4YTD'))
        context.set('id', 'synthetic_quarter')
        context.find('.//{http://www.xbrl.org/2003/instance}startDate').text = '2019-10-01'
        root.append(context)
        fact = deepcopy(next(f for f in root if etree.QName(f).localname == 'GeneralAndAdministrativeExpense'
                             and f.get('contextRef') == 'FD2019Q4YTD'))
        fact.tag = f'{{{etree.QName(fact).namespace}}}SellingGeneralAndAdministrativeExpense'
        fact.set('id', 'synthetic_quarter_sgna')
        fact.set('contextRef', 'synthetic_quarter')
        fact.text = '400000000'
        root.append(fact)
    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019,
        source_edit=lambda folder: edit_instance(folder, with_quarter)).set_index('canonical_account_id')
    assert frame.loc['SGNA', 'normalized_amount'] == 1658000000
    folder = tmp_path / 'silver/normalized/accessions/ATVI'
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    sgna = pd.read_csv(folder / manifest['normalized_path']).set_index('canonical_account_id').loc['SGNA']
    quarter = [row for row in json.loads(sgna.reported_durations) if row['period_start'] == '2019-10-01']
    assert len(quarter) == 1 and quarter[0]['normalized_amount'] == 400000000


def test_lease_liabilities_include_operating_and_finance_obligations(tmp_path):
    frame = normalize_retained_bundle(tmp_path, 'ALXN', '10-Q',
        '0000899866-19-000061', 2019).set_index('canonical_account_id')
    # Original Q1 2019 lease disclosure: operating 168.4m, finance 81.8m.
    # https://www.sec.gov/Archives/edgar/data/899866/000089986619000061/alxn3311910q.htm
    assert frame.loc['LEASE_LIABILITY', 'normalized_amount'] == 250200000


def edit_instance(folder, change):
    metadata = folder / 'filing.json'
    manifest = json.loads(metadata.read_bytes())
    document = next(item for item in manifest['xbrl_documents'] if item['role'] == 'instance')
    path = folder / document['document_name']
    tree = etree.parse(str(path))
    change(tree.getroot())
    tree.write(str(path), encoding='UTF-8', xml_declaration=True)
    document.update(sha256=sha256(path.read_bytes()).hexdigest(), byte_size=path.stat().st_size)
    metadata.write_text(json.dumps(manifest), 'utf-8')


@pytest.mark.parametrize('split_operating,split_finance', [(True, False), (False, True), (True, True)])
def test_current_and_noncurrent_lease_components_replace_an_absent_class_total(tmp_path, split_operating, split_finance):
    def without_total(root):
        for fact in list(root):
            tag = etree.QName(fact)
            if split_operating and tag.localname == 'OperatingLeaseLiability':
                root.remove(fact)
            elif split_finance and tag.localname == 'FinanceLeaseLiability':
                # Synthetic equivalent presentation: split the disclosed zero
                # into explicit zero current and noncurrent obligations.
                if fact.text == '0':
                    for suffix in ['Current', 'Noncurrent']:
                        component = deepcopy(fact)
                        component.tag = f'{{{tag.namespace}}}{tag.localname}{suffix}'
                        component.set('id', fact.get('id', 'finance') + suffix)
                        root.append(component)
                root.remove(fact)
    frame = normalize_retained_bundle(tmp_path, 'TWTR', '10-K',
        '0001418091-22-000029', 2021,
        source_edit=lambda folder: edit_instance(folder, without_total)).set_index('canonical_account_id')
    # Synthetic omission of the aggregate from the original 2021 disclosure.
    # Current 222,346k + noncurrent 1,071,209k; finance explicitly zero.
    assert frame.loc['LEASE_LIABILITY', 'normalized_amount'] == 1293555000


def test_an_undisclosed_lease_class_is_not_assumed_to_be_zero(tmp_path):
    def without_finance(root):
        for fact in list(root):
            if etree.QName(fact).localname.startswith('FinanceLeaseLiability'):
                root.remove(fact)
    frame = normalize_retained_bundle(tmp_path, 'TWTR', '10-K',
        '0001418091-22-000029', 2021,
        source_edit=lambda folder: edit_instance(folder, without_finance))
    assert not frame.canonical_account_id.eq('LEASE_LIABILITY').any()


@pytest.mark.parametrize('change', ['missing', 'period', 'entity', 'dimension', 'unit'])
def test_filing_components_require_the_same_consolidated_disclosure_scope(tmp_path, change):
    def alter(root):
        fact = next(f for f in root if etree.QName(f).localname == 'GeneralAndAdministrativeExpense'
                    and f.get('contextRef') == 'FD2019Q4YTD')
        if change == 'missing':
            root.remove(fact)
            return
        xbrli = 'http://www.xbrl.org/2003/instance'
        if change == 'unit':
            unit = deepcopy(next(el for el in root if el.get('id') == 'usd'))
            unit.set('id', 'synthetic_gbp')
            unit.find(f'{{{xbrli}}}measure').text = 'iso4217:GBP'
            root.append(unit)
            fact.set('unitRef', 'synthetic_gbp')
            return
        context = deepcopy(next(el for el in root if el.get('id') == 'FD2019Q4YTD'))
        context.set('id', 'synthetic_admin_context')
        if change == 'period':
            context.find(f'.//{{{xbrli}}}startDate').text = '2019-04-01'
        elif change == 'entity':
            context.find(f'.//{{{xbrli}}}identifier').text = '0000816284'
        else:
            entity = context.find(f'{{{xbrli}}}entity')
            segment = etree.SubElement(entity, f'{{{xbrli}}}segment')
            member = etree.SubElement(segment, '{http://xbrl.org/2006/xbrldi}explicitMember')
            member.set('dimension', 'us-gaap:StatementBusinessSegmentsAxis')
            member.text = 'us-gaap:ReportableSegmentMember'
        root.append(context)
        fact.set('contextRef', 'synthetic_admin_context')
    frame = normalize_retained_bundle(tmp_path, 'ATVI', '10-K',
        '0000718877-20-000003', 2019,
        source_edit=lambda folder: edit_instance(folder, alter))
    assert not frame.canonical_account_id.eq('SGNA').any()
