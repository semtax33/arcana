"""Find official CIK research leads without declaring security identities."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import sha256_file
from engine.extractors.sec_submissions import SEC_SUBMISSIONS_URL
from engine.transformers.listing_identity import _name


def discover_sec_issuers(*, history, submissions_manifest, output_dir):
    manifest_path = Path(submissions_manifest)
    source = json.loads(manifest_path.read_bytes())
    if (source['source_url'] != SEC_SUBMISSIONS_URL
            or sha256_file(source['source_path']) != source['source_sha256']
            or sha256_file(source['issuer_index']['path']) != source['issuer_index']['sha256']
            or sha256_file(history['candidate_index']['path']) != history['candidate_index']['sha256']):
        raise ValueError('Issuer discovery input integrity mismatch')
    inputs = dict(listing_generation=history['generation'], listing_sha256=history['candidate_index']['sha256'],
        submissions_manifest=str(manifest_path.resolve()), submissions_manifest_sha256=sha256_file(manifest_path),
        source_sha256=source['source_sha256'], index_sha256=source['issuer_index']['sha256'],
        code_sha256=sha256_file(__file__), name_rule_sha256=sha256_file(Path(__file__).with_name('listing_identity.py')))
    generation = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = Path(output_dir) / 'generations' / generation
    saved_path = folder / 'manifest.json'
    if saved_path.exists():
        saved = json.loads(saved_path.read_bytes())
        if saved['inputs'] == inputs and all(
                Path(saved[key]['path']).resolve() == (folder / name).resolve()
                and Path(saved[key]['path']).exists()
                and sha256_file(saved[key]['path']) == saved[key]['sha256']
                for key, name in [('candidate_discovery','candidate_discovery.parquet'),
                    ('collection_candidates','collection_candidates.parquet')]):
            return dict(saved, generation_reused=True)
    candidates = pd.read_parquet(history['candidate_index']['path'])
    wanted = {key for names in candidates.observed_names for name in json.loads(names) if (key := _name(name))}
    matches = defaultdict(list)
    scanned, ignored_names = 0, 0
    columns = ['issuer_cik','reported_name','reported_tickers','reported_exchanges',
        'former_names','source_member','member_sha256','review_reasons']
    for batch in pq.ParquetFile(source['issuer_index']['path']).iter_batches(batch_size=5000, columns=columns):
        for filer in batch.to_pylist():
            scanned += 1
            names = [(filer['reported_name'], 'current', None, None)]
            former = json.loads(filer['former_names'])
            if isinstance(former, list):
                for item in former:
                    if isinstance(item, dict) and isinstance(item.get('name'), str):
                        names.append((item['name'], 'former', item.get('from'), item.get('to')))
                    else:
                        ignored_names += 1
            elif former is not None:
                ignored_names += 1
            for reported, kind, start, stop in names:
                key = _name(reported)
                if key not in wanted:
                    continue
                matches[key].append(dict(issuer_cik=filer['issuer_cik'], reported_name=reported,
                    reported_tickers=json.loads(filer['reported_tickers']),
                    reported_exchanges=json.loads(filer['reported_exchanges']),
                    name_kind=kind, reported_from=start, reported_to=stop,
                    source_member=filer['source_member'], member_sha256=filer['member_sha256'],
                    source_review_reasons=json.loads(filer['review_reasons'])))
    rows, queue = [], {}
    for original in candidates.to_dict('records'):
        evidence = []
        seen = set()
        for name in json.loads(original['observed_names']):
            for match in matches.get(_name(name), []):
                edge = dict(match, provider_name=name)
                fingerprint = json.dumps(edge, sort_keys=True)
                if fingerprint not in seen:
                    evidence.append(edge)
                    seen.add(fingerprint)
        ciks = sorted({edge['issuer_cik'] for edge in evidence})
        status = 'no_name_candidate' if not ciks else 'unique_name_candidate' if len(ciks) == 1 else 'multiple_name_candidates'
        rows.append(dict(**original, discovery_ciks=json.dumps(ciks), discovery_status=status,
            discovery_evidence=json.dumps(evidence, ensure_ascii=False), collection_only=True))
        if original['assetType'] == 'Stock':
            for cik in ciks:
                entry = queue.setdefault(cik, dict(issuer_cik=cik, provider_listing_ids=set(), symbols=set()))
                entry['provider_listing_ids'].add(original['provider_listing_id'])
                entry['symbols'].add(original['symbol'])
    frame = pd.DataFrame(rows)
    collection = pd.DataFrame([dict(issuer_cik=cik, provider_listing_ids=json.dumps(sorted(row['provider_listing_ids'])),
        symbols=json.dumps(sorted(row['symbols'])), source_member=f'CIK{cik}.json', collection_only=True)
        for cik, row in sorted(queue.items())], columns=['issuer_cik','provider_listing_ids','symbols','source_member','collection_only'])
    result = dict(schema_version=1, generation=generation, inputs=inputs,
        candidate_discovery=export_frame(folder / 'candidate_discovery.parquet', frame),
        collection_candidates=export_frame(folder / 'collection_candidates.parquet', collection),
        source_url=source['source_url'], source_retrieved_at=source['retrieved_at'],
        source_index_complete=source['metadata_index_complete'], scanned_filers=scanned,
        provider_candidates=len(frame), discovery_counts=dict(Counter(frame.discovery_status)),
        collection_ciks=len(collection), ignored_malformed_names=ignored_names,
        collection_only=True, registered_identities=0, coverage_complete=False,
        policy='Current/former name matches select CIKs for disclosure research only. Name dates are not listing dates; no security, price, or point-in-time factor binding is approved.')
    export_json(saved_path, result)
    return dict(result, generation_reused=False)
