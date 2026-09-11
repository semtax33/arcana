"""Public SEC notice collection retains evidence and resumes without refetching."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pandas as pd


def inventory(root, rows=None):
    rows = rows or [dict(issuer_cik='0000000123', accession='0000000123-20-000001',
        filing_date='2020-01-03', form='25-NSE', form_group='exchange_removal_notice',
        primary_document='xslF25X02/primary_doc.xml', source_member='CIK0000000123.json',
        source_row=1, member_sha256='a'*64, on_or_before_cutoff=True)]
    path = root/'silver/inventory.parquet'
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return dict(as_of='2020-01-03', generation='fixture',
        filings=dict(path=str(path), sha256=sha256(path.read_bytes()).hexdigest()))


DOCUMENT = b'''<SEC-DOCUMENT>0000000123-20-000001.txt : 20200103
<SEC-HEADER>
ACCESSION NUMBER: 0000000123-20-000001
CONFORMED SUBMISSION TYPE: 25-NSE
FILED AS OF DATE: 20200103
SUBJECT COMPANY:
    CENTRAL INDEX KEY: 0000000123
</SEC-HEADER>
<DOCUMENT>
<TYPE>25-NSE
<TEXT><xml>Notice retained as submitted</xml></TEXT>
</DOCUMENT>
</SEC-DOCUMENT>'''


def test_public_notice_collector_preserves_response_and_reuses_verified_source(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    source = inventory(tmp_path)
    with patch('engine.extractors.sec_notice_documents.urlopen', return_value=BytesIO(DOCUMENT)) as http:
        first = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
        repeated = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
    assert http.call_count == 1
    assert first['counts']['retained'] == 1
    assert repeated['counts']['reused'] == 1
    records = pd.read_parquet(first['records']['path'])
    metadata = json.loads(Path(records.iloc[0].metadata_path).read_bytes())
    assert Path(metadata['source_path']).read_bytes() == DOCUMENT
    assert metadata['source_sha256'] == sha256(DOCUMENT).hexdigest()
    assert Path(metadata['source_path']).is_relative_to(tmp_path/'bronze/notices')
    assert metadata['source_url'] == 'https://www.sec.gov/Archives/edgar/data/123/0000000123-20-000001.txt'
    assert metadata['accession'] == '0000000123-20-000001'
    assert first['coverage_complete'] is False
    assert first['registered_identities'] == 0


def test_sec_access_denial_is_retained_and_stops_batch_without_repeating_failed_request(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    first = dict(issuer_cik='0000000123', accession='0000000123-20-000001',
        filing_date='2020-01-03', form='25-NSE', form_group='exchange_removal_notice',
        primary_document='primary_doc.xml', source_member='CIK0000000123.json',
        source_row=1, member_sha256='a'*64, on_or_before_cutoff=True)
    source = inventory(tmp_path, [first, dict(first, accession='0000000123-20-000002', source_row=2)])
    refusal = HTTPError('https://www.sec.gov', 403, 'Forbidden', {}, BytesIO(b'SEC access denied'))
    with patch('engine.extractors.sec_notice_documents.urlopen', side_effect=refusal) as http:
        result = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
        repeated = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
    assert http.call_count == 1
    assert result['counts']['failed'] == 1
    assert result['counts']['pending'] == 1
    assert result['document_collection_complete'] is False
    assert repeated['counts']['failed_cached'] == 1
    rows = pd.read_parquet(result['records']['path'])
    metadata = json.loads(Path(rows.iloc[0].metadata_path).read_bytes())
    assert metadata['http_status'] == 403
    assert Path(metadata['source_path']).read_bytes() == b'SEC access denied'


def test_paper_notices_and_conflicting_occurrences_remain_explicit_without_http(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    base = dict(issuer_cik='0000000123', accession='0000000123-20-000001',
        filing_date='2020-01-03', form='25-NSE', form_group='exchange_removal_notice',
        primary_document='notice.paper', source_member='CIK0000000123.json',
        source_row=1, member_sha256='a'*64, on_or_before_cutoff=True)
    conflict = dict(base, accession='0000000123-20-000002', primary_document='notice.xml')
    future = dict(base, accession='0000000123-21-000001', filing_date='2021-01-03', on_or_before_cutoff=False)
    source = inventory(tmp_path, [base, conflict, dict(conflict, filing_date='2020-01-02'), future])
    with patch('engine.extractors.sec_notice_documents.urlopen', return_value=BytesIO(DOCUMENT)) as http:
        result = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
    assert http.call_count == 0
    assert result['source_occurrences'] == 3
    assert result['counts']['paper_notice_requires_review'] == 1
    assert result['counts']['metadata_conflict'] == 1
    assert result['document_collection_complete'] is False


def test_no_download_replay_uses_retained_evidence_even_with_force_flag(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    source = inventory(tmp_path)
    with patch('engine.extractors.sec_notice_documents.urlopen', return_value=BytesIO(DOCUMENT)) as http:
        download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
        replay = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices', download=False, force=True)
    assert http.call_count == 1
    assert replay['counts'].get('reused') == 1


def test_retry_failed_only_preserves_successful_source_and_prior_failure(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    original = inventory(tmp_path)
    rows = pd.read_parquet(original['filings']['path']).to_dict('records')
    source = inventory(tmp_path, rows + [dict(rows[0], accession='0000000123-20-000002', source_row=2)])
    failure = HTTPError('https://www.sec.gov', 503, 'Unavailable', {}, BytesIO(b'Temporarily unavailable'))
    with patch('engine.extractors.sec_notice_documents.urlopen', side_effect=[BytesIO(DOCUMENT), failure]):
        initial = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices')
    failed = pd.read_parquet(initial['records']['path']).iloc[1]
    old_meta = Path(failed.metadata_path).read_bytes()
    old_body = Path(json.loads(old_meta)['source_path'])
    second_document = DOCUMENT.replace(b'0000000123-20-000001', b'0000000123-20-000002')
    with patch('engine.extractors.sec_notice_documents.urlopen', return_value=BytesIO(second_document)) as http:
        retry = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices', retry_failed=True)
    assert http.call_count == 1
    assert retry['counts']['reused'] == 1
    assert retry['counts']['retained'] == 1
    assert Path(failed.metadata_path).read_bytes() == old_meta
    assert old_body.read_bytes() == b'Temporarily unavailable'


def test_legacy_headerless_source_is_reassessed_without_rewriting_or_downloading(tmp_path):
    from engine.extractors.sec_notice_documents import download_sec_notice_documents
    source = inventory(tmp_path)
    folder = tmp_path/'bronze/notices/0000000123/0000000123-20-000001/old_attempt'
    folder.mkdir(parents=True)
    raw = b'<DOCUMENT>\n<TYPE>25-NSE\n<TEXT>Legacy notice body</TEXT>\n</DOCUMENT>\n'
    original = folder/'submission.txt'; original.write_bytes(raw)
    metadata = dict(status='failed', http_status=200, error_type='ValueError',
        issuer_cik='0000000123', accession='0000000123-20-000001',
        source_url='https://www.sec.gov/Archives/edgar/data/123/0000000123-20-000001.txt',
        source_path=str(original), source_sha256=sha256(raw).hexdigest(),
        retrieved_at='2026-09-11T00:00:00+00:00')
    meta_path = folder/'metadata.json'; meta_path.write_text(json.dumps(metadata))
    original_meta = meta_path.read_bytes()
    (folder.parent/'last_attempt.json').write_text(json.dumps(dict(metadata_path=str(meta_path),
        metadata_sha256=sha256(original_meta).hexdigest())))
    with patch('engine.extractors.sec_notice_documents.urlopen') as http:
        result = download_sec_notice_documents(inventory=source, source_dir=tmp_path/'bronze/notices',
            output_dir=tmp_path/'silver/notices', download=False, retry_failed=True)
    assert http.call_count == 0
    assert result['counts'].get('legacy_document_requires_review') == 1
    assert meta_path.read_bytes() == original_meta
    assert original.read_bytes() == raw
    assert result['document_collection_complete'] is False
