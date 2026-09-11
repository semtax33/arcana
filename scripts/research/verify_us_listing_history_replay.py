"""Verify retained listing history and incremental replay at the public workflow."""
from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from engine.core.serving_storage import export_json
from engine.workflows.survivorship import run_survivorship_refresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--identities', type=Path)
    parser.add_argument('--submissions', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.gold.exists():
        raise ValueError('Use new verification and Gold directories')
    args.output.mkdir(parents=True)
    raw_columns = ['symbol', 'name', 'exchange', 'assetType', 'ipoDate', 'delistingDate', 'status']
    timings, runs, link_runs, discovery_runs = [], [], [], []
    for attempt in range(2):
        started = time.perf_counter()
        result = run_survivorship_refresh(market='us', end_date=args.as_of, source_dir=args.sources,
            manifest_path=args.output / 'no_manual_approval.json', output_dir=args.output / 'refresh',
            gold_dir=args.gold, identity_dir=args.identities, sec_submissions_manifest=args.submissions,
            download=False, load_clickhouse=False)
        timings.append(time.perf_counter() - started)
        history = json.loads(Path(result['listing_history']['path']).read_bytes())
        runs.append(history)
        export_json(args.output / f'run_{attempt + 1}.json', history)
        linkage = json.loads(Path(result['identity_linkage']['path']).read_bytes())
        link_runs.append(linkage)
        export_json(args.output / f'link_run_{attempt + 1}.json', linkage)
        if 'issuer_discovery' in result:
            discovery = json.loads(Path(result['issuer_discovery']['path']).read_bytes())
            discovery_runs.append(discovery)
            export_json(args.output / f'discovery_run_{attempt + 1}.json', discovery)
    first, repeated = runs
    assert first['generation'] == repeated['generation']
    assert repeated['cache'] == dict(partitions_built=0, partitions_reused=first['source_files'])
    assert repeated['candidate_index_reused'] is True
    independent_keys, count = set(), 0
    pins = {}
    for source, artifact in zip(first['sources'], first['partitions'], strict=True):
        path = Path(source['source_path'])
        raw = path.read_bytes()
        assert sha256(raw).hexdigest() == source['source_sha256']
        metadata_path = Path(source['metadata_path'])
        assert sha256(metadata_path.read_bytes()).hexdigest() == source['metadata_sha256']
        pins[str(path)] = source['source_sha256']
        pins[str(metadata_path)] = source['metadata_sha256']
        original = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        normalized = pd.read_parquet(artifact['path'])
        assert sha256(Path(artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
        assert normalized[raw_columns].to_dict('records') == original
        assert normalized.snapshot_date.eq(source['snapshot_date']).all()
        assert normalized.retrieved_at.eq(source['retrieved_at']).all()
        assert normalized.observation_id.is_unique
        count += len(original)
        independent_keys.update((row['symbol'], row['exchange'], row['assetType'], row['ipoDate']) for row in original)
    assert count == first['observation_rows']
    assert len(independent_keys) == first['provider_listing_keys']
    candidates = pd.read_parquet(first['candidate_index']['path'])
    assert candidates.observations.sum() == count
    assert candidates.provider_listing_id.is_unique
    assert candidates.identity_status.eq('unresolved').all()
    linkage, repeated_linkage = link_runs
    assert linkage['generation'] == repeated_linkage['generation']
    assert repeated_linkage['generation_reused'] is True
    linked = pd.read_parquet(linkage['candidate_links']['path'])
    assert sha256(Path(linkage['candidate_links']['path']).read_bytes()).hexdigest() == linkage['candidate_links']['sha256']
    # Compare all original candidate columns, not just the number of matches.
    pd.testing.assert_frame_equal(candidates, linked[list(candidates.columns)])
    assert linked.listing_interval_verified.eq(False).all()
    for evidence in linkage['reports']:
        for path_key, hash_key in [('report_path', 'report_sha256'), ('source_manifest', 'manifest_sha256')]:
            assert sha256(Path(evidence[path_key]).read_bytes()).hexdigest() == evidence[hash_key]
            pins[evidence[path_key]] = evidence[hash_key]
        report = json.loads(Path(evidence['report_path']).read_bytes())
        if report.get('instance_source'):
            instance = report['instance_source']
            assert sha256(Path(instance['path']).read_bytes()).hexdigest() == instance['sha256']
            pins[instance['path']] = instance['sha256']
    corroborated = linked[linked.link_status.eq('corroborated_source_observation')]
    assert corroborated.candidate_ciks.map(lambda value: len(json.loads(value)) == 1).all()
    assert corroborated.link_review_reasons.eq('[]').all()
    discovery_verification = None
    if discovery_runs:
        discovered, cached = discovery_runs
        assert cached['generation_reused'] is True and cached['generation'] == discovered['generation']
        frame = pd.read_parquet(discovered['candidate_discovery']['path'])
        queue = pd.read_parquet(discovered['collection_candidates']['path'])
        pd.testing.assert_frame_equal(candidates, frame[list(candidates.columns)])
        assert frame.collection_only.eq(True).all() and frame.identity_status.eq('unresolved').all()
        expected_ciks = {cik for row in frame[frame.assetType.eq('Stock')].itertuples()
            for cik in json.loads(row.discovery_ciks)}
        assert queue.issuer_cik.is_unique and set(queue.issuer_cik) == expected_ciks
        assert queue.collection_only.eq(True).all()
        assert queue.source_member.eq('CIK' + queue.issuer_cik + '.json').all()
        for key in ['candidate_discovery', 'collection_candidates']:
            assert sha256(Path(discovered[key]['path']).read_bytes()).hexdigest() == discovered[key]['sha256']
        manifest_path = discovered['inputs']['submissions_manifest']
        pins[manifest_path] = discovered['inputs']['submissions_manifest_sha256']
        assert sha256(Path(manifest_path).read_bytes()).hexdigest() == pins[manifest_path]
        discovery_verification = dict(provider_candidates=len(frame), collection_ciks=len(queue),
            discovery_counts=discovered['discovery_counts'], scanned_filers=discovered['scanned_filers'],
            collection_only=True, generation_reused=True)
    assert not (args.gold / 'events.json').exists()
    export_json(args.output / 'source_pins.json', pins)
    report = dict(status='source_history_and_incremental_replay_verified', as_of=args.as_of,
        source_files=first['source_files'], observation_rows=count,
        provider_listing_keys=len(independent_keys), first_run_seconds=timings[0], replay_seconds=timings[1],
        replay_cache=repeated['cache'], candidate_index_reused=True,
        review_reason_counts=first['review_reason_counts'], source_pins_unchanged=True,
        registered_identities=0, database_changed=False, coverage_complete=False,
        sec_reports=linkage['sec_reports'], corroborated_candidates=len(corroborated),
        linkage_review_reason_counts=linkage['review_reason_counts'],
        linkage_generation_reused=True, identity_linkage=result['identity_linkage'],
        issuer_discovery=discovery_verification,
        history=result['listing_history'])
    artifact = export_json(args.output / 'verification.json', report)
    export_json(args.gold / 'verification.json', {**report, 'silver_verification': artifact})
    print(json.dumps(report))


if __name__ == '__main__':
    main()
