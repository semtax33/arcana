import copy
import hashlib
import json
from decimal import Decimal
from dataclasses import replace

import pytest

from engine.transformers.us_split_evidence import contract_ratio, source_path, supplement_ledger, verify_record


@pytest.fixture
def reviewed(tmp_path):
    marker='every two shares of Company Common Stock were automatically combined into one share'
    sources=[]
    for filename,roles,text in [
        ('execution.htm',['exact_ratio','actual_execution'],marker+'; became effective June 29, 2026'),
        ('release.htm',['trading_date'],'began trading on a split-adjusted basis on June 29, 2026')]:
        raw=('<html><body>'+text+'</body></html>').encode()
        source={'cik':'123','source_id':'0000000123-26-000001','filename':filename,
            'source_url':'https://www.sec.gov/Archives/edgar/data/123/000000012326000001/'+filename,
            'source_sha256':hashlib.sha256(raw).hexdigest(),'published_date':'2026-06-29',
            'roles':roles,'markers':[marker] if filename=='execution.htm' else [text]}
        path=source_path(tmp_path,source);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        path.with_suffix(path.suffix+'.metadata.json').write_text(json.dumps({**source,'security_id':'SEC_US_X'}))
        sources.append(source)
    return {'security_id':'SEC_US_X','effective_date':'2026-06-29','new_shares':'1','old_shares':'2',
        'ratio_marker':marker,'sources':sources,'review_reference':'source review',
        'other_actions':[{'kind':'spin_off_distribution','reason':'distributed shares need wealth accounting'}]}


def test_verified_contract_preserves_exact_units_and_distribution_review(tmp_path, reviewed):
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'records':[reviewed]}))
    ledger={'as_of':'2026-09-09','events':[],'review':[]}
    result=supplement_ledger(ledger,tmp_path,manifest)
    assert Decimal(result['events'][0]['ratio'])==Decimal('.5')
    assert len(result['official_vendor_overrides'])==1
    assert result['return_review_events'][0]['kind']=='spin_off_distribution'
    again=supplement_ledger(result,tmp_path,manifest)
    assert len(again['events'])==len(again['official_vendor_overrides'])==1


def test_record_ratio_cannot_disagree_with_source_words(tmp_path, reviewed):
    reviewed['old_shares']='20'
    with pytest.raises(ValueError,match='operative text'):verify_record(reviewed,tmp_path,'2026-09-09')


def test_changed_source_fails_closed(tmp_path, reviewed):
    path=source_path(tmp_path,reviewed['sources'][0]);path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash changed'):verify_record(reviewed,tmp_path,'2026-09-09')


def test_changed_markers_and_future_evidence_are_rejected(tmp_path, reviewed):
    bad=copy.deepcopy(reviewed);bad['sources'][0]['markers'].append('nonexistent operative clause')
    with pytest.raises(ValueError,match='text is missing'):verify_record(bad,tmp_path,'2026-09-09')
    with pytest.raises(ValueError,match='Future'):verify_record(reviewed,tmp_path,'2026-06-28')


def test_authorized_share_counts_are_not_an_operative_conversion():
    with pytest.raises(ValueError):contract_ratio('authorized shares reduced from 1,666,666,667 to 555,555,556')
    assert contract_ratio('1-for-1,500 reverse stock split')==Decimal(1)/1500


def test_thousand_and_each_issued_share_contracts():
    assert contract_ratio('at a ratio of one-for-one thousand')==Decimal('0.001')
    assert contract_ratio('each 1,000 shares of Common Stock issued and outstanding shall be reclassified and combined into one validly issued share')==Decimal('0.001')
    for marker in ['one-for-two thousand','one-for-one thousand million','one-for-one thousand five',
                   'one-for-one thousand and one','each two thousand shares combined into one share']:
        with pytest.raises(ValueError):contract_ratio(marker)


@pytest.mark.parametrize('marker',['1-for-0 reverse stock split','0-for-5 reverse stock split'])
def test_nonpositive_contract_units_fail_closed(marker):
    with pytest.raises(ValueError,match='positive'):contract_ratio(marker)


@pytest.mark.parametrize('old',['0','NaN','Infinity'])
def test_invalid_reviewed_counts_fail_closed(tmp_path,reviewed,old):
    reviewed['old_shares']=old
    with pytest.raises(ValueError):verify_record(reviewed,tmp_path,'2026-09-09')


