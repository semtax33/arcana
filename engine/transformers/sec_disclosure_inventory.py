"""Inventory all filing occurrences for discovered CIKs in one bulk ZIP session."""
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote
from zipfile import ZipFile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.extractors.sec_submissions import SEC_SUBMISSIONS_URL


def _form_group(form):
    base = form.upper().removesuffix('/A')
    if base in {'25', '25-NSE'}:
        return 'exchange_removal_notice'
    if base in {'15', '15-12B', '15-12G', '15-15D', '15F-12B', '15F-12G', '15F-15D'}:
        return 'reporting_termination_notice'
    if base in {'10-K', '10-Q', '20-F', '40-F'}:
        return 'financial_report'
    if base in {'8-K', '6-K'}:
        return 'current_report'
    if base in {'S-4', 'F-4', 'S-4MEF', 'F-4MEF', 'DEFM14A', 'PREM14A', 'DEFM14C', 'PREM14C', 'SC 13E3', 'SC TO-T', 'SC TO-I', '425'}:
        return 'transaction_document'
    return 'other'


def build_sec_disclosure_inventory(*, discovery, end_date, output_dir):
    cutoff = date.fromisoformat(end_date).isoformat()
    source_path = Path(discovery['inputs']['submissions_manifest'])
    source = json.loads(source_path.read_bytes())
    queue_artifact = discovery['collection_candidates']
    if (sha256_file(source_path) != discovery['inputs']['submissions_manifest_sha256']
            or source['source_url'] != SEC_SUBMISSIONS_URL
            or sha256_file(source['source_path']) != source['source_sha256']
            or sha256_file(queue_artifact['path']) != queue_artifact['sha256']):
        raise ValueError('SEC disclosure inventory input integrity mismatch')
    inputs = dict(source_sha256=source['source_sha256'], queue_sha256=queue_artifact['sha256'],
        discovery_generation=discovery['generation'], cutoff=cutoff, code_sha256=sha256_file(__file__))
    generation = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir) / 'generations' / generation
    manifest_path = folder / 'manifest.json'
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_bytes())
        if saved['inputs'] == inputs and all(Path(saved[key]['path']).resolve() == (folder / filename).resolve()
                and Path(saved[key]['path']).exists() and sha256_file(saved[key]['path']) == saved[key]['sha256']
                for key, filename in [('filings','filings.parquet'),('reviews','reviews.json'),('cik_coverage','cik_coverage.json')]):
            return dict(saved, generation_reused=True)
    queue = pd.read_parquet(queue_artifact['path'])
    if not queue.issuer_cik.is_unique or not queue.issuer_cik.str.fullmatch(r'[0-9]{10}').all():
        raise ValueError('Invalid or duplicated collection CIK')
    folder.mkdir(parents=True, exist_ok=True)
    temporary, target = folder / 'filings.parquet.partial', folder / 'filings.parquet'
    string_fields = ['issuer_cik','accession','filing_date','acceptance_datetime','form','form_group',
        'primary_document','document_url_candidate','source_member','member_sha256','reported_row','review_reasons']
    schema = pa.schema([(key,pa.string()) for key in string_fields] + [
        ('source_row',pa.int64()),('on_or_before_cutoff',pa.bool_()),('collection_only',pa.bool_())])
    counts, groups, reviews, coverage, buffer = Counter(), Counter(), [], [], []
    progress_at = time.monotonic()
    with ZipFile(source['source_path']) as archive, pq.ParquetWriter(temporary, schema) as writer:
        for cik in queue.issuer_cik:
            issuer_rows, issuer_members, issuer_failures = 0, 0, 0
            seen = {}
            primary_name = f'CIK{cik}.json'
            try:
                raw = archive.read(primary_name)
                primary = json.loads(raw)
                if str(primary['cik']).zfill(10) != cik:
                    raise ValueError('Primary CIK mismatch')
                filings = primary.get('filings') or {}
                members = [(primary_name, raw, filings.get('recent') or {}, None)]
                refs = filings.get('files') or []
                if len({ref['name'] for ref in refs}) != len(refs):
                    raise ValueError('Duplicated continuation reference')
                for ref in refs:
                    name = ref['name']
                    if not re.fullmatch(rf'CIK{cik}-submissions-[0-9]+\.json', name):
                        raise ValueError('Continuation belongs to another CIK or has an invalid name')
                    # Load one issuer's history, never reopen the archive per CIK.
                    try:
                        older_raw = archive.read(name)
                        members.append((name,older_raw,json.loads(older_raw),ref.get('filingCount')))
                    except Exception as exc:
                        issuer_failures += 1
                        reviews.append(dict(issuer_cik=cik,source_member=name,reason='CONTINUATION_READ_FAILED',error_type=type(exc).__name__))
            except Exception as exc:
                reviews.append(dict(issuer_cik=cik,source_member=primary_name,reason='PRIMARY_HISTORY_FAILED',error_type=type(exc).__name__))
                coverage.append(dict(issuer_cik=cik,rows=0,members=0,failed_members=1))
                counts['failed_members'] += 1
                continue
            for name, raw, columns, declared_count in members:
                try:
                    if not isinstance(columns, dict) or any(not isinstance(values,list) for values in columns.values()):
                        raise ValueError('Filing history must contain column arrays')
                    n = len(columns.get('accessionNumber', []))
                    if columns and (not {'accessionNumber','filingDate','form'}.issubset(columns)
                            or any(len(values) != n for values in columns.values())):
                        raise ValueError('Filing column lengths or required fields mismatch')
                except Exception as exc:
                    issuer_failures += 1
                    reviews.append(dict(issuer_cik=cik,source_member=name,reason='MALFORMED_HISTORY_COLUMNS',error_type=type(exc).__name__))
                    continue
                issuer_members += 1
                if declared_count is not None and n != declared_count:
                    reviews.append(dict(issuer_cik=cik,source_member=name,reason='DECLARED_COUNT_MISMATCH',reported=declared_count,actual=n))
                member_hash = hashlib.sha256(raw).hexdigest()
                for i in range(n):
                    reported = {key:values[i] for key,values in columns.items()}
                    encoded = json.dumps(reported, ensure_ascii=False, sort_keys=True)
                    accn = str(reported.get('accessionNumber') or '')
                    filed = str(reported.get('filingDate') or '')
                    form = str(reported.get('form') or '')
                    document = str(reported.get('primaryDocument') or '')
                    reasons, before = [], False
                    valid_accn = bool(re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accn))
                    if not valid_accn:
                        reasons.append('INVALID_ACCESSION')
                    try:
                        before = date.fromisoformat(filed).isoformat() <= cutoff
                    except ValueError:
                        reasons.append('INVALID_FILING_DATE')
                    if not form:
                        reasons.append('MISSING_FORM')
                    decoded = unquote(document)
                    safe_document = bool(decoded) and not any(part in {'','.', '..'} for part in decoded.split('/')) and not any(char in decoded for char in '\\?#:')
                    url = ''
                    if safe_document and valid_accn:
                        url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace("-", "")}/{quote(decoded, safe="/")}'
                    else:
                        reasons.append('MISSING_OR_UNSAFE_PRIMARY_DOCUMENT')
                    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
                    if accn in seen and seen[accn] != fingerprint:
                        reviews.append(dict(issuer_cik=cik,accession=accn,reason='ACCESSION_METADATA_CONFLICT'))
                    elif accn in seen:
                        counts['repeated_accession_occurrences'] += 1
                    seen[accn] = fingerprint
                    group = _form_group(form)
                    buffer.append(dict(issuer_cik=cik,accession=accn,filing_date=filed,
                        acceptance_datetime=str(reported.get('acceptanceDateTime') or ''),form=form,form_group=group,
                        primary_document=document,document_url_candidate=url,source_member=name,member_sha256=member_hash,
                        reported_row=encoded,review_reasons=json.dumps(reasons),source_row=i+1,
                        on_or_before_cutoff=before,collection_only=True))
                    issuer_rows += 1
                    counts['filing_occurrences'] += 1
                    counts['filings_on_or_before_cutoff' if before else 'filings_after_cutoff_or_invalid_date'] += 1
                    if reasons:
                        counts['rows_requiring_review'] += 1
                    if before:
                        groups[group] += 1
                    if len(buffer) >= 5000:
                        writer.write_table(pa.Table.from_pylist(buffer,schema=schema));buffer.clear()
            counts['failed_members'] += issuer_failures
            coverage.append(dict(issuer_cik=cik,rows=issuer_rows,members=issuer_members,failed_members=issuer_failures))
            if time.monotonic()-progress_at >= 20:
                print(f'[SEC INVENTORY] CIKs={len(coverage)}/{len(queue)} rows={counts["filing_occurrences"]}',flush=True)
                progress_at=time.monotonic()
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer,schema=schema))
    temporary.replace(target)
    result = dict(schema_version=1,generation=generation,inputs=inputs,as_of=cutoff,
        filings=dict(path=str(target.resolve()),sha256=sha256_file(target),rows=counts['filing_occurrences']),
        reviews=export_json(folder/'reviews.json',reviews),cik_coverage=export_json(folder/'cik_coverage.json',coverage),
        counts=dict(counts),form_groups_on_or_before_cutoff=dict(groups),requested_ciks=len(queue),
        metadata_inventory_complete=counts['failed_members']==0 and not reviews,
        collection_only=True,registered_identities=0,coverage_complete=False,
        policy='Filing occurrences and constructed document URLs are collection candidates. Filing/acceptance dates are not corporate-action effective dates; duplicates and conflicts remain explicit.')
    export_json(manifest_path,result)
    return dict(result,generation_reused=False)
