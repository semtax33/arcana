import hashlib
import json

import pandas as pd


def test_signal_day_stock_class_and_listing_status_precede_size_rank(tmp_path,monkeypatch):
    from engine.core.paths import DataLakePaths
    import scripts.research_cross_market_corrected_cache as research
    out=tmp_path/'research';out.mkdir()
    lake=DataLakePaths(tmp_path/'lake')
    monkeypatch.setattr(research,'OUT',out);monkeypatch.setattr(research,'DATA_LAKE',lake)
    rows=[]
    for day in ['2020-03-31','2020-06-30']:
        for symbol,cap in [('ETF',10000),('PREF',9000),('A',100),('B',90),('C',80),('D',70)]:
            rows.append({'trade_date':pd.Timestamp(day),'security_id':'SEC_US_'+symbol,'market_cap':cap})
        listing=pd.DataFrame({'symbol':['ETF','PREF','A','B','C','D'],
            'name':['Equity ETF','Series A Preferred Shares','Company A','Company B','Company C','Company D'],
            'assetType':['ETF','Stock','Stock','Stock','Stock','Stock'],
            'status':['Active','Active','Active' if day=='2020-03-31' else 'Delisted','Active','Active','Active']})
        path=lake.bronze('alpha-vantage','listings',f'snapshot_date={day}','active.csv')
        path.parent.mkdir(parents=True);listing.to_csv(path,index=False)
        path.with_suffix('.metadata.json').write_text(json.dumps({'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
    pd.DataFrame(rows).to_parquet(out/'US_universe.parquet')
    research.apply_us_listing_universe()
    actual=pd.read_parquet(out/'US_universe.parquet')
    first=actual[actual.trade_date.eq('2020-03-31')]
    assert first.size_count.unique().tolist()==[4]
    assert set(first.loc[first.eligible,'security_id'])=={'SEC_US_A','SEC_US_B','SEC_US_C'}
    second=actual[actual.trade_date.eq('2020-06-30')]
    assert second.size_count.unique().tolist()==[3] and second.eligible.all()
    assert 'SEC_US_A' not in set(second.security_id)
    assert len(pd.read_parquet(out/'US_cap_observations.parquet'))==12
