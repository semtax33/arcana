"""SEC financial normalization retains the filing's own security identifiers."""
import json
from copy import deepcopy

from lxml import etree

from test_us_filing_account_scope import normalize_retained_bundle
from test_us_complete_account_components import edit_instance


def test_financial_normalization_preserves_the_reported_security_class_and_time(tmp_path):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    path = tmp_path / 'silver/normalized/security_identity/TWTR/0001418091-22-000029.json'
    assert path.exists()
    result = json.loads(path.read_bytes())
    observation, = result['observations']
    assert observation['cik'] == '0001418091'
    assert observation['reported_symbol'] == 'TWTR'
    assert observation['issuer_name'] == 'Twitter, Inc.'
    assert observation['security_title'] == 'Common Stock, par value $0.000005 per share'
    assert observation['exchange'] == 'NYSE'
    assert observation['published_date'] == '2022-02-16'
    assert observation['reported_period_end'] == '2021-12-31'
    assert observation['security_type'] == 'common_stock'
    assert observation['listing_interval_verified'] is False
    assert 'valid_from' not in observation


def test_other_entity_names_cannot_override_the_context_issuer(tmp_path):
    def add_other_entity(root):
        issuer = next(e for e in root if isinstance(e.tag, str) and etree.QName(e).localname == 'EntityRegistrantName')
        context = next(e for e in root if e.get('id') == issuer.get('contextRef'))
        foreign_context = deepcopy(context)
        foreign_context.set('id', 'ForeignEntityContext')
        for child in foreign_context.iter():
            if etree.QName(child).localname == 'identifier':
                child.text = '0000999999'
        foreign_name = deepcopy(issuer)
        foreign_name.set('contextRef', 'ForeignEntityContext')
        foreign_name.text = 'A Different Legal Entity'
        root.extend([foreign_context, foreign_name])
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021,
        source_edit=lambda folder: edit_instance(folder, add_other_entity))
    report = json.loads((tmp_path / 'silver/normalized/security_identity/TWTR/0001418091-22-000029.json').read_bytes())
    observation, = report['observations']
    assert observation['issuer_name'] == 'Twitter, Inc.'
    assert observation['cik'] == '0001418091'


def test_declared_symbol_is_preserved_when_it_differs_from_the_requested_alias(tmp_path):
    def different_symbol(root):
        for fact in root:
            if isinstance(fact.tag, str) and etree.QName(fact).localname == 'TradingSymbol':
                fact.text = 'OTHER'
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021,
        source_edit=lambda folder: edit_instance(folder, different_symbol))
    report = json.loads((tmp_path / 'silver/normalized/security_identity/TWTR/0001418091-22-000029.json').read_bytes())
    assert report['observations'][0]['reported_symbol'] == 'OTHER'
    assert 'REQUESTED_SYMBOL_DIFFERS_FROM_FILING' in report['review_reasons']


def test_multiple_share_classes_keep_their_own_context_and_do_not_conflict_with_the_requested_class(tmp_path):
    def add_second_class(root):
        labels = {'TradingSymbol': 'TWTR.B', 'Security12bTitle': 'Class B Common Stock, par value $0.000005',
                  'SecurityExchangeName': 'NASDAQ'}
        originals = [e for e in root if isinstance(e.tag, str) and etree.QName(e).localname in labels]
        context = next(e for e in root if e.get('id') == originals[0].get('contextRef'))
        second = deepcopy(context); second.set('id', 'ClassBContext')
        entity = next(e for e in second if etree.QName(e).localname == 'entity')
        segment = etree.SubElement(entity, '{http://www.xbrl.org/2003/instance}segment')
        member = etree.SubElement(segment, '{http://xbrl.org/2006/xbrldi}explicitMember',
            dimension='us-gaap:StatementClassOfStockAxis')
        member.text = 'twtr:ClassBMember'
        root.append(second)
        for original in originals:
            fact = deepcopy(original); fact.set('contextRef', 'ClassBContext')
            fact.text = labels[etree.QName(fact).localname]; root.append(fact)
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021,
        source_edit=lambda folder: edit_instance(folder, add_second_class))
    report = json.loads((tmp_path / 'silver/normalized/security_identity/TWTR/0001418091-22-000029.json').read_bytes())
    assert {r['reported_symbol']: r['exchange'] for r in report['observations']} == {'TWTR': 'NYSE', 'TWTR.B': 'NASDAQ'}
    assert len({r['context_ref'] for r in report['observations']}) == 2
    assert 'REQUESTED_SYMBOL_DIFFERS_FROM_FILING' not in report['review_reasons']


def test_older_filings_retain_issuer_evidence_without_inventing_a_trading_symbol(tmp_path):
    normalize_retained_bundle(tmp_path, 'GOOG', '10-K', '0001652044-17-000008', 2016)
    report = json.loads((tmp_path / 'silver/normalized/security_identity/GOOG/0001652044-17-000008.json').read_bytes())
    assert report['cik'] == '0001652044'
    assert report['issuer_name'] == 'Alphabet Inc.'
    assert report['published_date'] == '2017-02-03'
    assert report['observations'] == []
    assert 'INCOMPLETE_OR_AMBIGUOUS_SECURITY_CONTEXT' in report['review_reasons']
