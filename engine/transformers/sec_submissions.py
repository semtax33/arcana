"""Index issuer discovery evidence from a retained SEC bulk submission ZIP."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.extractors.sec_submissions import SEC_SUBMISSIONS_URL

FIELDS = ['issuer_cik', 'reported_name', 'reported_tickers', 'reported_exchanges',
    'former_names', 'entity_type', 'filing_continuations', 'recent_filing_count',
    'source_member', 'member_sha256', 'source_retrieved_at', 'review_reasons']


def normalize_sec_submissions_bulk(*, source, output_dir):
    path = Path(source['source_path'])
    if source['source_url'] != SEC_SUBMISSIONS_URL or sha256_file(path) != source['source_sha256']:
        raise ValueError('SEC bulk source integrity mismatch')
    retrieved = datetime.fromisoformat(source['retrieved_at'])
    if retrieved.tzinfo is None:
        raise ValueError('SEC bulk retrieval timestamp requires timezone')
    version = sha256_file(__file__)
    generation = hashlib.sha256(json.dumps(dict(source=source['source_sha256'],
        retrieved_at=source['retrieved_at'], version=version), sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir) / 'generations' / generation
    manifest_path, target = folder / 'manifest.json', folder / 'issuer_index.parquet'
    if manifest_path.exists() and target.exists():
        saved = json.loads(manifest_path.read_bytes())
        if saved['generation'] == generation and sha256_file(target) == saved['issuer_index']['sha256']:
            return dict(saved, generation_reused=True)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / 'issuer_index.parquet.partial'
    schema = pa.schema([(field, pa.string()) for field in FIELDS] + [('historical_listing_intervals_verified', pa.bool_())])
    buffer, failures, counters, review_counts = [], [], Counter(), Counter()
    progress_at = time.monotonic()
    with ZipFile(path) as archive, pq.ParquetWriter(temporary, schema) as writer:
        members = archive.infolist()
        names = {info.filename for info in members}
        if len(names) != len(members):
            raise ValueError('Duplicated SEC archive members')
        for info in members:
            match = re.fullmatch(r'CIK([0-9]{10})\.json', info.filename)
            if not match:
                counters['continuation_members' if re.fullmatch(r'CIK[0-9]{10}-submissions-[0-9]+\.json', info.filename) else 'other_members'] += 1
                continue
            counters['primary_members'] += 1
            try:
                raw = archive.read(info)
                payload = json.loads(raw)
                cik = str(payload['cik']).zfill(10)
                if not cik.isdigit() or cik != match.group(1):
                    raise ValueError('CIK does not match archive member')
                reasons = []
                for field in ['tickers', 'exchanges', 'formerNames']:
                    if not isinstance(payload.get(field), list):
                        reasons.append(f'INVALID_{field.upper()}')
                if not isinstance(payload.get('name'), str) or not payload['name'].strip():
                    reasons.append('MISSING_REPORTED_NAME')
                filings = payload.get('filings') or {}
                continuations = filings.get('files', [])
                if any(item.get('name') not in names for item in continuations):
                    reasons.append('MISSING_CONTINUATION_MEMBER')
                record = dict(issuer_cik=cik, reported_name=str(payload.get('name') or ''),
                    reported_tickers=json.dumps(payload.get('tickers'), ensure_ascii=False),
                    reported_exchanges=json.dumps(payload.get('exchanges'), ensure_ascii=False),
                    former_names=json.dumps(payload.get('formerNames'), ensure_ascii=False),
                    entity_type=str(payload.get('entityType') or ''),
                    filing_continuations=json.dumps(continuations),
                    recent_filing_count=str(len((filings.get('recent') or {}).get('accessionNumber', []))),
                    source_member=info.filename, member_sha256=hashlib.sha256(raw).hexdigest(),
                    source_retrieved_at=source['retrieved_at'], review_reasons=json.dumps(reasons),
                    historical_listing_intervals_verified=False)
                buffer.append(record)
                counters['indexed_issuers'] += 1
                if payload.get('tickers'):
                    counters['issuers_with_reported_tickers'] += 1
                if payload.get('formerNames'):
                    counters['issuers_with_former_names'] += 1
                review_counts.update(reasons)
            except Exception as exc:
                failures.append(dict(source_member=info.filename, error_type=type(exc).__name__))
            if len(buffer) >= 5000:
                writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
                buffer.clear()
            if time.monotonic() - progress_at >= 20:
                print(f'[SEC BULK INDEX] primary={counters["primary_members"]} indexed={counters["indexed_issuers"]} failed={len(failures)}', flush=True)
                progress_at = time.monotonic()
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=schema))
    temporary.replace(target)
    result = dict(schema_version=1, generation=generation, code_sha256=version,
        source_path=str(path.resolve()), source_sha256=source['source_sha256'],
        source_url=source['source_url'], retrieved_at=source['retrieved_at'],
        counts=dict(counters), failed_primary_members=len(failures),
        failures=export_json(folder / 'failures.json', failures), review_reason_counts=dict(review_counts),
        issuer_index=dict(path=str(target.resolve()), sha256=sha256_file(target), rows=counters['indexed_issuers']),
        metadata_index_complete=not failures, historical_listing_intervals_verified=False,
        policy='Issuer discovery observations; current/former names and their reported dates do not establish security listing lifetimes.')
    export_json(manifest_path, result)
    return dict(result, generation_reused=False)
