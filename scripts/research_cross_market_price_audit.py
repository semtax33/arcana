"""Archived diagnostic experiment; superseded by official corporate-action panels.

The old exchange-reference/Yahoo reconstruction is not a portfolio wealth series.
Its CLI is disabled to prevent using it for the current FactorLab research.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time
import warnings

import numpy as np
import pandas as pd

from scripts.research_cross_market_top70 import OUT, dump, log


def kr_prices():
    base=pd.read_parquet(OUT/'KR_prices.parquet')
    ids=set(base.security_id)
    frames=[]
    for year in range(2010,2027):
        path=Path(f'data-lake/bronze/marcap/data/marcap-{year}.parquet')
        data=pd.read_parquet(path)
        change=next(c for c in ['ChangesRatio','ChagesRatio'] if c in data)
        data['security_id']='SEC_KR_'+data.Code.astype(str).str.zfill(6)
        data=data[data.security_id.isin(ids)].copy()
        data['trade_date']=pd.to_datetime(data.Date)
        data=data[(data.trade_date>='2010-01-01')&(data.trade_date<='2026-09-04')]
        data['reported_return']=pd.to_numeric(data[change],errors='coerce')/100
        frames.append(data[['trade_date','security_id','reported_return']])
    daily=pd.concat(frames).drop_duplicates(['trade_date','security_id'],keep='last')
    result=base.merge(daily,on=['trade_date','security_id'],how='left').sort_values(['security_id','trade_date'])
    raw=result.groupby('security_id').close.pct_change(fill_method=None)
    result['source_return']=result.reported_return
    # Missing reported returns cannot be certified; recorded explicitly.
    result['research_return']=result.reported_return.where(result.reported_return.notna(),raw)
    invalid=(result.research_return <= -1)|(~np.isfinite(result.research_return))
    result.loc[invalid,'research_return']=np.nan
    result['research_price']=(1+result.research_return.fillna(0)).groupby(result.security_id).cumprod()*100
    result['price_source']='marcap_exchange_reported_change_price_index'
    result.to_parquet(OUT/'KR_research_prices.parquet',index=False)
    differences=(raw-result.reported_return).abs()
    audit={'source':'local marcap exchange-reported daily percentage changes, price return only',
           'reported_rows':int(result.reported_return.notna().sum()),'fallback_rows':int(result.reported_return.isna().sum()),
           'return_discrepancies_over_10pct':int(differences.gt(.10).sum()),
           'invalid_returns':int(invalid.sum()),'note':'Chained daily exchange reference-price changes; cash dividends omitted; rights/spinoffs need separate accounting.'}
    dump('KR_price_reconstruction.json',audit)
    sample=result[(result.security_id=='SEC_KR_005930')&(result.trade_date=='2018-05-04')]
    assert abs(float(sample.research_return.iloc[0])+.0208)<1e-8
    log('KR research price',audit,'Samsung split check PASS')


def us_prices():
    import yfinance as yf
    source_dir=OUT/'price_refresh'
    source_dir.mkdir(exist_ok=True)
    base=pd.read_parquet(OUT/'US_prices.parquet').sort_values(['security_id','trade_date'])
    base=base[base.trade_date>='2010-01-01'].copy()
    changes=base.groupby('security_id').adj_close.pct_change(fill_method=None)
    flagged=sorted(base.loc[(changes>.75)|(changes<-.50),'security_id'].unique())
    dump('US_price_refresh_targets.json',{'rule':'refresh entire vendor history for every security with a >75% or <-50% stored adjusted daily return; no clipping/exclusion based on returns',
                                         'ids':flagged,'count':len(flagged)})
    log('US refresh targets',len(flagged))
    def fetch(security):
        symbol=security.removeprefix('SEC_US_')
        path=source_dir/f'{symbol}.parquet'
        if path.exists():
            return security,'cached',len(pd.read_parquet(path))
        for attempt in range(2):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    frame=yf.Ticker(symbol).history(start='2010-01-01',end='2026-09-05',auto_adjust=False,repair=True,timeout=25,raise_errors=True)
                if frame.empty:
                    raise ValueError('empty response')
                frame.index=frame.index.tz_localize(None)
                frame.index.name='trade_date'
                frame.to_parquet(path)
                return security,'ok',len(frame)
            except Exception as exc:
                error=f'{type(exc).__name__}: {str(exc)[:180]}'
                time.sleep(1)
        return security,error,0
    status=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks={pool.submit(fetch,s):s for s in flagged}
        for future in as_completed(tasks):
            status.append(future.result())
            if len(status)%20==0 or len(status)==len(flagged):
                log('US price refresh',len(status),'/',len(flagged),'success',sum(r[2]>0 for r in status))
                dump('US_price_refresh_status.json',status)
    base['research_price']=base.adj_close.where(base.adj_close.gt(0),base.close)
    base['price_source']='arcana_stored_adjusted'
    missing_dates=[]
    hashes={}
    for security,state,count in status:
        if not count:
            continue
        symbol=security.removeprefix('SEC_US_')
        path=source_dir/f'{symbol}.parquet'
        fresh=pd.read_parquet(path)
        hashes[security]=hashlib.sha256(path.read_bytes()).hexdigest()
        selected=base.security_id.eq(security)
        values=base.loc[selected,'trade_date'].map(fresh['Adj Close'])
        missing_dates.append({'security_id':security,'missing_dates':int(values.isna().sum())})
        # Do not splice incompatible adjusted vintages on missing dates.
        base.loc[selected,'research_price']=values.to_numpy()
        base.loc[selected,'price_source']='yahoo_fresh_full_history_repair'
    base.to_parquet(OUT/'US_research_prices.parquet',index=False)
    dump('US_price_reconstruction.json',{'target_count':len(flagged),'success':sum(r[2]>0 for r in status),
                                        'failures':[r for r in status if r[2]==0], 'fresh_missing':missing_dates,'sha256':hashes})
    rcon=base[base.security_id=='SEC_US_RCON'].set_index('trade_date')
    if pd.Timestamp('2026-08-06') in rcon.index:
        ret=rcon.research_price.pct_change(fill_method=None).loc['2026-08-06']
        assert ret<1, f'RCON scaling error persists: {ret}'
        log('RCON check PASS',ret)


if __name__=='__main__':
    raise SystemExit('Superseded: use engine.workflows.stock_splits and scripts.research_cross_market_corrected_cache; this experimental reconstruction must not feed a backtest.')
    p=argparse.ArgumentParser();p.add_argument('market',choices=['KR','US']);args=p.parse_args()
    {'KR':kr_prices,'US':us_prices}[args.market]()
