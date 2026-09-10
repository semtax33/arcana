"""Authenticated OpenDART document collection, with cached public search indexes."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import io
import json
import re
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pandas as pd

from engine.core.local_secrets import get_local_secret
from engine.core.source_storage import write_source_bytes
from engine.extractors.stock_splits import OfficialSession, root_for, parse_dart_search, _json


class OpenDartClient:
    def __init__(self,key=None):
        self.key=key or get_local_secret('DART_API_KEY')
        if not self.key:raise ValueError('DART_API_KEY is required')
        self.session=OfficialSession(.5)

    def get(self,endpoint,**params):
        try:
            return self.session.request('GET',f'https://opendart.fss.or.kr/api/{endpoint}',params={'crtfc_key':self.key,**params})
        except Exception as exc:
            # requests error URLs contain crtfc_key. Never propagate them.
            raise RuntimeError(f'OpenDART {endpoint} request failed ({type(exc).__name__})') from None


def _status(content):
    root=ET.fromstring(content)
    return root.findtext('status','unknown')


def _corporations(client,root,force=False):
    path=root/'corpCode.zip'
    if not path.exists() or force:
        response=client.get('corpCode.xml')
        if not response.content.startswith(b'PK'):raise ValueError(f'OpenDART corporate mapping status={_status(response.content)}')
        write_source_bytes(path,response.content,source='OpenDART-corporation-mapping')
    with ZipFile(path) as z:
        member=next(n for n in z.namelist() if n.upper()=='CORPCODE.XML')
        tree=ET.fromstring(z.read(member))
    return {r.findtext('corp_code'):r.findtext('stock_code','').strip() for r in tree.findall('list')}


def _list_period(client,root,start,end):
    page=1;records=[]
    while True:
        params={'bgn_de':start,'end_de':end,'pblntf_detail_ty':'I001','last_reprt_at':'N','page_count':100,'page_no':page,'sort':'date','sort_mth':'asc'}
        digest=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:20]
        path=root/'api_search'/f'{digest}.json'
        if path.exists() and pd.Timestamp(end).date()<date.today()-timedelta(days=7):
            payload=json.loads(path.read_text(encoding='utf-8'))
        else:
            payload=client.get('list.json',**params).json()
            if payload.get('status') not in {'000','013'}:raise ValueError(f'OpenDART list status={payload.get("status")}')
            _json(path,{'request':params,**payload})
        if payload['status']=='013':return records
        for row in payload.get('list',[]):
            title=row['report_nm'].replace(' ','')
            if any(t in title for t in ['주식분할결정','주식병합결정']):
                records.append({'source_id':row['rcept_no'],'corp_code':row['corp_code'],'title':row['report_nm'],
                                'published_date':str(pd.Timestamp(row['rcept_dt']).date()),'rm':row.get('rm','')})
        if page>=int(payload['total_page']):return records
        page+=1


def download_document(client,root,record,code,force=False):
    if not re.fullmatch(r'[0-9]{14}',record['source_id']) or not re.fullmatch(r'[A-Z0-9]{6}',code):
        raise ValueError('invalid OpenDART receipt or stock code')
    receipt=record['source_id'];folder=root/'disclosures'/code
    path=folder/f'{receipt}.html';meta=path.with_suffix('.html.metadata.json')
    if path.exists() and meta.exists() and not force:return 'cached'
    archive=root/'document_archives'/f'{receipt}.zip'
    unavailable=archive.with_suffix('.unavailable.json')
    if unavailable.exists() and not force:return 'unavailable'
    if not archive.exists() or force:
        response=client.get('document.xml',rcept_no=receipt)
        if not response.content.startswith(b'PK'):
            status=_status(response.content)
            if status=='014':
                _json(unavailable,{'source_id':receipt,'status':status,'source_url':f'https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}'})
                return 'unavailable'
            raise ValueError(f'OpenDART document status={status}')
        write_source_bytes(archive,response.content,source='OpenDART-disclosure-archive')
    with ZipFile(archive) as z:
        names=[n for n in z.namelist() if Path(n).name==f'{receipt}.xml']
        if len(names)!=1:raise ValueError('missing or ambiguous main XML in OpenDART ZIP')
        if z.getinfo(names[0]).file_size>100_000_000:raise ValueError('oversized OpenDART document')
        content=z.read(names[0])
    write_source_bytes(path,content,source='OpenDART-disclosure')
    published=record.get('published_date') or f'{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}'
    _json(meta,{**record,'provider':'DART','security_id':f'SEC_KR_{code}','stock_code':code,
                'published_date':published,'family_id':f'api:{receipt}',
                'source_url':f'https://opendart.fss.or.kr/api/document.xml?rcept_no={receipt}',
                'source_sha256':hashlib.sha256(content).hexdigest(),'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                'path':str(path.resolve())})
    return 'downloaded'


def download_opendart_splits(*,symbols=None,start_date='20100101',end_date=None,force=False,output_dir=None,**unused):
    root=Path(output_dir or root_for('kr'));root.mkdir(parents=True,exist_ok=True)
    client=OpenDartClient();mapping=_corporations(client,root,force=force)
    start=pd.Timestamp(str(start_date));end=pd.Timestamp(str(end_date or date.today()))
    wanted={str(s).zfill(6) for s in symbols} if symbols else None
    records={};coverage=[]
    # Reuse the downloaded, counted marketwide indexes. The API's document
    # endpoint is independent of the public viewer that may be unavailable.
    for path in root.glob('search/*.html'):
        sidecar=path.with_suffix('.html.metadata.json')
        if not sidecar.exists():continue
        metadata=json.loads(sidecar.read_text(encoding='utf-8'))
        request=metadata.get('request',{})
        if request.get('reportName') not in ['주식분할결정','주식병합결정']:continue
        rows,total=parse_dart_search(path.read_bytes())
        records.update({r['source_id']:r for r in rows if start.strftime('%Y%m%d')<=r['source_id'][:8]<=end.strftime('%Y%m%d')})
        coverage.append((request['startDate'],request['endDate'],request['reportName'],int(request['currentPage']),total))
    # Verify all pages of both report types before treating a cached day as searched.
    complete=[]
    for a,b in sorted({(a,b) for a,b,_,_,_ in coverage}):
        valid=True
        for title in ['주식분할결정','주식병합결정']:
            pages={page:total for aa,bb,t,page,total in coverage if (aa,bb,t)==(a,b,title)}
            expected=max(1,(max(pages.values(),default=0)+99)//100)
            valid &= bool(pages) and set(range(1,expected+1))<=set(pages)
        if valid:complete.append((pd.Timestamp(a),pd.Timestamp(b)))
    missing=[];cursor=start
    while cursor<=end:
        covered=next((b for a,b in complete if a<=cursor<=b),None)
        if covered is not None:cursor=covered+pd.Timedelta(days=1);continue
        stop=min(end,cursor+pd.DateOffset(months=3)-pd.Timedelta(days=1))
        next_cached=min((a for a,b in complete if a>cursor),default=stop+pd.Timedelta(days=1))
        stop=min(stop,next_cached-pd.Timedelta(days=1))
        print(f'[SPLITS] OpenDART index {cursor.date()}..{stop.date()}',flush=True)
        found=_list_period(client,root,cursor.strftime('%Y%m%d'),stop.strftime('%Y%m%d'))
        records.update({r['source_id']:r for r in found});missing.append([str(cursor.date()),str(stop.date())])
        cursor=stop+pd.Timedelta(days=1)
    report={'provider':'OpenDART','start_date':str(start.date()),'end_date':str(end.date()),'indexed_disclosures':len(records),
            'cached_search_windows':len(complete),'api_search_windows':missing,'downloaded_or_cached':0,'unavailable_documents':[],
            'outside_symbols':0,'unmapped_corporations':[],'errors':[]}
    for receipt,record in sorted(records.items()):
        code=mapping.get(record['corp_code'],'')
        if not code:
            report['unmapped_corporations'].append(record);continue
        if wanted is not None and code not in wanted:report['outside_symbols']+=1;continue
        try:
            status=download_document(client,root,record,code,force=force)
            if status=='unavailable':report['unavailable_documents'].append(record)
            else:report['downloaded_or_cached']+=1
        except Exception as exc:
            report['errors'].append({'source_id':receipt,'error':str(exc)})
            if len(report['errors'])>=5:break
        count=report['downloaded_or_cached']+len(report['unavailable_documents'])+len(report['errors'])
        if count%25==0:
            print(f'[SPLITS] OpenDART downloaded={report["downloaded_or_cached"]} unavailable={len(report["unavailable_documents"])} errors={len(report["errors"])}',flush=True)
            _json(root/'download_report.json',report)
    _json(root/'download_report.json',report)
    from engine.extractors.kind_stock_splits import download_kind_splits
    report['kind']=download_kind_splits(unavailable=report['unavailable_documents'],corporation_mapping=mapping,
                                       symbols=symbols,start_date=start_date,end_date=end_date,output_dir=root,force=force)
    _json(root/'download_report.json',report)
    return report
