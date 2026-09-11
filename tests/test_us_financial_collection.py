"""The financial collection workflow retains missing and ambiguous listings."""
import json
from pathlib import Path

import pandas as pd

from test_listing_history_replay import snapshot


def test_plan_keeps_every_listing_and_collects_resolvable_retired_issuers(tmp_path):
    from engine.workflows.us_financial_collection import plan_us_financial_collection
    root = tmp_path / 'bronze/listings'
    snapshot(root, '2020-01-03', 'active', [
        ['LIVE', 'Live Issuer', 'NYSE', 'Stock', '2000-01-01', '', 'Active'],
        ['REUSE', 'New Issuer', 'NYSE', 'Stock', '2019-01-01', '', 'Active']])
    snapshot(root, '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted'],
        ['UNKNOWN', 'Unknown Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted'],
        ['REUSE', 'Prior Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    current = tmp_path / 'silver/current.csv'
    current.parent.mkdir()
    current.write_text('cik,ticker,title\n123,LIVE,Live Issuer\n456,REUSE,New Issuer\n', 'utf-8')
    aliases = tmp_path / 'silver/aliases.csv'
    aliases.write_text('cik,ticker,title\n789,OLD,Old Issuer\n', 'utf-8')
    result = plan_us_financial_collection(as_of='2020-01-03', listing_root=root,
        ticker_map_path=current, ticker_aliases_path=aliases,
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold')
    ledger = pd.read_parquet(result['listing_collection_ledger']['path'])
    assert len(ledger) == 5
    assert ledger.set_index('symbol').loc['UNKNOWN', 'collection_status'] == 'missing_cik'
    assert ledger.loc[ledger.symbol.eq('REUSE'), 'collection_status'].tolist() == [
        'ambiguous_listing_identity', 'ambiguous_listing_identity']
    selected = pd.read_csv(result['ticker_map']['path'], dtype=str, keep_default_na=False)
    assert selected.ticker.tolist() == ['LIVE', 'OLD']
    assert selected.cik.tolist() == ['123', '789']
    assert result['coverage_complete'] is False
    assert result['unresolved_listing_keys'] == 3
    assert json.loads((tmp_path / 'gold/collection_plan.json').read_bytes())['unresolved_listing_keys'] == 3
    again = plan_us_financial_collection(as_of='2020-01-03', listing_root=root,
        ticker_map_path=current, ticker_aliases_path=aliases,
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold')
    assert again['generation'] == result['generation']


def test_delisted_symbol_does_not_take_an_unrelated_current_issuer_cik(tmp_path):
    from engine.workflows.us_financial_collection import plan_us_financial_collection
    snapshot(tmp_path / 'bronze', '2020-01-03', 'delisted', [
        ['OLD', 'Former Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    mapping = tmp_path / 'current.csv'
    mapping.write_text('cik,ticker,title\n456,OLD,Different Current Issuer\n', 'utf-8')
    result = plan_us_financial_collection(as_of='2020-01-03', listing_root=tmp_path / 'bronze',
        ticker_map_path=mapping, ticker_aliases_path=tmp_path / 'missing.csv',
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold')
    assert result['collection_symbols'] == []
    assert result['status_counts'] == {'issuer_name_mismatch': 1}


def test_planned_collection_reuses_sources_and_missing_cik_does_not_abort_batch(tmp_path):
    from engine.workflows.us_financial_collection import plan_us_financial_collection, collect_us_financial_plan
    from io import BytesIO
    from unittest.mock import patch
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted'],
        ['MISSING', 'Missing Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    mapping = tmp_path / 'current.csv'
    mapping.write_text('cik,ticker,title\n123,OLD,Old Issuer\n', 'utf-8')
    plan = plan_us_financial_collection(as_of='2020-01-03', listing_root=tmp_path / 'bronze/listings',
        ticker_map_path=mapping, ticker_aliases_path=tmp_path / 'none.csv',
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold')
    filing_calls = []
    def external_filings(company, forms, start, end):
        filing_calls.append(company['cik'])
        return []
    raw = b'{"cik":123,"entityName":"Old Issuer","facts":{}}'
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(raw)) as http:
        first = collect_us_financial_plan(plan, start_date='1994-01-01',
            filings_dir=tmp_path / 'bronze/filings', companyfacts_dir=tmp_path / 'bronze/companyfacts',
            filings_provider=external_filings, sleep_seconds=0)
        again = collect_us_financial_plan(plan, start_date='1994-01-01',
            filings_dir=tmp_path / 'bronze/filings', companyfacts_dir=tmp_path / 'bronze/companyfacts',
            filings_provider=external_filings, sleep_seconds=0)
    assert http.call_count == 1
    assert len(filing_calls) == 1
    assert first['unresolved_listing_keys'] == 1
    assert first['coverage_complete'] is False
    assert again['filings']['symbols_resumed'] == 1
    assert (tmp_path / 'bronze/companyfacts/CIK0000000123.json').read_bytes() == raw


def test_unavailable_companyfacts_response_is_retained_and_not_retried_in_same_day(tmp_path):
    from engine.extractors.sec_filings import download_us_companyfacts
    from io import BytesIO
    from urllib.error import HTTPError
    from unittest.mock import patch
    mapping = tmp_path / 'map.csv'
    mapping.write_text('cik,ticker,title\n123,OLD,Old Issuer\n', 'utf-8')
    root = tmp_path / 'bronze/companyfacts'
    body = b'{"error":"No Company Facts found"}'
    failure = HTTPError('https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json',
        404, 'Not Found', {}, BytesIO(body))
    with patch('engine.extractors._internal.sec_filings.urlopen', side_effect=failure) as http:
        assert download_us_companyfacts(symbols=['OLD'], ticker_map_path=mapping, output_dir=root, sleep_seconds=0) == []
        assert download_us_companyfacts(symbols=['OLD'], ticker_map_path=mapping, output_dir=root, sleep_seconds=0) == []
    assert http.call_count == 1
    response, = root.rglob('response.bin')
    assert response.read_bytes() == body
    metadata = json.loads(response.with_name('metadata.json').read_bytes())
    assert metadata['http_status'] == 404
    assert metadata['source_url'].endswith('/CIK0000000123.json')


def test_retained_bulk_names_supply_financial_sources_without_binding_a_historical_symbol(tmp_path):
    from io import BytesIO
    from unittest.mock import patch
    from test_sec_bulk_submissions import archive_bytes
    from engine.workflows.sec_submissions import run_sec_submissions_refresh
    from engine.workflows.us_financial_collection import plan_us_financial_collection

    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted'],
        ['MISSING', 'Unmatched Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    mapping = tmp_path / 'map.csv'
    mapping.write_text('cik,ticker,title\n456,OTHER,Other Issuer\n', 'utf-8')
    with patch('urllib.request.urlopen', side_effect=AssertionError('Planning must use local sources')):
        plan = plan_us_financial_collection(as_of='2020-01-03',
            listing_root=tmp_path / 'bronze/listings', ticker_map_path=mapping,
            ticker_aliases_path=tmp_path / 'none.csv', submissions_manifest=tmp_path / 'silver/bulk/latest.json',
            output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold/plan')
    candidates = pd.read_parquet(plan['source_candidates']['path'])
    assert candidates.symbol.tolist() == ['OLD']
    assert candidates.issuer_cik.tolist() == ['0000000123']
    assert candidates.identity_verified.tolist() == [False]
    assert plan['source_candidate_ciks'] == ['0000000123']
    assert plan['collection_symbols'] == []
    assert plan['unresolved_listing_keys'] == 2
    ledger = pd.read_parquet(plan['listing_collection_ledger']['path']).set_index('symbol')
    assert ledger.loc['OLD', 'delistingDate'] == '2005-12-31'
    assert ledger.collection_cik.isna().all()

    from engine.workflows.us_financial_collection import collect_us_financial_plan
    raw = b'{"cik":123,"entityName":"Current Name","facts":{}}'
    with patch('engine.extractors._internal.sec_filings.urlopen', return_value=BytesIO(raw)) as http:
        result = collect_us_financial_plan(plan, start_date='1994-01-01',
            companyfacts_dir=tmp_path / 'bronze/companyfacts', sleep_seconds=0)
        again = collect_us_financial_plan(plan, start_date='1994-01-01',
            companyfacts_dir=tmp_path / 'bronze/companyfacts', sleep_seconds=0)
    assert http.call_count == 1
    assert http.call_args.args[0].full_url.endswith('/CIK0000000123.json')
    assert (tmp_path / 'bronze/companyfacts/CIK0000000123.json').read_bytes() == raw
    assert len(result['companyfacts_written']) == 1
    assert result['filings'] is None
    assert result['unresolved_listing_keys'] == 2
    assert result['financial_history_verified'] is False
    assert again['companyfacts_written'] == []


def test_unavailable_financial_sources_do_not_report_a_complete_reload(tmp_path):
    from io import BytesIO
    from urllib.error import HTTPError
    from unittest.mock import patch
    from engine.workflows.us_financial_collection import plan_us_financial_collection, collect_us_financial_plan
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    mapping = tmp_path / 'map.csv'
    mapping.write_text('cik,ticker,title\n123,OLD,Old Issuer\n', 'utf-8')
    plan = plan_us_financial_collection(as_of='2020-01-03', listing_root=tmp_path / 'bronze/listings',
        ticker_map_path=mapping, ticker_aliases_path=tmp_path / 'none.csv',
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold/plan')
    failure = HTTPError('https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json',
        404, 'Not Found', {}, BytesIO(b''))
    with patch('engine.extractors._internal.sec_filings.urlopen', side_effect=failure):
        result = collect_us_financial_plan(plan, start_date='1994-01-01', sleep_seconds=0,
            filings_provider=lambda *args: [], filings_dir=tmp_path / 'bronze/filings',
            companyfacts_dir=tmp_path / 'bronze/companyfacts')
    assert result['coverage_complete'] is False
    assert result['companyfacts_inventory']['status_counts'] == {'unavailable_http_404': 1}


def test_source_audit_reuses_verified_bytes_but_detects_a_changed_issuer(tmp_path):
    from engine.workflows.us_financial_collection import plan_us_financial_collection, audit_us_companyfacts_collection
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    mapping = tmp_path / 'map.csv'
    mapping.write_text('cik,ticker,title\n123,OLD,Old Issuer\n', 'utf-8')
    plan = plan_us_financial_collection(as_of='2020-01-03', listing_root=tmp_path / 'bronze/listings',
        ticker_map_path=mapping, ticker_aliases_path=tmp_path / 'none.csv',
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold/plan')
    root = tmp_path / 'bronze/companyfacts'
    root.mkdir()
    source = root / 'CIK0000000123.json'
    source.write_text('{"cik":123,"entityName":"Old Issuer","facts":{"us-gaap":{"Assets":{"units":{"USD":[{"val":100}]}}}}}', 'utf-8')
    first = audit_us_companyfacts_collection(plan, companyfacts_dir=root)
    again = audit_us_companyfacts_collection(plan, companyfacts_dir=root)
    assert first['status_counts'] == {'retained': 1}
    assert first['reused_payload_verifications'] == 0
    assert again['reused_payload_verifications'] == 1
    assert again['generation'] == first['generation']
    source.write_text('{"cik":456,"facts":{"us-gaap":{"Assets":{}}}}', 'utf-8')
    changed = audit_us_companyfacts_collection(plan, companyfacts_dir=root)
    assert changed['status_counts'] == {'invalid_payload': 1}
    assert changed['reused_payload_verifications'] == 0
    assert changed['all_sources_available'] is False
    assert changed['generation'] != first['generation']


def test_bulk_reported_ticker_and_former_name_resolve_retired_financial_issuer(tmp_path):
    from io import BytesIO
    from unittest.mock import patch
    from test_sec_bulk_submissions import archive_bytes
    from engine.workflows.sec_submissions import run_sec_submissions_refresh
    from engine.workflows.us_financial_collection import plan_us_financial_collection
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['ABC', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted'],
        ['UNRELATED', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    mapping = tmp_path / 'map.csv'
    mapping.write_text('cik,ticker,title\n456,CURRENT,Other Issuer\n', 'utf-8')
    plan = plan_us_financial_collection(as_of='2020-01-03', listing_root=tmp_path / 'bronze/listings',
        ticker_map_path=mapping, ticker_aliases_path=tmp_path / 'none.csv',
        submissions_manifest=tmp_path / 'silver/bulk/latest.json',
        output_dir=tmp_path / 'silver/plan', gold_dir=tmp_path / 'gold/plan')
    selected = pd.read_csv(plan['ticker_map']['path'], dtype=str)
    assert selected.ticker.tolist() == ['ABC']
    assert selected.cik.tolist() == ['123']
    ledger = pd.read_parquet(plan['listing_collection_ledger']['path']).set_index('symbol')
    assert ledger.loc['ABC', 'collection_status'] == 'ready'
    assert ledger.loc['ABC', 'delistingDate'] == '2005-12-31'
    evidence, = json.loads(ledger.loc['ABC', 'mapping_evidence'])
    assert evidence['mapping_kind'] == 'sec_submissions_ticker_and_name'
    assert evidence['reported_tickers'] == ['ABC', 'ABC-B']
    assert ledger.loc['UNRELATED', 'collection_status'] == 'missing_cik'
    assert plan['unresolved_listing_keys'] == 1

    # The recovered public plan must drive a real historical statement, not
    # only a new entry in a collection report.
    from engine.transformers.sec_filings import normalize_us_sec_filings
    facts = tmp_path / 'bronze/companyfacts'
    facts.mkdir()
    (facts / 'CIK0000000123.json').write_text(json.dumps(dict(cik=123, entityName='Former Name',
        facts={'us-gaap': {'Revenues': {'label': 'Revenue', 'units': {'USD': [dict(
            start='2004-01-01', end='2004-12-31', val=77, accn='0000000123-05-000001',
            fy=2004, fp='FY', form='10-K', filed='2005-02-15', frame='CY2004')]}}}})), 'utf-8')
    outputs = normalize_us_sec_filings(symbols=plan['collection_symbols'], start_year=2004, end_year=2004,
        companyfacts_dir=facts, filings_dir=tmp_path / 'bronze/no-filings',
        notes_root=tmp_path / 'bronze/no-notes', ticker_map_path=plan['ticker_map']['path'],
        output_dir=tmp_path / 'silver/normalized', report_metadata_path=tmp_path / 'silver/reports.csv',
        use_edgartools=False, workers=1, log_progress=False)
    assert [path.name for path in outputs] == ['us_normalized_ABC.csv']
    statement = pd.read_csv(outputs[0])
    revenue = statement.loc[statement.canonical_account_id.eq('REVENUE')]
    assert revenue.normalized_amount.tolist() == [77]


def test_explicit_symbol_refresh_restores_a_retired_statement_and_retains_missing_cik(tmp_path):
    from argparse import Namespace
    from io import BytesIO
    from unittest.mock import patch
    from engine.core.paths import DataLakePaths
    from engine.workflows.refresh import run_us_filing_refresh
    lake = DataLakePaths(tmp_path / 'data-lake')
    snapshot(lake.bronze('alpha-vantage', 'listings'), '2020-01-03', 'delisted', [
        ['OLD', 'Old Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted'],
        ['MISSING', 'Missing Issuer', 'NYSE', 'Stock', '1990-01-01', '2010-01-01', 'Delisted']])
    aliases = lake.meta('sec_ticker_aliases.csv')
    aliases.parent.mkdir(parents=True)
    aliases.write_text('cik,ticker,title\n123,OLD,Old Issuer\n', 'utf-8')
    calls, filing_queries = [], []
    def get(request, **kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith('/company_tickers.json'):
            return BytesIO(b'{"0":{"cik_str":456,"ticker":"LIVE","title":"Live Issuer"}}')
        assert request.full_url.endswith('/CIK0000000123.json')
        return BytesIO(json.dumps(dict(cik=123, entityName='Old Issuer', facts={'us-gaap': {
            'Revenues': {'label': 'Revenue', 'units': {'USD': [dict(start='2004-01-01', end='2004-12-31',
                val=77, accn='0000000123-05-000001', fy=2004, fp='FY', form='10-K',
                filed='2005-02-15', frame='CY2004')]}}}})).encode())
    def filings(company, forms, start, end):
        filing_queries.append((company['cik'], start, end))
        return []
    args = Namespace(symbols='OLD,MISSING', dry_run=False, workers=1, sleep_seconds=0,
        skip_clickhouse=True, progress_interval=10, force_full=False)
    with patch('engine.extractors._internal.sec_filings.urlopen', side_effect=get):
        result = run_us_filing_refresh(args, ['OLD', 'MISSING'], '20200103', None,
            data_lake=lake, filings_provider=filings)
    assert len(calls) == 2
    assert filing_queries == [('123', '1994-01-01', '2020-01-03')]
    assert result['unresolved_listing_keys'] == 1
    assert result['coverage_complete'] is False
    assert result['normalization_files'] == 1
    frame = pd.read_csv(lake.silver('sec', 'normalized', 'us_normalized_OLD.csv'))
    assert frame.loc[frame.canonical_account_id.eq('REVENUE'), 'normalized_amount'].tolist() == [77]
    report = json.loads(lake.gold('survivorship', 'us', 'financial_collection', 'refresh_result.json').read_bytes())
    assert report['unresolved_listing_keys'] == 1


def test_explicit_refresh_initializes_retained_alpha_history_before_financial_planning(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.workflows.refresh import RefreshState, resolve_us_refresh_symbols
    monkeypatch.setenv('ALPHA_VANTAGE_API_KEY', 'test-key')
    calls = []
    header = 'symbol,name,exchange,assetType,ipoDate,delistingDate,status\n'
    def get(url, *, params, **kwargs):
        calls.append(params['state'])
        row = ('OLD,Old Issuer,NYSE,Stock,1990-01-01,2010-01-01,Delisted\n'
            if params['state'] == 'delisted' else 'LIVE,Live Issuer,NYSE,Stock,1990-01-01,null,Active\n')
        return SimpleNamespace(status_code=200, content=(header + row).encode())
    monkeypatch.setattr('requests.get', get)
    state = RefreshState(tmp_path / 'state.json', {'completed_steps': []}, enabled=False)
    root = tmp_path / 'bronze/listings'
    result = resolve_us_refresh_symbols(['OLD'], targets={'filings'}, state=state, dry_run=False,
        end_date='2020-01-03', listing_source_dir=root, refresh_listings=True)
    assert result == ['OLD']
    assert calls == ['active', 'delisted']
    assert (root / 'snapshot_date=2020-01-03/delisted.csv').is_file()
    resolve_us_refresh_symbols(['OLD'], targets={'filings'}, state=state, dry_run=False,
        end_date='2020-01-03', listing_source_dir=root, refresh_listings=True)
    assert calls == ['active', 'delisted']
