"""Check retained Alpha population through the real database universe reader."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.serving_storage import export_json
from engine.loaders.survivorship import load_survivorship, SCHEMAS
from engine.workflows.survivorship import read_reviewed_manifest
from api.repository.universe_query import load_universe_details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--reviewed', type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads((args.gold / 'summary.json').read_bytes())
    assert not any(name in summary for name in ['issuer_discovery', 'filing_inventory', 'notice_documents'])
    for artifact in summary['artifacts']:
        assert sha256(Path(artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
    silver = Path(summary['output_dir'])
    kinds = ('listing_episodes', 'events', 'entitlements', 'trading_halts', 'sources', 'unresolved')
    bundle = {kind: json.loads((silver / f'{kind}.json').read_bytes())['rows'] for kind in kinds}
    reviewed = read_reviewed_manifest(args.reviewed, market='us', end_date=summary['as_of'])
    for kind in ('listing_episodes', 'events', 'entitlements', 'trading_halts'):
        assert all(row in bundle[kind] for row in reviewed[kind]), f'Reviewed {kind} changed'
    population = json.loads((args.gold / 'alpha_listing_population.json').read_bytes())
    history = json.loads((args.gold / 'listing_history.json').read_bytes())
    for source in history['sources']:
        assert sha256(Path(source['source_path']).read_bytes()).hexdigest() == source['source_sha256']
        assert sha256(Path(source['metadata_path']).read_bytes()).hexdigest() == source['metadata_sha256']
    listing = pd.read_parquet(population['listing_population']['path']).set_index('provider_listing_id')
    verified = 0
    for episode in bundle['listing_episodes']:
        if episode.get('listing_source') != 'ALPHA_VANTAGE':
            continue
        row = listing.loc[episode['provider_listing_id']]
        assert episode['source_sha256'] == row.source_sha256
        assert episode['valid_from'] == row.ipoDate
        if episode['security_type'] != 'unresolved_provider_alias':
            expected_end = row.delistingDate if row.delistingDate not in ('', 'null', 'None') else None
            assert episode['valid_until'] == expected_end
        verified += 1
    client = get_clickhouse_client()
    prefix = 'verify_alpha_' + uuid4().hex + '_'
    try:
        publication = load_survivorship(bundle, market='us', client=client, table_prefix=prefix)
        unchanged = load_survivorship(bundle, market='us', client=client, table_prefix=prefix)
        assert unchanged['status'] == 'unchanged'
        universe, _ = load_universe_details(client, dates=['2005-01-03', '2015-01-02', '2020-01-03', '2026-09-04'],
            market='us', universe={'size_percentile': {'side': 'top', 'percent': 70}},
            listing_table=prefix + 'security_listing_episodes',
            trading_halt_table=prefix + 'security_trading_halts')
        report = dict(status='verified', source_files=len(history['sources']),
            provider_episode_rows_checked=verified,
            episode_types=dict(Counter(row['security_type'] for row in bundle['listing_episodes'])),
            unresolved_reasons=dict(Counter(row.get('reason', 'other') for row in bundle['unresolved'])),
            reviewed_events_preserved=len(reviewed['events']), isolated_publication=publication,
            repeated_publication=unchanged, dated_universe=universe,
            production_membership_published=False, full_price_financial_reload_complete=False,
            terminal_proceeds_resolved=False)
        export_json(args.gold / 'membership_verification.json', report)
        print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        for table, _ in SCHEMAS.values():
            client.command(f'DROP VIEW IF EXISTS {prefix}{table}')
        for table in ('survivorship_rows', 'survivorship_publications'):
            client.command(f'DROP TABLE IF EXISTS {prefix}{table}')
        client.close()


if __name__ == '__main__':
    main()
