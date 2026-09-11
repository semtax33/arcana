"""Audit retained notice sources and the public pipeline's normalized scope."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd

from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collection = json.loads((args.gold/'notice_documents.json').read_bytes())
    normalized = json.loads((args.gold/'notice_observations.json').read_bytes())
    assert collection['counts'].get('requests', 0) == 0, 'Verify the no-download public pipeline replay'
    assert collection['registered_identities'] == normalized['registered_identities'] == 0
    assert not collection['coverage_complete'] and not normalized['coverage_complete']
    records = pd.read_parquet(collection['records']['path'])
    outcomes = json.loads(Path(normalized['outcomes']['path']).read_bytes())
    observations = pd.read_parquet(normalized['observations']['path'])
    assert len(records) == collection['requested_submissions'] == len(outcomes)
    assert int(records.source_occurrences.sum()) == collection['source_occurrences']
    assert not records.duplicated(['issuer_cik','accession']).any()
    assert sum(row['observations'] for row in outcomes) == len(observations)
    assert dict(Counter(row['status'] for row in outcomes)) == normalized['counts']
    assert not observations.event_verified.any()
    assert 'effective_date' not in observations.columns
    pins, statuses, headerless = {}, Counter(), []
    for artifact in (collection['records'], normalized['outcomes'], normalized['observations']):
        pins[artifact['path']] = artifact['sha256']
    for row in records.itertuples():
        if not row.metadata_path:
            continue
        metadata = json.loads(Path(row.metadata_path).read_bytes())
        pins[row.metadata_path] = row.metadata_sha256
        pins[metadata['source_path']] = metadata['source_sha256']
        statuses[(metadata.get('http_status'),metadata['status'])] += 1
        assert metadata['issuer_cik'] == row.issuer_cik
        assert metadata['accession'] == row.accession
        assert Path(metadata['source_path']).is_relative_to(ROOT/'data-lake/bronze/sec/notice-submissions')
        if metadata['status'] == 'failed' and metadata.get('http_status') == 200:
            headerless.append(dict(issuer_cik=row.issuer_cik, accession=row.accession,
                error_type=metadata.get('error_type'), source_path=metadata['source_path']))
    for row in observations.itertuples():
        assert pins[row.source_path] == row.source_sha256
    assert all(sha256_file(path) == digest for path, digest in pins.items())
    # Literal expectations were read from these retained official submissions.
    aar = observations.loc[observations.accession.eq('0000876661-20-000888')].iloc[0]
    assert aar.reported_issuer_name == 'AAR CORP'
    assert aar.reported_exchange_name == 'NEW YORK STOCK EXCHANGE LLC'
    assert aar.reported_security_title == 'Preferred Stock Purchase Rights'
    assert aar.reported_filing_date == '20201006'
    ktron = observations.loc[observations.accession.eq('0001354457-10-000074')].iloc[0]
    assert ktron.reported_security_title == 'Common Stock'
    assert ktron.reported_issuer_name == 'K TRON INTERNATIONAL INC'
    result = dict(collection_counts=collection['counts'], requested_submissions=len(records),
        source_occurrences=int(records.source_occurrences.sum()), normalized_counts=normalized['counts'],
        structured_observations=len(observations), observation_scope_counts=observations.scope_status.value_counts().to_dict(),
        retained_response_counts=[dict(http_status=key[0], status=key[1], count=value) for key,value in statuses.items()],
        successful_http_requiring_format_review=headerless, verified_pins=pins,
        raw_field_audit='Two literal official-source examples; all row totals, source associations and hashes checked. Not an all-XML-field audit.',
        registered_identities=0, coverage_complete=False, database_changed=False)
    export_json(args.output,result)
    print(json.dumps({key:value for key,value in result.items() if key not in {'verified_pins','successful_http_requiring_format_review'}},indent=2))


if __name__ == '__main__':
    main()
