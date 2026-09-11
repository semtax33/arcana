from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from engine.extractors.stock_splits import parse_dart_search, dart_family
from engine.transformers.stock_splits import (
    SplitEvent, adjust_prices, resolve_events, parse_dart_split,
    parse_edgar_split, confirm_dart_with_prices, parse_dart_resumption,
)

FIXTURES=Path(__file__).resolve().parents[1]/'data-lake/bronze/fixtures/stock_splits'


def test_stock_split_refresh_writes_user_summary_to_gold(tmp_path, monkeypatch):
    import json
    from engine.core.paths import DataLakePaths
    from engine.extractors import stock_splits as extractor
    from engine.transformers import stock_splits as transformer
    from engine.workflows import stock_splits as workflow
    lake = DataLakePaths(tmp_path / 'data-lake')
    for module in (extractor, transformer, workflow):
        monkeypatch.setattr(module, 'DATA_LAKE', lake)
    raw = lake.bronze('krx', 'price', 'kr_005930.csv')
    raw.parent.mkdir(parents=True)
    raw.write_text('날짜,시가,고가,저가,종가,거래량\n2026-09-09,100,100,100,100,10\n', encoding='utf-8')
    source_bytes = raw.read_bytes()
    result = workflow.run_stock_split_refresh(market='kr', end_date='2026-09-10',
        symbols=['005930'], download=False, build_prices=True)
    assert Path(result['ledger_path']) == lake.silver('corporate_actions', 'kr_stock_splits.json')
    report = lake.root / 'gold/corporate_actions/kr/summary.json'
    assert report.exists()
    assert json.loads(report.read_text(encoding='utf-8'))['events'] == 0
    import pandas as pd
    prices = pd.read_parquet(report.parent / 'prices/kr_005930.parquet')
    assert prices.close.tolist() == prices.adj_close.tolist() == [100.]
    assert raw.read_bytes() == source_bytes
    assert not list((tmp_path / 'docs').glob('**/*'))


def test_edgar_trading_date_does_not_cross_into_meeting_sentence():
    text='''The Company approved a 1-for-20 reverse stock split. The Company expects that
    upon the opening of trading on April 12, 2024, the Common Stock will begin trading
    on a post-split basis under CUSIP number 00847G 804. As discussed below, on April 3,
    2024, the stockholders approved the proposal.'''
    kwargs=dict(security_id='SEC_US_AGEN',source_id='fixture',source_url='https://www.sec.gov/fixture',published_date='2024-04-05')
    events,issues=parse_edgar_split(text,**kwargs)
    assert not issues and events[0].effective_date=='2024-04-12'
    assert not parse_edgar_split(text.replace('upon the opening of trading on April 12, 2024,',''),**kwargs)[0]


def test_edgar_trading_time_abbreviation_stays_inside_sentence():
    text='A 1-for-20 reverse stock split. Shares begin trading on a split-adjusted basis at 9:30 a.m. Eastern Time on April 12, 2024.'
    events,issues=parse_edgar_split(text,security_id='SEC_US_X',source_id='fixture',source_url='https://www.sec.gov/fixture',published_date='2024-04-05')
    assert not issues and events[0].effective_date=='2024-04-12'


def test_suspended_quote_unit_switch_cannot_create_technical_momentum():
    import numpy as np
    from engine.transformers._internal.factor_metrics import add_price_momentum_factors
    n=280;day=pd.bdate_range('2025-01-01',periods=n)
    raw=np.full(n,10.);raw[250:]=80.
    adjusted=np.full(n,80.);adjusted[250:260]=640.
    volume=np.ones(n);volume[250:260]=0
    frame=pd.DataFrame({'trade_date':day,'close':raw,'adj_close':adjusted,'high':raw,'low':raw,
        'volume':volume,'shares':1000.,'listing_episode':0})
    result=add_price_momentum_factors(frame)
    np.testing.assert_allclose(result.ma_50,80.)
    np.testing.assert_allclose(result.ret_1m.iloc[21:],0.)
    np.testing.assert_allclose(result.tr_12_1.iloc[252:],0.)
    np.testing.assert_allclose(result.vol_12_1_ann.dropna(),0.)
    np.testing.assert_array_equal(result.close,frame.close)


def test_kind_viewer_matches_nested_exchange_title_without_guessing_document():
    from engine.extractors.kind_stock_splits import parse_kind_viewer
    html='''<h1>Test (000001)</h1><select id="mainDoc">
    <option value="20111214000067|Y" selected>상장안내 (2011.12.14)</option></select>'''
    for title in ['상장안내(보통주 추가(주식매수선택권행사) 및 변경상장(액면분할))',
                  '상장안내(보통주 추가상장(국내CB전환) 및 변경상장(액면분할/감자(주식병합))']:
        code,chosen,_=parse_kind_viewer(html,{'published_date':'2011-12-14','title':title})
        assert code=='000001' and chosen['kind_doc_no']=='20111214000067'
    with pytest.raises(ValueError):
        parse_kind_viewer(html,{'published_date':'2011-12-15','title':'상장안내'})


