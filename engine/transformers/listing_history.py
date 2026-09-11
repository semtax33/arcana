"""Normalize retained provider observations without inventing legal identities."""
from __future__ import annotations

from datetime import date, datetime
from collections import Counter
from hashlib import sha256
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from engine.core.serving_storage import export_frame, export_json
from engine.transformers.listing_source_quality import COLUMNS, PROVIDER_KEY


def _candidate_index(partitions):
    columns = list(COLUMNS) + ['provider_listing_id', 'snapshot_date', 'state']
    observations = pd.concat([pd.read_parquet(p['path'], columns=columns) for p in partitions], ignore_index=True)
    missing = {'', 'null', 'None'}
    ipo = pd.to_datetime(observations.ipoDate.where(~observations.ipoDate.isin(missing)), errors='coerce', format='%Y-%m-%d')
    terminal = pd.to_datetime(observations.delistingDate.where(~observations.delistingDate.isin(missing)), errors='coerce', format='%Y-%m-%d')
    snapshots = pd.to_datetime(observations.snapshot_date)
    flags = {
        'MISSING_PROVIDER_KEY': observations[list(PROVIDER_KEY)].isin(missing).any(axis=1),
        'INVALID_DATE': ((~observations.ipoDate.isin(missing) & ipo.isna())
                         | (~observations.delistingDate.isin(missing) & terminal.isna())),
        'IPO_AFTER_SNAPSHOT': ipo.gt(snapshots),
        'STATE_STATUS_MISMATCH': observations.state.ne(observations.status.str.lower()),
        'MISSING_DELISTING_DATE': observations.state.eq('delisted') & observations.delistingDate.isin(missing),
        'INVALID_LIFETIME': terminal.lt(ipo),
    }
    for reason, mask in flags.items():
        observations[reason] = mask
    keys_per_symbol = observations.groupby('symbol').provider_listing_id.nunique()
    records = []
    for key, group in observations.groupby('provider_listing_id', sort=True):
        first = group.iloc[0]
        names = sorted(set(group['name']) - {''})
        terminal_dates = sorted(set(group.delistingDate) - {'', 'null', 'None'})
        reasons = [reason for reason in flags if group[reason].any()]
        if keys_per_symbol[first.symbol] > 1:
            reasons.append('SYMBOL_MULTIPLE_PROVIDER_KEYS')
        if len(terminal_dates) > 1:
            reasons.append('DELISTING_DATE_CHANGED')
        if len(names) > 1:
            reasons.append('NAME_CHANGED')
        if group.groupby('snapshot_date').state.nunique().gt(1).any():
            reasons.append('SIMULTANEOUS_ACTIVE_DELISTED')
        records.append(dict(provider_listing_id=key,
            **{column: first[column] for column in PROVIDER_KEY},
            first_snapshot=group.snapshot_date.min(), last_snapshot=group.snapshot_date.max(),
            observations=len(group), observed_names=json.dumps(names, ensure_ascii=False),
            observed_delisting_dates=json.dumps(terminal_dates),
            review_reasons=json.dumps(reasons), identity_status='unresolved'))
    return pd.DataFrame(records)


