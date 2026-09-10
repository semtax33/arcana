"""Published SEC share counts aligned to the share units traded each day."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_text


def disclosed_shares_path():
    return DATA_LAKE.silver('corporate_actions','us_disclosed_shares.parquet')


def parse_share_observations(payload, *, security_id, source_sha256, as_of):
    rows=[]
    for namespace,concept,priority in [('dei','EntityCommonStockSharesOutstanding',0),
                                        ('us-gaap','CommonStockSharesOutstanding',1)]:
        observations=payload.get('facts',{}).get(namespace,{}).get(concept,{}).get('units',{}).get('shares',[])
        for observation in observations:
            try:
                available=pd.Timestamp(observation['filed'])
                measured=pd.Timestamp(observation['end'])
                value=float(observation['val'])
                if measured>available or available>pd.Timestamp(as_of) or not np.isfinite(value) or value<=0:continue
                if observation.get('form') not in {'10-K','10-K/A','10-Q','10-Q/A','20-F','20-F/A','40-F','40-F/A'}:continue
                accession=observation['accn']
            except (KeyError,TypeError,ValueError):continue
            rows.append({'security_id':security_id,'trade_date':available,'value_date':measured,
                         'shares':value,'concept':namespace+':'+concept,'priority':priority,
                         'accession':accession,'source_sha256':source_sha256,
                         'source_url':f'https://www.sec.gov/Archives/edgar/data/{int(payload["cik"])}/{accession.replace("-","")}/'})
    if not rows:return pd.DataFrame()
    frame=pd.DataFrame(rows)
    # A later filing's comparative prior-period count must not supersede the
    # newest measurement already publicly known. Never use EPS average shares.
    frame=frame.sort_values(['trade_date','value_date','priority','accession'],ascending=[True,False,True,False])
    frame=frame.drop_duplicates('trade_date',keep='first').sort_values('trade_date')
    latest=frame.value_date.cummax()
    return frame[frame.value_date.eq(latest)].drop(columns='priority').reset_index(drop=True)


def align_disclosed_shares(prices, observations):
    columns=['security_id','trade_date','shares','market_cap']
    if prices.empty or observations.empty:return pd.DataFrame(columns=columns)
    prices=prices.sort_values('trade_date').copy()
    observations=observations.sort_values('trade_date').copy()
    # F(day)/F(measurement) contains exactly the executed unit changes between
    # the two dates. Later splits cancel from the numerator and denominator.
    factor_by_date=prices.set_index('trade_date')['split_adjustment_factor'].sort_index()
    value_dates=pd.DatetimeIndex(observations.value_date)
    # Before the first quote the unit basis is unknown; keep that observation
    # missing until a measurement within the documented price history exists.
    basis=factor_by_date.reindex(factor_by_date.index.union(value_dates.unique())).sort_index().ffill().reindex(value_dates).to_numpy()
    observations['share_basis_factor']=basis
    merged=pd.merge_asof(prices,observations.drop(columns='security_id'),on='trade_date',direction='backward')
    merged['shares']=merged['shares']*merged.split_adjustment_factor/merged.share_basis_factor
    merged['market_cap']=merged.close*merged.shares
    return merged[columns]


def build_disclosed_shares(*, symbols=None, as_of='2026-09-09', output_path=None):
    from engine.transformers._internal.factor_metrics import us_filing_share_fallback_is_unambiguous
    mapping=pd.read_csv(DATA_LAKE.meta('sec_company_tickers.csv'),dtype=str)
    if symbols is not None:mapping=mapping[mapping.ticker.isin(symbols)]
    mapping=mapping.drop_duplicates('ticker')
    frames=[];review=[]
    if symbols is not None:
        review.extend({'symbol':symbol,'reason':'missing_official_ticker_cik_mapping'}
                      for symbol in sorted(set(symbols)-set(mapping.ticker)))
    for row in mapping.itertuples():
        symbol=row.ticker
        if not us_filing_share_fallback_is_unambiguous(symbol):
            review.append({'symbol':symbol,'reason':'issuer_has_multiple_or_ambiguous_listed_tickers'});continue
        path=DATA_LAKE.bronze('sec','companyfacts',f'CIK{int(row.cik):010d}.json')
        if not path.exists():
            review.append({'symbol':symbol,'reason':'missing_official_companyfacts'});continue
        raw=path.read_bytes()
        payload=json.loads(raw)
        if int(payload.get('cik',-1))!=int(row.cik):
            review.append({'symbol':symbol,'reason':'companyfacts_cik_mismatch'});continue
        frame=parse_share_observations(payload,security_id=f'SEC_US_{symbol}',
                                       source_sha256=hashlib.sha256(raw).hexdigest(),as_of=as_of)
        if frame.empty:review.append({'symbol':symbol,'reason':'no_disclosed_outstanding_common_share_count'})
        else:frames.append(frame)
        if (len(frames)+len(review))%100==0:
            print(f'[SEC SHARES] securities={len(frames)} review={len(review)}',flush=True)
    result=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    if result.empty:raise ValueError('No valid disclosed SEC share counts')
    output=Path(output_path or disclosed_shares_path());output.parent.mkdir(parents=True,exist_ok=True)
    combined=result
    if symbols is not None and output.exists():
        existing=pd.read_parquet(output)
        existing=existing[~existing.security_id.isin({f'SEC_US_{s}' for s in symbols})]
        combined=pd.concat([existing,result],ignore_index=True)
    temporary=output.with_name(output.name+'.'+uuid4().hex+'.tmp');combined.to_parquet(temporary,index=False);temporary.replace(output)
    report={'as_of':as_of,'securities':result.security_id.nunique(),'rows':len(result),'review':review,
            'semantics':'Outstanding common shares, available on filed date, measured on end date; split units aligned independently. No weighted-average EPS share fallback.'}
    write_source_text(output.with_suffix('.json'),json.dumps(report,ensure_ascii=False,indent=2),source='sec-disclosed-share-counts')
    return report
