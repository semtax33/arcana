"""Download immutable DART/EDGAR split disclosures with resumable search evidence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
import pandas as pd
import requests

from engine.core.paths import DATA_LAKE, resolve_sec_ticker_map
from engine.core.source_storage import write_source_bytes, write_source_text
from engine.transformers._internal.edgar_identity import resolve_edgar_identity


def root_for(market):
    return DATA_LAKE.bronze('dart' if market=='kr' else 'sec','stock_splits')


def iso(value):
    return pd.Timestamp(str(value)).date().isoformat()


def _json(path,payload):
    write_source_text(path,json.dumps(payload,ensure_ascii=False,indent=2),source='official-stock-split-metadata')


class OfficialSession:
    def __init__(self,interval=.22):
        self.session=requests.Session()
        self.interval=max(.12,interval)
        self.last=0.0

    def request(self,method,url,**kwargs):
        for attempt in range(4):
            time.sleep(max(0,self.interval-(time.monotonic()-self.last)))
            self.last=time.monotonic()
            try:
                response=self.session.request(method,url,timeout=35,**kwargs)
                if response.status_code not in {429,500,502,503,504}:
                    response.raise_for_status()
                    return response
            except (requests.ConnectionError,requests.Timeout):
                if attempt==3:raise
            time.sleep(min(2**attempt,8))
        response.raise_for_status()
        raise RuntimeError('official source request exhausted retries')


def _save_response(root,name,response,metadata):
    path=root/name
    write_source_bytes(path,response.content,source='official-stock-split-disclosure')
    item={**metadata,'source_url':response.url,'source_sha256':hashlib.sha256(response.content).hexdigest(),
          'retrieved_at':datetime.now(timezone.utc).isoformat(),'content_type':response.headers.get('Content-Type',''),
          'path':str(path.resolve())}
    _json(path.with_suffix(path.suffix+'.metadata.json'),item)
    return item


def _retain_failed_dart_document(root,record,code,family,stage,error,*,response=None,source_url):
    """Retain a failed request or navigation body as an unavailable document version."""
    receipt=record['source_id']
    raw=response.content if response is not None else b''
    digest=hashlib.sha256(raw).hexdigest()
    family=sorted(set([receipt,*family]))
    metadata={**record,'provider':'DART','security_id':f'SEC_KR_{code}','stock_code':code,
        'published_date':record.get('published_date'),
        'family':family,'family_id':min(family),'source_validation':'failed',
        'document_stage':stage,'validation_error':str(error),'error_type':type(error).__name__,
        'http_response_received':response is not None,
        'http_status':response.status_code if response is not None else None,'byte_count':len(raw)}
    path=Path(root)/'public_documents'/receipt/f'{digest}.{stage}.html'
    if raw:
        return _save_response(path.parent,path.name,response,metadata)
    item={**metadata,'source_url':response.url if response is not None else source_url,
          'source_sha256':digest,'path':None,'retrieved_at':datetime.now(timezone.utc).isoformat()}
    _json(path.with_suffix('.html.metadata.json'),item)
    return item


def _refresh_cached_dart_publication(root,record,metadata_path):
    """Correct legacy availability metadata while retaining its exact prior version."""
    if not record.get('published_date'):return
    raw=metadata_path.read_bytes()
    metadata=json.loads(raw)
    same_date=metadata.get('published_date')==record['published_date']
    if same_date and (metadata.get('publication_date_source') or not record.get('publication_date_source')):return
    digest=hashlib.sha256(raw).hexdigest()
    archive=Path(root)/'metadata_versions'/record['source_id']/f'{digest}.metadata.json'
    write_source_bytes(archive,raw,source='DART-publication-date-metadata-before-correction')
    correction={key:record[key] for key in ['published_date','publication_date_text','publication_date_source']
                if key in record}
    _json(metadata_path,{**metadata,**correction,'publication_date_prior_metadata':{
        'path':str(archive.resolve()),'sha256':digest,'published_date':metadata.get('published_date')},
        'publication_date_corrected_at':datetime.now(timezone.utc).isoformat()})


def parse_dart_search(html):
    soup=BeautifulSoup(html,'lxml')
    records=[]
    for anchor in soup.find_all('a',href=True):
        href=anchor['href']
        match=re.search(r'rcpNo=(\d{14})',href)
        if not match or 'dsaf001' not in href:continue
        row=anchor.find_parent('tr')
        if row is None:continue
        corp=re.search(r"openCorpInfoNew\(\s*['\"](\d{8})",str(row))
        title=anchor.get_text(' ',strip=True)
        if not any(k in re.sub(r'\s+','',title) for k in ['주식분할','주식병합']):continue
        record={'source_id':match.group(1),'corp_code':corp.group(1) if corp else '',
                'title':title,'main_url':urljoin('https://dart.fss.or.kr',href)}
        displayed_dates=[cell.get_text(' ',strip=True) for cell in row.find_all('td',recursive=False)
                         if re.fullmatch(r'\d{4}\.\d{2}\.\d{2}',cell.get_text(' ',strip=True))]
        if len(displayed_dates)>1:raise ValueError('Ambiguous DART receipt-date cells')
        if displayed_dates:
            record.update(published_date=iso(displayed_dates[0]),publication_date_text=displayed_dates[0])
        records.append(record)
    text=soup.get_text(' ',strip=True)
    totals=re.findall(r'총\s*([\d,]+)\s*건',text)
    if not totals:
        # An error/block page must not be recorded as a successful empty search.
        empty_cell=soup.select_one('table.tbList tbody td.no_data')
        official_empty=empty_cell is not None and empty_cell.get_text(strip=True)=='조회 결과가 없습니다.'
        if not records and not official_empty and not any(s in text for s in ['조회된 데이타가 없습니다','검색된 자료가 없습니다','검색결과가 없습니다','조회된 데이터가 없습니다']):
            raise ValueError('DART response has neither a result count nor a recognized empty-result marker')
    total=int(totals[-1].replace(',','')) if totals else len(records)
    return records,total


def dart_family(html):
    soup=BeautifulSoup(html,'lxml')
    select=soup.find('select',id='family')
    if select is None:return []
    return sorted(set(re.findall(r'rcpNo=(\d{14})',str(select))))


def dart_family_dates(html):
    """Read each receipt's own displayed publication date from the official family."""
    soup=BeautifulSoup(html,'lxml')
    result={}
    for option in soup.select('select#family option'):
        receipt=re.search(r'rcpNo=(\d{14})',option.get('value',''))
        displayed=re.match(r'(\d{4}\.\d{2}\.\d{2})(?:\s|$)',option.get_text(' ',strip=True))
        if receipt and displayed:
            value={'published_date':iso(displayed[1]),'publication_date_text':displayed[1]}
            if option.get('title'):value['title']=option['title']
            if receipt[1] in result and result[receipt[1]]['published_date']!=value['published_date']:
                raise ValueError('Conflicting DART family publication dates')
            result[receipt[1]]=value
    return result