def event(**kwargs):
    defaults=dict(security_id='SEC_KR_005930',effective_date='2018-05-04',
                  new_shares='50',old_shares='1',source='DART',source_id='20180316800856',
                  source_url='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20180316800856',
                  source_sha256='fixture',published_date='2018-03-16')
    return SplitEvent(**(defaults|kwargs))


def test_samsung_actual_boundary_and_repeated_runs():
    prices=pd.DataFrame({'security_id':['SEC_KR_005930']*3,
                         'trade_date':['2018-04-27','2018-05-03','2018-05-04'],
                         'close':[2650000,2650000,51900], 'volume':[10,0,20]})
    adjusted=adjust_prices(prices,[event()])
    assert adjusted.split_adj_close.tolist()==[53000,53000,51900]
    assert adjusted.close.tolist()==prices.close.tolist()
    assert adjusted.split_adj_volume.tolist()==[500,0,20]
    pd.testing.assert_frame_equal(adjusted,adjust_prices(adjusted,[event()]))
    assert adjusted.split_adj_close.pct_change().iloc[-1]==pytest.approx(-.02075471698)


def test_forward_then_reverse_multiply_and_do_not_cross_asof():
    first=event(effective_date='2020-01-02',new_shares='4',published_date='2019-12-01')
    second=event(effective_date='2020-01-06',new_shares='1',old_shares='10',source_id='second',published_date='2019-12-02',action_type='reverse_split')
    prices=pd.DataFrame({'security_id':[first.security_id]*3,'trade_date':['2020-01-01','2020-01-02','2020-01-06'],'close':[100,25,250]})
    assert adjust_prices(prices,[first,second]).split_adj_close.tolist()==[250,250,250]
    assert adjust_prices(prices.iloc[:2],[first,second],as_of='2020-01-02').split_adj_close.tolist()==[25,25]
    assert adjust_prices(prices,[first,second],price_basis='split_adjusted').split_adj_close.tolist()==[100,25,250]
    with pytest.raises(ValueError,match='after'):
        adjust_prices(prices,[first],as_of='2020-01-02')


def test_conflict_cancellation_and_knowledge_date():
    original=event()
    with pytest.raises(ValueError,match='conflicting'):
        resolve_events([original,event(new_shares='20',source_id='wrong')])
    cancelled=event(status='cancelled',source_id='correction',supersedes=original.source_id,published_date='2018-04-01')
    assert resolve_events([original,cancelled])==[]
    assert resolve_events([original,cancelled],knowledge_date='2018-03-20')==[original]
    assert len(resolve_events([original,original]))==1


@pytest.mark.parametrize('filename,receipt,expected_date,ratio',[
    ('samsung_original_20180131800068_viewer.html','20180131800068','2018-05-16','50'),
    ('samsung_amended_20180316800856_viewer.html','20180316800856','2018-05-04','50'),
    ('canvasn_amended_20260811900499_viewer.html','20260811900499','2026-08-20','0.4'),
])
def test_official_dart_fixtures_choose_operative_table(filename,receipt,expected_date,ratio):
    found,issues=parse_dart_split((FIXTURES/filename).read_bytes(),security_id='SEC_KR_005930',source_id=receipt,
                                 source_url=f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}',published_date='2026-09-09')
    assert not issues
    assert found[0].effective_date==expected_date
    assert found[0].ratio==Decimal(ratio)
    assert found[0].status=='announced'


def test_dart_marketwide_search_and_revision_family():
    rows,total=parse_dart_search((FIXTURES/'marketwide_split_search_20180101_20180510_page1.html').read_bytes())
    assert total==45 and len(rows)==45
    assert any(r['corp_code']=='00126380' for r in rows)
    family=dart_family((FIXTURES/'samsung_amended_20180316800856_main.html').read_bytes())
    assert family==['20180131800068','20180316800856']


def test_dart_execution_requires_reported_exchange_return_not_only_price_jump():
    planned=event(status='announced')
    prices=pd.DataFrame({'trade_date':['2018-05-03','2018-05-04'],'close':[2650000,51900],'volume':[0,200],'change_rate':[0,-2.08]})
    checked,issues=confirm_dart_with_prices(planned,prices)
    assert checked.status=='confirmed' and not issues
    prices.loc[1,'change_rate']=0
    checked,issues=confirm_dart_with_prices(planned,prices)
    assert checked.status=='announced' and issues


def parse_us(text):
    return parse_edgar_split(text,security_id='SEC_US_X',source_id='accession',
                             source_url='https://www.sec.gov/Archives/test.htm',published_date='2026-08-13')


