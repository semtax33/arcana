"""Direction-constrained discovery, with a locked 2024+ assessment period."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.research_cross_market_top70 import OUT, dump, log
from scripts.research_cross_market_corrected_cache import digest
from scripts.research_cross_market_signal_quality import technical_lookback,uses_beta_history

SEED = 20260909
POSITIONS = (30, 50, 80)
TRAIN_END = pd.Timestamp('2020-12-31')
SELECTION_END = pd.Timestamp('2023-12-31')
START = pd.Timestamp('2017-01-01')
RF = 0.04


def metrics(returns, dates):
    r = np.asarray(returns, dtype=float)
    dates = pd.DatetimeIndex(dates)
    good = np.isfinite(r)
    r, dates = r[good], dates[good]
    if len(r) < 10:
        return {'cagr': None, 'sharpe': None, 'sharpe_rf4': None, 'mdd': None, 'days': len(r)}
    nav = np.cumprod(1+r)
    years = max((dates[-1]-dates[0]).days/365.25, len(r)/252)
    vol = r.std(ddof=1)*np.sqrt(252)
    cash_daily = (1+RF)**(1/252)-1
    return {'cagr': float(nav[-1]**(1/years)-1), 'sharpe': float(r.mean()*252/vol) if vol else None,
            'sharpe_rf4': float((r.mean()-cash_daily)*252/vol) if vol else None,
            'mdd': float(np.min(nav/np.maximum.accumulate(np.r_[1,nav])[1:]-1)),
            'volatility': float(vol), 'cumulative_return': float(nav[-1]-1), 'days': len(r)}


@dataclass
class Segment:
    signal: pd.Timestamp
    execution: pd.Timestamp
    ids: np.ndarray
    scores: pd.DataFrame
    raw: pd.DataFrame
    dates: pd.DatetimeIndex
    relatives: np.ndarray
    missing_entry: np.ndarray
    unpriced_exit: np.ndarray
    episode_crossing: np.ndarray
    unresolved_distribution: np.ndarray
    unresolved_price_event: np.ndarray
    price_review_age: np.ndarray | None = None
    beta_source_review: np.ndarray | None = None


class MarketData:
    def __init__(self, market, policy, include_holdout=False):
        self.market = market
        self.policy = policy
        self.include_holdout = include_holdout
        manifest_path=OUT/f'{market}_corrected_manifest.json'
        if not manifest_path.exists():
            raise ValueError(f'{market}: assemble source-verified corrected inputs before strategy research')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('status')!='ready' or not (manifest.get('style_factors') or manifest.get('style_scores_omitted_by_user')):
            raise ValueError(f'{market}: assemble corrected factors and resolve the requested style scope first')
        if manifest['signal_quality_code_sha256']!=digest('scripts/research_cross_market_signal_quality.py'):
            raise ValueError(f'{market}: signal price-quality rules changed after assembly')
        expected={f'{market}_prices.parquet':manifest['prices_sha256'],
                  f'{market}_universe.parquet':manifest['universe_sha256'],
                  f'{market}_schedule.json':manifest['schedule_sha256'],
                  f'{market}_observed_price_qa.json':manifest['observed_price_qa_sha256'],
                  f'{market}_economic_validity.json':manifest['economic_validity_sha256'],
                  f'{market}_bases.json':manifest['bases_sha256'],**manifest['factor_files']}
        for name,sha in expected.items():
            if digest(OUT/name)!=sha:raise ValueError(f'{market}: assembled input changed: {name}')
        end = pd.Timestamp('2026-09-04') if include_holdout else SELECTION_END
        frames = [pd.read_parquet(p) for p in sorted(OUT.glob(f'{market}_factors_*.parquet'))
                  if 2016 <= int(p.stem[-4:]) <= end.year]
        raw = pd.concat(frames).sort_index()
        raw = raw[~raw.index.duplicated(keep='last')].replace([np.inf,-np.inf],np.nan)
        raw = raw.loc[(raw.index.get_level_values(0) >= '2016-12-01') & (raw.index.get_level_values(0) <= end)]
        uv = pd.read_parquet(OUT/f'{market}_universe.parquet')
        uv = uv[uv.eligible.astype(bool)].set_index(['trade_date','security_id']).sort_index()
        uv=uv.loc[(uv.index.get_level_values(0)>='2016-12-01')&(uv.index.get_level_values(0)<=end)]
        raw = raw.reindex(uv.index)
        p = pd.read_parquet(OUT/f'{market}_prices.parquet')
        p = p[(p.trade_date >= '2016-12-01') & (p.trade_date <= end)].copy()
        p['chosen'] = p.adj_close.where(p.adj_close.gt(0),p.close.where(p.close.gt(0)))
        # Zero-volume suspension rows can contain a carried price in an old
        # share unit. They are not executable quotes; holdings carry their last
        # traded mark and new allocations stay in cash at such an entry date.
        p['chosen']=p.chosen.where(p.volume.gt(0))
        prices = p.pivot(index='trade_date',columns='security_id',values='chosen').sort_index()
        episodes=p.pivot(index='trade_date',columns='security_id',values='listing_episode').sort_index()
        distributions=p.pivot(index='trade_date',columns='security_id',values='return_review_event').sort_index()
        price_events=p.pivot(index='trade_date',columns='security_id',values='unresolved_price_event').sort_index()
        history_age=p.pivot(index='trade_date',columns='security_id',values='unresolved_history_age_rows').sort_index()
        beta_reviews=p.pivot(index='trade_date',columns='security_id',values='beta_source_review').sort_index()
        self.price_audit = {'rows':len(p), 'adjusted_present':int(p.adj_close.gt(0).sum()),
                            'close_fallback':int((~p.adj_close.gt(0)&p.close.gt(0)).sum())}
        schedule = json.loads((OUT/f'{market}_schedule.json').read_text())
        pairs = [(pd.Timestamp(r),pd.Timestamp(s)) for r,s in schedule['pairs'] if START <= pd.Timestamp(r) <= end]
        self.segments = []
        self.coverage = {}
        # Invalid observations remain missing; never renormalize a partial final score.
        for j,(execution,signal) in enumerate(pairs):
            if signal not in raw.index.get_level_values(0):
                raise ValueError(f'{market}: signal {signal} has no cached observations')
            frame = raw.xs(signal).sort_index()
            ids = frame.index.to_numpy()
            frame = frame.reindex(ids)
            scores = pd.DataFrame(index=ids)
            for f,spec in policy.items():
                if spec['role'] not in {'signal','derived_style'} or f not in frame or spec['direction'] not in ('higher','lower'):
                    continue
                if spec.get('source_qa_withheld_reason') or market in spec.get('source_qa_withheld_by_market',{}):
                    continue
                values = frame[f].copy()
                # Negative valuation multiples cannot mean economically cheap.
                if f in {'per','pbr','pcr','psr','peg','ev_to_ebitda','ev_to_nopat'}:
                    values = values.where(values.gt(0))
                if values.notna().sum() < 20 or values.nunique() < 3:
                    continue
                ranks = values.rank(method='average',ascending=spec['direction']=='higher')
                count = values.notna().sum()
                scores[f] = 100*(ranks-1)/(count-1)
            next_execution = pairs[j+1][0] if j+1<len(pairs) else end
            segment_prices = prices.loc[(prices.index>=execution)&(prices.index<=next_execution)].reindex(columns=ids)
            if segment_prices.empty or segment_prices.index[0] != execution:
                raise ValueError(f'{market} missing execution price date {execution}')
            # No entry on a missing execution quote; that allocation stays cash.
            entry = segment_prices.iloc[0].to_numpy(dtype=float)
            values = segment_prices.ffill().to_numpy(dtype=float)
            relative = np.divide(values,entry, out=np.ones_like(values),where=np.isfinite(entry)&(entry>0))
            relative[~np.isfinite(relative)] = 1.0
            missing_entry=~np.isfinite(entry)|(entry<=0)
            unpriced_exit=~missing_entry & ~np.isfinite(segment_prices.iloc[-1].to_numpy(dtype=float))
            episode=episodes.reindex(index=segment_prices.index,columns=ids).ffill()
            episode_crossing=episode.ne(episode.iloc[0],axis='columns').where(episode.notna(),False).any(axis=0).to_numpy() & ~missing_entry
            distribution=distributions.reindex(index=segment_prices.index,columns=ids).fillna(False)
            unresolved_distribution=distribution.loc[distribution.index>execution].any(axis=0).to_numpy() & ~missing_entry
            price_event=price_events.reindex(index=segment_prices.index,columns=ids).fillna(False)
            unresolved_price_event=price_event.loc[price_event.index>=execution].any(axis=0).to_numpy() & ~missing_entry
            self.segments.append(Segment(signal,execution,ids,scores,frame,segment_prices.index,relative,
                                         missing_entry,unpriced_exit,episode_crossing,unresolved_distribution,unresolved_price_event,
                                         history_age.reindex(index=[signal],columns=ids).iloc[0].to_numpy(dtype=float),
                                         beta_reviews.reindex(index=[signal],columns=ids).fillna(False).iloc[0].to_numpy(dtype=bool)))
        common = set.intersection(*(set(s.scores.columns) for s in self.segments))
        for f in sorted(common):
            counts = [int(s.scores[f].notna().sum()) for s in self.segments]
            fractions = [s.scores[f].notna().mean() for s in self.segments]
            self.coverage[f] = {'min_count':min(counts),'min_fraction':float(min(fractions))}
        self.usable = [f for f,v in self.coverage.items() if v['min_count']>=200 and v['min_fraction']>=0.30]
        log(market, 'segments',len(self.segments),'usable signals',len(self.usable),'prices',self.price_audit)

    def evaluate(self, weights, positions=50, gate=None, cost_bps=50, keep=False):
        all_returns, all_dates, holdings, counts = [],[],[],[]
        execution_issues=[];cash_allocations=0
        factors = sorted(weights)
        w = np.array([weights[f] for f in factors],dtype=float)
        for j,s in enumerate(self.segments):
            values = s.scores.reindex(columns=factors).to_numpy(dtype=float)
            valid = np.all(np.isfinite(values),axis=1)
            if gate == 'positive_profit':
                g=s.raw.get('roa',pd.Series(np.nan,index=s.ids))
                valid &= g.gt(0).to_numpy()
            elif gate == 'positive_momentum':
                g=s.raw.get('tr_12_1',pd.Series(np.nan,index=s.ids))
                valid &= g.gt(0).to_numpy()
            elif gate == 'quality_momentum':
                g=s.raw.get('roa',pd.Series(np.nan,index=s.ids))
                m=s.raw.get('tr_12_1',pd.Series(np.nan,index=s.ids))
                valid &= (g.gt(0)&m.gt(0)).to_numpy()
            score=values@w
            available=np.flatnonzero(valid)
            order=available[np.argsort(-score[available],kind='stable')]
            selected=order[:positions]
            cash_allocations+=int(s.missing_entry[selected].sum())
            lookback=technical_lookback(factors,gate=gate)
            if lookback and s.price_review_age is not None:
                affected=selected[np.isfinite(s.price_review_age[selected]) & (s.price_review_age[selected]<lookback)]
                if len(affected):execution_issues.append({'signal_date':str(s.signal.date()),
                    'execution_date':str(s.execution.date()),'reason':'unverified_price_history_for_signal',
                    'security_ids':s.ids[affected].tolist()})
            if uses_beta_history(factors) and s.beta_source_review is not None:
                affected=selected[s.beta_source_review[selected]]
                if len(affected):execution_issues.append({'signal_date':str(s.signal.date()),
                    'execution_date':str(s.execution.date()),'reason':'unverified_weekly_beta_price_input',
                    'security_ids':s.ids[affected].tolist()})
            for column,reason in [(s.unpriced_exit,'nonexecutable_exit'),(s.episode_crossing,'different_listing_episode'),
                                  (s.unresolved_distribution,'unmodeled_distribution_to_holder'),
                                  (s.unresolved_price_event,'unresolved_observed_price_action')]:
                affected=selected[column[selected]]
                if len(affected):execution_issues.append({'execution_date':str(s.execution.date()),
                    'end_date':str(s.dates[-1].date()),'reason':reason,'security_ids':s.ids[affected].tolist()})
            counts.append(len(selected))
            if len(selected):
                nav=s.relatives[:,selected].mean(axis=1)
                r=np.r_[0,nav[1:]/nav[:-1]-1]
                # Full turnover both ways, irrespective of actual overlap.
                cost=cost_bps/10000*(1 if j==0 else 2)
                r[0]=(1+r[0])*(1-cost)-1
            else:
                r=np.zeros(len(s.dates))
            all_returns.extend(r.tolist())
            all_dates.extend(s.dates.tolist())
            if keep:
                holdings.append({'signal_date':str(s.signal.date()),'execution_date':str(s.execution.date()),
                                 'eligible_count':len(available),'selected':s.ids[selected].tolist(),
                                 'scores':score[selected].tolist()})
        # On rebalance close, old holdings earn that day's return, then new positions incur costs.
        ser=pd.Series(np.asarray(all_returns),index=pd.DatetimeIndex(all_dates))
        ser=(1+ser).groupby(level=0).prod()-1
        result={'full':metrics(ser.to_numpy(),ser.index),
                'train':metrics(ser.loc[ser.index<=TRAIN_END],ser.index[ser.index<=TRAIN_END]),
                'validation':metrics(ser.loc[(ser.index>TRAIN_END)&(ser.index<=SELECTION_END)],ser.index[(ser.index>TRAIN_END)&(ser.index<=SELECTION_END)]),
                'holdout':metrics(ser.loc[ser.index>SELECTION_END],ser.index[ser.index>SELECTION_END]),
                'min_positions':min(counts),'max_positions':max(counts),
                'cash_entry_allocations':cash_allocations,'execution_issues':execution_issues,
                'returns_verified':not execution_issues}
        if keep:
            result['holdings']=holdings
            result['returns']=ser
        return result


def quality_key(results):
    full=[results[m]['full'] for m in results]
    val=[results[m]['validation'] for m in results]
    if any(v['sharpe_rf4'] is None for v in full+val):
        return (-1e9,-1e9,-1e9)
    min_sharpe=min(v['sharpe_rf4'] for v in full)
    pass_guard=all(v['cagr']>0 and v['mdd']>-.55 for v in val)
    pass_all=min_sharpe>1 and pass_guard
    growth=float(np.mean([math.log1p(max(v['cagr'],-.999)) for v in full]))
    # Preserve every historical portfolio, but do not rank unverified wealth
    # paths as validated successes. This does not filter the dated universe.
    if any(not r['returns_verified'] for r in results.values()):
        issues=sum(len(r['execution_issues']) for r in results.values())
        return (-1,-issues,growth)
    if pass_all:
        return (1,growth,min_sharpe)
    penalty=sum(max(0,1-v['sharpe_rf4']) for v in full)+sum(max(0,-v['cagr'])*5 for v in val)+sum(max(0,-.55-v['mdd'])*5 for v in val)
    return (0,-penalty,growth)


def load_policy():
    p=json.loads((OUT/'factor_policy.json').read_text(encoding='utf-8'))
    policy={r['factor_id']:r for r in p['factors']}
    qa_path=OUT/'factor_source_qa.json'
    if qa_path.exists():
        qa=json.loads(qa_path.read_text(encoding='utf-8'))
        for factor,reason in qa.get('withheld_factors',{}).items():
            if factor in policy:policy[factor]['source_qa_withheld_reason']=reason
        for factor,reason in qa.get('excluded_by_user',{}).items():
            if factor in policy:policy[factor]['source_qa_withheld_reason']=reason
        for market,factors in qa.get('withheld_by_market',{}).items():
            for factor,reason in factors.items():
                if factor in policy:policy[factor].setdefault('source_qa_withheld_by_market',{})[market]=reason
    return policy


def screen():
    policy=load_policy()
    markets={m:MarketData(m,policy) for m in ['KR','US']}
    rows=[]
    all_factors=sorted(set.union(*(set(d.usable) for d in markets.values())))
    for idx,f in enumerate(all_factors):
        for n in POSITIONS:
            results={m:d.evaluate({f:1.0},n) for m,d in markets.items() if f in d.usable}
            rows.append({'factor_id':f,'family':policy[f]['economic_family'],'positions':n,'results':results,
                         'key':quality_key(results) if len(results)==2 else (-1e6,-1e6,-1e6)})
        if idx%20==0:
            log('single factor',idx+1,'/',len(all_factors),f)
    dump('single_factor_screen.json',{'candidates':rows,'coverage':{m:d.coverage for m,d in markets.items()},
                                     'price_audit':{m:d.price_audit for m,d in markets.items()},'seed':SEED,
                                     'source_qa_withheld':{f:p['source_qa_withheld_reason'] for f,p in policy.items() if p.get('source_qa_withheld_reason')},
                                     'source_qa_withheld_by_market':{f:p['source_qa_withheld_by_market'] for f,p in policy.items() if p.get('source_qa_withheld_by_market')}})
    summary=[]
    for row in rows:
        for m,r in row['results'].items():
            summary.append({'market':m,'factor_id':row['factor_id'],'family':row['family'],'positions':row['positions'],
                            **{f'{p}_{k}':v for p in ['full','train','validation'] for k,v in r[p].items()}})
    pd.DataFrame(summary).to_csv(OUT/'single_factor_screen.csv',index=False,encoding='utf-8-sig')
    log('top common',[(r['factor_id'],r['positions'],r['key']) for r in sorted(rows,key=lambda x:tuple(x['key']),reverse=True)[:15]])


def combinations():
    if (OUT/'holdout_assessment.json').exists():
        raise ValueError('Holdout has been opened; do not retune the frozen selection against it')
    policy=load_policy()
    markets={m:MarketData(m,policy) for m in ['KR','US']}
    singles=json.loads((OUT/'single_factor_screen.json').read_text())['candidates']
    common=[r for r in singles if len(r['results'])==2]
    # One fixed portfolio size for factor-pool selection avoids triple counting.
    common=[r for r in common if r['positions']==50]
    groups={}
    for r in sorted(common,key=lambda x:tuple(x['key']),reverse=True):
        group=r['family']
        groups.setdefault(group,[])
        if len(groups[group])<2:
            groups[group].append(r['factor_id'])
    dump('candidate_pool.json',{'groups':groups,'selection_end':str(SELECTION_END.date()),'seed':SEED})
    log('candidate pool',groups)
    rng=np.random.default_rng(SEED)
    candidates={}
    def add(weights,n=50,gate=None):
        weights={f:float(w) for f,w in weights.items() if w>0}
        total=sum(weights.values())
        weights={f:round(w/total,8) for f,w in sorted(weights.items())}
        spec={'weights':weights,'positions':n,'gate':gate}
        key=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:16]
        candidates[key]=spec
    for group,fs in groups.items():
        for n in POSITIONS:
            add({f:1/len(fs) for f in fs},n)
    group_names=sorted(groups)
    # Limited random search; strictly positive economic-prior weights.
    for _ in range(360):
        n_groups=min(len(group_names),int(rng.integers(3,6)))
        chosen=list(rng.choice(group_names,n_groups,replace=False))
        mass=rng.dirichlet(np.ones(n_groups)*2)
        weights={}
        for g,w in zip(chosen,mass):
            f=str(rng.choice(groups[g]))
            weights[f]=float(w)
        add(weights,int(rng.choice(POSITIONS)))
    for gate in [None,'positive_profit','positive_momentum','quality_momentum']:
        for n in POSITIONS:
            add({f:1 for fs in groups.values() for f in fs[:1]},n,gate)
    rows=[]
    for i,(key,spec) in enumerate(candidates.items()):
        results={m:d.evaluate(spec['weights'],spec['positions'],spec['gate']) for m,d in markets.items()}
        rank=quality_key(results)
        if any(r['min_positions']<max(20,spec['positions']//2) for r in results.values()):
            rank=(-1e5,-1e5,-1e5)
        rows.append({'id':key,**spec,'results':results,'key':rank})
        if i%40==0:
            best=max(rows,key=lambda x:tuple(x['key']))
            log('combination',i+1,'/',len(candidates),'best',best['id'],best['key'])
            dump('combination_checkpoint.json',{'candidates':rows})
    # Apply economically motivated gates only to the best prespecified recipes.
    seeds=sorted(rows,key=lambda x:tuple(x['key']),reverse=True)[:8]
    for seed in seeds:
        for gate in ['positive_profit','positive_momentum','quality_momentum']:
            spec={'weights':seed['weights'],'positions':seed['positions'],'gate':gate}
            key=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:16]
            results={m:d.evaluate(**spec) for m,d in markets.items()}
            rank=quality_key(results)
            if any(r['min_positions']<20 for r in results.values()):
                rank=(-1e5,-1e5,-1e5)
            rows.append({'id':key,**spec,'results':results,'key':rank})
    rows.sort(key=lambda x:tuple(x['key']),reverse=True)
    dump('combinations.json',{'candidates':rows,'seed':SEED,'holdout_accessed':False})
    final={'common':rows[0], 'country_specific':{},
           'selection_contract':'Frozen research candidates; target claims require verified execution and the locked assessment'}
    # A factor available in only one country can still be its best defensible
    # standalone strategy. Retain all screened sizes in the country comparison.
    country_candidates=list(rows)
    for single in singles:
        spec={'weights':{single['factor_id']:1.0},'positions':single['positions'],'gate':None}
        key=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:16]
        country_candidates.append({'id':key,**spec,'results':single['results'],'source':'single_factor_screen'})
    for m in markets:
        ranked=sorted([r for r in country_candidates if m in r['results']],key=lambda r:quality_key({m:r['results'][m]})
                      if r['results'][m]['min_positions']>=max(20,r['positions']//2)
                      else (-1e5,-1e5,-1e5),reverse=True)
        final['country_specific'][m]=ranked[0]
    dump('frozen_selection.json',final)
    log('frozen common candidate',rows[0]['id'],rows[0]['key'])


def holdout():
    """Assess only already-frozen recipes; never select or alter their weights."""
    selection_path=OUT/'frozen_selection.json'
    selection=json.loads(selection_path.read_text(encoding='utf-8'))
    selection_sha=digest(selection_path)
    policy=load_policy()
    markets={m:MarketData(m,policy,include_holdout=True) for m in ['KR','US']}
    assessment={'selection_sha256':selection_sha,'holdout_start':'2024-01-01',
                'costs_one_way_bps':[50,100,150], 'results':[],
                'rule':'No recipe changes or winner reselection after observing holdout returns',
                'return_basis':'Split-adjusted price return; cash dividends are not included'}
    for market,data in markets.items():
        recipes={'common':selection['common'],'country_specific':selection['country_specific'][market]}
        for label,spec in recipes.items():
            if set(spec['weights'])-set(data.usable):
                # Report changed coverage; do not silently replace components.
                assessment['results'].append({'market':market,'recipe_kind':label,'recipe_id':spec['id'],
                    'status':'insufficient_holdout_factor_coverage','missing':sorted(set(spec['weights'])-set(data.usable))})
                continue
            for cost in assessment['costs_one_way_bps']:
                result=data.evaluate(spec['weights'],spec['positions'],spec.get('gate'),cost_bps=cost,keep=True)
                returns=result.pop('returns');holdings=result.pop('holdings')
                name=f'locked_{market}_{label}_{cost}bps'
                returns.rename('return').to_csv(OUT/f'{name}_returns.csv',index_label='trade_date')
                dump(f'{name}_holdings.json',holdings)
                result.update(market=market,recipe_kind=label,recipe_id=spec['id'],cost_bps=cost)
                held=result['holdout']
                result['holdout_sharpe_target_met']=(held['sharpe_rf4'] is not None and held['sharpe_rf4']>1)
                result['validated_target_met']=result['holdout_sharpe_target_met'] and result['returns_verified']
                assessment['results'].append(result)
    if digest(selection_path)!=selection_sha:raise ValueError('Frozen selection changed during holdout assessment')
    dump('holdout_assessment.json',assessment)
    log('locked holdout assessment saved; selection unchanged')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('phase',choices=['screen','combinations','holdout'])
    args=parser.parse_args()
    globals()[args.phase]()
