"""Retained listing observations survive the public survivorship refresh."""
import csv
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from engine.workflows.survivorship import run_survivorship_refresh


def snapshot(root, day, state, rows):
    path = root / f'snapshot_date={day}' / f'{state}.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['symbol', 'name', 'exchange', 'assetType', 'ipoDate', 'delistingDate', 'status'])
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps(dict(
        provider='ALPHA_VANTAGE', snapshot_date=day, state=state, rows=len(rows),
        source_url=f'https://www.alphavantage.co/query?function=LISTING_STATUS&date={day}&state={state}',
        retrieved_at='2026-09-10T22:05:00+00:00',
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())), 'utf-8')
    return path


def replay(root, day='2020-01-03'):
    return run_survivorship_refresh(market='us', end_date=day,
        source_dir=root / 'bronze/listings', manifest_path=root / 'silver/not-reviewed.json',
        sec_submissions_manifest=root / 'silver/no_bulk.json',
        output_dir=root / 'silver/survivorship', gold_dir=root / 'gold/survivorship',
        download=False, load_clickhouse=False)


def history_from(summary):
    return json.loads(Path(summary['listing_history']['path']).read_text('utf-8'))


def test_default_refresh_reads_alpha_delistings_without_opening_sec_inventory(tmp_path):
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted']])
    # A retained SEC index must not activate an unrelated collection pipeline.
    bulk = tmp_path / 'silver/no_bulk.json'
    bulk.parent.mkdir(parents=True)
    bulk.write_text('SEC inventory must not be read for Alpha delistings', 'utf-8')
    summary = replay(tmp_path)
    assert not any(key in summary for key in (
        'issuer_discovery', 'filing_inventory', 'notice_documents',
        'notice_observations', 'notice_event_evidence'))
    history = history_from(summary)
    assert history['observation_rows'] == 1


def test_alpha_delisting_dates_are_published_in_bulk_without_sec_or_manual_manifest(tmp_path):
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-02', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-29', 'Delisted']])
    latest = snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted']])
    snapshot(root, '2020-01-03', 'active', [
        ['OLD', 'Different Issuer', 'NASDAQ', 'Stock', '2020-01-01', '', 'Active'],
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    summary = replay(tmp_path)
    listing = json.loads(Path(summary['alpha_listing_population']['path']).read_bytes())
    population = pd.read_parquet(listing['listing_population']['path'])
    dead = pd.read_parquet(listing['delistings']['path'])
    assert len(population) == 3  # Reused symbols must not collapse two listings.
    assert listing['delisted_listing_keys'] == 1
    assert dead.symbol.tolist() == ['OLD']
    assert dead.delistingDate.tolist() == ['2019-12-30']
    assert dead.ipoDate.tolist() == ['1990-01-01']
    assert dead.source_path.tolist() == [str(latest.resolve())]
    assert 'DELISTING_DATE_CHANGED' in json.loads(dead.review_reasons.iloc[0])
    assert 'SYMBOL_MULTIPLE_PROVIDER_KEYS' in json.loads(dead.review_reasons.iloc[0])
    gold = pd.read_csv(summary['alpha_delistings']['path'], keep_default_na=False)
    assert gold.delistingDate.tolist() == ['2019-12-30']
    assert listing['terminal_proceeds_resolved'] is False
    assert 'notice_documents' not in summary


def test_alpha_listing_periods_feed_the_published_universe_without_manual_approval(tmp_path):
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted'],
        ['FUND', 'An ETF', 'NYSE ARCA', 'ETF', '2000-01-01', '2019-12-30', 'Delisted']])
    summary = replay(tmp_path)
    episodes = json.loads((tmp_path / 'gold/survivorship/listing_episodes.json').read_bytes())['rows']
    episode, = [row for row in episodes if row['security_type'] == 'provider_stock']
    assert [row['symbol'] for row in episodes if row['security_type'] == 'provider_etf'] == ['FUND']
    assert episode['security_id'] == 'SEC_US_OLD'
    assert episode['security_type'] == 'provider_stock'
    assert episode['valid_from'] == '1990-01-01'
    assert episode['valid_until'] == '2019-12-30'
    assert episode['status'] == 'confirmed'
    assert episode['listing_source'] == 'ALPHA_VANTAGE'
    event, = json.loads((tmp_path / 'gold/survivorship/events.json').read_bytes())['rows']
    assert event['event_type'] == 'delisting'
    assert event['cash_per_share'] is None
    assert event['entitlements_complete'] is False
    assert summary['listing_episodes'] == 2