def normalize_alpha_listing_history(*, root, end_date, output_dir):
    """Retain each source row and distinguish query date from retrieval time.

    A provider listing key identifies observations only. It does not establish
    common-stock class, issuer continuity, execution, or terminal proceeds.
    """
    root, output = Path(root), Path(output_dir)
    cutoff = date.fromisoformat(end_date).isoformat()
    version = sha256(Path(__file__).read_bytes() + '|'.join(COLUMNS).encode()).hexdigest()
    sources, partitions = [], []
    cache = dict(partitions_reused=0, partitions_built=0)
    for path in sorted(root.glob('snapshot_date=*/*.csv')):
        if path.stem not in {'active', 'delisted'}:
            continue
        day = date.fromisoformat(path.parent.name.removeprefix('snapshot_date=')).isoformat()
        if day > cutoff:
            continue
        raw = path.read_bytes()
        metadata_path = path.with_suffix('.metadata.json')
        metadata_raw = metadata_path.read_bytes()
        metadata = json.loads(metadata_raw)
        digest = sha256(raw).hexdigest()
        url = urlparse(metadata.get('source_url', ''))
        query = parse_qs(url.query)
        if (metadata.get('provider') != 'ALPHA_VANTAGE'
                or metadata.get('snapshot_date') != day or metadata.get('state') != path.stem
                or metadata.get('source_sha256') != digest
                or url.scheme != 'https' or url.hostname not in {'alphavantage.co', 'www.alphavantage.co'}
                or url.path != '/query' or query != {
                    'function': ['LISTING_STATUS'], 'date': [day], 'state': [path.stem]}):
            raise ValueError(f'Listing history source provenance mismatch: {day}/{path.stem}')
        retrieved = datetime.fromisoformat(metadata['retrieved_at'].replace('Z', '+00:00'))
        if retrieved.tzinfo is None:
            raise ValueError('Listing history retrieval time must include a time zone')
        source_version = sha256(raw + metadata_raw).hexdigest()
        source = dict(snapshot_date=day, state=path.stem, source_path=str(path.resolve()),
            source_sha256=digest, metadata_path=str(metadata_path.resolve()),
            metadata_sha256=sha256(metadata_raw).hexdigest(), source_url=metadata['source_url'],
            retrieved_at=metadata['retrieved_at'], source_version=source_version)
        sources.append(source)
        target = output / 'partitions' / f'normalizer={version}' / f'source={source_version}' / 'observations.parquet'
        cache_path = target.with_suffix('.metadata.json')
        if target.exists() and cache_path.exists():
            saved = json.loads(cache_path.read_bytes())
            if (saved.get('normalizer_version') == version and saved.get('source_version') == source_version
                    and saved.get('artifact', {}).get('rows') == metadata.get('rows')
                    and saved.get('artifact', {}).get('sha256') == sha256(target.read_bytes()).hexdigest()):
                partitions.append(dict(saved['artifact'], path=str(target.resolve())))
                cache['partitions_reused'] += 1
                continue
        frame = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
        if not set(COLUMNS).issubset(frame.columns) or not len(frame) or len(frame) != metadata.get('rows'):
            raise ValueError(f'Listing history rows or columns mismatch: {day}/{path.stem}')
        frame = frame[list(COLUMNS)].copy()
        frame['provider_listing_id'] = [sha256(json.dumps(list(key), ensure_ascii=False).encode()).hexdigest()
            for key in frame[list(PROVIDER_KEY)].itertuples(index=False, name=None)]
        frame['source_row'] = range(1, len(frame) + 1)
        frame['observation_id'] = [sha256(f'{source_version}:{row}'.encode()).hexdigest()
            for row in frame.source_row]
        for key, value in source.items():
            frame[key] = value
        frame['provider'] = 'ALPHA_VANTAGE'
        frame['market'] = 'us'
        artifact = export_frame(target, frame)
        export_json(cache_path, dict(normalizer_version=version, source_version=source_version, artifact=artifact))
        partitions.append(artifact)
        cache['partitions_built'] += 1
    if not sources:
        return None
    generation = sha256(json.dumps(dict(normalizer=version, sources=sources, as_of=cutoff), sort_keys=True).encode()).hexdigest()
    generation_dir = output / 'generations' / generation
    manifest_path = generation_dir / 'manifest.json'
    candidates = None
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_bytes())
        index_path = generation_dir / 'candidate_index.parquet'
        if (saved.get('sources') == sources and saved.get('partitions') == partitions
                and saved.get('normalizer_version') == version and saved.get('as_of') == cutoff
                and index_path.exists()
                and saved.get('candidate_index', {}).get('sha256') == sha256(index_path.read_bytes()).hexdigest()):
            candidates = pd.read_parquet(index_path)
    index_reused = candidates is not None
    if candidates is None:
        candidates = _candidate_index(partitions)
    candidate_artifact = export_frame(generation_dir / 'candidate_index.parquet', candidates)
    reasons = Counter(reason for value in candidates.review_reasons
        for reason in (json.loads(value) or ['NO_DETECTED_SOURCE_CONFLICT']))
    summary = dict(schema_version=1, market='us', provider='ALPHA_VANTAGE', as_of=cutoff,
        status='listing_observations_normalized_identity_resolution_pending',
        normalizer_version=version, generation=generation, sources=sources, partitions=partitions,
        candidate_index=candidate_artifact, provider_listing_keys=len(candidates),
        review_reason_counts=dict(sorted(reasons.items())),
        observation_rows=sum(p['rows'] for p in partitions), source_files=len(sources),
        coverage_complete=False, registered_identities=0,
        policy='Snapshot dates are provider query dates, not retrieval or publication dates. '
               'Provider observations do not establish common-stock identity, tradability, or settlement rights.')
    summary['silver_manifest'] = export_json(manifest_path, summary)
    summary['cache'] = cache
    summary['candidate_index_reused'] = index_reused
    return summary