def test_us_reverse_split_requires_trading_date_and_exact_ratio():
    events,issues=parse_us('A 1-for-200 reverse stock split will become effective August 17, 2026. '
                          'Shares will begin trading on a split-adjusted basis on August 18, 2026.')
    assert not issues and events[0].ratio==Decimal('0.005')
    assert events[0].effective_date=='2026-08-18'
    assert not parse_us('A 1-for-200 reverse stock split will become legally effective August 17, 2026.')[0]
    assert not parse_us('Authorized a range between 1-for-10 and 1-for-200 reverse stock split. '
                        'Shares will begin trading on a split-adjusted basis on August 18, 2026.')[0]


@pytest.mark.parametrize('wording',[
    'The board approved a 20-for-1 reverse stock split.',
    'The board approved a 20-to-1 reverse split.',
    'A reverse stock split of common stock at a ratio of 20 to 1.',
    'A reverse stock split of common stock at a ratio of 1 to 20.',
])
def test_us_reverse_ratio_direction_is_the_share_count_direction(wording):
    events,issues=parse_us(wording+' Shares will begin trading on a split-adjusted basis on August 18, 2026.')
    assert not issues and events[0].ratio==Decimal('.05')
    assert events[0].action_type=='reverse_split'


def test_us_does_not_extract_authorized_share_counts_or_unselected_range():
    trading=' Shares will begin trading on a split-adjusted basis on August 18, 2026.'
    cases=[
        'In connection with the reverse stock split, authorized shares are reduced from 1,666,666,667 to 555,555,556.',
        'The reverse stock split was approved at a ratio of not less than 1-for-3 and not more than 1-for-20.',
        'The board approved a 1-for-20-five reverse stock split.',
    ]
    for wording in cases:assert not parse_us(wording+trading)[0]
    events,issues=parse_us(cases[1]+' The board has now approved a 1-for-7 reverse stock split.'+trading)
    # A nearby authorization range may conservatively withhold an otherwise
    # final ratio; it must never emit either end of that range.
    assert not events or events[0].ratio==Decimal(1)/7


def test_us_board_selects_ratio_after_unlabelled_authorization_range():
    events,issues=parse_us('Shareholders approved the Reverse Stock Split at a ratio of 1-for-5 to 1-for-25 '
        'to be determined at the discretion of the Board of Directors. '
        'The Board determined to set the Reverse Stock Split ratio at 1-for-20. '
        'Shares will begin trading on a split-adjusted basis on August 18, 2026.')
    assert not issues and events[0].ratio==Decimal('.05')


def test_us_explicit_conversion_must_agree_with_headline_ratio():
    events,_=parse_us('The board approved a 20-for-1 reverse stock split. '
        'Every 25 shares will be combined into one common share. '
        'Shares will begin trading on a split-adjusted basis on August 18, 2026.')
    assert not events


@pytest.mark.parametrize('number,denominator', [('twenty-five',25),('thirty-five',35),('one hundred',100),('one-hundred-and-fifty',150)])
def test_us_compound_english_ratio_is_not_partially_normalized(number,denominator):
    events,issues=parse_us('A one-for-'+number+' reverse stock split. '
        'Shares will begin trading on a split-adjusted basis on August 18, 2026.')
    assert not issues and events[0].ratio==Decimal(1)/denominator


def test_edgar_xhtml_encoding_header_does_not_break_source_parsing():
    html=('<?xml version="1.0" encoding="UTF-8"?><html><body>A one-for-twenty-five reverse stock split. '
          'Shares will begin trading on a split-adjusted basis on August 18, 2026.</body></html>').encode()
    events,issues=parse_us(html)
    assert not issues and events[0].ratio==Decimal('.04')


def test_invalid_zero_nan_ratios_rejected():
    for value in ['0','NaN','-2','Infinity']:
        with pytest.raises(ValueError):
            resolve_events([event(new_shares=value)])


def test_resumption_confirms_legal_effective_date_is_not_trading_date():
    result=parse_dart_resumption((FIXTURES/'canvasn_resumption_20260819900375_viewer.html').read_bytes())
    assert result['effective_date']=='2026-08-20'
    assert result['share_class']=='common'


def test_unrelated_historical_trading_date_does_not_match_current_ratio():
    events,issues=parse_us('Restated for the 1-for-18 reverse stock split on May 1, 2024. '
                          + 'Unrelated financial statement content. '*100
                          + 'The name change was reflected with the NASDAQ Capital Market at the open of business on April 12, 2021.')
    assert not events and issues