def test_reused_symbol_keeps_both_listing_periods_and_blocks_the_legacy_alias(tmp_path):
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-03', 'delisted', [
        ['REUSE', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    snapshot(root, '2020-01-03', 'active', [
        ['REUSE', 'New Issuer', 'NASDAQ', 'Stock', '2019-01-01', '', 'Active']])
    replay(tmp_path)
    episodes = json.loads((tmp_path / 'gold/survivorship/listing_episodes.json').read_bytes())['rows']
    stocks = [row for row in episodes if row['security_type'] == 'provider_stock']
    assert len(stocks) == 2
    assert len({row['security_id'] for row in stocks}) == 2
    assert all(row['security_id'] != 'SEC_US_REUSE' for row in stocks)
    assert {row['valid_from'] for row in stocks} == {'1990-01-01', '2019-01-01'}
    aliases = [row for row in episodes if row['security_type'] == 'unresolved_provider_alias']
    assert len(aliases) == 1 and aliases[0]['security_id'] == 'SEC_US_REUSE'
    assert aliases[0]['status'] == 'confirmed'  # suppress current-master fallback


def test_default_alpha_price_download_includes_retired_stocks_from_retained_listings(tmp_path, monkeypatch):
    from engine.core.paths import DataLakePaths
    import engine.extractors.alpha_vantage_prices as prices
    from test_stock_splits import alpha_payload
    lake = DataLakePaths(tmp_path / 'data-lake')
    monkeypatch.setattr(prices, 'DATA_LAKE', lake)
    monkeypatch.setenv('ALPHA_VANTAGE_API_KEY', 'synthetic-test-key')
    def no_other_listing_provider(*args, **kwargs):
        raise AssertionError('Use retained Alpha listings, not a current-only universe download')
    monkeypatch.setattr('engine.extractors._internal.yfinance_market_prices.urlopen', no_other_listing_provider)
    root = lake.bronze('alpha-vantage', 'listings')
    snapshot(root, '2020-01-03', 'active', [
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted'],
        ['ETF', 'ETF', 'NYSE', 'ETF', '1990-01-01', '2019-12-30', 'Delisted']])
    calls = []
    def get(url, *, params, timeout):
        from types import SimpleNamespace
        calls.append(params['symbol'])
        return SimpleNamespace(status_code=200, json=lambda: alpha_payload(params['symbol']))
    result = prices.download_alpha_vantage_prices(as_of='2020-01-03', http_get=get, workers=1)
    assert sorted(calls) == ['LIVE', 'OLD']
    assert result['downloaded'] == 2 and not result['errors']
    again = prices.download_alpha_vantage_prices(as_of='2020-01-03', http_get=get, workers=1)
    assert again['cached'] == 2 and len(calls) == 2


def test_default_factor_generation_includes_retired_provider_stock_and_skips_ambiguous_alias(tmp_path, monkeypatch):
    from engine.core.paths import DataLakePaths
    import engine.loaders.factors as factors
    lake = DataLakePaths(tmp_path / 'data-lake')
    monkeypatch.setattr(factors, 'DATA_LAKE', lake)
    root = lake.bronze('alpha-vantage', 'listings')
    snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2020-01-01', 'Delisted'],
        ['REUSE', 'Prior Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    snapshot(root, '2020-01-03', 'active', [
        ['REUSE', 'New Issuer', 'NYSE', 'Stock', '2019-01-01', '', 'Active']])
    run_survivorship_refresh(market='us', end_date='2020-01-03', source_dir=root,
        output_dir=lake.silver('survivorship', 'us'), gold_dir=lake.gold('survivorship', 'us'),
        manifest_path=tmp_path / 'no-review.json', download=False, load_clickhouse=False)
    price_path = tmp_path / 'prices.csv'
    shares_path = tmp_path / 'shares.csv'
    stale = lake.silver('sec', 'normalized', 'us_normalized_REUSE.csv')
    stale.parent.mkdir(parents=True)
    stale.write_text('fiscal_year,financial_period\n', 'utf-8')
    pd.DataFrame([dict(security_id='SEC_US_' + symbol, trade_date='2019-12-30',
        open=10., high=10., low=10., close=10., adj_close=10., volume=100., currency='USD')
        for symbol in ['OLD', 'REUSE']]).to_csv(price_path, index=False)
    pd.DataFrame([dict(security_id='SEC_US_' + symbol, trade_date='2019-12-30',
        shares=100000000., market_cap=1000000000.) for symbol in ['OLD', 'REUSE']]).to_csv(shares_path, index=False)
    result = factors.create_daily_factor_rows(market='us', reader_mode='csv',
        factor_ids=['mcap_mil'], start_date='2019-12-30', end_date='2019-12-30',
        price_path=price_path, shares_path=shares_path, financial_dir=tmp_path / 'no-financials',
        report_metadata_path=tmp_path / 'no-reports.csv', use_edgartools=False)
    assert result.security_id.tolist() == ['SEC_US_OLD']
    assert result.factor_value.tolist() == [1000.]


