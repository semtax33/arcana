"""Build the US listing population directly from retained Alpha observations."""
from hashlib import sha256
import json
from pathlib import Path
from datetime import date

import pandas as pd

from engine.core.serving_storage import export_frame, export_json


def build_alpha_listing_population(*, history, output_dir):
    """Use the latest provider observation per listing; retain delisted listings.

    Repeated tickers remain distinct by provider listing key. Date corrections
    use the most recent snapshot up to the requested cutoff. All original rows
    and conflict flags remain in listing_history, including unresolved dates.
    No SEC notice, manual approval, or price availability is needed for inclusion.
    """
    artifacts = [*history['partitions'], history['candidate_index']]
    for artifact in artifacts:
        if sha256(Path(artifact['path']).read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('Alpha listing history artifact hash mismatch')
    generation = sha256(json.dumps(dict(history=history['generation'],
        implementation=sha256(Path(__file__).read_bytes()).hexdigest()), sort_keys=True).encode()).hexdigest()
    output = Path(output_dir) / 'generations' / generation
    observations = pd.concat([pd.read_parquet(p['path']) for p in history['partitions']], ignore_index=True)
    # Delisted wins a same-day active/delisted tie, with the source contradiction
    # still explicit in review_reasons. It is not silently accepted as tradable.
    observations = observations.sort_values(
        ['snapshot_date', 'state', 'source_path', 'source_row'], kind='stable')
    index = pd.read_parquet(history['candidate_index']['path'])
    flags = index[['provider_listing_id', 'review_reasons']]
    population = observations.drop_duplicates('provider_listing_id', keep='last').merge(
        flags, on='provider_listing_id', validate='one_to_one')
    delistings = observations.loc[observations.state.eq('delisted')].drop_duplicates(
        'provider_listing_id', keep='last').merge(flags, on='provider_listing_id', validate='one_to_one')
    summary = dict(schema_version=1, market='us', provider='ALPHA_VANTAGE', as_of=history['as_of'],
        generation=generation, status='provider_listing_population_ready',
        provider_listing_keys=len(population), delisted_listing_keys=len(delistings),
        source_files=history['source_files'], observation_rows=history['observation_rows'],
        listing_population=export_frame(output / 'listing_population.parquet', population),
        delistings=export_frame(output / 'delistings.parquet', delistings),
        source_history=history['silver_manifest'], terminal_proceeds_resolved=False,
        policy='Alpha Vantage supplies listing membership and ipoDate/delistingDate. '
               'Latest retained query-date observations up to the cutoff take precedence; '
               'all historical rows and conflicts are retained. Delisted rows remain included '
               'without SEC or manual review. Provider dates are not settlement amounts, '
               'last executable trade dates, or evidence of point-in-time publication.')
    summary['silver_manifest'] = export_json(output / 'manifest.json', summary)
    return summary


def resolve_alpha_listing_symbols(*, as_of, source_dir=None, output_dir=None):
    """Include active and retired Stock symbols in source collection targets."""
    from engine.core.paths import DATA_LAKE
    from engine.transformers.listing_history import normalize_alpha_listing_history
    history = normalize_alpha_listing_history(
        root=source_dir or DATA_LAKE.bronze('alpha-vantage', 'listings'), end_date=as_of,
        output_dir=output_dir or DATA_LAKE.silver('survivorship', 'us', 'listing_history'))
    if history is None:
        raise ValueError('US history requires retained Alpha Vantage LISTING_STATUS snapshots')
    index = pd.read_parquet(history['candidate_index']['path'])
    return sorted(set(index.loc[index.assetType.eq('Stock'), 'symbol']) - {''})


def apply_alpha_listing_population(bundle, *, population):
    """Attach provider listing periods without inferring merger consideration.

    Reviewed security lifecycles take precedence. Alpha's Stock classification
    stays explicit; it is not relabeled as an independently verified common class.
    """
    result = {key: list(value) for key, value in bundle.items()}
    artifact = population['listing_population']
    if sha256(Path(artifact['path']).read_bytes()).hexdigest() != artifact['sha256']:
        raise ValueError('Alpha listing population hash mismatch')
    frame = pd.read_parquet(artifact['path'])
    covered_symbols = {row['symbol'] for row in result['listing_episodes']}
    symbol_counts = frame.groupby('symbol').provider_listing_id.nunique()
    blocked_aliases = set()
    for row in frame.to_dict('records'):
        if row['assetType'] not in {'Stock', 'ETF'} or row['symbol'] in covered_symbols:
            continue
        key = row['provider_listing_id']
        reasons = json.loads(row['review_reasons'])
        try:
            start = date.fromisoformat(row['ipoDate'])
            end = date.fromisoformat(row['delistingDate']) if row['delistingDate'] not in ('', 'null', 'None') else None
            if (start < date(1900, 1, 1) or start > date(2299, 12, 31)
                    or (end and (end <= start or end > date(2299, 12, 31)))
                    or (row['state'] == 'delisted' and end is None)):
                raise ValueError('Invalid provider listing interval')
        except ValueError:
            result['unresolved'].append(dict(provider_listing_id=key, symbol=row['symbol'],
                reason='INVALID_PROVIDER_LISTING_INTERVAL', source_path=row['source_path'],
                source_sha256=row['source_sha256']))
            continue
        ambiguous = symbol_counts[row['symbol']] != 1
        if ambiguous:
            result['unresolved'].append(dict(provider_listing_id=key, symbol=row['symbol'],
                reason='PROVIDER_SYMBOL_REUSE', source_path=row['source_path'],
                source_sha256=row['source_sha256']))
        sid = 'SEC_US_ALPHA_' + key if ambiguous else 'SEC_US_' + row['symbol']
        source_id = 'ALPHA_LISTING_' + row['source_version']
        published = str(row['retrieved_at'])[:10]
        if not any(source['source_id'] == source_id for source in result['sources']):
            result['sources'].append(dict(source_id=source_id, provider='ALPHA_VANTAGE',
                path=row['source_path'], source_url=row['source_url'], source_sha256=row['source_sha256'],
                published_date=published, availability_basis='retrieval_date', snapshot_date=row['snapshot_date']))
        episode = dict(episode_id='ALPHA_' + key, security_id=sid,
            issuer_id='ISS_US_ALPHA_' + key if ambiguous else 'ISS_US_' + row['symbol'],
            symbol=row['symbol'], country='US', exchange_code=row['exchange'],
            security_type='provider_stock' if row['assetType'] == 'Stock' else 'provider_etf',
            status='confirmed', valid_from=start.isoformat(), valid_until=end.isoformat() if end else None,
            published_date=published, source_ids=[source_id], source_url=row['source_url'],
            source_sha256=row['source_sha256'], listing_source='ALPHA_VANTAGE',
            provider_listing_id=key, review_reasons=reasons, source_row=row['source_row'],
            availability_basis='retrospective_provider_listing_history')
        result['listing_episodes'].append(episode)
        if ambiguous and row['symbol'] not in blocked_aliases:
            # The legacy symbol-only ID cannot stand in for two issuers. Keep
            # it covered but non-investable, so current-master fallback cannot
            # reintroduce the very alias whose binding is unresolved.
            blocked_aliases.add(row['symbol'])
            result['listing_episodes'].append(dict(episode,
                episode_id='ALPHA_ALIAS_' + row['symbol'], security_id='SEC_US_' + row['symbol'],
                security_type='unresolved_provider_alias', valid_until=None))
        if row['assetType'] == 'Stock' and end and end.isoformat() <= population['as_of']:
            result['events'].append(dict(event_id='ALPHA_DELISTING_' + key, security_id=sid,
                event_type='delisting', effective_date=end.isoformat(), published_date=published,
                cash_payment_date=None, cash_per_share=None, currency='USD', status='confirmed',
                entitlements_complete=False, source_ids=[source_id], source_url=row['source_url'],
                source_sha256=row['source_sha256'], listing_source='ALPHA_VANTAGE'))
    return result
