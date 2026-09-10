"""Read-only SQL execution tests using synthetic external tables."""
import csv
import io
from datetime import date

import numpy as np
import pandas as pd
import pytest
from clickhouse_connect.driver.external import ExternalData

from api.config.clickhouse import get_clickhouse_client
from api.repository.backtest_query import build_portfolio_return_query
from api.service.backtest_service import _segment_end_date


@pytest.fixture(scope='module')
def client():
    try:
        connection = get_clickhouse_client(connect_timeout=2)
        connection.command('SELECT 1')
    except Exception as exc:
        pytest.skip(f'Local ClickHouse unavailable: {type(exc).__name__}')
    yield connection
    connection.close()


def csv_bytes(rows):
    buffer = io.StringIO(newline='')
    csv.writer(buffer, lineterminator='\n').writerows(rows)
    return buffer.getvalue().encode()


def execute(client, prices, segments=None):
    days = ['2026-01-02', '2026-01-05', '2026-01-06']
    segments = segments or [dict(security_ids=['A', 'B'], start_date=days[0], end_date=days[-1])]
    query, params, positions = build_portfolio_return_query(
        segments=segments, trading_days=days, price_table='execution_test_prices')
    external = ExternalData(file_name='portfolio_positions.csv', data=csv_bytes(positions), fmt='CSV',
        structure='segment_id UInt32, security_id String, start_date Date, end_date Date, transaction_cost_bps Float64')
    external.add_file(file_name='execution_test_prices.csv', data=csv_bytes(prices), fmt='CSV',
        structure='security_id String, trade_date Date, close Float64, adj_close Float64, volume Float64, updated_at UInt32')
    return client.query_df(query, parameters=params, external_data=external)


def quotes(a, b):
    days = ['2026-01-02', '2026-01-05', '2026-01-06']
    return [(sid, day, price, price, volume, 1)
            for sid, values in [('A', a), ('B', b)]
            for day, (price, volume) in zip(days, values) if price is not None]


def test_fixed_shares_drift_between_rebalances(client):
    result = execute(client, quotes([(100, 1), (200, 1), (200, 1)], [(100, 1), (100, 1), (200, 1)]))
    np.testing.assert_allclose(result.daily_return, [0, .5, 1/3])
    assert np.prod(1 + result.daily_return) == pytest.approx(2)


def test_missing_entry_stays_cash_even_if_quotes_appear_later(client):
    result = execute(client, quotes([(100, 1), (200, 1), (300, 1)], [(None, 0), (10, 1), (1000, 1)]))
    np.testing.assert_allclose(result.daily_return, [0, .5, 1/3])
    assert result.missing_entry_count.tolist() == [1, 1, 1]


def test_suspension_carries_holding_and_latest_zero_volume_wins(client):
    prices = quotes([(100, 1), (200, 1), (300, 1)], [(100, 1), (100, 1), (200, 1)])
    prices.append(('B', '2026-01-05', 5000, 5000, 0, 2))
    result = execute(client, prices)
    np.testing.assert_allclose(result.daily_return, [0, .5, 2.5/1.5 - 1])


def test_each_rebalance_buys_at_its_own_close_and_costs_once(client):
    prices = quotes([(100, 1), (200, 1), (300, 1)], [(100, 1), (100, 1), (200, 1)])
    segments = [dict(security_ids=['A'], start_date='2026-01-02', end_date='2026-01-05', transaction_cost_bps=50),
                dict(security_ids=['B'], start_date='2026-01-05', end_date='2026-01-06', transaction_cost_bps=100)]
    result = execute(client, prices, segments)
    np.testing.assert_allclose(result.daily_return, [-.005, 1, -.01, 1])
    assert np.prod(1 + result.daily_return) == pytest.approx(.995 * 2 * .99 * 2)
    days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    assert _segment_end_date(days, days[:2], current_index=0, final_end_date=days[-1]) == days[1]


def test_nonexecutable_exit_is_reported_not_redistributed(client):
    result = execute(client, quotes([(100, 1), (200, 1), (300, 1)], [(100, 1), (100, 0), (100, 0)]))
    np.testing.assert_allclose(result.daily_return, [0, .5, 1/3])
    assert result.unpriced_exit_count.tolist() == [0, 0, 1]


def test_factorlab_ranks_inside_top70_before_complete_case_filter(client):
    from api.repository.factor_lab_query import compile_factor_lab_graph
    from scripts.research_cross_market_factorlab import recipe_graph
    policy={'roe':{'direction':'higher'},'tr_12_1':{'direction':'higher'}}
    spec={'id':'synthetic_contract','weights':{'roe':.6,'tr_12_1':.4},'gate':None}
    graph=recipe_graph(spec,'US',policy,{},start='2023-12-29',end='2023-12-29')
    compiled=compile_factor_lab_graph(graph,known_factor_ids=set(policy),trade_dates=['2023-12-29'])
    day='2023-12-29';ids=[f'SEC_US_{i:02}' for i in range(10)]
    rows=[]
    roe=[1,2,2,4,None,6,7,1000,2000,3000]
    momentum=[7,6,5,4,3,2,1,1000,2000,3000]
    for i,sid in enumerate(ids):
        rows.append((sid,day,'mcap_mil','annual',100-i,'USD',1))
        if roe[i] is not None:rows.append((sid,day,'roe','annual',roe[i],'USD',1))
        rows.append((sid,day,'tr_12_1','annual',momentum[i],'USD',1))
    external=ExternalData(file_name='fact_daily_factors.csv',data=csv_bytes(rows),fmt='CSV',
        structure='security_id String, trade_date Date, factor_id String, financial_basis String, factor_value Float64, currency String, updated_at UInt32')
    external.add_file(file_name='security_master.csv',data=csv_bytes([(s,s,'US','NASDAQ',1) for s in ids]),fmt='CSV',
        structure='security_id String, issuer_id String, country String, exchange_code String, updated_at UInt32')
    external.add_file(file_name='issuers.csv',data=csv_bytes([(s,'TECH','SOFTWARE',1) for s in ids]),fmt='CSV',
        structure='issuer_id String, sector_code String, industry_group_code String, updated_at UInt32')
    actual=client.query_df(compiled.query,parameters=compiled.parameters,external_data=external)
    actual=actual[actual.is_valid].set_index('security_id').value.sort_index()
    source=pd.DataFrame({'roe':roe[:7],'tr_12_1':momentum[:7]},index=ids[:7])
    scores=source.apply(lambda c:100*(c.rank(method='average')-1)/(c.notna().sum()-1))
    expected=(scores.roe*.6+scores.tr_12_1*.4).dropna().sort_index()
    assert actual.index.tolist()==expected.index.tolist()
    np.testing.assert_allclose(actual,expected,rtol=1e-12)
