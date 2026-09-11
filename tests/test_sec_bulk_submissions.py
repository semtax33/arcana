"""Whole SEC submission archives are retained once and replayed locally."""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
import pandas as pd
import pytest


def archive_bytes():
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('CIK0000000123.json', json.dumps(dict(cik='123', name='Current Name',
            tickers=['ABC', 'ABC-B'], exchanges=['NYSE', 'NYSE'],
            formerNames=[dict(name='Former Name', **{'from':'2001-01-01', 'to':'2010-01-01'})],
            filings=dict(recent={}, files=[]))))
    return buffer.getvalue()


def test_bulk_download_preserves_bytes_and_reuses_a_verified_daily_archive(tmp_path):
    from engine.extractors.sec_submissions import download_sec_submissions_bulk
    raw = archive_bytes()
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(raw)) as http:
        first = download_sec_submissions_bulk(source_dir=tmp_path / 'bronze/sec/submissions')
        second = download_sec_submissions_bulk(source_dir=tmp_path / 'bronze/sec/submissions')
    assert Path(first['source_path']).read_bytes() == raw
    assert first['members'] == 1
    assert first['cache_reused'] is False
    assert second['cache_reused'] is True
    assert first['source_sha256'] == second['source_sha256']
    assert http.call_count == 1
    assert first['source_url'] == 'https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip'


def test_bulk_normalization_preserves_former_names_without_inventing_listing_dates(tmp_path):
    from engine.extractors.sec_submissions import download_sec_submissions_bulk
    from engine.transformers.sec_submissions import normalize_sec_submissions_bulk
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(archive_bytes())):
        source = download_sec_submissions_bulk(source_dir=tmp_path / 'bronze/sec/submissions')
    first = normalize_sec_submissions_bulk(source=source, output_dir=tmp_path / 'silver/sec/submissions')
    again = normalize_sec_submissions_bulk(source=source, output_dir=tmp_path / 'silver/sec/submissions')
    frame = pd.read_parquet(first['issuer_index']['path'])
    assert frame.issuer_cik.tolist() == ['0000000123']
    assert frame.reported_name.tolist() == ['Current Name']
    assert json.loads(frame.iloc[0].reported_tickers) == ['ABC', 'ABC-B']
    assert json.loads(frame.iloc[0].former_names) == [{'name':'Former Name', 'from':'2001-01-01', 'to':'2010-01-01'}]
    assert frame.iloc[0].historical_listing_intervals_verified == False
    assert 'valid_from' not in frame.columns
    assert again['generation_reused'] is True


def test_incomplete_response_keeps_previous_source_pointer(tmp_path):
    from engine.extractors.sec_submissions import download_sec_submissions_bulk
    root = tmp_path / 'bronze/sec/submissions'
    raw = archive_bytes()
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(raw)):
        source = download_sec_submissions_bulk(source_dir=root)
    previous = (root / 'latest.json').read_bytes()
    incomplete = BytesIO(raw)
    incomplete.headers = {'Content-Length':str(len(raw) + 10)}
    with patch('engine.extractors.sec_submissions.urlopen', return_value=incomplete):
        with pytest.raises(ValueError, match='Incomplete'):
            download_sec_submissions_bulk(source_dir=root, force=True)
    assert (root / 'latest.json').read_bytes() == previous
    assert Path(source['source_path']).read_bytes() == raw
    failed, = root.rglob('response.partial')
    assert failed.read_bytes() == raw


def test_public_pipeline_reports_failed_issuer_metadata_without_claiming_complete_history(tmp_path):
    from engine.workflows.sec_submissions import run_sec_submissions_refresh
    buffer = BytesIO(archive_bytes())
    with ZipFile(buffer, 'a') as archive:
        archive.writestr('CIK0000000456.json', json.dumps(dict(cik='789', name='Wrong CIK')))
        archive.writestr('CIK0000000123-submissions-001.json', '{}')
    with patch('engine.extractors.sec_submissions.urlopen', return_value=BytesIO(buffer.getvalue())):
        result = run_sec_submissions_refresh(source_dir=tmp_path / 'bronze/sec',
            output_dir=tmp_path / 'silver/sec', gold_dir=tmp_path / 'gold/sec')
    assert result['counts']['indexed_issuers'] == 1
    assert result['counts']['primary_members'] == 2
    assert result['counts']['continuation_members'] == 1
    assert result['failed_primary_members'] == 1
    assert result['metadata_index_complete'] is False
    assert json.loads((tmp_path / 'gold/sec/summary.json').read_bytes())['historical_listing_intervals_verified'] is False
    assert json.loads(Path(result['failures']['path']).read_bytes()) == [
        {'source_member':'CIK0000000456.json', 'error_type':'ValueError'}]