def dart_viewer_parameters(html,receipt):
    text=html.decode('utf-8-sig') if isinstance(html,bytes) else html
    for match in re.finditer(r'\bviewDoc\s*\(([^()]*)\)',text):
        tokens=[value.strip() for value in match[1].split(',')]
        if len(tokens)<6:continue
        values=[]
        for token in tokens[:6]:
            if token=='null':values.append(None)
            elif re.fullmatch(r'''"[^"\\]*"|'[^'\\]*' ''',token,re.VERBOSE):values.append(token[1:-1])
            else:break
        if len(values)!=6 or values[0]!=receipt:continue
        params=dict(zip(['rcpNo','dcmNo','eleId','offset','length','dtd'],values))
        if not re.fullmatch(r'\d+',params['dcmNo'] or ''):continue
        if params['dtd']=='HTML':
            return {key:params[key] for key in ['rcpNo','dcmNo','dtd']}
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',params['dtd'] or '') or any(
            not re.fullmatch(r'\d+',params[key] or '') for key in ['eleId','offset','length']
        ):raise ValueError('Incomplete DART XML viewer parameters')
        # The official main page uses searchGubun=1 for the entire document.
        # Omitting offsets gives an empty response; the first node alone is
        # often just the correction cover. Keep the actual document DTD.
        return {**params,'keyword':'주식','searchGubun':'1'}
    raise ValueError('DART document number not found for requested receipt')


