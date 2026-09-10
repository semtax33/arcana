"""Assemble corrected quarterly factor caches; retain the full size universe."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import numpy as np
import pandas as pd

from scripts.research_cross_market_corrected_cache import (
    OUT,BASES,digest,load_json,save_json,save_frame,verified_panel,
)
from engine.workflows.stock_splits import ledger_path
from scripts.research_cross_market_signal_quality import price_review_age_rows,beta_price_source_review,economic_validity


def numeric_values(frame):
    """Preserve numeric parquet columns and coerce only other representations."""
    result=frame.copy()
    for column in result.select_dtypes(exclude='number'):
        result[column]=pd.to_numeric(result[column],errors='coerce')
    return result


def stock_coverage(sid, *, market, folder, config, eligible):
    """Verify one stock without mutations or shared intermediate DataFrames."""
    label=market.upper();symbol=sid.removeprefix(f'SEC_{label}_')
    metadata=folder/f'ticker={symbol}.json'
    if not metadata.exists():raise ValueError(f'factor build not finished: {symbol}')
    meta=load_json(metadata)
    if meta.get('status')!='ready':return None,[],[],meta
    sources=meta['sources']
    if sources['config_sha256']!=config['sha256']:raise ValueError(f'stale factor configuration: {symbol}')
    panel,_=verified_panel(market,symbol)
    if digest(panel)!=sources['panel_sha256']:raise ValueError(f'stale price input: {symbol}')
    if digest(panel.with_suffix('.metadata.json'))!=sources['panel_metadata_sha256']:
        raise ValueError(f'stale price provenance: {symbol}')
    for path,expected in sources['financial_files'].items():
        if digest(path)!=expected:raise ValueError(f'stale financial input: {symbol}')
    coverage=[];economic_reviews=[]
    for basis in BASES:
        f=pd.read_parquet(folder/f'ticker={symbol}_{basis}.parquet')
        if 'report_date' in f and (pd.to_datetime(f.report_date)>f.trade_date).any():
            raise ValueError(f'future financial disclosure: {symbol}/{basis}')
        f=f.set_index(['trade_date','security_id'])
        f=f.reindex(eligible.index.intersection(f.index))
        f,rejected=economic_validity(f)
        if any(rejected.values()):economic_reviews.append({'security_id':sid,'financial_basis':basis,'rejected':rejected})
        cols=[c for c in config['factors'] if c in f]
        count=numeric_values(f[cols]).notna().astype(int)
        count=count.groupby(level='trade_date').sum()
        count=count.stack().rename('valid_count').reset_index()
        count.columns=['trade_date','factor_id','valid_count'];count['financial_basis']=basis
        coverage.append(count)
    return (sid,symbol,panel),coverage,economic_reviews,None


def assemble(market,workers=4):
    label=market.upper();folder=OUT/'stocks'/market
    config=load_json(OUT/f'{label}_build_config.json')
    for path,expected in config['code'].items():
        if digest(path)!=expected:raise ValueError(f'factor calculation code changed: {path}')
    uv=pd.read_parquet(OUT/f'{label}_universe.parquet')
    qa_path=OUT/f'{label}_observed_price_qa.json'
    qa=load_json(qa_path)
    if (qa['universe_sha256']!=digest(OUT/f'{label}_universe.parquet') or qa.get('errors')
            or qa['ledger_sha256']!=digest(ledger_path(market))):
        raise ValueError(f'{label}: complete the observed-price audit against the current universe')
    price_flags=defaultdict(set)
    for event in qa['large_observed_moves']:
        # This is a review flag, never a price correction or a universe filter.
        # Confirmed listing boundaries have their own execution diagnostic.
        if not event['listing_episode_crossing']:
            price_flags[event['security_id']].add(pd.Timestamp(event['trade_date']))
    eligible=uv[uv.eligible].set_index(['trade_date','security_id']).sort_index()
    ids=sorted(eligible.index.get_level_values('security_id').unique())
    stocks=[];issues=[];coverage=[];economic_reviews=[]
    verify=partial(stock_coverage,market=market,folder=folder,config=config,eligible=eligible)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Batches bound queued DataFrames; map preserves original stock order.
        for offset in range(0,len(ids),100):
            for stock,counts,rejected,issue in pool.map(verify,ids[offset:offset+100]):
                if issue is not None:issues.append(issue);continue
                stocks.append(stock);coverage.extend(counts);economic_reviews.extend(rejected)
            if coverage:
                coverage=[pd.concat(coverage).groupby(['trade_date','factor_id','financial_basis'],
                    as_index=False).valid_count.sum()]
            print(label,'verified factor coverage',min(offset+100,len(ids)),'/',len(ids),flush=True)
    cov=pd.concat(coverage).groupby(['trade_date','factor_id','financial_basis'],as_index=False).valid_count.sum()
    save_frame(OUT/f'{label}_coverage.parquet',cov)
    economic_qa_path=OUT/f'{label}_economic_validity.json'
    save_json(economic_qa_path,{'rule':'ROE requires positive average parent equity and current parent equity; debt/equity requires positive total equity; same financial basis',
              'raw_caches_preserved':True,'dated_universe_preserved':True,'observations':economic_reviews})
    # Basis selection observes availability in training/validation only.
    pre=cov[cov.trade_date.between('2017-01-01','2023-12-31')&cov.valid_count.gt(0)]
    scores=pre.groupby(['factor_id','financial_basis']).valid_count.agg(['sum','count']).reset_index()
    scores['priority']=scores.financial_basis.map({'annual':3,'ttm':2,'quarterly':1})
    scores=scores.sort_values(['factor_id','count','sum','priority'],ascending=[True,False,False,False]).drop_duplicates('factor_id')
    selected=dict(zip(scores.factor_id,scores.financial_basis))
    save_json(OUT/f'{label}_bases.json',selected)
    pieces=[];prices=[];files={}
    for stock_number,(sid,symbol,panel) in enumerate(stocks,1):
        frames=[]
        for basis in BASES:
            f=pd.read_parquet(folder/f'ticker={symbol}_{basis}.parquet').set_index(['trade_date','security_id'])
            f,_=economic_validity(f)
            cols=[c for c in f if selected.get(c)==basis]
            frames.append(numeric_values(f[cols]))
        pieces.append(pd.concat(frames,axis=1))
        if stock_number%500==0:print(label,'combined factors',stock_number,'/',len(stocks),flush=True)
    # Price availability is independent of whether a financial factor could be built.
    for stock_number,sid in enumerate(ids,1):
        symbol=sid.removeprefix(f'SEC_{label}_')
        try:
            panel,_=verified_panel(market,symbol)
        except (FileNotFoundError,ValueError) as exc:
            issues.append({'security_id':sid,'price_error':str(exc)})
            continue
        p=pd.read_parquet(panel).sort_values('trade_date')
        if 'return_review_event' not in p:p['return_review_event']=False
        provenance={'panel_sha256':digest(panel),'panel_metadata_sha256':digest(panel.with_suffix('.metadata.json'))}
        if qa['source_files'].get(sid)!=provenance:raise ValueError(f'stale observed-price audit: {symbol}')
        p['unresolved_price_event']=p.trade_date.isin(price_flags[sid]) | p.get('price_source_review_event',False)
        p['unresolved_history_age_rows']=price_review_age_rows(p)
        p['beta_source_review']=beta_price_source_review(p)
        p=p[['trade_date','security_id','close','split_adj_close','volume','listing_episode','return_review_event','unresolved_price_event','unresolved_history_age_rows','beta_source_review']]
        p=p[p.trade_date.between('2016-12-01','2026-09-04')].rename(columns={'split_adj_close':'adj_close'})
        prices.append(p)
        if stock_number%500==0:print(label,'combined prices',stock_number,'/',len(ids),flush=True)
    # Missing observations keep their original eligible row, preserving the
    # cap-ranked population independently of source/factor/quote coverage.
    result=pd.concat(pieces).reindex(eligible.index).astype('float64')
    for year,frame in result.groupby(result.index.get_level_values('trade_date').year):
        path=OUT/f'{label}_factors_{year}.parquet'
        save_frame(path,frame);files[path.name]=digest(path)
    price=pd.concat(prices,ignore_index=True)
    save_frame(OUT/f'{label}_prices.parquet',price)
    manifest={'status':'ready','price_basis':'split_adjusted','us_price_provider':'ALPHA_VANTAGE',
              'universe':'top 70 percent by count before factor availability',
              'factor_source':'Arcana create_stock_factor_dataframe; strict report metadata',
              'factor_config_sha256':config['sha256'],'universe_sha256':digest(OUT/f'{label}_universe.parquet'),
              'prices_sha256':digest(OUT/f'{label}_prices.parquet'),'securities':len(ids),'ready_securities':len(stocks),
              'factor_files':files,'schedule_sha256':digest(OUT/f'{label}_schedule.json'),
              'bases_sha256':digest(OUT/f'{label}_bases.json'),
              'observed_price_qa_sha256':digest(qa_path),
              'signal_quality_code_sha256':digest('scripts/research_cross_market_signal_quality.py'),
              'economic_validity_sha256':digest(economic_qa_path),
              'issues':issues,'last_price_date':str(price.trade_date.max().date()),
              'basis_selection_end':'2023-12-31','factor_count_with_preholdout_observations':len(selected)}
    save_json(OUT/f'{label}_corrected_manifest.json',manifest)
    print(label,'assembled',result.shape,'prices',price.shape,'issues',len(issues),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market',required=True,choices=['kr','us'])
    parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args();assemble(args.market,args.workers)
