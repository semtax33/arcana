"""Retain the SEC's bulk submission history without per-issuer HTTP requests."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import new_source_run_id, sha256_file
from engine.transformers._internal.edgar_identity import resolve_edgar_identity

SEC_SUBMISSIONS_URL = 'https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip'


def download_sec_submissions_bulk(*, source_dir=None, force=False):
    root = Path(source_dir or DATA_LAKE.bronze('sec', 'submissions-bulk')).resolve()
    today = datetime.now(timezone.utc).date().isoformat()
    latest = root / 'latest.json'
    if latest.exists() and not force:
        pointer = json.loads(latest.read_bytes())
        metadata_path = Path(pointer['metadata_path']).resolve()
        if not metadata_path.is_relative_to(root) or sha256_file(metadata_path) != pointer['metadata_sha256']:
            raise ValueError('SEC bulk archive pointer integrity mismatch')
        saved = json.loads(metadata_path.read_bytes())
        source = Path(saved['source_path']).resolve()
        if not source.is_relative_to(root) or saved['source_url'] != SEC_SUBMISSIONS_URL:
            raise ValueError('SEC bulk archive source provenance mismatch')
        if (saved['retrieved_at'][:10] == today and source.is_file()
                and source.stat().st_size == saved['bytes'] and sha256_file(source) == saved['source_sha256']):
            return dict(saved, cache_reused=True)
    folder = root / new_source_run_id()
    folder.mkdir(parents=True, exist_ok=False)
    partial = folder / 'response.partial'
    metadata_path = folder / 'metadata.json'
    metadata = dict(provider='SEC', source_url=SEC_SUBMISSIONS_URL, status='downloading',
        retrieved_at=datetime.now(timezone.utc).isoformat(), source_path=str(partial), bytes=0,
        historical_listing_intervals_verified=False)
    export_json(metadata_path, metadata)
    digest, received, progress_at = hashlib.sha256(), 0, time.monotonic()
    try:
        request = Request(SEC_SUBMISSIONS_URL, headers={'User-Agent':resolve_edgar_identity(), 'Accept-Encoding':'identity'})
        failure = None
        try:
            response = urlopen(request, timeout=60)
        except HTTPError as exc:
            response, failure = exc, exc
        with response, partial.open('wb') as stream:
            headers = getattr(response, 'headers', {}) or {}
            metadata.update(http_status=getattr(response, 'status', 200),
                content_length=headers.get('Content-Length'), last_modified=headers.get('Last-Modified'),
                etag=headers.get('ETag'), http_date=headers.get('Date'))
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if time.monotonic() - progress_at >= 20:
                    print(f'[SEC BULK] received_bytes={received}', flush=True)
                    export_json(metadata_path, dict(metadata, bytes=received))
                    progress_at = time.monotonic()
        if failure is not None:
            raise failure
        if metadata['content_length'] is not None and received != int(metadata['content_length']):
            raise ValueError('Incomplete SEC bulk response')
        with ZipFile(partial) as archive:
            names = archive.namelist()
            if not names or len(set(names)) != len(names):
                raise ValueError('Empty or duplicate SEC bulk archive members')
            metadata['members'] = len(names)
        target = folder / 'submissions.zip'
        partial.replace(target)
        metadata.update(status='retained', source_path=str(target), bytes=received,
            source_sha256=digest.hexdigest(), retrieved_at=datetime.now(timezone.utc).isoformat())
        export_json(metadata_path, metadata)
        export_json(latest, dict(metadata_path=str(metadata_path), metadata_sha256=sha256_file(metadata_path)))
        return dict(metadata, cache_reused=False)
    except Exception as exc:
        export_json(metadata_path, dict(metadata, status='failed', bytes=received,
            source_sha256=digest.hexdigest(), error_type=type(exc).__name__))
        raise