def test_cached_refresh_preserves_active_and_delisted_observations_without_approving_rights(tmp_path):
    root = tmp_path / 'bronze/listings'
    active = snapshot(root, '2020-01-03', 'active', [
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    dead = snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted']])
    originals = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    summary = replay(tmp_path)
    history = history_from(summary)
    frame = pd.concat([pd.read_parquet(p['path']) for p in history['partitions']], ignore_index=True)
    assert set(frame.symbol) == {'LIVE', 'OLD'}
    assert set(frame.state) == {'active', 'delisted'}
    assert set(frame.snapshot_date) == {'2020-01-03'}
    assert set(frame.retrieved_at) == {'2026-09-10T22:05:00+00:00'}
    assert frame.provider_listing_id.nunique() == 2
    assert history['coverage_complete'] is False
    assert history['registered_identities'] == 0
    events = json.loads((tmp_path / 'gold/survivorship/events.json').read_bytes())['rows']
    assert len(events) == 1 and events[0]['entitlements_complete'] is False
    assert events[0]['cash_per_share'] is None
    assert summary['listing_episodes'] == 2
    assert {p: p.read_bytes() for p in originals} == originals
    assert set(frame.source_path) == {str(active.resolve()), str(dead.resolve())}


def test_unchanged_snapshots_are_reused_and_one_changed_snapshot_is_rebuilt(tmp_path):
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-03', 'active', [
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2019-12-30', 'Delisted']])
    first = history_from(replay(tmp_path))
    retained = {Path(p['path']): Path(p['path']).read_bytes() for p in first['partitions']}
    manifest = Path(first['silver_manifest']['path'])
    manifest_bytes = manifest.read_bytes()
    again = history_from(replay(tmp_path))
    assert again['cache'] == {'partitions_reused': 2, 'partitions_built': 0}
    assert again['generation'] == first['generation']
    assert again['candidate_index_reused'] is True
    assert manifest.read_bytes() == manifest_bytes
    snapshot(root, '2020-01-03', 'active', [
        ['LIVE', 'Changed Issuer Name', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    changed = history_from(replay(tmp_path))
    assert changed['cache'] == {'partitions_reused': 1, 'partitions_built': 1}
    assert changed['generation'] != first['generation']
    assert {p: p.read_bytes() for p in retained} == retained


def test_requested_cutoffs_have_distinct_manifests_even_without_new_snapshots(tmp_path):
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'active', [
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    first = history_from(replay(tmp_path))
    manifest = Path(first['silver_manifest']['path'])
    previous = manifest.read_bytes()
    later = history_from(replay(tmp_path, '2020-01-04'))
    assert later['as_of'] == '2020-01-04'
    assert later['generation'] != first['generation']
    assert manifest.read_bytes() == previous


def test_history_groups_symbol_reuse_and_changed_dates_without_erasing_observations(tmp_path):
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-02', 'active', [
        ['REUSE', 'Old Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    snapshot(root, '2020-01-03', 'active', [
        ['REUSE', 'New Issuer', 'NYSE', 'Stock', '2019-01-01', '', 'Active']])
    for day, terminal in [('2020-01-02', '2019-12-30'), ('2020-01-03', '2019-12-31')]:
        snapshot(root, day, 'delisted', [
            ['MOVE', 'Former Issuer', 'NYSE', 'Stock', '2001-01-01', terminal, 'Delisted']])
    # Even a corrupt future input must not leak into an earlier requested slice.
    future = root / 'snapshot_date=2021-01-01/active.csv'
    future.parent.mkdir(); future.write_text('not a usable listing response', 'utf-8')
    history = history_from(replay(tmp_path))
    index = pd.read_parquet(history['candidate_index']['path'])
    assert history['observation_rows'] == 4
    assert history['provider_listing_keys'] == 3
    reuse = index[index.symbol.eq('REUSE')]
    assert len(reuse) == 2
    assert reuse.review_reasons.map(lambda value: 'SYMBOL_MULTIPLE_PROVIDER_KEYS' in json.loads(value)).all()
    move = index[index.symbol.eq('MOVE')].iloc[0]
    assert json.loads(move.observed_delisting_dates) == ['2019-12-30', '2019-12-31']
    assert 'DELISTING_DATE_CHANGED' in json.loads(move.review_reasons)
    assert set(index.identity_status) == {'unresolved'}


@pytest.mark.parametrize('ipo,terminal,state,status,reason', [
    ('', '', 'active', 'Active', 'MISSING_PROVIDER_KEY'),
    ('not-a-date', '', 'active', 'Active', 'INVALID_DATE'),
    ('2021-01-01', '', 'active', 'Active', 'IPO_AFTER_SNAPSHOT'),
    ('2000-01-01', '', 'active', 'Delisted', 'STATE_STATUS_MISMATCH'),
    ('2000-01-01', '', 'delisted', 'Delisted', 'MISSING_DELISTING_DATE'),
    ('2000-01-01', '1999-01-01', 'delisted', 'Delisted', 'INVALID_LIFETIME'),
])
def test_conflicting_observations_are_retained_with_a_common_reason(tmp_path, ipo, terminal, state, status, reason):
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', state, [
        ['EXAMPLE', 'Example Issuer', 'NYSE', 'Stock', ipo, terminal, status]])
    history = history_from(replay(tmp_path))
    assert history['observation_rows'] == 1
    index = pd.read_parquet(history['candidate_index']['path'])
    assert reason in json.loads(index.review_reasons.iloc[0])
    assert history['registered_identities'] == 0


@pytest.mark.parametrize('change', ['bytes', 'domain', 'query', 'timezone'])
def test_invalid_source_cannot_replace_the_published_history(tmp_path, change):
    source = snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'active', [
        ['LIVE', 'Listed Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active']])
    replay(tmp_path)
    gold = tmp_path / 'gold/survivorship'
    originals = {p: p.read_bytes() for p in gold.rglob('*') if p.is_file()}
    metadata = source.with_suffix('.metadata.json')
    value = json.loads(metadata.read_bytes())
    if change == 'bytes':
        source.write_bytes(source.read_bytes() + b'changed')
    elif change == 'domain':
        value['source_url'] = value['source_url'].replace('www.alphavantage.co', 'example.test')
    elif change == 'query':
        value['source_url'] = value['source_url'].replace('2020-01-03', '2021-01-03')
    else:
        value['retrieved_at'] = '2026-09-10T22:05:00'
    metadata.write_text(json.dumps(value), 'utf-8')
    with pytest.raises(ValueError):
        replay(tmp_path)
    assert {p: p.read_bytes() for p in originals} == originals