def test_superseded_schedule_requires_its_own_verified_cancellation_text(tmp_path,reviewed):
    proof=reviewed['sources'][1]
    marker='The previously announced June 16, 2026 trading schedule was cancelled.'
    path=source_path(tmp_path,proof);raw=path.read_bytes()+marker.encode();path.write_bytes(raw)
    proof['source_sha256']=hashlib.sha256(raw).hexdigest()
    proof['roles'].append('supersedes_prior_schedule');proof['markers'].append(marker)
    path.with_suffix(path.suffix+'.metadata.json').write_text(json.dumps({**proof,'security_id':'SEC_US_X'}))
    final=verify_record(reviewed,tmp_path,'2026-09-09')
    old=replace(final,effective_date='2026-06-16',source_id='0000000123-26-000000',source_sha256='old-source-sha')
    reviewed['supersedes_sources']=[{'effective_date':old.effective_date,'source_id':old.source_id,
        'source_sha256':old.source_sha256,'proof_source_url':proof['source_url'],'proof_marker':marker}]
    manifest=tmp_path/'reviewed.json';manifest.write_text(json.dumps({'records':[reviewed]}))
    original={'as_of':'2026-09-09','events':[old.to_dict()],'review':[]}
    result=supplement_ledger(copy.deepcopy(original),tmp_path,manifest)
    assert [e['effective_date'] for e in result['events']]==['2026-06-29']
    assert result['review'][0]['reason']=='superseded_announced_schedule'
    assert supplement_ledger(result,tmp_path,manifest)['events']==result['events']
    reviewed['supersedes_sources'][0]['proof_marker']='unsupported cancellation'
    manifest.write_text(json.dumps({'records':[reviewed]}))
    rejected=supplement_ledger(original,tmp_path,manifest)
    assert [e['effective_date'] for e in rejected['events']]==['2026-06-16']
    assert rejected['review'][0]['reason']=='reviewed_us_evidence_unavailable'


@pytest.mark.parametrize('change,should_pass',[
    ({'calendarDay':'2026-06-16'},True),
    ({'exDate':'2026-06-16 00:00:00.0'},False),
    ({'reverseSplitRate':'1:20'},False),
    ({'dailyListEventCode':'DD','commentText':'cancelled'},False),
])
def test_finra_uses_active_ex_date_and_exact_ratio_not_partition_date(tmp_path,reviewed,change,should_pass):
    from engine.transformers.us_split_evidence import FINRA_URL
    row={'OTCDailyListID':12345,'exDate':'2026-06-29 00:00:00.0','calendarDay':'2026-06-20',
         'dailyListDatetime':'2026-06-28 12:00:00.0','reverseSplitRate':'1:2','oldSymbolCode':'X',
         'dailyListEventCode':'DA','dailyListReasonDescription':'Reverse Split/CUSIP Change',**change}
    raw=json.dumps([row]).encode()
    source={'provider':'FINRA','source_id':'FINRA-OTCDAILYLIST-X-202606','source_url':FINRA_URL,
            'source_sha256':hashlib.sha256(raw).hexdigest(),'published_date':'2026-06-28','finra_list_id':12345,
            'request_body':{'limit':1},'roles':['trading_date'],'markers':['Reverse Split/CUSIP Change']}
    path=source_path(tmp_path,source);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    path.with_suffix('.json.metadata.json').write_text(json.dumps({**source,'security_id':'SEC_US_X'}))
    reviewed['sources'].append(source)
    if should_pass:assert verify_record(reviewed,tmp_path,'2026-09-09').effective_date=='2026-06-29'
    else:
        with pytest.raises(ValueError):verify_record(reviewed,tmp_path,'2026-09-09')


def test_acquisition_exhibit_exclusion_is_narrow_and_hash_guarded(tmp_path,reviewed):
    source=reviewed['sources'][0]
    source['markers']+=['Company refers to Target Inc.','trading symbol “Y”']
    path=source_path(tmp_path,source)
    raw=path.read_bytes()+' Company refers to Target Inc. trading symbol “Y”'.encode()
    path.write_bytes(raw);source['source_sha256']=hashlib.sha256(raw).hexdigest()
    path.with_suffix('.htm.metadata.json').write_text(json.dumps(source|{'security_id':'SEC_US_X'}))
    event=verify_record(reviewed,tmp_path,'2026-09-09')
    unrelated=replace(event,effective_date='2026-07-01')
    exclusion={k:getattr(event,k) for k in ['security_id','effective_date','new_shares','old_shares']}
    exclusion.update(reason='different_issuer_in_acquisition_exhibit',affected_issuer_symbol='Y',
                     review_reference='review',source=source)
    manifest=tmp_path/'excluded.json'
    manifest.write_text(json.dumps({'records':[],'event_exclusions':[exclusion]}))
    original={'as_of':'2026-09-09','events':[event.to_dict(),unrelated.to_dict()],'review':[]}
    result=supplement_ledger(copy.deepcopy(original),tmp_path,manifest)
    assert [e['effective_date'] for e in result['events']]==['2026-07-01']
    assert result['review'][0]['excluded_event']['event_id']==event.event_id
    path.write_bytes(raw+b'changed')
    failed=supplement_ledger(original,tmp_path,manifest)
    assert len(failed['events'])==2
    assert failed['review'][0]['reason']=='source_event_exclusion_unverified'


