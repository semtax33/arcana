"""Public refresh links provider observations to retained SEC evidence."""
import json
from pathlib import Path

import pandas as pd
import pytest
from lxml import etree

from engine.workflows.survivorship import run_survivorship_refresh
from test_listing_history_replay import snapshot
from test_us_filing_account_scope import normalize_retained_bundle
from test_us_complete_account_components import edit_instance


def refresh(root, cutoff='2022-03-01'):
    summary = run_survivorship_refresh(market='us', end_date=cutoff,
        source_dir=root / 'bronze/listings', identity_dir=root / 'silver/normalized/security_identity',
        sec_submissions_manifest=root / 'silver/no_bulk.json',
        manifest_path=root / 'silver/not-reviewed.json', output_dir=root / 'silver/survivorship',
        gold_dir=root / 'gold/survivorship', download=False, load_clickhouse=False)
    result = json.loads(Path(summary['identity_linkage']['path']).read_bytes())
    return result, pd.read_parquet(result['candidate_links']['path'])


def test_refresh_corroborates_reported_class_and_preserves_unmatched_population(tmp_path):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    snapshot(tmp_path / 'bronze/listings', '2022-03-01', 'active', [
        ['TWTR', 'Twitter Inc', 'NYSE', 'Stock', '2013-11-07', '', 'Active'],
        ['OTHER', 'Unresolved Issuer', 'NYSE', 'Stock', '2001-01-01', '', 'Active']])
    result, frame = refresh(tmp_path)
    assert len(frame) == 2
    twitter = frame.set_index('symbol').loc['TWTR']
    assert json.loads(twitter.candidate_ciks) == ['0001418091']
    assert twitter.link_status == 'corroborated_source_observation'
    assert twitter.identity_status == 'unresolved'
    assert json.loads(twitter.evidence)[0]['accession'] == '0001418091-22-000029'
    other = frame.set_index('symbol').loc['OTHER']
    assert 'NO_AVAILABLE_REPORTED_CLASS' in json.loads(other.link_review_reasons)
    assert result['registered_identities'] == 0
    assert result['coverage_complete'] is False


def test_unchanged_linkage_reuses_generation_and_preserves_earlier_cutoff(tmp_path):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    snapshot(tmp_path / 'bronze/listings', '2021-12-31', 'active', [
        ['TWTR', 'Twitter Inc', 'NYSE', 'Stock', '2013-11-07', '', 'Active']])
    early, early_frame = refresh(tmp_path, '2021-12-31')
    assert early_frame.candidate_ciks.tolist() == ['[]']
    manifest = Path(early['silver_manifest']['path'])
    previous = manifest.read_bytes()
    late, late_frame = refresh(tmp_path)
    assert late_frame.link_status.tolist() == ['corroborated_source_observation']
    again, _ = refresh(tmp_path)
    assert again['generation'] == late['generation']
    assert again['generation_reused'] is True
    assert late['generation_reused'] is False
    assert manifest.read_bytes() == previous


def test_conflicting_issuer_evidence_cannot_be_hidden_by_a_matching_name(tmp_path):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    def reused_code(root):
        for fact in root:
            if isinstance(fact.tag, str):
                local = etree.QName(fact).localname
                if local == 'TradingSymbol':
                    fact.text = 'TWTR'
                elif local == 'SecurityExchangeName':
                    fact.text = 'NYSE'
    # Explicit synthetic symbol reuse; the retained second issuer's CIK/name
    # remain different even though one of the names matches the provider.
    normalize_retained_bundle(tmp_path, 'ATVI', '10-K', '0000718877-20-000003', 2019,
        source_edit=lambda folder: edit_instance(folder, reused_code))
    snapshot(tmp_path / 'bronze/listings', '2022-03-01', 'active', [
        ['TWTR', 'Twitter Inc', 'NYSE', 'Stock', '2013-11-07', '', 'Active']])
    _, frame = refresh(tmp_path)
    row = frame.iloc[0]
    assert row.link_status == 'review_required'
    assert 'MULTIPLE_SEC_ISSUERS' in json.loads(row.link_review_reasons)


@pytest.mark.parametrize('name,exchange,asset,terminal,reason', [
    ('Another Issuer', 'NYSE', 'Stock', '', 'ISSUER_NAME_MISMATCH'),
    ('Twitter Inc', 'NASDAQ', 'Stock', '', 'EXCHANGE_MISMATCH'),
    ('Twitter Inc', 'NYSE', 'ETF', '', 'SECURITY_CLASS_NOT_COMMON_STOCK'),
    ('Twitter Inc', 'NYSE', 'Stock', '2021-12-31', 'PUBLICATION_OUTSIDE_PROVIDER_DATES'),
])
def test_symbol_alone_cannot_corroborate_a_different_provider_security(
        tmp_path, name, exchange, asset, terminal, reason):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    state = 'delisted' if terminal else 'active'
    snapshot(tmp_path / 'bronze/listings', '2022-03-01', state, [
        ['TWTR', name, exchange, asset, '2013-11-07', terminal, state.title()]])
    _, frame = refresh(tmp_path)
    assert len(frame) == 1
    assert frame.iloc[0].link_status == 'review_required'
    assert frame.iloc[0].candidate_ciks == '[]'
    assert reason in json.loads(frame.iloc[0].link_review_reasons)


def test_changed_sec_source_cannot_reuse_cached_links_or_replace_gold(tmp_path):
    normalize_retained_bundle(tmp_path, 'TWTR', '10-K', '0001418091-22-000029', 2021)
    snapshot(tmp_path / 'bronze/listings', '2022-03-01', 'active', [
        ['TWTR', 'Twitter Inc', 'NYSE', 'Stock', '2013-11-07', '', 'Active']])
    result, _ = refresh(tmp_path)
    published = {p: p.read_bytes() for p in (tmp_path / 'gold').rglob('*') if p.is_file()}
    report = json.loads(Path(result['reports'][0]['report_path']).read_bytes())
    raw = Path(report['instance_source']['path'])
    raw.write_bytes(raw.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='instance/publication integrity'):
        refresh(tmp_path)
    assert all(p.read_bytes() == contents for p, contents in published.items())
