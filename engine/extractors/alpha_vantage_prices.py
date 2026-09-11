"""Alpha Vantage full daily histories; raw OHLC and vendor adjustments retained."""
from __future__ import annotations

from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import hashlib
from io import StringIO
import json
import re
from pathlib import Path
import time

import pandas as pd
import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_bytes, write_source_text
from engine.extractors._internal.us_consensus import (
    RollingRateLimiter, AlphaVantageRateLimitError, ProviderAuthenticationError, _is_authentication_failure,
)

FUNCTION='TIME_SERIES_DAILY_ADJUSTED'


def price_root():
    return DATA_LAKE.bronze('alpha-vantage','price')


def download_alpha_vantage_listings(*, dates, states=('active',), force=False, output_dir=None, http_get=None):
    """Historical stock/ETF classification and listing status, with raw provenance."""
    from engine.core.local_secrets import get_local_secret
    root=Path(output_dir or DATA_LAKE.bronze('alpha-vantage','listings'))
    get=http_get or requests.get;limiter=RollingRateLimiter(max_calls_per_minute=75)
    report={'provider':'ALPHA_VANTAGE','function':'LISTING_STATUS','snapshots':[],'errors':[]}
    if not set(states)<={'active','delisted'}:raise ValueError('invalid listing state')
    for day in sorted({str(pd.Timestamp(d).date()) for d in dates}):
        if day<'2010-01-01':raise ValueError('Alpha Vantage listing history starts in 2010')
        for state in states:
            path=root/f'snapshot_date={day}'/f'{state}.csv';meta_path=path.with_suffix('.metadata.json')
            if path.exists() and meta_path.exists() and not force:
                meta=json.loads(meta_path.read_text(encoding='utf-8'))
                if hashlib.sha256(path.read_bytes()).hexdigest()==meta['source_sha256']:
                    report['snapshots'].append(meta);continue
            key=get_local_secret('ALPHA_VANTAGE_API_KEY')
            if not key:raise ValueError('ALPHA_VANTAGE_API_KEY is required for listing history')
            limiter.acquire()
            try:
                response=get('https://www.alphavantage.co/query',params={
                    'function':'LISTING_STATUS','date':day,'state':state,'apikey':key},timeout=(10,60))
                if response.status_code!=200:raise ValueError('provider returned an unsuccessful status')
                raw=response.content
                frame=pd.read_csv(StringIO(raw.decode('utf-8-sig')),dtype=str)
                required={'symbol','name','exchange','assetType','ipoDate','delistingDate','status'}
                if not required.issubset(frame) or frame.empty:
                    raise ValueError('provider returned no valid listing CSV')
                meta={'snapshot_date':day,'state':state,'rows':len(frame),'provider':'ALPHA_VANTAGE',
                      'source_url':f'https://www.alphavantage.co/query?function=LISTING_STATUS&date={day}&state={state}',
                      'source_sha256':hashlib.sha256(raw).hexdigest(),'retrieved_at':datetime.now(timezone.utc).isoformat()}
                write_source_bytes(path,raw,source='alpha-vantage-listing-status')
                write_source_text(meta_path,json.dumps(meta,indent=2),source='alpha-vantage-listing-metadata')
                report['snapshots'].append(meta)
            except Exception:
                # Network exception URLs can contain credentials; never serialize them.
                report['errors'].append({'snapshot_date':day,'state':state,'error':'listing request or CSV validation failed'})
        print(f'[ALPHA LISTINGS] snapshots={len(report["snapshots"])} errors={len(report["errors"])}',flush=True)
    write_source_text(root/'download_report.json',json.dumps(report,indent=2),source='alpha-vantage-listing-report')
    return report


def parse_daily_payload(payload,*,symbol):
    metadata=payload.get('Meta Data',{})
    if str(metadata.get('2. Symbol','')).upper()!=symbol.upper():
        raise ValueError('Alpha Vantage response symbol does not match request')
    series=payload.get('Time Series (Daily)')
    if not isinstance(series,dict) or not series:
        raise ValueError('Alpha Vantage response contains no daily prices')
    mapping={'1. open':'open','2. high':'high','3. low':'low','4. close':'close',
             '5. adjusted close':'vendor_adj_close','6. volume':'volume',
             '7. dividend amount':'dividend_amount','8. split coefficient':'split_coefficient'}
    frame=pd.DataFrame.from_dict(series,orient='index').rename(columns=mapping)
    missing=set(mapping.values())-set(frame)
    if missing:raise ValueError(f'Alpha Vantage daily fields missing: {sorted(missing)}')
    # Retain decimal text in the raw JSON; Float64 is only used for calculation.
    frame['trade_date']=pd.to_datetime(frame.index,errors='raise')
    for column in mapping.values():frame[column]=pd.to_numeric(frame[column],errors='raise')
    if not ((frame.close>0)&(frame.split_coefficient>0)).all():
        raise ValueError('Alpha Vantage contains nonpositive close or split coefficient')
    frame['security_id']=f'SEC_US_{symbol.upper()}'
    frame['currency']='USD'
    return frame.sort_values('trade_date').reset_index(drop=True)


