import pandas as pd

from engine.transformers.filing_periods import attach_report_metadata
from engine.transformers._internal.factor_metrics import _strictly_disclosed_financial_rows


def test_dividend_publication_keeps_financial_publication_date(monkeypatch):
    import engine.transformers._internal.factor_metrics as metrics
    events=pd.DataFrame([{'report_date':pd.Timestamp('2020-04-01'),'bsns_year':2019,
                         'report_name':'Annual dividend','annual_dividend_per_share':100.,
                         'payout_ratio':30.,'total_dividend_amount':1000.}])
    monkeypatch.setattr(metrics,'silver_dividend_asof_events',lambda symbol:events)
    daily=pd.DataFrame({'trade_date':pd.to_datetime(['2020-03-31','2020-04-01']),
                        'report_date':pd.to_datetime(['2020-03-15']*2),'close':[10000.,10000.]})
    result=metrics.add_kr_dividend_factors(daily,'000001')
    assert result.report_date.eq(pd.Timestamp('2020-03-15')).all()
    assert pd.isna(result.dividend_report_date.iloc[0])
    assert result.dividend_report_date.iloc[1]==pd.Timestamp('2020-04-01')


def test_noncalendar_fiscal_year_uses_actual_period_end_without_early_availability(tmp_path):
    path=tmp_path/'metadata.csv'
    pd.DataFrame([{'stock_code':'AAPL','fiscal_year':2019,'fiscal_month':12,
                   'period_end_date':'2019-09-28','report_date':'2019-10-31',
                   'rcept_no':'0000320193-19-000119','source_type':'statement'}]).to_csv(path,index=False)
    snapshot=pd.DataFrame([{'stock_code':'AAPL','fiscal_year':2019,'fiscal_month':12,
                            'financial_period':'2019-12-31','TOTAL_ASSETS':100.}])
    frame=attach_report_metadata(snapshot,path,fallback_to_period_end=False)
    assert frame.financial_period.iloc[0]==pd.Timestamp('2019-09-28')
    assert frame.report_date.iloc[0]==pd.Timestamp('2019-10-31')
    assert len(_strictly_disclosed_financial_rows(frame))==1
    days=pd.DataFrame({'trade_date':pd.to_datetime(['2019-09-30','2019-10-30','2019-10-31'])})
    joined=pd.merge_asof(days,frame,left_on='trade_date',right_on='report_date')
    assert joined.TOTAL_ASSETS.iloc[:2].isna().all()
    assert joined.TOTAL_ASSETS.iloc[2]==100.
