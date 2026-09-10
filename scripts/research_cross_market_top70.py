"""Reproducible, local research over non-lab factors and the Factor Lab universe.

All source queries are read-only. Final experiments use FactorLabService.
No historical return is used to choose financial basis or factor direction.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import time

import numpy as np
import pandas as pd
from clickhouse_connect.driver.external import ExternalData

from api.config.clickhouse import get_clickhouse_client
from api.repository.universe_query import build_universe_ctes
from api.service.backtest_service import BacktestService, _rebalance_dates, _previous_trading_day

OUT = Path(os.environ.get('ARCANA_RESEARCH_OUT','deliverables/cross_market_top70_20260909'))
UNIVERSE = {'size_percentile': {'side': 'top', 'percent': 70}}
START = date(2010, 1, 1)
END = date(2026, 9, 4)


def log(*args):
    print(time.strftime('%H:%M:%S'), *args, flush=True)


def dump(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def key_data(keys):
    return ExternalData(file_name='eligible_keys', data=''.join(f'{d}\t{s}\n' for d,s in keys).encode(),
                        fmt='TabSeparated', structure=['trade_date Date','security_id String'])


def schedule(client, market):
    trading = BacktestService()._load_trading_days(client, START, END, market=market)
    rebalances = _rebalance_dates(trading, start_date=START, end_date=END, frequency='quarterly')
    pairs = [(r, _previous_trading_day(trading, r)) for r in rebalances]
    pairs = [(r, s) for r, s in pairs if s]
    return trading, pairs


def universe(client, market, signals):
    ctes, params = build_universe_ctes(
        dates_sql='SELECT arrayJoin({days:Array(Date)}) AS trade_date',
        universe=UNIVERSE, market=market)
    params['days'] = signals
    query = 'WITH ' + ',\n'.join(ctes) + '''
SELECT r.trade_date, r.security_id, r.market_cap, r.size_rank_high, r.size_count,
       (r.trade_date,r.security_id) IN (SELECT trade_date,security_id FROM uv_eligible) AS eligible
FROM uv_ranked_caps r ORDER BY trade_date,security_id'''
    return client.query_df(query, parameters=params)


def audit():
    client = get_clickhouse_client(send_receive_timeout=600, settings={'max_threads': 4, 'max_execution_time': 600})
    catalog = client.query_df("SELECT * FROM factor_catalog FINAL WHERE NOT startsWith(factor_id,'lab_') ORDER BY factor_id")
    catalog.to_csv(OUT / 'catalog.csv', index=False, encoding='utf-8-sig')
    log('catalog', len(catalog))
    manifest = {'universe': UNIVERSE, 'start': START, 'end': END, 'factor_count': len(catalog), 'markets': {}}
    for market in ['KR', 'US']:
        trading, pairs = schedule(client, market)
        signals = [s for _, s in pairs]
        dump(f'{market}_schedule.json', {'trading': trading, 'pairs': pairs})
        uv_path = OUT / f'{market}_universe.parquet'
        if uv_path.exists():
            uv = pd.read_parquet(uv_path)
        else:
            log(market, 'universe query', len(signals))
            uv = universe(client, market, signals)
            uv.to_parquet(uv_path, index=False)
        log(market, 'eligible range', uv[uv.eligible.astype(bool)].groupby('trade_date').size().agg(['min','max']).to_dict())
        coverage_frames = []
        for year in sorted({s.year for s in signals}):
            path = OUT / f'{market}_coverage_{year}.parquet'
            if path.exists():
                coverage_frames.append(pd.read_parquet(path))
                continue
            days = [s for s in signals if s.year == year]
            securities = uv[uv.eligible.astype(bool) & uv.trade_date.isin(pd.to_datetime(days))]
            if securities.empty:
                continue
            keys = list(zip(securities.trade_date.dt.date, securities.security_id))
            query = '''
SELECT trade_date, factor_id, financial_basis,
 countIf(isFinite(value) AND source_date <= trade_date) AS valid_count,
 countIf(source_date > trade_date) AS future_source_count
FROM (
 SELECT trade_date,security_id,factor_id,financial_basis,
 tupleElement(argMax(tuple(factor_value),updated_at),1) AS value,
 argMax(source_trade_date,updated_at) AS source_date
 FROM fact_daily_factor_snapshot
 WHERE trade_date IN {days:Array(Date)} AND factor_id IN {factors:Array(String)}
 AND (trade_date,security_id) IN (SELECT trade_date,security_id FROM eligible_keys)
 GROUP BY trade_date,security_id,factor_id,financial_basis
) GROUP BY trade_date,factor_id,financial_basis'''
            frame = client.query_df(query, parameters={'days': days, 'factors': catalog.factor_id.tolist()}, external_data=key_data(keys))
            frame.to_parquet(path, index=False)
            coverage_frames.append(frame)
            log(market, year, 'coverage rows', len(frame))
        cov = pd.concat(coverage_frames, ignore_index=True)
        cov.to_parquet(OUT / f'{market}_coverage.parquet', index=False)
        by_year = cov.assign(year=cov.trade_date.dt.year).groupby('year').agg(factors=('factor_id','nunique'),dates=('trade_date','nunique'),cells=('valid_count','sum'))
        manifest['markets'][market] = by_year.reset_index().to_dict('records')
        log(market, by_year.to_string())
        dump('audit.json', manifest)
    client.close()


def cache():
    client = get_clickhouse_client(send_receive_timeout=600, settings={'max_threads': 4, 'max_execution_time': 600})
    catalog = pd.read_csv(OUT / 'catalog.csv').fillna('')
    for market in ['KR','US']:
        uv = pd.read_parquet(OUT / f'{market}_universe.parquet')
        uv = uv[uv.eligible.astype(bool)]
        cov = pd.read_parquet(OUT / f'{market}_coverage.parquet')
        # Basis decisions use availability only in pre-holdout history.
        pre = cov[(cov.trade_date >= '2017-01-01') & (cov.trade_date < '2024-01-01')]
        scores = pre.groupby(['factor_id','financial_basis']).valid_count.agg(['sum','count']).reset_index()
        scores['basis_priority'] = scores.financial_basis.map({'annual':3,'ttm':2,'quarterly':1}).fillna(0)
        scores = scores.sort_values(['factor_id','count','sum','basis_priority'], ascending=[True,False,False,False]).drop_duplicates('factor_id')
        selected = dict(zip(scores.factor_id, scores.financial_basis))
        dump(f'{market}_bases.json', selected)
        for year in range(2010,2027):
            path = OUT / f'{market}_factors_{year}.parquet'
            if path.exists() and all(dtype == np.dtype('float64') for dtype in pd.read_parquet(path).dtypes):
                continue
            subset = uv[uv.trade_date.dt.year == year]
            if subset.empty:
                continue
            days = sorted(set(subset.trade_date.dt.date))
            keys = list(zip(subset.trade_date.dt.date, subset.security_id))
            query = '''
SELECT trade_date,security_id,factor_id,
 if(source_date <= trade_date AND isFinite(value),value,NULL) AS value
FROM (
 SELECT trade_date,security_id,factor_id,
 tupleElement(argMax(tuple(factor_value),updated_at),1) AS value,
 argMax(source_trade_date,updated_at) AS source_date
 FROM fact_daily_factor_snapshot
 WHERE trade_date IN {days:Array(Date)}
 AND (factor_id,financial_basis) IN {pairs:Array(Tuple(String,String))}
 AND (trade_date,security_id) IN (SELECT trade_date,security_id FROM eligible_keys)
 GROUP BY trade_date,security_id,factor_id
)'''
            frame = client.query_df(query, parameters={'days':days,'pairs':list(selected.items())}, external_data=key_data(keys))
            if not frame.empty:
                wide = frame.pivot(index=['trade_date','security_id'],columns='factor_id',values='value')
                wide = wide.astype('float64')
                wide.to_parquet(path)
                log(market, year, 'cached', wide.shape)
        ppath = OUT / f'{market}_prices.parquet'
        if not ppath.exists():
            ids = sorted(uv.security_id.unique())
            log(market, 'prices', len(ids))
            frame = client.query_df('''SELECT trade_date,security_id,
 toFloat64(argMax(close,updated_at)) AS close,
 toFloat64(argMax(adj_close,updated_at)) AS adj_close,
 argMax(volume,updated_at) AS volume
 FROM price_daily WHERE security_id IN {ids:Array(String)}
 AND trade_date BETWEEN {start:Date} AND {end:Date}
 GROUP BY trade_date,security_id''', parameters={'ids':ids,'start':date(2009,12,1),'end':END})
            frame.to_parquet(ppath, index=False)
            log(market, 'prices cached', frame.shape)
    client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['audit','cache'])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    globals()[args.phase]()