def validate_dart_viewer(content,main,params):
    from engine.transformers._internal.dart_document import _decode
    decoded,encoding=_decode(content)
    soup=BeautifulSoup(decoded,'lxml')
    if soup.body is None or not soup.body.get_text(strip=True):
        raise ValueError('DART viewer did not return a readable HTML document')
    if params['dtd']=='HTML':
        main_soup=BeautifulSoup(main,'lxml')
        title=main_soup.title.get_text(strip=True) if main_soup.title else ''
        parts=[re.sub(r'\s+','',part) for part in title.split('/')]
        returned=re.sub(r'\s+','',soup.title.get_text() if soup.title else '')
        if len(parts)<3 or any(not part or part not in returned for part in parts) or soup.body.find('table') is None:
            raise ValueError('DART HTML viewer does not identify the requested disclosure')
        return {'encoding':encoding,'full_document_verified':True,'toc_sections':None,
                'document_title':title,'verification_basis':'Full HTML endpoint; issuer, report and date in document title'}
    text=main.decode('utf-8-sig') if isinstance(main,bytes) else main
    nodes=[]
    pattern=r'''node(\d+)\['text'\]\s*=\s*"([^"]+)";([\s\S]*?)node\1\['dtd'\]\s*=\s*"([^"]+)";'''
    for match in re.finditer(pattern,text):
        fields=dict(re.findall(r'''\['(rcpNo|dcmNo|eleId)'\]\s*=\s*"([^"]+)"''',match[3]))
        if fields.get('rcpNo')==params['rcpNo'] and fields.get('dcmNo')==params['dcmNo']:
            nodes.append((fields.get('eleId'),match[2]))
    expected={f'toc{element}' for element,_ in nodes}
    actual={anchor.get('name') for anchor in soup.find_all('a',attrs={'name':re.compile(r'^toc\d+$')})}
    compact=re.sub(r'\s+','',soup.body.get_text())
    if (not nodes or len(expected)!=len(nodes) or expected!=actual or
        any(re.sub(r'\s+','',title) not in compact for _,title in nodes)):
        raise ValueError('DART viewer is missing declared document sections')
    return {'encoding':encoding,'full_document_verified':True,'toc_sections':len(nodes)}


def _dart_symbol(session,root,corp):
    path=root/'corporations'/f'{corp}.json'
    if path.exists():return json.loads(path.read_text(encoding='utf-8'))['stock_code']
    r=session.request('POST','https://dart.fss.or.kr/dsae001/selectPopup.ax',data={'selectKey':corp})
    soup=BeautifulSoup(r.content,'lxml')
    code=None
    for row in soup.find_all('tr'):
        cells=[c.get_text(' ',strip=True) for c in row.find_all(['th','td'])]
        if any('종목코드' in c for c in cells):
            code=next((c.strip() for c in cells if re.fullmatch(r'\d{6}',c.strip())),None)
    if not code:raise ValueError(f'no listed stock code in DART corporation {corp}')
    meta=_save_response(root/'corporations',f'{corp}.html',r,{'corp_code':corp,'stock_code':code})
    _json(path,meta)
    return code


