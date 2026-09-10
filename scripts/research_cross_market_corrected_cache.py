"""Stage source-verified FactorLab inputs without altering production tables.

The universe is ranked before factor availability is inspected. Per-security
factor files are resumable and retain the input hashes used to calculate them.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4
import warnings

import numpy as np
import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.transformers._internal import factor_metrics as fm
from engine.transformers._internal.statement_files import consolidated_statement_path
from engine.transformers.sec_shares import disclosed_shares_path, align_disclosed_shares
from engine.workflows.stock_splits import price_panel_dir

BASE=Path('deliverables/cross_market_top70_20260909')
OUT=BASE/'corrected'
START='2016-12-01'
END='2026-09-04'
BASES=('annual','quarterly','ttm')
CACHE=None
CONFIG=None


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def save_json(path,payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    temporary.replace(path)


def save_frame(path,frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    frame.to_parquet(temporary);temporary.replace(path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def pin_inputs(market):
    folder=OUT/'inputs'/market;folder.mkdir(parents=True,exist_ok=True)
    paths={'report_metadata_path':DATA_LAKE.silver('sec' if market=='us' else 'dart',f'{market}_report_metadata.csv'),
           'dividend_path':fm.dividend_path_for_market(market),
           'wacc_risk_free_path':fm.SILVER_RISK_FREE_RATE_PATH,
           'wacc_erp_path':fm.SILVER_COUNTRY_ERP_PATH,
           'wacc_assumptions_path':fm.SILVER_WACC_ASSUMPTIONS_PATH,
           'wacc_benchmark_path':fm.SILVER_WACC_BENCHMARK_WEEKLY_RETURNS_PATH,
           'real_consensus_daily_path':fm.HANKYUNG_CONSENSUS_DAILY_PATH,
           'target_price_consensus_path':fm.HANKYUNG_TARGET_PRICE_CONSENSUS_PATH,
           'us_consensus_factors_path':fm.US_CONSENSUS_FACTORS_PATH}
    paths['disclosed_shares' if market=='us' else 'shares_path']=disclosed_shares_path() if market=='us' else fm.shares_path_for_market(market)
    manifest={}
    for key,source in paths.items():
        source=Path(source);target=folder/(key+source.suffix)
        if source.exists() and not target.exists():shutil.copyfile(source,target)
        manifest[key]={'path':str(target.resolve()),'source_path':str(source.resolve()),
                       'sha256':digest(target) if target.exists() else None}
    for name in ['catalog.csv','factor_policy.json','security_classification.parquet',f'{market.upper()}_schedule.json']:
        target=OUT/name
        if not target.exists():shutil.copyfile(BASE/name,target)
    save_json(folder/'manifest.json',manifest)
    return manifest


def verified_panel(market,symbol):
    path=price_panel_dir(market)/f'{market}_{symbol}.parquet'
    meta=load_json(path.with_suffix('.metadata.json'))
    if meta.get('status')!='ready' or not path.exists():raise ValueError('price panel is not ready')
    if market=='us' and meta.get('price_provider')!='ALPHA_VANTAGE':raise ValueError('US prices must be Alpha Vantage')
    return path,meta


def apply_us_listing_universe():
    """Filter actual signal-day listed stocks before the size percentile rank."""
    caps_path=OUT/'US_cap_observations.parquet'
    if not caps_path.exists():
        save_frame(caps_path,pd.read_parquet(OUT/'US_universe.parquet'))
    caps=pd.read_parquet(caps_path);frames=[];audit=[]
    folder=OUT/'inputs'/'us'/'listings';folder.mkdir(parents=True,exist_ok=True)
    for day,group in caps.groupby('trade_date',sort=True):
        day_text=str(day.date());source=DATA_LAKE.bronze('alpha-vantage','listings',f'snapshot_date={day_text}','active.csv')
        target=folder/f'{day_text}.csv'
        if not target.exists():
            metadata=load_json(source.with_suffix('.metadata.json'))
            if metadata['source_sha256']!=digest(source):raise ValueError('listing source hash changed')
            shutil.copyfile(source,target)
        listing=pd.read_csv(target,dtype=str).fillna('')
        stocks=listing[listing.assetType.eq('Stock')&listing.status.eq('Active')].copy()
        # An issuer's common-share count cannot be assigned to its preferred
        # shares, warrants or subscription rights merely because a ticker maps.
        excluded_class=stocks.name.str.contains(r'preferred|preference|warrants?|subscription rights',case=False,regex=True)
        stocks=stocks[~excluded_class]
        ids={'SEC_US_'+s for s in stocks.symbol}
        valid=group[group.security_id.isin(ids)].copy()
        frames.append(valid)
        audit.append({'trade_date':day_text,'listing_sha256':digest(target),'provider_stock_count':len(stocks),
                      'arcana_valid_cap_before_listing_filter':len(group),'eligible_cap_count':len(valid),
                      'omitted_security_ids':sorted(set(group.security_id)-ids)})
    frame=pd.concat(frames,ignore_index=True).sort_values(['trade_date','market_cap','security_id'],ascending=[True,False,True])
    frame['size_rank_high']=frame.groupby('trade_date').cumcount()+1
    frame['size_count']=frame.groupby('trade_date').security_id.transform('size')
    frame['eligible']=frame.size_rank_high.le(np.ceil(frame.size_count*.7))
    save_frame(OUT/'US_universe.parquet',frame)
    save_json(OUT/'US_listing_universe_audit.json',{'source':'Alpha Vantage historical LISTING_STATUS',
              'rule':'Active Stock on each signal date; preferred shares and warrants excluded before market-cap ranking',
              'master_scope':'Arcana securities with unambiguous published common shares and Alpha Vantage prices',
              'dates':audit})
    print('US listed stock universe',frame.security_id.nunique(),'ever top70',frame.loc[frame.eligible,'security_id'].nunique(),flush=True)


def universe(market):
    inputs=pin_inputs(market)
    master=pd.read_parquet(OUT/'security_classification.parquet')
    master=master[master.market.eq(market.upper())]
    schedule=load_json(OUT/f'{market.upper()}_schedule.json')
    signals=pd.DatetimeIndex([s for r,s in schedule['pairs'] if START<=s<=END])
    if market=='us':
        obs=pd.read_parquet(inputs['disclosed_shares']['path'])
        groups={sid:g for sid,g in obs.groupby('security_id',sort=False)}
    else:
        shares=pd.read_csv(inputs['shares_path']['path'],parse_dates=['trade_date'])
        groups={sid:g for sid,g in shares.groupby('security_id',sort=False)}
    frames=[];reviews=[]
    def read_market_cap(row):
        observations=groups.get(row.security_id,pd.DataFrame())
        if observations.empty:
            return None,{'security_id':row.security_id,'reason':'missing_unambiguous_published_shares'}
        try:
            if market=='us':
                path,meta=verified_panel(market,row.symbol)
                panel=pd.read_parquet(path,columns=['security_id','trade_date','close','split_adjustment_factor'])
                stock=align_disclosed_shares(panel,observations)
            else:stock=observations
            stock=stock[stock.trade_date.isin(signals)].copy()
            stock['market_cap']=pd.to_numeric(stock.market_cap,errors='coerce')/1e6
            stock=stock[np.isfinite(stock.market_cap)&stock.market_cap.gt(0)]
            return stock[['trade_date','security_id','market_cap']] if not stock.empty else None,None
        except (FileNotFoundError,ValueError) as exc:
            return None,{'security_id':row.security_id,'reason':str(exc)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for count,(stock,review) in enumerate(pool.map(read_market_cap,master.itertuples()),1):
            if stock is not None:frames.append(stock)
            if review is not None:reviews.append(review)
            if count%500==0:print(market,'market caps',count,'/',len(master),'valid',len(frames),flush=True)
    frame=pd.concat(frames,ignore_index=True).drop_duplicates(['trade_date','security_id'],keep='last')
    frame=frame.sort_values(['trade_date','market_cap','security_id'],ascending=[True,False,True])
    frame['size_rank_high']=frame.groupby('trade_date').cumcount()+1
    frame['size_count']=frame.groupby('trade_date').security_id.transform('size')
    frame['eligible']=frame.size_rank_high.le(np.ceil(frame.size_count*.7))
    save_frame(OUT/f'{market.upper()}_universe.parquet',frame)
    if market=='us':save_frame(OUT/'US_cap_observations.parquet',frame)
    save_json(OUT/f'{market.upper()}_universe_audit.json',{'source':'published shares and raw daily prices',
              'classification_basis':'current','master_count':len(master),'reviews':reviews,
              'dates':frame.groupby('trade_date').agg(valid_cap_count=('security_id','size'),eligible_count=('eligible','sum')).reset_index().to_dict('records'),
              'shares_input_sha256':inputs['disclosed_shares' if market=='us' else 'shares_path']['sha256']})
    print(market,'valid securities',frame.security_id.nunique(),'ever eligible',frame[frame.eligible].security_id.nunique(),'review',len(reviews),flush=True)
    if market=='us':apply_us_listing_universe()


class ResearchCache(fm.FactorMarketDataCache):
    def __init__(self,market,inputs):
        kwargs={k:v['path'] for k,v in inputs.items() if k in {
            'shares_path','dividend_path','wacc_risk_free_path','wacc_erp_path','wacc_assumptions_path','wacc_benchmark_path'}}
        super().__init__(market=market,start_date=START,end_date=END,**kwargs)
        if market=='us':
            self.uses_disclosed_shares=True
            self._disclosed_share_groups={sid:g for sid,g in pd.read_parquet(inputs['disclosed_shares']['path']).groupby('security_id',sort=False)}


def init_worker(config):
    global CACHE,CONFIG
    warnings.filterwarnings('ignore',category=pd.errors.PerformanceWarning)
    warnings.filterwarnings('ignore',category=FutureWarning)
    CONFIG=config;CACHE=ResearchCache(config['market'],config['inputs'])


def stock_sources(symbol):
    market=CONFIG['market'];panel,meta=verified_panel(market,symbol)
    financial=fm.financial_dir_for_market(market)
    consolidated=consolidated_statement_path(financial,symbol,market=market)
    files=[consolidated] if consolidated.exists() else list(financial.glob(f'{market}_{symbol}_*.csv'))
    return {'panel_sha256':digest(panel),'panel_metadata_sha256':digest(panel.with_suffix('.metadata.json')),
            'financial_files':{str(p.resolve()):digest(p) for p in files},'config_sha256':CONFIG['sha256']}


def build_stock(symbol):
    market=CONFIG['market'];folder=OUT/'stocks'/market
    metadata=folder/f'ticker={symbol}.json';started=time.monotonic()
    try:
        sources=stock_sources(symbol)
        paths={basis:folder/f'ticker={symbol}_{basis}.parquet' for basis in BASES}
        if metadata.exists() and load_json(metadata).get('sources')==sources and all(p.exists() for p in paths.values()):
            return {'symbol':symbol,'status':'cached'}
        days=pd.DatetimeIndex(CONFIG['signals']);counts={}
        kwargs={k:v['path'] for k,v in CONFIG['inputs'].items() if k in {
            'report_metadata_path','real_consensus_daily_path','target_price_consensus_path','us_consensus_factors_path'}}
        for basis in BASES:
            frame=fm.create_stock_factor_dataframe(symbol,market=market,financial_basis=basis,
                    market_data_cache=CACHE,start_date=START,end_date=END,require_report_metadata=True,
                    use_edgartools=False,wacc_online_backfill=False,**kwargs)
            if frame.empty:raise ValueError('empty factor source frame')
            # Exact signal-day data only; no future or synthetic period-end fill.
            frame=frame[frame.trade_date.isin(days)]
            columns=['trade_date','security_id','report_date','financial_period','currency']
            columns += [f for f in CONFIG['factors'] if f in frame]
            frame=frame.reindex(columns=columns).replace([np.inf,-np.inf],np.nan)
            save_frame(paths[basis],frame);counts[basis]=len(frame)
        if stock_sources(symbol)!=sources:raise ValueError('source changed during stock calculation; retry required')
        result={'symbol':symbol,'status':'ready','sources':sources,'rows':counts,'seconds':round(time.monotonic()-started,2)}
        save_json(metadata,result);return {k:v for k,v in result.items() if k!='sources'}
    except Exception as exc:
        result={'symbol':symbol,'status':'failed','error':f'{type(exc).__name__}: {exc}'}
        save_json(metadata,result);return result


def build(market,workers,limit=None):
    inputs=pin_inputs(market)
    uv=pd.read_parquet(OUT/f'{market.upper()}_universe.parquet')
    ids=sorted(uv.loc[uv.eligible,'security_id'].str.split('_',n=2).str[-1].unique())
    if limit:ids=ids[:limit]
    # Factor calculations consume verified price panels, not disclosure parsers.
    # Panel + metadata hashes already invalidate every affected security when
    # upstream corporate-action evidence changes. Hash actual calculation code.
    code_files=[Path(__file__),Path(fm.__file__),Path('engine/transformers/_internal/filing_periods.py'),
                Path('engine/transformers/sec_shares.py')]
    config={'market':market,'inputs':inputs,'factors':pd.read_csv(OUT/'catalog.csv').factor_id.tolist(),
            'signals':sorted(uv.trade_date.dt.strftime('%Y-%m-%d').unique()),
            'code':{str(p):digest(p) for p in code_files},'policy_sha256':digest(OUT/'factor_policy.json')}
    config['sha256']=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    save_json(OUT/f'{market.upper()}_build_config.json',config)
    report={'market':market,'requested':len(ids),'completed':[],'config_sha256':config['sha256']}
    with ProcessPoolExecutor(max_workers=workers,initializer=init_worker,initargs=(config,)) as pool:
        futures={pool.submit(build_stock,s):s for s in ids}
        for future in as_completed(futures):
            report['completed'].append(future.result())
            if len(report['completed'])%10==0 or len(report['completed'])==len(ids):
                save_json(OUT/f'{market.upper()}_build_progress.json',report)
                print(market,len(report['completed']),'/',len(ids),'failed',sum(r['status']=='failed' for r in report['completed']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['universe','build','listing-universe'])
    parser.add_argument('--market',required=True,choices=['kr','us'])
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    if args.phase=='universe':universe(args.market)
    elif args.phase=='listing-universe':
        if args.market!='us':raise ValueError('historical listing filter currently applies to US')
        apply_us_listing_universe()
    else:build(args.market,args.workers,args.limit)
