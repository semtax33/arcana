"""Historical provider candidates find official CIK leads in the public pipeline."""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pandas as pd
import pytest

from engine.workflows.sec_submissions import run_sec_submissions_refresh
from engine.workflows.survivorship import run_survivorship_refresh
from test_sec_bulk_submissions import archive_bytes
from test_listing_history_replay import snapshot


def refresh(root):
    result = run_survivorship_refresh(market='us', end_date='2020-01-03', download=False,
        load_clickhouse=False, source_dir=root / 'bronze/listings',
        output_dir=root / 'silver/survivorship', gold_dir=root / 'gold/survivorship',
        manifest_path=root / 'silver/not_reviewed.json', identity_dir=root / 'silver/no_dei',
        sec_submissions_manifest=root / 'silver/bulk/latest.json', sec_evidence=True,
        sec_notice_source_dir=root / 'bronze/notices')
    discovery = json.loads(Path(result['issuer_discovery']['path']).read_bytes())
    return discovery, pd.read_parquet(discovery['candidate_discovery']['path'])


def test_former_name_finds_delisted_issuer_without_approving_security_identity(tmp_path):
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted'],
        ['MISSING', 'No Such Issuer', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    result, frame = refresh(tmp_path)
    assert len(frame) == 2
    row = frame.set_index('symbol').loc['OLD']
    assert json.loads(row.discovery_ciks) == ['0000000123']
    assert row.discovery_status == 'unique_name_candidate'
    assert row.identity_status == 'unresolved'
    evidence = json.loads(row.discovery_evidence)[0]
    assert evidence['name_kind'] == 'former'
    assert evidence['reported_from'] == '2001-01-01'
    assert evidence['reported_to'] == '2010-01-01'
    assert frame.set_index('symbol').loc['MISSING'].discovery_status == 'no_name_candidate'
    assert result['collection_only'] is True
    assert result['registered_identities'] == 0
    queue = pd.read_parquet(result['collection_candidates']['path'])
    assert queue.issuer_cik.tolist() == ['0000000123']


def test_same_name_multiple_ciks_are_kept_and_fund_rows_are_not_silently_removed(tmp_path):
    buffer = BytesIO(archive_bytes())
    with ZipFile(buffer, 'a') as archive:
        archive.writestr('CIK0000000456.json', json.dumps(dict(cik='456', name='Former Name',
            tickers=[], exchanges=[], formerNames=[], filings=dict(recent={},files=[]))))
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(buffer.getvalue())):
        run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted'],
        ['FUND', 'Current Name', 'NYSE', 'ETF', '1995-01-01', '2005-12-31', 'Delisted']])
    result, frame = refresh(tmp_path)
    assert len(frame) == 2
    assert frame.set_index('symbol').loc['OLD'].discovery_status == 'multiple_name_candidates'
    assert json.loads(frame.set_index('symbol').loc['OLD'].discovery_ciks) == ['0000000123', '0000000456']
    queue = pd.read_parquet(result['collection_candidates']['path'])
    assert queue.issuer_cik.tolist() == ['0000000123', '0000000456']
    assert queue.symbols.tolist() == ['["OLD"]', '["OLD"]']
    assert frame.identity_status.eq('unresolved').all()


def test_unchanged_discovery_is_reused_and_changed_names_preserve_previous_generation(tmp_path):
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    first, _ = refresh(tmp_path)
    prior = Path(first['candidate_discovery']['path']).read_bytes()
    repeated, _ = refresh(tmp_path)
    assert repeated['generation_reused'] is True
    assert repeated['generation'] == first['generation']
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Unmatched Corrected Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    changed, frame = refresh(tmp_path)
    assert changed['generation'] != first['generation']
    assert changed['generation_reused'] is False
    assert frame.discovery_status.tolist() == ['no_name_candidate']
    assert Path(first['candidate_discovery']['path']).read_bytes() == prior


def test_modified_bulk_index_cannot_reuse_discovery_or_replace_gold(tmp_path):
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        bulk = run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/bulk',
            output_dir=tmp_path / 'silver/bulk', gold_dir=tmp_path / 'gold/bulk')
    snapshot(tmp_path / 'bronze/listings', '2020-01-03', 'delisted', [
        ['OLD', 'Former Name', 'NYSE', 'Stock', '1995-01-01', '2005-12-31', 'Delisted']])
    refresh(tmp_path)
    gold = {p:p.read_bytes() for p in (tmp_path / 'gold/survivorship').rglob('*') if p.is_file()}
    index = Path(bulk['issuer_index']['path'])
    index.write_bytes(index.read_bytes() + b'changed index')
    with pytest.raises(ValueError, match='integrity mismatch'):
        refresh(tmp_path)
    assert all(path.read_bytes() == raw for path, raw in gold.items())
