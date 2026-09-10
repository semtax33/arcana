"""Reproduce frozen research recipes with the actual FactorLab service.

Only a new, dedicated research database is written. Production tables are not
changed, and all numerical inputs come from the verified assembled artifacts.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

import numpy as np
import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.repository.factor_lab_query import validate_factor_lab_graph
from api.service.dto import FactorLabGraphDto, FactorLabRunRequestDto, FactorLabBacktestRequestDto
from api.service.factor_lab_service import FactorLabService
from api.service.style_score_catalog import STYLE_SCORE_FACTORS, style_score_factor_metadata
from scripts.research_cross_market_corrected_cache import OUT, digest, load_json, save_json

POSITIVE_MULTIPLES={'per','pbr','pcr','psr','peg','ev_to_ebitda','ev_to_nopat'}
PREFIX='arcana_research_top70_20260909_'
STAGING_NORMALIZATION={'numeric_zero':'Canonicalize signed zero to +0.0 before SQL tie partitions'}
STAGE_TABLES=['security_master','issuers','identifiers','factor_catalog','fact_daily_factors',
              'fact_daily_factor_snapshot','benchmark_price_daily']


def verify_source_engines(client,source):
    # Cloning a Distributed/external engine could route research inserts to
    # another destination. Only local storage engines may be copied verbatim.
    rows=client.query('SELECT name, engine FROM system.tables WHERE database={db:String} AND name IN {tables:Array(String)}',
                      parameters={'db':source,'tables':STAGE_TABLES}).result_rows
    engines=dict(rows)
    if set(engines)!=set(STAGE_TABLES) or any(e not in {'MergeTree','ReplacingMergeTree'} for e in engines.values()):
        raise ValueError('Research staging requires independently stored local MergeTree source schemas')
    return engines


def recipe_graph(spec, market, policy, bases, *, start='2017-01-01', end='2026-09-04'):
    nodes=[];edges=[];inputs={}
    def node(name,kind,config=None):
        nodes.append({'id':name,'type':kind,'config':config or {}});return name
    def edge(source,target,handle='input'):
        edges.append({'source':source,'target':target,'target_handle':handle})
    def source(factor):
        if factor not in inputs:
            inputs[factor]=node('src_'+factor,'factor_input',{'factor_id':factor,
                'financial_basis':bases.get(factor,'annual'),'missing_policy':'drop'})
        return inputs[factor]
    def positive(factor):
        zero='zero'
        if not any(n['id']==zero for n in nodes):node(zero,'constant',{'value':0})
        name=node('positive_'+factor,'greater_than');edge(source(factor),name,'left');edge(zero,name,'right')
        return name
    handles={}
    for factor,weight in sorted(spec['weights'].items()):
        raw=source(factor)
        if factor in POSITIVE_MULTIPLES:
            condition=positive(factor);raw=node('valid_'+factor,'filter')
            edge(source(factor),raw);edge(condition,raw,'condition')
        score=node('rank_'+factor,'dense_score',{'group_by':['trade_date'],
            'order':'desc' if policy[factor]['direction']=='higher' else 'asc',
            'scale':'0_100','tie_method':'average'})
        edge(raw,score);handles['w_'+factor]=float(weight)
    final=node('combined','weighted_score',{'weights':handles,'missing_weight_renormalize':False})
    for factor in spec['weights']:edge('rank_'+factor,final,'w_'+factor)
    gate=spec.get('gate')
    if gate:
        factors={'positive_profit':['roa'],'positive_momentum':['tr_12_1'],
                 'quality_momentum':['roa','tr_12_1']}[gate]
        conditions=[positive(f) for f in factors]
        condition=conditions[0]
        if len(conditions)==2:
            condition=node('gate_condition','and');edge(conditions[0],condition,'left');edge(conditions[1],condition,'right')
        masked=node('gated_score','filter');edge(final,masked);edge(condition,masked,'condition');final=masked
    graph={'version':2,'experiment':{'name':f'Cross-market top70 {market} {spec["id"]}',
        'market':market,'start_date':start,'end_date':end,
        'universe':{'size_percentile':{'side':'top','percent':70}},
        'rebalance':{'frequency':'quarterly','signal_lag_days':1,'transaction_cost_bps':100}},
        'nodes':nodes,'edges':edges,'outputs':{'final_node_id':final}}
    validation=validate_factor_lab_graph(graph,known_factor_ids=set(policy))
    if not validation.valid:raise ValueError([issue.message for issue in validation.errors])
    return graph


def verify_inputs():
    files={'frozen_selection.json':digest(OUT/'frozen_selection.json'),
           'factor_policy.json':digest(OUT/'factor_policy.json'),
           'factor_source_qa.json':digest(OUT/'factor_source_qa.json'),
           'inputs/issuer_classification.parquet':digest(OUT/'inputs'/'issuer_classification.parquet'),
           'catalog.csv':digest(OUT/'catalog.csv')}
    for market in ['KR','US']:
        manifest=load_json(OUT/f'{market}_corrected_manifest.json')
        if manifest.get('status')!='ready' or not (manifest.get('style_factors') or manifest.get('style_scores_omitted_by_user')):
            raise ValueError(f'{market}: complete corrected assembly and resolve the requested style scope first')
        if manifest['signal_quality_code_sha256']!=digest('scripts/research_cross_market_signal_quality.py'):
            raise ValueError(f'{market}: signal price-quality rules changed after assembly')
        files['../../../scripts/research_cross_market_signal_quality.py']=manifest['signal_quality_code_sha256']
        expected={f'{market}_prices.parquet':manifest['prices_sha256'],
            f'{market}_universe.parquet':manifest['universe_sha256'],
            f'{market}_schedule.json':manifest['schedule_sha256'],
            f'{market}_observed_price_qa.json':manifest['observed_price_qa_sha256'],
            f'{market}_economic_validity.json':manifest['economic_validity_sha256'],
            f'{market}_bases.json':manifest['bases_sha256'],**manifest['factor_files']}
        for name,sha in expected.items():
            if digest(OUT/name)!=sha:raise ValueError('Changed assembled input: '+name)
        files.update(expected)
    return files


def insert_frame(client, table, frame, batch=100000):
    for offset in range(0,len(frame),batch):
        chunk=frame.iloc[offset:offset+batch].copy()
        if table=='fact_daily_factors':
            # ClickHouse orders -0 and +0 together but partitions their bit
            # patterns separately. Both represent the same economic value.
            chunk.loc[chunk.factor_value.eq(0),'factor_value']=0.0
        client.insert_df(table,chunk)


def stage():
    files=verify_inputs()
    fingerprint=hashlib.sha256(json.dumps({'files':files,'normalization':STAGING_NORMALIZATION},sort_keys=True).encode()).hexdigest()
    state_path=OUT/'factorlab_stage.json'
    previous=load_json(state_path) if state_path.exists() else {}
    if previous.get('status')=='ready' and previous.get('fingerprint')==fingerprint:
        return previous
    database=PREFIX+fingerprint[:10]+'_'+uuid4().hex[:6]
    assert re.fullmatch(PREFIX+r'[a-f0-9]{10}_[a-f0-9]{6}',database)
    state={'status':'building','database':database,'fingerprint':fingerprint,'files':files,
           'normalization':STAGING_NORMALIZATION,'tables':{}}
    save_json(state_path,state)
    base=get_clickhouse_client()
    try:
        source=str(base.command('SELECT currentDatabase()'))
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',source):raise ValueError('Invalid source database')
        if source.startswith(PREFIX):raise ValueError('Source must be the configured Arcana database')
        state['source_engines']=verify_source_engines(base,source)
        base.command(f'CREATE DATABASE {database}')
        for table in STAGE_TABLES:
            base.command(f'CREATE TABLE {database}.{table} AS {source}.{table}')
    finally:base.close()
    client=get_clickhouse_client(database=database)
    try:
        client.command("""CREATE TABLE price_daily (
            security_id String, trade_date Date, open Nullable(Float64), high Nullable(Float64),
            low Nullable(Float64), close Nullable(Float64), volume Nullable(UInt64),
            adj_close Nullable(Float64), currency LowCardinality(String),
            updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
        ) ENGINE=ReplacingMergeTree(updated_at) ORDER BY (security_id,trade_date)""")
        classification=pd.read_parquet(OUT/'inputs'/'issuer_classification.parquet').fillna('')
        security=classification[['security_id','issuer_id','country']].copy()
        security['exchange_code']=classification.market_mic.replace({'XKRX':'KOSPI','XKOS':'KOSDAQ'})
        insert_frame(client,'security_master',security)
        issuers=classification[['issuer_id','country','company_name','sector_code','industry_group_code','industry_group_name']].drop_duplicates('issuer_id')
        issuers=issuers.rename(columns={'country':'domicile_country','company_name':'legal_name_en'})
        insert_frame(client,'issuers',issuers)
        catalog=pd.read_csv(OUT/'catalog.csv').fillna('')
        catalog=catalog[~catalog.factor_id.str.startswith('lab_')].drop(columns=['created_at','updated_at'])
        if not all(load_json(OUT/f'{market}_corrected_manifest.json').get('style_scores_omitted_by_user') for market in ['KR','US']):
            styles=pd.DataFrame([style_score_factor_metadata(f) for f in STYLE_SCORE_FACTORS]).fillna('')
            catalog=pd.concat([catalog,styles],ignore_index=True).drop_duplicates('factor_id')
        insert_frame(client,'factor_catalog',catalog)
        for market in ['KR','US']:
            currency='KRW' if market=='KR' else 'USD'
            bases=load_json(OUT/f'{market}_bases.json')
            for path in sorted(OUT.glob(f'{market}_factors_*.parquet')):
                wide=pd.read_parquet(path).drop(columns=['mcap_mil'],errors='ignore')
                frame=wide.stack().rename('factor_value').reset_index()
                frame.columns=['trade_date','security_id','factor_id','factor_value']
                frame['financial_basis']=frame.factor_id.map(bases).fillna('annual')
                frame['currency']=currency;frame['trade_date']=frame.trade_date.dt.date
                frame=frame[np.isfinite(frame.factor_value)]
                insert_frame(client,'fact_daily_factors',frame)
                print('FactorLab staged',market,path.stem[-4:],len(frame),'factor rows',flush=True)
            caps=pd.read_parquet(OUT/f'{market}_universe.parquet')[['trade_date','security_id','market_cap']]
            caps=caps.rename(columns={'market_cap':'factor_value'})
            caps['trade_date']=caps.trade_date.dt.date;caps['financial_basis']='annual'
            caps['factor_id']='mcap_mil';caps['currency']=currency
            insert_frame(client,'fact_daily_factors',caps)
            prices=pd.read_parquet(OUT/f'{market}_prices.parquet')
            prices=prices[['security_id','trade_date','close','adj_close','volume']].copy()
            prices['trade_date']=prices.trade_date.dt.date;prices['currency']=currency
            prices['volume']=prices.volume.fillna(0).clip(lower=0).astype('uint64')
            insert_frame(client,'price_daily',prices)
        # Verify the native cap-ranked population against every frozen date.
        from api.repository.universe_query import build_universe_ctes
        for market in ['KR','US']:
            uv=pd.read_parquet(OUT/f'{market}_universe.parquet')
            ctes,params=build_universe_ctes(dates_sql='SELECT arrayJoin({days:Array(Date)}) AS trade_date',
                universe={'size_percentile':{'side':'top','percent':70}},market=market)
            params['days']=sorted({str(d.date()) for d in uv.trade_date})
            rows=client.query_df('WITH '+',\n'.join(ctes)+' SELECT * FROM uv_eligible',parameters=params)
            actual={(str(r.trade_date)[:10],r.security_id) for r in rows.itertuples()}
            expected={(str(r.trade_date)[:10],r.security_id) for r in uv[uv.eligible].itertuples()}
            if actual!=expected:raise ValueError(f'{market}: native top70 universe differs from frozen inputs')
        if verify_inputs()!=files:raise ValueError('Inputs changed during FactorLab staging')
        for table in ['security_master','issuers','factor_catalog','fact_daily_factors','price_daily']:
            state['tables'][table]=int(client.command(f'SELECT count() FROM {table}'))
        state['status']='ready';save_json(state_path,state)
    finally:client.close()
    return state


def validate():
    state=load_json(OUT/'factorlab_stage.json')
    if state.get('status')!='ready' or verify_inputs()!=state['files']:
        raise ValueError('Stage the verified current inputs first')
    database=state['database']
    if not re.fullmatch(PREFIX+r'[a-f0-9]{10}_[a-f0-9]{6}',database):raise ValueError('Not a research database')
    service=FactorLabService(client_factory=lambda:get_clickhouse_client(database=database))
    client=get_clickhouse_client(database=database)
    try:
        for table in ['security_master','issuers','fact_daily_factors','price_daily']:
            if int(client.command(f'SELECT count() FROM {table}'))!=state['tables'][table]:
                raise ValueError('Staged row count changed: '+table)
    finally:client.close()
    selection=load_json(OUT/'frozen_selection.json')
    policy={r['factor_id']:r for r in load_json(OUT/'factor_policy.json')['factors']}
    source_qa=load_json(OUT/'factor_source_qa.json')
    results=[]
    for market in ['KR','US']:
        seen=set()
        withheld=set(source_qa['withheld_factors'])|set(source_qa.get('withheld_by_market',{}).get(market,{}))|set(source_qa.get('excluded_by_user',{}))
        for label,spec in [('common',selection['common']),('country_specific',selection['country_specific'][market])]:
            if spec['id'] in seen:continue
            seen.add(spec['id'])
            if set(spec['weights'])&set(withheld):raise ValueError('Frozen recipe includes quarantined inputs')
            graph=recipe_graph(spec,market,policy,load_json(OUT/f'{market}_bases.json'))
            save_json(OUT/f'factorlab_graph_{market}_{spec["id"]}.json',graph)
            run=service.run_graph(FactorLabRunRequestDto(graph=FactorLabGraphDto.model_validate(graph),mode='history',
                history_start_date=date(2017,1,1),history_end_date=date(2026,9,4),history_rebalance_frequency='quarterly'))
            result=service.run_backtest(run.run_id,FactorLabBacktestRequestDto(
                top_percent=100,max_positions=spec['positions'],start_date=date(2017,1,1),end_date=date(2026,9,4),
                market=market,benchmarks=[],rebalance_frequency='quarterly',transaction_cost_bps=100))
            result_file=f'factorlab_backtest_{market}_{spec["id"]}.json'
            save_json(OUT/result_file,asdict(result))
            results.append({'market':market,'recipe_kind':label,'recipe_id':spec['id'],
                            'run_id':str(run.run_id),'graph_sha256':digest(OUT/f'factorlab_graph_{market}_{spec["id"]}.json'),
                            'backtest_file':result_file})
            save_json(OUT/'factorlab_validation.json',{'database':database,'runs':results,
                'cost_contract':'100 bps per rebalance, including first; conservative relative to 50 bps initial research entry',
                'staging_fingerprint':state['fingerprint']})
            print('FactorLab backtest',market,spec['id'],run.run_id,flush=True)


def parity():
    """Compare frozen holdings, final graph scores, and each day's net NAV."""
    from scripts.research_cross_market_top70_search import MarketData, load_policy, OUT as search_out
    if search_out.resolve()!=OUT.resolve():
        raise ValueError('Set ARCANA_RESEARCH_OUT to the corrected artifact directory before invoking parity')
    state=load_json(OUT/'factorlab_stage.json')
    if state.get('status')!='ready' or verify_inputs()!=state['files']:
        raise ValueError('FactorLab inputs changed after staging')
    validation=load_json(OUT/'factorlab_validation.json')
    if validation['database']!=state['database']:
        raise ValueError('Validation database differs from staging')
    selection=load_json(OUT/'frozen_selection.json')
    expected_runs={(m,s['id']) for m in ['KR','US']
                   for s in [selection['common'],selection['country_specific'][m]]}
    if {(r['market'],r['recipe_id']) for r in validation['runs']}!=expected_runs:
        raise ValueError('Complete every frozen market/recipe native run before parity')
    recipes={s['id']:s for s in [selection['common'],*selection['country_specific'].values()]}
    policy=load_policy();checks=[]
    client=get_clickhouse_client(database=state['database'])
    try:
        for market in ['KR','US']:
            data=MarketData(market,policy,include_holdout=True)
            for run in [r for r in validation['runs'] if r['market']==market]:
                spec=recipes[run['recipe_id']]
                expected=data.evaluate(spec['weights'],spec['positions'],spec.get('gate'),cost_bps=50,keep=True)
                actual=load_json(OUT/run['backtest_file'])
                history=actual['rebalance_history']
                if len(history)!=len(expected['holdings']):raise ValueError('Rebalance count differs')
                values=client.query_df('SELECT trade_date,security_id,factor_value FROM factor_lab_values FINAL '
                    'WHERE run_id={run_id:UUID} AND is_valid',parameters={'run_id':run['run_id']})
                values['trade_date']=pd.to_datetime(values.trade_date).dt.strftime('%Y-%m-%d')
                scores=values.set_index(['trade_date','security_id']).factor_value
                errors=[]
                for native,research in zip(history,expected['holdings']):
                    if (native['rebalance_date'],native['signal_date'])!=(research['execution_date'],research['signal_date']):
                        raise ValueError('Signal/execution date differs')
                    if {p['security_id'] for p in native['positions']}!=set(research['selected']):
                        raise ValueError(f'{market} selected holdings differ at '+research['execution_date'])
                    for sid,value in zip(research['selected'],research['scores']):
                        errors.append(abs(float(scores.loc[(research['signal_date'],sid)])-value))
                    if native['positions']:
                        np.testing.assert_allclose([p['weight'] for p in native['positions']],1/len(native['positions']),atol=1e-12)
                max_score_error=max(errors,default=0)
                if max_score_error>1e-8:raise ValueError('Final graph scores differ')
                expected_nav=(1+expected['returns']).cumprod()
                # Native applies 100 bps at the first entry as well. Research
                # charges 50 bps there, then 100 bps at subsequent rebalances.
                if expected['holdings'][0]['selected']:expected_nav*=.99/.995
                native_nav=pd.Series({pd.Timestamp(p['trade_date']):p['strategy_nav'] for p in actual['equity_curve']}).sort_index()
                if not native_nav.index.equals(expected_nav.index):raise ValueError('NAV trading calendar differs')
                np.testing.assert_allclose(native_nav,expected_nav,rtol=1e-9,atol=1e-10)
                checks.append({**run,'holdings_match':True,'max_score_error':max_score_error,
                    'max_nav_absolute_error':float(np.max(np.abs(native_nav-expected_nav))),
                    'days':len(native_nav),'returns_verified':expected['returns_verified'],
                    'execution_issues':expected['execution_issues']})
                save_json(OUT/'factorlab_parity.json',{'status':'running','checks':checks})
                print('FactorLab parity',market,spec['id'],'matched',len(native_nav),'days',flush=True)
    finally:client.close()
    save_json(OUT/'factorlab_parity.json',{'status':'passed','checks':checks,
        'interpretation':'Numerical reproduction does not resolve flagged corporate-action or execution evidence gaps'})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=['stage','validate','parity'])
    globals()[parser.parse_args().phase]()
