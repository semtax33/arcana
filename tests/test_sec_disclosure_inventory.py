"""Public survivorship refresh inventories primary and older filing metadata."""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pandas as pd

from engine.workflows.sec_submissions import run_sec_submissions_refresh
from test_listing_history_replay import snapshot
from test_sec_issuer_discovery import refresh


def bulk_fixture():
    older = dict(accessionNumber=['0000000123-05-000001'],filingDate=['2005-01-03'],
        form=['25-NSE'],primaryDocument=['form25.htm'],acceptanceDateTime=['2005-01-03T20:00:00Z'])
    recent = dict(accessionNumber=['0000000123-19-000001','0000000123-21-000001'],
        filingDate=['2019-01-03','2021-01-03'],form=['8-K','10-K'],
        primaryDocument=['current.htm','future.htm'],acceptanceDateTime=['2019-01-03T20:00:00Z','2021-01-03T20:00:00Z'])
    name='CIK0000000123-submissions-001.json'
    primary=dict(cik='123',name='Former Name',tickers=[],exchanges=[],formerNames=[],
        filings=dict(recent=recent,files=[dict(name=name,filingCount=1,filingFrom='2005-01-03',filingTo='2005-01-03')]))
    buffer=BytesIO()
    with ZipFile(buffer,'w') as archive:
        archive.writestr('CIK0000000123.json',json.dumps(primary))
        archive.writestr(name,json.dumps(older))
    return buffer.getvalue()


def test_public_refresh_retains_old_and_future_filings_without_inventing_terminal_dates(tmp_path):
    with patch('engine.extractors.sec_submissions.urlopen',return_value=BytesIO(bulk_fixture())):
        run_sec_submissions_refresh(source_dir=tmp_path/'bronze/bulk',
            output_dir=tmp_path/'silver/bulk',gold_dir=tmp_path/'gold/bulk')
    snapshot(tmp_path/'bronze/listings','2020-01-03','delisted',[
        ['OLD','Former Name','NYSE','Stock','1995-01-01','2005-12-31','Delisted']])
    refresh(tmp_path)
    summary=json.loads((tmp_path/'gold/survivorship/summary.json').read_bytes())
    inventory=json.loads(Path(summary['filing_inventory']['path']).read_bytes())
    rows=pd.read_parquet(inventory['filings']['path']).set_index('accession')
    assert len(rows)==3
    older=rows.loc['0000000123-05-000001']
    assert older.source_member=='CIK0000000123-submissions-001.json'
    assert older.form_group=='exchange_removal_notice'
    assert older.filing_date=='2005-01-03'
    assert bool(older.on_or_before_cutoff)
    assert not bool(rows.loc['0000000123-21-000001'].on_or_before_cutoff)
    assert 'effective_date' not in rows.columns
    assert inventory['collection_only'] is True
    assert inventory['counts']['filings_on_or_before_cutoff']==2


def altered_archive(edit):
    with ZipFile(BytesIO(bulk_fixture())) as source:
        documents={name:json.loads(source.read(name)) for name in source.namelist()}
    edit(documents)
    result=BytesIO()
    with ZipFile(result,'w') as target:
        for name,payload in documents.items():
            target.writestr(name,json.dumps(payload))
    return result.getvalue()


def run_inventory(root,raw):
    with patch('engine.extractors.sec_submissions.urlopen',return_value=BytesIO(raw)):
        run_sec_submissions_refresh(source_dir=root/'bronze/bulk',
            output_dir=root/'silver/bulk',gold_dir=root/'gold/bulk')
    snapshot(root/'bronze/listings','2020-01-03','delisted',[
        ['OLD','Former Name','NYSE','Stock','1995-01-01','2005-12-31','Delisted']])
    refresh(root)
    summary=json.loads((root/'gold/survivorship/summary.json').read_bytes())
    return json.loads(Path(summary['filing_inventory']['path']).read_bytes())


def test_malformed_continuation_is_reported_without_truncating_other_sources(tmp_path):
    raw=altered_archive(lambda docs: docs['CIK0000000123-submissions-001.json'].update(form=[]))
    result=run_inventory(tmp_path,raw)
    rows=pd.read_parquet(result['filings']['path'])
    assert len(rows)==2
    assert result['metadata_inventory_complete'] is False
    assert result['counts']['failed_members']==1
    assert json.loads(Path(result['reviews']['path']).read_bytes())[0]['reason']=='MALFORMED_HISTORY_COLUMNS'


def test_conflicting_accession_occurrences_remain_visible_and_block_completeness(tmp_path):
    def duplicate(docs):
        docs['CIK0000000123.json']['filings']['recent']['accessionNumber'][0]='0000000123-05-000001'
    result=run_inventory(tmp_path,altered_archive(duplicate))
    rows=pd.read_parquet(result['filings']['path'])
    assert rows.accession.eq('0000000123-05-000001').sum()==2
    assert result['metadata_inventory_complete'] is False
    assert any(row['reason']=='ACCESSION_METADATA_CONFLICT' for row in json.loads(Path(result['reviews']['path']).read_bytes()))


def test_public_inventory_reuses_unchanged_source_and_has_no_terminal_event_output(tmp_path):
    first=run_inventory(tmp_path,bulk_fixture())
    original=Path(first['filings']['path']).read_bytes()
    refresh(tmp_path)
    summary=json.loads((tmp_path/'gold/survivorship/summary.json').read_bytes())
    again=json.loads(Path(summary['filing_inventory']['path']).read_bytes())
    assert again['generation']==first['generation']
    assert again['generation_reused'] is True
    assert Path(first['filings']['path']).read_bytes()==original
    events=json.loads((tmp_path/'gold/survivorship/events.json').read_bytes())['rows']
    assert all(not row['entitlements_complete'] for row in events)


def test_public_refresh_reports_pending_notice_sources_without_downloading(tmp_path):
    run_inventory(tmp_path,bulk_fixture())
    summary=json.loads((tmp_path/'gold/survivorship/summary.json').read_bytes())
    notices=json.loads(Path(summary['notice_documents']['path']).read_bytes())
    assert notices['counts']['pending']==1
    assert notices['counts'].get('requests',0)==0
    assert notices['document_collection_complete'] is False
    observations=json.loads(Path(summary['notice_observations']['path']).read_bytes())
    assert observations['structured_observation_count']==0
    assert observations['counts']['source_unavailable']==1
    evidence=json.loads(Path(summary['notice_event_evidence']['path']).read_bytes())
    assert evidence['claims']==0
    assert evidence['registered_identities']==0
    events=json.loads((tmp_path/'gold/survivorship/events.json').read_bytes())['rows']
    assert all(not row['entitlements_complete'] for row in events)