def test_momentum_does_not_treat_a_split_as_a_loss():
    from engine.transformers._internal.factor_metrics import add_price_momentum_factors
    days=pd.date_range('2020-01-01',periods=280)
    raw=[100.0]*140+[25.0]*140
    frame=pd.DataFrame({'trade_date':days,'close':raw,'adj_close':[25.0]*280,
                        'high':raw,'low':raw,'volume':[10.0]*140+[40.0]*140,'shares':[100.0]*140+[400.0]*140})
    result=add_price_momentum_factors(frame)
    assert result.ret_1m.dropna().eq(0).all()
    assert result.tr_12_1.dropna().eq(0).all()
    assert result.close.tolist()==raw


def alpha_payload(symbol='CON'):
    return {'Meta Data':{'2. Symbol':symbol},'Time Series (Daily)':{
        '2020-01-02':{'1. open':'25','2. high':'26','3. low':'24','4. close':'25',
                      '5. adjusted close':'25','6. volume':'400','7. dividend amount':'0','8. split coefficient':'4'},
        '2020-01-01':{'1. open':'100','2. high':'104','3. low':'96','4. close':'100',
                      '5. adjusted close':'25','6. volume':'100','7. dividend amount':'0','8. split coefficient':'1'},
    }}


def test_alpha_raw_close_preserved_and_windows_reserved_ticker_safe(tmp_path,monkeypatch):
    from engine.extractors.alpha_vantage_prices import download_alpha_vantage_prices,parse_daily_payload
    import engine.extractors.alpha_vantage_prices as module
    monkeypatch.setenv('ALPHA_VANTAGE_API_KEY','test-key')
    class Limiter:
        def __init__(self,**kwargs):pass
        def acquire(self):pass
    monkeypatch.setattr(module,'RollingRateLimiter',Limiter)
    calls=[]
    class Response:
        status_code=200
        headers={}
        def json(self):return alpha_payload()
    def get(url,**kwargs):calls.append(kwargs);return Response()
    result=download_alpha_vantage_prices(symbols=['CON'],as_of='2026-09-09',output_dir=tmp_path,http_get=get)
    assert result['downloaded']==1
    assert (tmp_path/'ticker=CON.json').is_file()
    assert calls[0]['params']['outputsize']=='full'
    assert 'test-key' not in (tmp_path/'ticker=CON.metadata.json').read_text()
    frame=parse_daily_payload(alpha_payload(),symbol='CON')
    assert frame.close.tolist()==[100,25]
    assert frame.vendor_adj_close.tolist()==[25,25]
    monkeypatch.delenv('ALPHA_VANTAGE_API_KEY')
    result=download_alpha_vantage_prices(symbols=['CON'],as_of='2026-09-09',output_dir=tmp_path,http_get=get)
    assert result['cached']==1 and len(calls)==1


def test_alpha_rejects_wrong_symbol_and_error_payloads():
    from engine.extractors.alpha_vantage_prices import parse_daily_payload
    with pytest.raises(ValueError,match='symbol'):
        parse_daily_payload(alpha_payload('AAPL'),symbol='NVDA')
    with pytest.raises(ValueError):
        parse_daily_payload({'Information':'rate limit'},symbol='AAPL')


def test_opendart_utf8_archive_with_stale_euckr_meta():
    raw=(FIXTURES/'canvasn_opendart_20260811900499.xml').read_bytes()
    assert b'charset=euc-kr' in raw
    events,issues=parse_dart_split(raw,security_id='SEC_KR_210120',source_id='20260811900499',
                                 source_url='https://opendart.fss.or.kr/api/document.xml?rcept_no=20260811900499',published_date='2026-08-11')
    assert not issues and events[0].ratio==Decimal('0.4')
    assert events[0].effective_date=='2026-08-20'


def test_opendart_unavailable_is_recorded_without_overwriting_source(tmp_path,monkeypatch):
    import requests
    from engine.extractors.opendart_stock_splits import OpenDartClient,download_document
    def unavailable_response(session,request,**kwargs):
        response=requests.Response()
        response.status_code=200
        response._content=b'<result><status>014</status><message>File does not exist</message></result>'
        response.url=request.url
        return response
    monkeypatch.setattr(requests.Session,'send',unavailable_response)
    status=download_document(OpenDartClient(key='transport-test'),tmp_path,{'source_id':'20180316800856'},'005930')
    assert status=='unavailable'
    assert not (tmp_path/'disclosures'/'005930'/'20180316800856.html').exists()
    assert (tmp_path/'document_archives'/'20180316800856.unavailable.json').exists()


