"""Verify a completed disclosure inventory and replay its public cached transform."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq

from engine.core.serving_storage import export_json
from engine.core.source_storage import sha256_file
from engine.transformers.sec_disclosure_inventory import build_sec_disclosure_inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads((args.gold / 'filing_inventory.json').read_bytes())
    discovery = json.loads((args.gold / 'issuer_discovery.json').read_bytes())
    source = json.loads(Path(discovery['inputs']['submissions_manifest']).read_bytes())
    artifact = Path(inventory['filings']['path'])
    started = time.perf_counter()
    repeated = build_sec_disclosure_inventory(discovery=discovery,
        end_date=inventory['as_of'], output_dir=artifact.parents[2])
    replay_seconds = time.perf_counter() - started
    assert repeated['generation_reused'] is True
    assert repeated['generation'] == inventory['generation']
    assert repeated['filings'] == inventory['filings']
    parquet = pq.ParquetFile(artifact)
    assert parquet.metadata.num_rows == inventory['filings']['rows']
    assert 'effective_date' not in parquet.schema.names
    counts, groups, reasons, ciks, members = Counter(), Counter(), Counter(), Counter(), set()
    for batch in parquet.iter_batches(batch_size=50000, columns=[
            'issuer_cik', 'source_member', 'form_group', 'review_reasons',
            'on_or_before_cutoff', 'collection_only']):
        for row in batch.to_pylist():
            counts['rows'] += 1
            counts['eligible'] += row['on_or_before_cutoff']
            assert row['collection_only'] is True
            ciks[row['issuer_cik']] += 1
            members.add(row['source_member'])
            review = json.loads(row['review_reasons'])
            counts['review_rows'] += bool(review)
            reasons.update(review)
            if row['on_or_before_cutoff']:
                groups[row['form_group']] += 1
    coverage = json.loads(Path(inventory['cik_coverage']['path']).read_bytes())
    assert len(coverage) == inventory['requested_ciks']
    assert len({row['issuer_cik'] for row in coverage}) == len(coverage)
    assert all(ciks[row['issuer_cik']] == row['rows'] for row in coverage)
    assert sum(row['failed_members'] for row in coverage) == inventory['counts']['failed_members']
    assert counts['eligible'] == inventory['counts']['filings_on_or_before_cutoff']
    assert counts['review_rows'] == inventory['counts']['rows_requiring_review']
    assert dict(groups) == inventory['form_groups_on_or_before_cutoff']
    # Fixed spread samples compare preserved fields with raw ZIP rows, not a
    # second invocation of the production normalizer. This is a sample audit.
    group_ids = sorted({round(i * (parquet.num_row_groups - 1) / 63) for i in range(64)})
    samples = []
    with ZipFile(source['source_path']) as archive:
        for group_id in group_ids:
            rows = parquet.read_row_group(group_id).to_pylist()
            for row in (rows[0], rows[-1]):
                raw = archive.read(row['source_member'])
                payload = json.loads(raw)
                columns = payload if '-submissions-' in row['source_member'] else payload['filings']['recent']
                original = {key: values[row['source_row'] - 1] for key, values in columns.items()}
                assert sha256(raw).hexdigest() == row['member_sha256']
                assert json.loads(row['reported_row']) == original
                for output_key, source_key in [('accession', 'accessionNumber'),
                        ('filing_date', 'filingDate'), ('form', 'form'),
                        ('primary_document', 'primaryDocument'),
                        ('acceptance_datetime', 'acceptanceDateTime')]:
                    assert row[output_key] == str(original.get(source_key) or '')
                samples.append(dict(issuer_cik=row['issuer_cik'], member=row['source_member'],
                    source_row=row['source_row'], accession=row['accession']))
    reviews = json.loads(Path(inventory['reviews']['path']).read_bytes())
    pins = {str(Path(discovery['inputs']['submissions_manifest'])):
            discovery['inputs']['submissions_manifest_sha256'],
        source['source_path']: source['source_sha256'],
        discovery['collection_candidates']['path']: discovery['collection_candidates']['sha256']}
    pins.update({inventory[key]['path']: inventory[key]['sha256']
        for key in ('filings', 'reviews', 'cik_coverage')})
    assert all(sha256_file(path) == digest for path, digest in pins.items())
    result = dict(generation=inventory['generation'], replay_seconds=replay_seconds,
        generation_reused=True, full_row_counts=dict(counts), form_groups=dict(groups),
        row_review_reasons=dict(reasons), history_review_reasons=dict(Counter(r['reason'] for r in reviews)),
        represented_ciks=len(ciks), represented_members=len(members), requested_ciks=len(coverage),
        sample_rows=len(samples), sample_continuation_rows=sum('-submissions-' in r['member'] for r in samples),
        samples=samples, verified_pins=pins, metadata_inventory_complete=inventory['metadata_inventory_complete'],
        source_audit_scope='All output row counts and CIK coverage; 128 fixed spread raw-row samples, not all raw fields.',
        registered_identities=0, coverage_complete=False, production_data_changed=False)
    export_json(args.output, result)
    print(json.dumps({key: value for key, value in result.items() if key not in {'samples', 'verified_pins'}}, indent=2))


if __name__ == '__main__':
    main()