def test_legal_execution_bridge_needs_exact_time_and_source_marker(tmp_path,reviewed):
    source=reviewed['sources'][0]
    marker='Effective at 5:00 p.m. Eastern Daylight Time on June 26, 2026'
    path=source_path(tmp_path,source);raw=path.read_bytes()+marker.encode();path.write_bytes(raw)
    source['source_sha256']=hashlib.sha256(raw).hexdigest()
    source['markers'].append(marker);source['roles'].append('legal_effectiveness')
    path.with_suffix('.htm.metadata.json').write_text(json.dumps(source|{'security_id':'SEC_US_X'}))
    reviewed['allow_missing_vendor_action']=True
    reviewed['execution_bridge']={'source_url':source['source_url'],'marker':marker,'legal_effective_date':'2026-06-26'}
    assert verify_record(reviewed,tmp_path,'2026-09-09')
    reviewed['execution_bridge']['legal_effective_date']='2026-06-25'
    with pytest.raises(ValueError,match='effective time'):verify_record(reviewed,tmp_path,'2026-09-09')


@pytest.mark.parametrize('mode',['replace_date','finra_missing_quote','execution_bridge','unverified'])
def test_price_builder_preserves_vendor_rows_and_applies_only_reviewed_unit_event(tmp_path,monkeypatch,mode):
    import pandas as pd
    from engine.core.paths import DataLakePaths
    import engine.workflows.stock_splits as workflow
    import engine.extractors.alpha_vantage_prices as alpha
    from engine.transformers.stock_splits import SplitEvent
    lake=DataLakePaths(tmp_path/'lake')
    monkeypatch.setattr(workflow,'DATA_LAKE',lake);monkeypatch.setattr(alpha,'DATA_LAKE',lake)
    days=['2026-06-26','2026-06-29','2026-06-30']
    prices=[10.,20.,21.]
    if mode=='finra_missing_quote':days=['2026-06-26','2026-06-30'];prices=[10.,21.]
    series={d:{'1. open':str(p),'2. high':str(p),'3. low':str(p),'4. close':str(p),
        '5. adjusted close':str(p),'6. volume':'100','7. dividend amount':'0',
        '8. split coefficient':'0.5' if mode=='replace_date' and d=='2026-06-30' else '1.0'}
        for d,p in zip(days,prices)}
    path=alpha.price_root()/'ticker=X.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'Meta Data':{'2. Symbol':'X'},'Time Series (Daily)':series}))
    event=SplitEvent(security_id='SEC_US_X',effective_date='2026-06-29',new_shares='1',old_shares='2',
        source='EDGAR',source_id='verified',source_url='https://www.sec.gov/verified',
        source_sha256='official-sha',published_date='2026-06-29',action_type='reverse_split')
    proof={'security_id':event.security_id,'event_id':event.event_id,'effective_date':event.effective_date,
           'allow_missing_vendor_action':True}
    if mode=='replace_date':
        proof['vendor_action_replacement']={'effective_date':'2026-06-30','reported_ratio':'0.5',
            'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if mode=='finra_missing_quote':proof['market_date_confirmation']='FINRA_exDate'
    if mode=='execution_bridge':proof['execution_bridge']={'legal_effective_date':'2026-06-26'}
    ledger={'as_of':'2026-09-09','events':[event.to_dict()],
            'official_vendor_overrides':[] if mode=='unverified' else [proof],
            'return_review_events':[{'security_id':event.security_id,'effective_date':event.effective_date,
                                     'kind':'price_source_review'}]}
    result=workflow.build_split_price_panels('us',ledger,refresh_us=False)
    if mode=='unverified':
        assert len(result['errors'])==1 and not result['processed'];return
    assert not result['errors'] and len(result['processed'])==1
    panel=pd.read_parquet(result['processed'][0]['panel_path'])
    assert panel.close.tolist()==prices
    assert panel.split_adj_close.tolist()==[20.]+prices[1:]
    flagged=panel.loc[panel.price_source_review_event,'trade_date'].dt.strftime('%Y-%m-%d').tolist()
    assert flagged==['2026-06-30' if mode=='finra_missing_quote' else '2026-06-29']
    assert not panel.return_review_event.any()
    assert result['processed'][0]['official_vendor_discrepancies']
    if mode=='replace_date':
        assert result['processed'][0]['vendor_actions']==[{'effective_date':'2026-06-30','ratio':'0.5'}]
        proof['vendor_action_replacement']['source_sha256']='changed'
        failed=workflow.build_split_price_panels('us',ledger,refresh_us=False)
        assert len(failed['errors'])==1
    if mode=='execution_bridge':
        proof['execution_bridge']['legal_effective_date']='2026-06-29'
        failed=workflow.build_split_price_panels('us',ledger,refresh_us=False)
        assert len(failed['errors'])==1
