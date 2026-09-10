import numpy as np
import pandas as pd

from engine.transformers.sec_shares import parse_share_observations,align_disclosed_shares


def test_common_share_count_is_unavailable_until_filed_and_changes_units_on_split():
    payload={'cik':1,'facts':{'dei':{'EntityCommonStockSharesOutstanding':{'units':{'shares':[
        {'end':'2020-01-01','filed':'2020-01-03','val':10,'accn':'0000000001-20-000001','form':'10-Q'},
        {'end':'2020-01-04','filed':'2020-01-05','val':22,'accn':'0000000001-20-000002','form':'10-Q'},
        {'end':'2019-01-01','filed':'2020-01-06','val':99,'accn':'0000000001-20-000003','form':'10-Q/A'},
        {'end':'2020-02-01','filed':'2020-02-03','val':999,'accn':'0000000001-20-000004','form':'10-Q'},
    ]}}}}}
    observations=parse_share_observations(payload,security_id='SEC_US_TEST',source_sha256='source',as_of='2020-01-06')
    assert observations.shares.tolist()==[10,22]
    prices=pd.DataFrame({'security_id':['SEC_US_TEST']*6,'trade_date':pd.date_range('2020-01-01',periods=6),
                         'close':[100,100,100,50,50,50],'split_adjustment_factor':[.5,.5,.5,1,1,1]})
    actual=align_disclosed_shares(prices,observations)
    assert actual.shares.iloc[:2].isna().all()
    assert actual.shares.iloc[2:].tolist()==[10,20,22,22]
    assert actual.market_cap.iloc[2:].tolist()==[1000,1000,1100,1100]
    future_adjusted=prices.copy();future_adjusted.split_adjustment_factor/=4
    np.testing.assert_allclose(align_disclosed_shares(future_adjusted,observations).shares,actual.shares,equal_nan=True)


def test_weighted_average_eps_denominator_is_not_outstanding_stock():
    payload={'cik':1,'facts':{'us-gaap':{'WeightedAverageNumberOfDilutedSharesOutstanding':{'units':{'shares':[
        {'end':'2020-01-01','filed':'2020-01-03','val':100,'accn':'1','form':'10-K'}]}}}}}
    assert parse_share_observations(payload,security_id='SEC_US_TEST',source_sha256='source',as_of='2020-02-01').empty


def test_repeated_weekend_measurement_is_aligned_without_duplicate_index():
    observations=pd.DataFrame({'security_id':['SEC_US_TEST']*2,
        'trade_date':pd.to_datetime(['2020-02-03','2020-02-04']),
        'value_date':pd.to_datetime(['2020-02-01','2020-02-01']),'shares':[100.,100.]})
    prices=pd.DataFrame({'security_id':['SEC_US_TEST']*3,
        'trade_date':pd.to_datetime(['2020-01-31','2020-02-03','2020-02-04']),
        'close':[20.,10.,11.],'split_adjustment_factor':[.5,1.,1.]})
    result=align_disclosed_shares(prices,observations)
    assert result.shares.iloc[1:].tolist()==[200.,200.]
    assert result.market_cap.iloc[1:].tolist()==[2000.,2200.]