def download_dart_splits(*,symbols=None,start_date='20020101',end_date=None,force=False,
                         sleep_seconds=.22,output_dir=None):
    root=Path(output_dir or root_for('kr'));root.mkdir(parents=True,exist_ok=True)
    session=OfficialSession(sleep_seconds)
    session.session.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Arcana Research',
                                    'Referer':'https://dart.fss.or.kr/','Accept-Language':'ko-KR,ko;q=0.9'})
    start=pd.Timestamp(iso(start_date));end=pd.Timestamp(iso(end_date or date.today()))
    wanted={str(s).zfill(6) for s in symbols} if symbols else None
    records={};errors=[];search_pages=0;outside_cutoff=[]
    for year in range(start.year,end.year+1):
        a=max(start,pd.Timestamp(year,1,1));b=min(end,pd.Timestamp(year,12,31))
        for title in ['주식분할결정','주식병합결정']:
            page=1
            while True:
                params={'currentPage':str(page),'maxResults':'100','maxLinks':'10','sort':'date','series':'desc',
                        'pageGubun':'corp','textCrpNm':'','textCrpCik':'','startDate':a.strftime('%Y%m%d'),
                        'endDate':b.strftime('%Y%m%d'),'autoSearch':'N','autoSearchCorp':'N','option':'corp',
                        'businessCode':'all','corporationType':'all','closingAccountsMonth':'all',
                        'reportName':title,'reportName2':title,'tocSrch2':''}
                key=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:20]
                cache=root/'search'/f'{key}.html'
                if cache.exists() and not force and b.date()<date.today()-timedelta(days=14):
                    content=cache.read_bytes()
                else:
                    r=session.request('POST','https://dart.fss.or.kr/dsab007/detailSearch.ax',data=params)
                    _save_response(root/'search',cache.name,r,{'request':params});content=r.content
                found,total=parse_dart_search(content)
                if not found and total>0:raise ValueError('DART search returned a nonempty count without parseable disclosures')
                for record in found:
                    if record.get('published_date'):
                        record['publication_date_source']={'kind':'DART_search_receipt_date',
                            'path':str(cache.resolve()),'sha256':hashlib.sha256(content).hexdigest()}
                records.update({r['source_id']:r for r in found});search_pages+=1
                if page*100>=total:break
                page+=1
                if page>500:raise ValueError('DART pagination exceeded bound; partition the search interval')
        print(f'[SPLITS] DART search year={year} disclosures={len(records)}',flush=True)
    done=0;skipped=0;consecutive_network_errors=0
    pending=list(sorted(records.items()))
    seen=set(records)
    for receipt,record in pending:
        try:
            code=_dart_symbol(session,root,record['corp_code'])
            if wanted is not None and code not in wanted:
                skipped+=1;continue
            folder=root/'disclosures'/code
            meta_path=folder/f'{receipt}.html.metadata.json'
            if meta_path.exists() and not force:
                _refresh_cached_dart_publication(root,record,meta_path)
                done+=1;continue
            main=None
            family=record.get('family') or [receipt]
            try:
                main=session.request('GET',record['main_url'])
                main_source=_save_response(folder,f'{receipt}.main.html',main,{**record,'stock_code':code})
                family=sorted(set([*family,*(dart_family(main.content) or [receipt])]))
                dates=dart_family_dates(main.content)
                date_source={'kind':'DART_family_receipt_date','path':main_source['path'],
                             'sha256':main_source['source_sha256']}
                if (record.get('published_date') and receipt in dates
                        and record['published_date']!=dates[receipt]['published_date']):
                    record={**record,'publication_date_conflict':[
                        {'published_date':record['published_date'],'source':record.get('publication_date_source')},
                        {'published_date':dates[receipt]['published_date'],'source':date_source}],
                        'published_date':None}
                    raise ValueError('Conflicting DART publication dates in the index and document family')
                if not record.get('published_date') and receipt in dates:
                    record={**record,**dates[receipt],'publication_date_source':date_source}
                if not record.get('published_date'):
                    raise ValueError('Missing evidenced DART publication date')
                if record['published_date']>end.date().isoformat():
                    outside_cutoff.append({key:record[key] for key in
                        ['source_id','published_date','publication_date_source'] if key in record})
                    continue
                # Preserve the observed family if a related main request later fails.
                for related in family:
                    related_date=dates.get(related,{}).get('published_date')
                    if related not in seen and (related_date is None or related_date<=end.date().isoformat()):
                        seen.add(related)
                        related_record={key:value for key,value in record.items()
                                        if key not in {'published_date','publication_date_text','publication_date_source'}}
                        related_record.update(source_id=related,family=family,
                            main_url=f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={related}')
                        if related in dates:
                            related_record.update(**dates[related],publication_date_source=date_source)
                        pending.append((related,related_record))
                params=dart_viewer_parameters(main.content,receipt)
            except (requests.RequestException,ValueError,UnicodeError) as exc:
                response=getattr(exc,'response',None)
                if response is None:response=main
                _retain_failed_dart_document(root,record,code,family,'main',exc,
                    response=response,source_url=record['main_url'])
                raise
            try:
                viewer=session.request('GET','https://dart.fss.or.kr/report/viewer.do',params=params)
            except requests.HTTPError as exc:
                if exc.response is not None:
                    viewer=exc.response
                else:
                    _retain_failed_dart_document(root,record,code,family,'viewer',exc,
                        source_url='https://dart.fss.or.kr/report/viewer.do?'+urlencode(params))
                    raise
            except requests.RequestException as exc:
                _retain_failed_dart_document(root,record,code,family,'viewer',exc,
                    response=getattr(exc,'response',None),
                    source_url='https://dart.fss.or.kr/report/viewer.do?'+urlencode(params))
                raise
            context={**record,'security_id':f'SEC_KR_{code}','stock_code':code,
                     'published_date':record['published_date'],
                     'family':family,'family_id':min(family),'provider':'DART','request':params,
                     'source_validation':'pending','http_status':viewer.status_code,'byte_count':len(viewer.content)}
            digest=hashlib.sha256(viewer.content).hexdigest()
            raw_folder=root/'public_documents'/receipt
            raw_path=raw_folder/f'{digest}.viewer.html'
            if viewer.content:
                source=_save_response(raw_folder,raw_path.name,viewer,context)
            else:
                source={**context,'source_url':viewer.url,'source_sha256':digest,'path':None,
                        'retrieved_at':datetime.now(timezone.utc).isoformat()}
                _json(raw_path.with_suffix('.html.metadata.json'),source)
            try:
                viewer.raise_for_status()
                verification=validate_dart_viewer(viewer.content,main.content,params)
            except (ValueError,requests.RequestException) as exc:
                _json(raw_path.with_suffix('.html.metadata.json'),{
                    **source,'source_validation':'failed','validation_error':str(exc)})
                raise
            _save_response(folder,f'{receipt}.html',viewer,{**context,**verification,
                'source_validation':'verified','retained_viewer_path':str(raw_path.resolve())})
            _json(raw_path.with_suffix('.html.metadata.json'),{**source,**verification,'source_validation':'verified'})
            done+=1
            consecutive_network_errors=0
        except Exception as exc:
            errors.append({'source_id':receipt,'error':f'{type(exc).__name__}: {exc}'})
            if isinstance(exc,requests.RequestException):
                consecutive_network_errors+=1
                if consecutive_network_errors>=5:
                    errors.append({'error':'DART collection paused after five consecutive network failures; cached sources retained'})
                    break
        if (done+len(errors))%25==0:
            print(f'[SPLITS] DART downloaded={done} errors={len(errors)}',flush=True)
    summary={'provider':'DART','start_date':str(start.date()),'end_date':str(end.date()),'search_pages':search_pages,
             'disclosures_found':len(records),'downloaded_or_cached':done,'outside_symbols':skipped,
             'outside_cutoff':outside_cutoff,'errors':errors}
    _json(root/'download_report.json',summary)
    return summary


EDGAR_QUERY='"stock split" OR "share consolidation" OR "reverse split"'


def download_edgar_splits(*,symbols=None,start_date='20020101',end_date=None,force=False,
                          sleep_seconds=.22,output_dir=None,only_known_actions=False):
    root=Path(output_dir or root_for('us'));root.mkdir(parents=True,exist_ok=True)
    mapping=pd.read_csv(resolve_sec_ticker_map(data_lake=DATA_LAKE),dtype=str,keep_default_na=False)
    mapping['cik']=mapping.cik.map(lambda c:str(int(c)))
    if symbols:
        selected_ciks=set(mapping.loc[mapping.ticker.isin({str(s).upper() for s in symbols}),'cik'])
        mapping=mapping[mapping.cik.isin(selected_ciks)]
    if mapping.empty:
        raise ValueError('no requested symbols could be mapped to an SEC CIK')
    # One issuer can have multiple listed classes: retain the class ambiguity in metadata.
    cik_symbols=mapping.groupby('cik').ticker.apply(list).to_dict()
    session=OfficialSession(sleep_seconds)
    session.session.headers.update({'User-Agent':resolve_edgar_identity(),'Accept':'application/json',
                                    'Referer':'https://www.sec.gov/edgar/search/'})
    start=pd.Timestamp(iso(start_date));end=pd.Timestamp(iso(end_date or date.today()))
    hits={};searches=0;errors=[]
    def search_window(a,b,ciks=None):
        nonlocal searches
        offset=0
        while True:
            params={'q':EDGAR_QUERY,'dateRange':'custom','startdt':str(a.date()),'enddt':str(b.date()),
                    'forms':'8-K,6-K','from':str(offset)}
            if ciks:params['ciks']=','.join(str(c).zfill(10) for c in ciks)
            key=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:20]
            path=root/'search'/f'{key}.json'
            if path.exists() and not force and b.date()<date.today()-timedelta(days=14):
                payload=json.loads(path.read_text(encoding='utf-8'))
            else:
                try:
                    r=session.request('GET','https://efts.sec.gov/LATEST/search-index',params=params)
                except requests.HTTPError as exc:
                    # Large result offsets sometimes produce a server error.
                    # Smaller date windows preserve coverage without changing
                    # the endpoint, credentials, issuer filter or search query.
                    if exc.response is not None and exc.response.status_code>=500 and (b-a).days>=1:
                        mid=a+(b-a)//2
                        search_window(a,mid,ciks);search_window(mid+pd.Timedelta(days=1),b,ciks);return
                    raise
                payload=r.json()
                if payload.get('timed_out') or payload.get('_shards',{}).get('failed',0):
                    raise ValueError('EDGAR search did not cover all shards')
                _json(path,payload)
            searches+=1
            total=payload['hits']['total']['value']
            if total>=10000 or payload['hits']['total'].get('relation')!='eq':
                if (b-a).days<1:raise ValueError('EDGAR search cap exceeded for one day')
                mid=a+(b-a)//2
                search_window(a,mid,ciks);search_window(mid+pd.Timedelta(days=1),b,ciks);return
            found=payload['hits']['hits']
            for item in found:
                hit_ciks={str(int(c)) for c in item['_source'].get('ciks',[])}
                if hit_ciks&set(cik_symbols):hits[item['_id']]=item
            offset+=len(found)
            if offset>=total:return
            if not found:raise ValueError('EDGAR pagination stopped before reported total')
    if symbols and len(cik_symbols)<=20:
        search_window(start,end,list(cik_symbols))
    else:
        for year in range(start.year,end.year+1):
            search_window(max(start,pd.Timestamp(year,1,1)),min(end,pd.Timestamp(year,12,31)))
            print(f'[SPLITS] EDGAR search year={year} matching_documents={len(hits)}',flush=True)
            _json(root/'indexed_hits.json',{'start_date':str(start.date()),'indexed_through':str(min(end,pd.Timestamp(year,12,31)).date()),'hits':list(hits.values())})
    action_dates={}
    if only_known_actions:
        from engine.workflows.stock_splits import load_alpha_split_candidates
        candidates,_=load_alpha_split_candidates(as_of=str(end.date()))
        for candidate in candidates:
            action_dates.setdefault(candidate['symbol'],set()).add(pd.Timestamp(candidate['effective_date']))
        # The daily price endpoint also supplies executed actions since the
        # independently downloaded SPLITS snapshot.
        for path in DATA_LAKE.bronze('alpha-vantage','price').glob('ticker=*.json'):
            if path.name.endswith('.metadata.json'):continue
            ticker=path.stem.removeprefix('ticker=')
            payload=json.loads(path.read_text(encoding='utf-8'))
            for day,row in payload.get('Time Series (Daily)',{}).items():
                if float(row['8. split coefficient'])!=1:
                    action_dates.setdefault(ticker,set()).add(pd.Timestamp(day))
    done=0
    outside_action_window=0
    for hit in hits.values():
        src=hit['_source'];accession,document=hit['_id'].split(':',1)
        if not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}',accession) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',document):
            errors.append({'source_id':hit['_id'],'error':'invalid EDGAR accession or document filename'});continue
        matched=sorted({str(int(c)) for c in src.get('ciks',[])}&set(cik_symbols))
        if len(matched)!=1:
            errors.append({'source_id':hit['_id'],'error':'ambiguous issuer CIK'});continue
        cik=matched[0];tickers=cik_symbols[cik]
        if only_known_actions and not any(abs((pd.Timestamp(src['file_date'])-day).days)<=120
                                         for ticker in tickers for day in action_dates.get(ticker,())):
            outside_action_window+=1;continue
        safe=re.sub(r'[^A-Za-z0-9._-]','_',document)
        folder=root/'disclosures'/cik
        file=f'{accession}_{safe}'
        path=folder/file
        if path.exists() and path.with_suffix(path.suffix+'.metadata.json').exists() and not force:
            done+=1;continue
        url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-","")}/{document}'
        try:
            response=session.request('GET',url)
            if b'<html' not in response.content[:2000].lower() and b'<HTML' not in response.content[:2000]:
                raise ValueError('expected an HTML disclosure')
            _save_response(folder,file,response,{'provider':'EDGAR','source_id':accession,'document_id':hit['_id'],
                           'published_date':src['file_date'],'cik':cik,'symbols':tickers,'form':src.get('form'),
                           'security_id':f'SEC_US_{tickers[0]}' if len(tickers)==1 else ''})
            done+=1
        except Exception as exc:
            errors.append({'source_id':hit['_id'],'error':f'{type(exc).__name__}: {exc}'})
        if (done+len(errors))%25==0:
            print(f'[SPLITS] EDGAR downloaded={done}/{len(hits)} errors={len(errors)}',flush=True)
            _json(root/'download_progress.json',{'provider':'EDGAR','matching_documents':len(hits),
                  'downloaded_or_cached':done,'outside_known_action_window':outside_action_window,'errors':errors})
    summary={'provider':'EDGAR','start_date':str(start.date()),'end_date':str(end.date()),'searches':searches,
             'query':EDGAR_QUERY,'forms':['8-K','6-K'],'matching_documents':len(hits),'downloaded_or_cached':done,'errors':errors,
             'known_action_window_days':120 if only_known_actions else None,'outside_known_action_window':outside_action_window,
             'coverage_note':'Full-text event-document search; excludes annual-report-only events and unindexed disclosures. Review coverage against independent action evidence.'}
    _json(root/'download_report.json',summary)
    return summary


def download_stock_splits(*,market,**kwargs):
    if market not in {'kr','us'}:raise ValueError('market must be kr or us')
    if market=='kr':
        from engine.core.local_secrets import get_local_secret
        if get_local_secret('DART_API_KEY'):
            from engine.extractors.opendart_stock_splits import download_opendart_splits
            return download_opendart_splits(**kwargs)
    return (download_dart_splits if market=='kr' else download_edgar_splits)(**kwargs)
