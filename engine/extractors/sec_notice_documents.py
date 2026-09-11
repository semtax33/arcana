"""Retain complete SEC terminal-notice submissions by CIK and accession."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pyarrow.dataset as ds

from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import new_source_run_id, sha256_file
from engine.transformers._internal.edgar_identity import resolve_edgar_identity


def _legacy_notice_document(path, expected_form):
    raw = Path(path).read_bytes().strip()
    if not re.fullmatch(rb'(?:<DOCUMENT>.*?</DOCUMENT>\s*)+', raw, re.S | re.I):
        return False
    documents = re.findall(rb'<DOCUMENT>(.*?)</DOCUMENT>', raw, re.S | re.I)
    if len(documents) != len(re.findall(rb'<DOCUMENT>', raw, re.I)):
        return False
    form = re.search(rb'<TYPE>[ \t]*([^\r\n<]+)', documents[0], re.I)
    return bool(form and form.group(1).decode('ascii', errors='replace').strip().upper() == expected_form.upper()
        and all(re.search(rb'<TEXT>.*?</TEXT>', document, re.S | re.I) for document in documents))


def download_sec_notice_documents(*, inventory, source_dir=None, output_dir=None,
                                  max_requests=None, download=True, force=False, retry_failed=False):
    source = inventory['filings']
    if sha256_file(source['path']) != source['sha256']:
        raise ValueError('SEC notice inventory integrity mismatch')
    if max_requests is not None and (isinstance(max_requests, bool) or max_requests < 0):
        raise ValueError('max_requests must be nonnegative or None')
    root = Path(source_dir or DATA_LAKE.bronze('sec', 'notice-submissions')).resolve()
    output = Path(output_dir or DATA_LAKE.silver('sec', 'notice-submissions')).resolve()
    selected = ds.dataset(source['path']).to_table(filter=(
        ds.field('form_group').isin(['exchange_removal_notice', 'reporting_termination_notice'])
        & ds.field('on_or_before_cutoff')), columns=['issuer_cik', 'accession', 'filing_date',
            'form', 'primary_document', 'source_member', 'source_row', 'member_sha256']).to_pandas()
    counts, records = Counter(), []
    last_request, access_denied, progress_at = 0.0, False, time.monotonic()
    for (cik, accession), rows in selected.groupby(['issuer_cik', 'accession'], sort=True):
        if not re.fullmatch(r'[0-9]{10}', cik) or not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession):
            raise ValueError('Invalid CIK or accession in notice inventory')
        url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}.txt'
        record = dict(issuer_cik=cik, accession=accession, source_url=url,
            source_occurrences=len(rows), source_references=rows.to_json(orient='records'),
            metadata_path='', metadata_sha256='', status='pending')
        folder = root / cik / accession
        pointer_path = folder / 'latest.json'
        attempt_path = folder / 'last_attempt.json'
        cached_pointer = pointer_path if pointer_path.exists() else attempt_path
        if any(rows[column].nunique(dropna=False) > 1 for column in ('filing_date','form','primary_document')):
            record['status'] = 'metadata_conflict'
        elif rows.primary_document.str.lower().str.endswith('.paper').any():
            record['status'] = 'paper_notice_requires_review'
        elif cached_pointer.exists() and (not force or not download):
            pointer = json.loads(cached_pointer.read_bytes())
            meta_path = Path(pointer['metadata_path']).resolve()
            if not meta_path.is_relative_to(folder) or sha256_file(meta_path) != pointer['metadata_sha256']:
                raise ValueError('SEC notice metadata integrity mismatch')
            metadata = json.loads(meta_path.read_bytes())
            original = Path(metadata['source_path']).resolve()
            if (not original.is_relative_to(folder) or metadata['source_url'] != url
                    or metadata['accession'] != accession or sha256_file(original) != metadata['source_sha256']):
                raise ValueError('SEC notice retained source integrity mismatch')
            record.update(status='reused' if metadata['status']=='retained' else 'failed_cached',
                metadata_path=str(meta_path), metadata_sha256=pointer['metadata_sha256'])
            if (metadata.get('http_status')==200
                    and (metadata['status']=='failed' or metadata.get('validation_status')=='legacy_document_requires_review')
                    and _legacy_notice_document(original, rows.form.iloc[0])):
                record['status'] = 'legacy_document_requires_review'
            elif (metadata['status']=='failed' and retry_failed and download and not access_denied
                    and (max_requests is None or counts['requests'] < max_requests)):
                record['status'] = 'pending'
            else:
                access_denied |= metadata.get('http_status') in {403, 429}
        elif access_denied or not download or (max_requests is not None and counts['requests'] >= max_requests):
            record['status'] = 'pending'
        if (record['status']=='pending' and download and not access_denied
                and (max_requests is None or counts['requests'] < max_requests)):
            wait = 0.25 - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            last_request = time.monotonic()
            counts['requests'] += 1
            retained = folder / new_source_run_id()
            retained.mkdir(parents=True, exist_ok=False)
            original = retained / 'submission.txt'
            meta_path = retained / 'metadata.json'
            metadata = dict(provider='SEC', source_url=url, issuer_cik=cik, accession=accession,
                retrieved_at=datetime.now(timezone.utc).isoformat(), source_path=str(original),
                status='downloading', collection_only=True)
            export_json(meta_path, metadata)
            failure = None
            try:
                try:
                    response = urlopen(Request(url, headers={'User-Agent':resolve_edgar_identity(),
                        'Accept-Encoding':'identity'}), timeout=45)
                except HTTPError as exc:
                    response, failure = exc, exc
                with response, original.open('wb') as handle:
                    headers = getattr(response, 'headers', {}) or {}
                    metadata.update(http_status=getattr(response, 'status', 200),
                        response_url=response.geturl() if hasattr(response, 'geturl') else url,
                        content_length=headers.get('Content-Length'), http_date=headers.get('Date'),
                        last_modified=headers.get('Last-Modified'), content_type=headers.get('Content-Type'))
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
                if failure is not None:
                    raise failure
                if metadata['content_length'] is not None and original.stat().st_size != int(metadata['content_length']):
                    raise ValueError('Incomplete SEC notice response')
                with original.open('rb') as handle:
                    header = handle.read(128 * 1024).decode('utf-8', errors='replace')
                if not re.search(r'ACCESSION NUMBER:\s*' + re.escape(accession) + r'\b', header):
                    if not _legacy_notice_document(original, rows.form.iloc[0]):
                        raise ValueError('Response is not the requested SEC submission')
                    metadata['validation_status'] = 'legacy_document_requires_review'
                metadata.update(status='retained', source_sha256=sha256_file(original), bytes=original.stat().st_size)
                export_json(meta_path, metadata)
                pointer = dict(metadata_path=str(meta_path), metadata_sha256=sha256_file(meta_path))
                export_json(pointer_path, pointer)
                export_json(attempt_path, pointer)
                record.update(status=metadata.get('validation_status','retained'), **pointer)
            except Exception as exc:
                if not original.exists():
                    original.touch()
                metadata.update(status='failed', source_sha256=sha256_file(original),
                    bytes=original.stat().st_size, error_type=type(exc).__name__)
                export_json(meta_path, metadata)
                pointer = dict(metadata_path=str(meta_path), metadata_sha256=sha256_file(meta_path))
                export_json(attempt_path, pointer)
                record.update(status='failed', **pointer)
                access_denied |= metadata.get('http_status') in {403, 429}
        counts[record['status']] += 1
        records.append(record)
        if time.monotonic() - progress_at >= 20:
            print(f'[SEC NOTICES] processed={len(records)} requests={counts["requests"]} retained={counts["retained"]} failed={counts["failed"]}', flush=True)
            progress_at = time.monotonic()
    run = output / 'runs' / new_source_run_id()
    result = dict(as_of=inventory['as_of'], inventory_generation=inventory['generation'],
        inventory_source=source, source_occurrences=len(selected), requested_submissions=len(records),
        counts=dict(counts), records=export_frame(run/'records.parquet', pd.DataFrame(records)),
        collection_only=True, registered_identities=0, coverage_complete=False,
        access_denied=access_denied, retry_failed=retry_failed, failed_requests_require_explicit_retry=True,
        document_collection_complete=all(row['status'] in {'retained','reused'} for row in records))
    export_json(run/'summary.json', result)
    export_json(output/'latest.json', result)
    return result
