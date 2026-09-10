import numpy as np
import pandas as pd


def test_unresolved_move_affects_quality_only_when_held_and_does_not_change_returns():
    from scripts.research_cross_market_top70_search import MarketData,Segment
    data=MarketData.__new__(MarketData)
    dates=pd.bdate_range('2020-01-02',periods=20)
    flags=np.array([True,False])
    clear=np.zeros(2,dtype=bool)
    relative=np.column_stack([np.linspace(1,1.2,20),np.linspace(1,1.1,20)])
    segment=Segment(dates[0]-pd.Timedelta(days=1),dates[0],np.array(['A','B']),
        pd.DataFrame({'f':[100.,0.]},index=['A','B']),pd.DataFrame(index=['A','B']),
        dates,relative,clear,clear,clear,clear,flags)
    data.segments=[segment]
    held=data.evaluate({'f':1.},positions=1,keep=True)
    assert held['holdings'][0]['selected']==['A']
    assert not held['returns_verified']
    assert held['execution_issues'][0]['reason']=='unresolved_observed_price_action'
    segment.unresolved_price_event=clear
    resolved=data.evaluate({'f':1.},positions=1,keep=True)
    pd.testing.assert_series_equal(held['returns'],resolved['returns'])
    assert resolved['returns_verified']
    segment.unresolved_price_event=flags
    segment.scores['f']=[0.,100.]
    unheld=data.evaluate({'f':1.},positions=1,keep=True)
    assert unheld['holdings'][0]['selected']==['B'] and unheld['returns_verified']


def test_price_history_flags_follow_signal_lookback_without_changing_trades():
    from scripts.research_cross_market_top70_search import MarketData,Segment
    from scripts.research_cross_market_signal_quality import price_review_age_rows
    frame=pd.DataFrame({'listing_episode':[0,0,0,1,1],
                        'unresolved_price_event':[False,True,False,False,True]})
    age=price_review_age_rows(frame)
    np.testing.assert_allclose(age,[np.nan,0,1,np.nan,0],equal_nan=True)
    data=MarketData.__new__(MarketData)
    dates=pd.bdate_range('2020-01-02',periods=20)
    clear=np.zeros(2,dtype=bool)
    segment=Segment(dates[0]-pd.Timedelta(days=1),dates[0],np.array(['A','B']),
        pd.DataFrame({'tr_12_1':[100.,0.],'quality':[100.,0.]},index=['A','B']),
        pd.DataFrame({'tr_12_1':[1.,1.]},index=['A','B']),dates,
        np.column_stack([np.linspace(1,1.2,20),np.linspace(1,1.1,20)]),
        clear,clear,clear,clear,clear,np.array([70.,np.nan]))
    data.segments=[segment]
    dirty=data.evaluate({'tr_12_1':1.},positions=1,keep=True)
    assert not dirty['returns_verified']
    assert dirty['execution_issues'][0]['reason']=='unverified_price_history_for_signal'
    clean=data.evaluate({'quality':1.},positions=1,keep=True)
    assert clean['returns_verified']
    pd.testing.assert_series_equal(dirty['returns'],clean['returns'])
    assert dirty['holdings'][0]['selected']==clean['holdings'][0]['selected']==['A']
    assert not data.evaluate({'quality':1.},positions=1,gate='positive_momentum')['returns_verified']
    segment.price_review_age=np.array([300.,np.nan])
    assert data.evaluate({'tr_12_1':1.},positions=1)['returns_verified']
    segment.scores['economic_profit_yield']=[100.,0.]
    segment.beta_source_review=np.array([True,False])
    dirty_beta=data.evaluate({'economic_profit_yield':1.},positions=1,keep=True)
    assert dirty_beta['execution_issues'][0]['reason']=='unverified_weekly_beta_price_input'
    pd.testing.assert_series_equal(dirty_beta['returns'],clean['returns'])
    assert data.evaluate({'quality':1.},positions=1)['returns_verified']


def test_beta_history_detects_nontraded_unit_quotes_and_propagates_dependencies():
    from scripts.research_cross_market_signal_quality import beta_price_source_review,uses_beta_history,technical_lookback
    f=pd.DataFrame({'split_adj_close':[1.,1.,8.,1.,1.], 'close':[1.,1.,8.,1.,1.],
                    'volume':[100,0,0,100,100], 'listing_episode':[0]*5,
                    'unresolved_price_event':[False]*5})
    assert beta_price_source_review(f).tolist()==[False,False,True,True,True]
    f.split_adj_close=1.;f.close=1.;f.listing_episode=[0,0,0,1,1]
    assert beta_price_source_review(f).tolist()==[False,False,False,True,True]
    assert uses_beta_history(['economic_profit_yield'])
    assert uses_beta_history(['intangible_adjusted_pvgo_gap_pct'])
    assert not uses_beta_history(['style_total_score'])  # Current native styles contain no beta/WACC.
    assert not uses_beta_history(['roa','tr_12_1'])
    assert technical_lookback(['style_total_score'])>=253


def test_positive_equity_conditions_keep_raw_inputs_and_population():
    from scripts.research_cross_market_signal_quality import economic_validity
    f=pd.DataFrame({'roe':[20.,30.,40.,50.],'avg_parent_equity':[10.,10.,-10.,np.nan],
                    'ceq':[8.,-8.,8.,8.],'debt_to_equity':[2.,-2.,3.,4.],
                    'seq':[5.,-5.,6.,7.]},index=['A','B','C','D'])
    raw=f.copy(deep=True)
    clean,counts=economic_validity(f)
    assert clean.index.equals(raw.index)
    pd.testing.assert_frame_equal(f,raw)
    assert clean.roe.notna().tolist()==[True,False,False,False]
    assert clean.debt_to_equity.notna().tolist()==[True,False,True,True]
    assert counts=={'roe':3,'debt_to_equity':1}
