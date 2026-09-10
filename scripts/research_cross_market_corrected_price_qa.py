"""Report large observed-price moves; never infer actions or modify returns."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json

import numpy as np
import pandas as pd

from engine.workflows.stock_splits import ledger_path
from scripts.research_cross_market_corrected_cache import OUT,verified_panel,digest,save_json,save_frame


def audit(market):
    label=market.upper();universe=OUT/f'{label}_universe.parquet'
    uv=pd.read_parquet(universe);ids=sorted(uv.loc[uv.eligible,'security_id'].unique())
    ledger=json.loads(ledger_path(market).read_text(encoding='utf-8'))
    actions={(e['security_id'],e['effective_date']):e for e in ledger['events']}
    rows=[];errors=[];source_files={};counts={'securities':0,'observed_price_rows':0,'nontrading_rows':0}
    def one(sid):
        symbol=sid.removeprefix(f'SEC_{label}_')
        try:
            path,meta=verified_panel(market,symbol)
            provenance={'panel_sha256':digest(path),'panel_metadata_sha256':digest(path.with_suffix('.metadata.json'))}
            f=pd.read_parquet(path).sort_values('trade_date')
            missing=int((~f.volume.gt(0)).sum())
            p=f.loc[f.volume.gt(0)&f.split_adj_close.gt(0)].copy()
            p['previous_trade_date']=p.trade_date.shift()
            p['observed_return']=p.split_adj_close.pct_change(fill_method=None)
            p['previous_episode']=p.listing_episode.shift()
            p['previous_raw_close']=p.close.shift()
            p['previous_adjusted_close']=p.split_adj_close.shift()
            suspects=p.loc[p.trade_date.between('2017-01-01','2026-09-04') &
                           ((p.observed_return>1)|(p.observed_return<-.7))].copy()
            result=[]
            for r in suspects.itertuples():
                day=str(r.trade_date.date());event=actions.get((sid,day))
                result.append({'security_id':sid,'trade_date':day,
                    'previous_trade_date':str(r.previous_trade_date.date()),
                    'observed_return':float(r.observed_return),'raw_close':float(r.close),
                    'previous_raw_close':float(r.previous_raw_close),
                    'adjusted_close':float(r.split_adj_close),'previous_adjusted_close':float(r.previous_adjusted_close),
                    'listing_episode_crossing':bool(r.listing_episode!=r.previous_episode),
                    'official_action':event,**provenance,
                    'interpretation':'Investigation flag only; no inferred split, clipping, or price alteration'})
            if provenance!={'panel_sha256':digest(path),'panel_metadata_sha256':digest(path.with_suffix('.metadata.json'))}:
                raise ValueError('Price inputs changed during observed-price audit')
            return result,{'securities':1,'observed_price_rows':len(p),'nontrading_rows':missing},None,(sid,provenance)
        except (OSError,ValueError,KeyError) as exc:
            return [],{}, {'security_id':sid,'error':str(exc)},None
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(one,sid) for sid in ids]
        for i,future in enumerate(as_completed(futures),1):
            found,n,error,provenance=future.result();rows.extend(found)
            if provenance:source_files[provenance[0]]=provenance[1]
            for k,v in n.items():counts[k]+=v
            if error:errors.append(error)
            if i%500==0:print(label,'price QA',i,'/',len(ids),'large moves',len(rows),flush=True)
    rows.sort(key=lambda r:abs(r['observed_return']),reverse=True)
    save_json(OUT/f'{label}_observed_price_qa.json',{'market':label,'scope':'Ever top70 in frozen dated universe',
        'universe_sha256':digest(universe),'ledger_sha256':digest(ledger_path(market)),
        'counts':counts,'errors':errors,'large_observed_moves':rows,'source_files':source_files,
        'thresholds':{'greater_than':1,'less_than':-.7},
        'limitation':'This threshold is a triage screen, not proof of complete corporate-action coverage'})
    print(label,'price QA done',counts,'large moves',len(rows),'errors',len(errors),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--market',choices=['kr','us'],required=True)
    audit(parser.parse_args().market)