def download_alpha_vantage_prices(*,symbols=None,as_of=None,force=False,max_calls_per_minute=75,
                                  output_dir=None,http_get=None,workers=4):
    root=Path(output_dir or price_root());root.mkdir(parents=True,exist_ok=True)
    snapshot=str(pd.Timestamp(as_of or date.today()).date())
    if symbols is None:
        from engine.transformers.alpha_listing_population import resolve_alpha_listing_symbols
        symbols=resolve_alpha_listing_symbols(as_of=snapshot,
            source_dir=DATA_LAKE.bronze('alpha-vantage','listings'),
            output_dir=DATA_LAKE.silver('survivorship','us','listing_history'))
    symbols=sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if any(not re.fullmatch(r'[A-Z0-9][A-Z0-9.^_-]{0,31}',symbol) for symbol in symbols):
        raise ValueError('invalid Alpha Vantage ticker filename')
    pending=[];cached=[]
    for symbol in symbols:
        # CON/PRN/AUX are valid tickers but reserved Windows device names.
        path=root/f'ticker={symbol}.json';meta=path.with_suffix('.metadata.json')
        if not force and path.exists() and meta.exists():
            prior=json.loads(meta.read_text(encoding='utf-8'))
            if prior.get('snapshot_date')==snapshot:
                cached.append(symbol);continue
        pending.append(symbol)
    # No key is needed to reproduce an already downloaded snapshot.
    from engine.core.local_secrets import get_local_secret
    api_key=get_local_secret('ALPHA_VANTAGE_API_KEY') if pending else ''
    if pending and not api_key:
        raise ValueError('ALPHA_VANTAGE_API_KEY must be configured before downloading prices')
    rolling=RollingRateLimiter(max_calls_per_minute=max_calls_per_minute)
    reservation_lock=threading.Lock()
    class SerializedLimiter:
        def acquire(self):
            with reservation_lock:rolling.acquire()
    limiter=SerializedLimiter()
    get=http_get or requests.get
    def fetch_payload(symbol):
        for attempt in range(3):
            limiter.acquire()
            try:
                response=get('https://www.alphavantage.co/query',
                             params={'function':FUNCTION,'symbol':symbol,'apikey':api_key,'outputsize':'full','datatype':'json'},
                             timeout=(10,60))
                payload=response.json()
            except Exception:
                if attempt==2:raise RuntimeError(f'Alpha Vantage network/JSON failure for {symbol}') from None
                time.sleep(2**attempt);continue
            status=int(getattr(response,'status_code',200))
            if _is_authentication_failure(status,payload):raise ProviderAuthenticationError('Alpha Vantage price authorization failed')
            if isinstance(payload,dict) and 'Error Message' in payload:
                raw=json.dumps(payload,ensure_ascii=False).replace(api_key,'<redacted>').encode()
                write_source_bytes(root/'rejected'/f'ticker={symbol}.json',raw,source='alpha-vantage-rejected-price-response')
                raise ValueError(f'Alpha Vantage rejected the daily-price request for {symbol}; no price history accepted')
            limited=status==429 or isinstance(payload,dict) and any(k in payload for k in ['Information','Note'])
            if status<400 and not limited and isinstance(payload,dict):return payload
            if attempt==2:
                if limited:raise AlphaVantageRateLimitError(f'Alpha Vantage price rate limit for {symbol}')
                raise RuntimeError(f'Alpha Vantage price HTTP {status} for {symbol}')
            time.sleep(60*(2**attempt) if limited else 2**attempt)
        raise AssertionError('unreachable')
    report={'provider':'ALPHA_VANTAGE','function':FUNCTION,'snapshot_date':snapshot,
            'requested':len(symbols),'cached':len(cached),'downloaded':0,'errors':[]}
    authorization_failed=threading.Event()
    def collect(symbol):
        if authorization_failed.is_set():return symbol,'download stopped after provider authorization failure'
        try:
            payload=fetch_payload(symbol)
            try:
                frame=parse_daily_payload(payload,symbol=symbol)
            except ValueError:
                rejected=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace(api_key,'<redacted>').encode()
                write_source_bytes(root/'rejected'/f'ticker={symbol}.json',rejected,source='alpha-vantage-rejected-price-response')
                raise
            path=root/f'ticker={symbol}.json'
            content=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
            write_source_bytes(path,content,source='alpha-vantage-daily-adjusted')
            metadata={'provider':'ALPHA_VANTAGE','function':FUNCTION,'symbol':symbol,'snapshot_date':snapshot,
                      'source_url':f'https://www.alphavantage.co/query?function={FUNCTION}&symbol={symbol}&outputsize=full',
                      'source_sha256':hashlib.sha256(content).hexdigest(),'retrieved_at':datetime.now(timezone.utc).isoformat(),
                      'rows':len(frame),'first_date':str(frame.trade_date.min().date()),'last_date':str(frame.trade_date.max().date()),
                      'price_basis':'raw','vendor_adj_close_basis':'split_and_dividend_adjusted'}
            write_source_text(path.with_suffix('.metadata.json'),json.dumps(metadata,indent=2),source='alpha-vantage-price-metadata')
            return symbol,None
        except Exception as exc:
            # Provider helper sanitizes credential-bearing URLs from exceptions.
            from engine.extractors._internal.us_consensus import ProviderAuthenticationError
            if isinstance(exc,ProviderAuthenticationError):
                authorization_failed.set()
                raise
            return symbol,f'{type(exc).__name__}: {exc}'
    with ThreadPoolExecutor(max_workers=max(1,min(int(workers),8))) as executor:
        futures={executor.submit(collect,symbol):symbol for symbol in pending}
        for future in as_completed(futures):
            symbol,error=future.result()
            if error:report['errors'].append({'symbol':symbol,'error':error})
            else:report['downloaded']+=1
            count=report['downloaded']+len(report['errors'])
            if count%25==0:
                print(f'[ALPHA PRICES] downloaded={report["downloaded"]}/{len(pending)} errors={len(report["errors"])}',flush=True)
                write_source_text(root/'download_report.json',json.dumps(report,indent=2),source='alpha-vantage-price-report')
    write_source_text(root/'download_report.json',json.dumps(report,indent=2),source='alpha-vantage-price-report')
    return report
