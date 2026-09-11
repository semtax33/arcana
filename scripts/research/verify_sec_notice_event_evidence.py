"""Audit event-claim spans and fixed source examples from public refresh output."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

import pandas as pd

from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    workflow=json.loads((args.gold/'summary.json').read_bytes())
    artifact=workflow['notice_event_evidence']
    assert sha256_file(artifact['path'])==artifact['sha256']
    result=json.loads(Path(artifact['path']).read_bytes())
    collection=json.loads(Path(workflow['notice_documents']['path']).read_bytes())
    assert collection['counts'].get('requests',0)==0
    texts=pd.read_parquet(result['exhibit_texts']['path'])
    evidence=pd.read_parquet(result['evidence']['path'])
    outcomes=json.loads(Path(result['outcomes']['path']).read_bytes())
    assert len(texts)==result['source_exhibits']
    assert len(evidence)==result['claims']==sum(row['claims'] for row in outcomes)
    assert texts.exhibit_id.is_unique
    indexed=texts.set_index('exhibit_id')
    for row in evidence.itertuples():
        original=indexed.loc[row.exhibit_id]
        assert original.normalized_text[row.text_start:row.text_end]==row.matched_text
        assert row.reported_date_literal in row.matched_text
        assert row.source_path==original.source_path and row.source_sha256==original.source_sha256
        assert row.reported_security_title==original.reported_security_title
        assert row.event_verified is False or not bool(row.event_verified)
    assert 'effective_date' not in evidence.columns
    assert dict(Counter(evidence.claim_kind))==result['claim_counts']
    aar=evidence.loc[evidence.accession.eq('0000876661-20-000888')].set_index('claim_kind')
    assert aar.loc['exchange_listing_removal','reported_date']=='2020-10-19'
    assert aar.loc['exchange_listing_removal','claim_status']=='proposed'
    assert aar.loc['trading_suspension','reported_date']=='2020-10-05'
    assert aar.loc['security_rights_extinguished','reported_date']=='2020-10-05'
    assert aar.reported_security_title.eq('Preferred Stock Purchase Rights').all()
    assert aar.reported_filing_date.eq('2020-10-06').all()
    pins=dict(result['inputs']['source_pins'])
    for part in [artifact,result['inputs']['notice_observations'],result['evidence'],result['exhibit_texts'],result['outcomes']]:
        pins[part['path']]=part['sha256']
    assert all(sha256_file(path)==digest for path,digest in pins.items())
    summary=dict(snapshot_only=True,source_notices=result['source_notices'],source_exhibits=len(texts),claims=len(evidence),
        claim_counts=result['claim_counts'],claim_status_counts=evidence.claim_status.value_counts().to_dict(),
        verified_pins=pins,all_claim_spans_checked=True,raw_semantic_sample='AAR rights notice: proposed October 19 versus reported October 5; not an all-source semantic audit.',
        registered_identities=0,coverage_complete=False,database_changed=False)
    export_json(args.output,summary)
    print(json.dumps({key:value for key,value in summary.items() if key!='verified_pins'},indent=2))


if __name__=='__main__':main()