@pytest.mark.parametrize('subsidiary_marker', ['none', 'title', 'header'])
def test_latest_board_decision_revision_replaces_original_date(tmp_path,monkeypatch,subsidiary_marker):
    from engine.core.paths import DataLakePaths
    import engine.workflows.stock_splits as workflow
    import hashlib,json
    lake=DataLakePaths(tmp_path/'lake')
    monkeypatch.setattr(workflow,'DATA_LAKE',lake)
    raw_path=lake.bronze('krx','price','kr_005930.csv')
    raw_path.parent.mkdir(parents=True)
    pd.DataFrame({'날짜':['2018-05-03','2018-05-04'],'시가':[0,51900],'고가':[0,51900],'저가':[0,51900],
                  '종가':[2650000,51900],'거래량':[0,200],'등락률':[0,-2.08]}).to_csv(raw_path,index=False)
    root=tmp_path/'official'
    folder=root/'disclosures'/'005930';folder.mkdir(parents=True)
    for receipt,kind in [('20180131800068','original'),('20180316800856','amended')]:
        raw=(FIXTURES/f'samsung_{kind}_{receipt}_viewer.html').read_bytes()
        path=folder/f'{receipt}.html';path.write_bytes(raw)
        meta={'provider':'DART','security_id':'SEC_KR_005930','stock_code':'005930','source_id':receipt,
              'source_url':f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}',
              'source_sha256':hashlib.sha256(raw).hexdigest(),'family_id':f'api:{receipt}',
              'published_date':f'{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}'}
        path.with_suffix('.html.metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    if subsidiary_marker != 'none':
        # A later subsidiary filing has the same board date and a different
        # timetable. It must not replace the listed parent's own amendment.
        child = raw.replace(b'2018.05.04', b'2018.05.10')
        if subsidiary_marker == 'header':
            child = ('<div>자회사인 별도법인 의 주요경영사항신고</div>' + child.decode('cp949')).encode('utf-8')
        path = folder / 'subsidiary.html'
        path.write_bytes(child)
        meta = dict(meta, source_id='20180317800001', family_id='api:subsidiary',
                    published_date='2018-03-17', source_sha256=hashlib.sha256(child).hexdigest(),
                    title='주식분할결정(종속회사의 주요경영사항)' if subsidiary_marker=='title' else '')
        path.with_suffix('.html.metadata.json').write_text(json.dumps(meta),encoding='utf-8')
    ledger=workflow.normalize_split_ledger('kr',as_of='2018-06-01',source_root=root,output_path=tmp_path/'ledger.json')
    assert len(ledger['events'])==1
    assert ledger['events'][0]['effective_date']=='2018-05-04'
    assert ledger['events'][0]['source_id']=='20180316800856'
    if subsidiary_marker != 'none':
        assert any('subsidiary_action_requires_affected_security_mapping' in r.get('reason', [])
                   for r in ledger['review'])


def test_kind_official_search_viewer_and_actual_listing_contract():
    from engine.extractors.kind_stock_splits import parse_kind_search,parse_kind_viewer,parse_kind_contents
    from engine.transformers.stock_splits import parse_kind_listing
    samples=Path(__file__).resolve().parents[1]/'data-lake/bronze/research/stock_splits/kr/stock_split_official_fallback_samples_20260909'
    rows,total=parse_kind_search((samples/'kind_samsung_amendment_search.html').read_bytes())
    assert total==1 and rows[0]['kind_acpt_no']=='20180316000856'
    code,doc,family=parse_kind_viewer((samples/'kind_samsung_amendment_viewer_main.html').read_bytes(),rows[0])
    assert code=='005930' and doc['kind_doc_no']=='20180314002608' and len(family)==2
    url=parse_kind_contents((samples/'kind_samsung_amendment_contents_response.html').read_bytes())
    assert url=='https://kind.krx.co.kr/external/2018/03/16/000856/20180314002608/91128.htm'
    raw=(samples/'samsung_20180503_actual_listing_kind.html').read_bytes()
    listing=parse_kind_listing(raw,security_id='SEC_KR_005930')
    assert listing['effective_date']=='2018-05-04' and Decimal(listing['ratio'])==50
    assert parse_kind_listing(raw,security_id='SEC_KR_005935') is None
    rows,total=parse_kind_search((samples/'kind_canvasn_resumption_search.html').read_bytes())
    canvas=next(r for r in rows if r['kind_acpt_no']=='20260819000375')
    code,doc,_=parse_kind_viewer((samples/'kind_canvasn_resumption_viewer_main.html').read_bytes(),canvas)
    assert code=='210120' and doc['kind_doc_no']=='20260819001063'


def test_kind_rejects_error_pages_wrong_dates_and_external_paths():
    from engine.extractors.kind_stock_splits import parse_kind_search,parse_kind_viewer,parse_kind_contents
    with pytest.raises(ValueError):parse_kind_search('<html>Service unavailable</html>')
    with pytest.raises(ValueError):parse_kind_contents("parent.setPath('','https://example.com/1.htm','x')")
    with pytest.raises(ValueError):parse_kind_contents("parent.setPath('','https://kind.krx.co.kr/external/../../1.htm','x')")
    html='<h1>Test (005930)</h1><select id="mainDoc"><option value="20180314002608|Y" selected>주식분할 결정 (2018.03.16)</option></select>'
    with pytest.raises(ValueError):parse_kind_viewer(html,{'published_date':'2018-01-31','title':'주식분할결정'})


def test_array_drawdown_matches_series_history_with_missing_values():
    import numpy as np
    from engine.transformers._internal.factor_metrics import max_drawdown,max_drawdown_array
    rng=np.random.default_rng(20260909)
    returns=pd.Series(rng.normal(0,.03,600));returns.iloc[::29]=np.nan
    expected=returns.rolling(231,min_periods=60).apply(max_drawdown,raw=False)
    actual=returns.rolling(231,min_periods=60).apply(max_drawdown_array,raw=True)
    np.testing.assert_allclose(actual,expected,rtol=1e-13,atol=1e-13,equal_nan=True)


def test_factor_price_reader_uses_ready_adjusted_panel_and_rejects_quarantine(tmp_path,monkeypatch):
    import json
    from engine.core.paths import DataLakePaths
    import engine.transformers._internal.factor_metrics as metrics
    lake=DataLakePaths(tmp_path)
    monkeypatch.setattr(metrics,'DATA_LAKE',lake)
    panel=lake.silver('corporate_actions','prices','us','us_TEST.parquet')
    panel.parent.mkdir(parents=True)
    frame=pd.DataFrame({'security_id':['SEC_US_TEST'],'trade_date':pd.to_datetime(['2020-01-02']),
                        'open':[100.],'high':[104.],'low':[96.],'close':[100.],'volume':[10.],
                        'split_adj_close':[25.],'currency':['USD']})
    frame.to_parquet(panel,index=False)
    meta=panel.with_suffix('.metadata.json');meta.write_text(json.dumps({'status':'ready'}))
    result=metrics.read_stock_prices('TEST',market='us')
    assert result.close.iloc[0]==100 and result.adj_close.iloc[0]==25
    cache=metrics.FactorMarketDataCache(market='us')
    assert cache.prices('SEC_US_TEST').adj_close.iloc[0]==25
    meta.write_text(json.dumps({'status':'failed'}))
    with pytest.raises(ValueError,match='not ready'):metrics.read_stock_prices('TEST',market='us')
    # An absent Alpha Vantage panel must not silently use legacy Yahoo prices.
    monkeypatch.setattr(metrics,'read_stock_csv_by_security_id',lambda *args:pytest.fail('legacy price fallback'))
    assert metrics.read_stock_prices('MISSING',market='us').empty
    assert cache.prices('SEC_US_MISSING').empty


def test_official_reference_price_resolves_tick_rounding_without_changing_ratio():
    from engine.transformers.stock_splits import parse_kind_reference_price,confirm_dart_with_prices
    from dataclasses import replace
    samples=Path(__file__).resolve().parents[1]/'data-lake/bronze/research/stock_splits/kr/stock_split_krx_reference_price_samples_20260910'
    notice=parse_kind_reference_price((samples/'000860_reference_price_notice.html').read_bytes())
    assert notice['reference_price']=='11930' and notice['effective_date']=='2025-04-30'
    event=SplitEvent(security_id='SEC_KR_000860',effective_date='2025-04-30',new_shares='2',old_shares='1',
                     source='DART',source_id='20250403800290',published_date='2025-04-03',
                     source_url='https://opendart.fss.or.kr/api/document.xml?rcept_no=20250403800290',source_sha256='fixture',status='announced')
    prices=pd.DataFrame({'security_id':['SEC_KR_000860']*2,'trade_date':pd.to_datetime(['2025-04-29','2025-04-30']),
                         'close':[23850.,11850.],'volume':[0.,100.],'change_rate':[0.,-.67]})
    assert confirm_dart_with_prices(event,prices)[0].status=='announced'
    confirmed,issues=confirm_dart_with_prices(event,prices,reference_price=notice['reference_price'],reference_exchange=notice['exchange'])
    assert not issues and confirmed.status=='confirmed' and confirmed.ratio==2
    wrong,issues=confirm_dart_with_prices(replace(event,new_shares='3'),prices,
                                        reference_price=notice['reference_price'],reference_exchange=notice['exchange'])
    assert wrong.status=='announced' and issues==['official_ratio_conflicts_with_reference_price']
    adjusted=adjust_prices(prices,[confirmed],as_of='2025-04-30')
    # Portfolio wealth uses the exact split units, not the rounded quote base.
    assert adjusted.split_adj_close.iloc[0]==11925
    raw=(samples/'000860_reference_price_notice.html').read_text(encoding='utf-8')
    assert parse_kind_reference_price(raw.replace('보통주식','우선주식')) is None
    raw=(samples/'cherrybro_reference_price_notice.html').read_bytes()
    notice=parse_kind_reference_price(raw,security_id='SEC_KR_066360')
    assert notice['reference_price']=='1340' and notice['ratio']=='0.5'
    assert notice['effective_date']=='2026-08-28'
    assert parse_kind_reference_price(raw,security_id='SEC_KR_005930') is None


def test_no_par_capital_per_share_cannot_supply_a_split_ratio():
    raw=(FIXTURES/'canvasn_opendart_20260811900499.xml').read_bytes().decode('utf-8')
    raw=raw.replace('</body>','<p>무액면 주식의 1주당 자본금은 반올림 표시입니다.</p></body>')
    found,issues=parse_dart_split(raw,
        security_id='SEC_KR_210120',source_id='test',published_date='2026-08-11',
        source_url='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=test')
    assert found==[] and issues==['no_par_share_requires_explicit_ratio_source']


def test_new_listing_episode_resets_technical_history():
    import numpy as np
    from engine.transformers._internal.factor_metrics import add_price_momentum_factors
    dates=pd.bdate_range('2018-01-01',periods=300)
    close=np.r_[np.full(260,15.),np.linspace(7750.,8000.,40)]
    data=pd.DataFrame({'trade_date':dates,'close':close,'adj_close':close,'high':close,'low':close,
                       'volume':100.,'shares':10000.,'listing_episode':np.r_[np.zeros(260),np.ones(40)]})
    result=add_price_momentum_factors(data)
    assert result.ret_1m.iloc[260:281].isna().all()
    assert result.tr_12_1.iloc[260:].isna().all()
    assert result.ma_50.iloc[260]==7750.
    assert np.isclose(result.ret_1m.iloc[281],close[281]/close[260]-1)


def test_reviewed_evidence_rejects_changed_bytes_and_ratio(tmp_path):
    import hashlib,json
    from engine.transformers.corporate_action_evidence import validate_evidence
    raw='<p>2026년 05월 28일 주식병합(50:1)을 실시</p>'.encode()
    folder=tmp_path/'disclosures'/'900300';folder.mkdir(parents=True)
    path=folder/'kind_20260601001556_20260601002507.html';path.write_bytes(raw)
    source={'source_id':'20260601001556','kind_doc_no':'20260601002507','published_date':'2026-06-01',
            'source_url':'https://kind.krx.co.kr/external/2026/06/01/001556/20260601002507/11013.htm',
            'source_sha256':hashlib.sha256(raw).hexdigest(),'role':'exact_ratio',
            'markers':['2026년 05월 28일 주식병합(50:1)을 실시']}
    path.with_suffix('.html.metadata.json').write_text(json.dumps(source|{'security_id':'SEC_KR_900300'}))
    actual_raw=b'<p>A900300 actual listing 2026-06-24</p>'
    actual=source|{'source_id':'20260623000445','kind_doc_no':'20260623001102','published_date':'2026-06-23',
                   'source_url':'https://kind.krx.co.kr/external/2026/06/23/000445/20260623001102/70763.htm',
                   'source_sha256':hashlib.sha256(actual_raw).hexdigest(),'role':'actual_listing',
                   'markers':['A900300 actual listing 2026-06-24']}
    actual_path=folder/'kind_20260623000445_20260623001102.html';actual_path.write_bytes(actual_raw)
    actual_path.with_suffix('.html.metadata.json').write_text(json.dumps(actual|{'security_id':'SEC_KR_900300'}))
    record={'kind':'share_consolidation','security_id':'SEC_KR_900300','effective_date':'2026-06-24',
            'new_shares':'1','old_shares':'50','ratio_marker':source['markers'][0],'sources':[source,actual]}
    manifest=tmp_path/'evidence.json'
    manifest.write_text(json.dumps({'records':[record]}))
    events,_,issues=validate_evidence(tmp_path,as_of='2026-06-24',manifest_path=manifest)
    assert not issues and events[0].ratio==Decimal('0.02')
    record['old_shares']='40';manifest.write_text(json.dumps({'records':[record]}))
    assert validate_evidence(tmp_path,as_of='2026-06-24',manifest_path=manifest)[0]==[]
    record['old_shares']='50';manifest.write_text(json.dumps({'records':[record]}))
    path.write_bytes(raw+b'changed')
    assert validate_evidence(tmp_path,as_of='2026-06-24',manifest_path=manifest)[0]==[]


def test_unpaid_consolidation_keeps_market_loss_and_rejects_cash_or_differential():
    from engine.transformers.stock_splits import parse_dart_capital_consolidation,parse_kind_capital_listing
    from dataclasses import replace
    html='''<table><tr><td>4. 감자전후 발행주식수</td><td>보통주식</td><td>3000</td><td>100</td></tr>
      <tr><td>7. 감자방법</td><td>기명식 보통주 30주를 동일한 액면주식 1주로 무상병합</td></tr>
      <tr><td>신주상장예정일</td><td>2026.05.06</td></tr>
      <tr><td>12. 이사회결의일</td><td>2026.03.13</td></tr></table>'''
    kwargs={'security_id':'SEC_KR_002070','source_id':'20260313001817','published_date':'2026-03-13',
            'source_url':'https://kind.krx.co.kr/external/2026/03/13/001817/20260313004516/11329.htm'}
    events,issues=parse_dart_capital_consolidation(html,**kwargs)
    assert not issues and events[0].ratio==Decimal(1)/30 and events[0].status=='announced'
    assert events[0].capital_action_kind=='capital_reduction_unpaid'
    assert not parse_dart_capital_consolidation(html.replace('무상병합','유상감자'),**kwargs)[0]
    assert not parse_dart_capital_consolidation(html.replace('기명식','차등감자 기명식'),**kwargs)[0]
    notice='변경상장(자본감소) 보통주 단축코드:A002070 변경상장일 : 2026년05월06일'
    actual=parse_kind_capital_listing(notice,security_id='SEC_KR_002070')
    assert actual['effective_date']=='2026-05-06'
    assert parse_kind_capital_listing(notice,security_id='SEC_KR_005930') is None
    prices=pd.DataFrame({'security_id':['SEC_KR_002070']*2,
                         'trade_date':pd.to_datetime(['2026-04-13','2026-05-06']),'close':[355.,11850.]})
    result=adjust_prices(prices,[replace(events[0],status='confirmed')],as_of='2026-05-06')
    # Holdings use the 30:1 share conversion, independently of the reopening auction base.
    assert result.split_adj_close.iloc[0]==10650.
    assert result.split_adj_close.pct_change().iloc[-1]==pytest.approx(11850/10650-1)


def test_conflicting_legal_and_trading_year_is_reviewed_without_guessing():
    raw='''The Company approved a 2-for-1 stock split. The amendment will become
      effective at 5:00 p.m., Eastern Time, on January 15, 2025.
      Trading is expected to commence on a split-adjusted basis on January 16, 2024.'''
    kwargs={'security_id':'SEC_US_TEST','source_id':'test','source_url':'https://www.sec.gov/test',
            'published_date':'2024-11-20'}
    found,issues=parse_edgar_split(raw,**kwargs)
    assert not found and issues==['inconsistent_legal_and_trading_dates']
    found,issues=parse_edgar_split(raw.replace('January 16, 2024','January 16, 2025'),**kwargs)
    assert not issues and found[0].effective_date=='2025-01-16'


@pytest.mark.parametrize('method',[
    '기명식 보통주식 20주를 동일 액면가의 기명식 보통주식 1주로 무상합병',
    '20:1 무상감자 (액면가 500원의 보통주식 20주를 동일 액면가의 보통주식 1주로 병합하는 무상감자)',
])
def test_uniform_unpaid_consolidation_method_wording(method):
    from engine.transformers.stock_splits import parse_dart_capital_consolidation
    html=f'''<table><tr><td>4. 감자전후 발행주식수</td><td>보통주식</td><td>2000</td><td>100</td></tr>
      <tr><td>7. 감자방법</td><td>{method}</td></tr>
      <tr><td>신주상장예정일</td><td>2022.12.19</td></tr>
      <tr><td>12. 이사회결의일</td><td>2022.10.11</td></tr></table>'''
    kwargs={'security_id':'SEC_KR_TEST','source_id':'test','published_date':'2022-10-11',
            'source_url':'https://kind.krx.co.kr/test'}
    events,issues=parse_dart_capital_consolidation(html,**kwargs)
    assert not issues and events[0].ratio==Decimal('0.05') and events[0].status=='announced'
    assert not parse_dart_capital_consolidation(html.replace('무상','유상'),**kwargs)[0]


def test_kind_no_par_reference_notice_explicit_ratio():
    from engine.transformers.stock_splits import parse_kind_reference_price
    html='''<h1>기준가격안내</h1><table>
      <tr><td>보통주식</td><td>A900300</td><td>680</td></tr>
      <tr><td>3. 적용일</td><td>2024-12-04</td></tr>
      <tr><td>4. 사유</td><td>주식병합</td><td>10:1 비율로 주식병합</td></tr></table>'''
    actual=parse_kind_reference_price(html,security_id='SEC_KR_900300')
    assert actual['ratio']=='0.1' and actual['effective_date']=='2024-12-04'
    assert 'ratio' not in parse_kind_reference_price(html.replace('10:1 비율로 주식병합','무액면 주식병합'))
    assert parse_kind_reference_price(html,security_id='SEC_KR_005930') is None
