from datetime import date, datetime
import os
import uuid

import pytest
from pydantic import ValidationError
from api.model.universe import UniverseFiltersDto, normalize_universe
from api.repository.universe_query import build_universe_ctes
from api.repository.factor_lab_query import compile_factor_lab_graph
from api.repository.factor_screen_query import build_factor_screen_query, FactorCondition
from api.repository.backtest_query import build_factor_raw_batch_query, build_factor_snapshot_batch_query, build_factor_snapshot_query
from engine.core.exchanges import normalize_exchange
from test_factor_lab_v2_clickhouse import isolated_lab


@pytest.mark.parametrize("value", [
    {"market_cap_min_mil": 11, "market_cap_max_mil": 10},
    {"market_cap_min_mil": float("nan")}, {"market_cap_max_mil": float("inf")},
    {"size_percentile": {"side": "top", "percent": 0}},
    {"size_percentile": {"side": "bottom", "percent": 101}},
])
def test_invalid_size_constraints(value):
    with pytest.raises(ValidationError):
        UniverseFiltersDto(**value)


def test_country_and_exchange_validation():
    with pytest.raises(ValueError):
        normalize_universe({"exchange_codes": ["KOSDAQ"]}, "US")
    with pytest.raises(ValueError):
        normalize_universe({"market_cap_min_mil": 1}, "ALL")
    assert normalize_exchange("US", "US") == ""
    assert normalize_exchange("Z", "US") == "OTHER"
    assert normalize_exchange("A", "US") == "NYSE_AMERICAN"


@pytest.fixture
def universe_db():
    if os.getenv("ARCANA_TEST_CLICKHOUSE") != "1":
        pytest.skip("set ARCANA_TEST_CLICKHOUSE=1 for isolated universe SQL tests")
    from api.config.clickhouse import get_clickhouse_client
    client = get_clickhouse_client()
    prefix = "uvtest_" + uuid.uuid4().hex
    tables = {key: prefix + "_" + key for key in ["securities", "issuers", "identifiers", "factors", "prices", "catalog"]}
    schemas = {
        "securities": "security_id String, issuer_id String, country String, exchange_code String, is_active Bool DEFAULT true, updated_at DateTime DEFAULT now()",
        "issuers": "issuer_id String, sector_code String, industry_group_code String, industry_group_name String DEFAULT '', legal_name_ko String DEFAULT '', legal_name_en String DEFAULT '', updated_at DateTime DEFAULT now()",
        "identifiers": "security_id String, id_value String, id_type String, is_primary Bool DEFAULT true",
        "factors": "security_id String, trade_date Date, factor_id String, financial_basis String, factor_value Nullable(Float64), currency String, source_trade_date Date, updated_at DateTime DEFAULT now()",
        "prices": "security_id String, trade_date Date",
        "catalog": "factor_id String, factor_name String, value_direction String, is_active Bool DEFAULT true",
    }
    for key, schema in schemas.items():
        client.command(f"CREATE TEMPORARY TABLE {tables[key]} ({schema}) ENGINE=Memory")
    client.insert(tables["securities"], [[s,s,"KR","KOSPI" if s=="D" else "KOSDAQ"] for s in "ABCDEF"], column_names=["security_id","issuer_id","country","exchange_code"])
    client.insert(tables["issuers"], [[s,"45","4510"] for s in "ABCDEF"], column_names=["issuer_id","sector_code","industry_group_code"])
    client.insert(tables["identifiers"], [[s,s,"TICKER"] for s in "ABCDEF"], column_names=["security_id","id_value","id_type"])
    client.insert(tables["catalog"], [["roe","ROE","HIGHER_BETTER"]], column_names=["factor_id","factor_name","value_direction"])
    rows = []
    for day, caps in [(2, [100,100,300,10000,None,0]), (5,[400,100,300,10000,None,0])]:
        d = date(2026,1,day)
        for s, cap in zip("ABCDEF",caps):
            rows.append([s,d,"mcap_mil","annual",cap,"KRW",d])
            # C has cap data but no ROE: it must still count in size percentiles.
            if s != "C" and not (day==5 and s=="B"):
                rows.append([s,d,"roe","annual",{"A":10,"B":20,"D":1000,"E":900,"F":800}[s],"KRW",d])
        client.insert(tables["prices"], [[s,d] for s in "ABCDEF"], column_names=["security_id","trade_date"])
    # An alternative basis must not override valid annual market cap.
    rows.append(["A",date(2026,1,2),"mcap_mil","ttm",999999,"KRW",date(2026,1,2)])
    client.insert(tables["factors"], rows, column_names=["security_id","trade_date","factor_id","financial_basis","factor_value","currency","source_trade_date"])
    try:
        yield client, tables
    finally:
        client.close()


def eligible(client, t, filters, day="2026-01-02"):
    ctes, params = build_universe_ctes(dates_sql="SELECT {day:Date} AS trade_date", universe=filters,
        market="KR", security_table=t["securities"], issuer_table=t["issuers"], cap_table=t["factors"])
    params["day"] = day
    return [r[0] for r in client.query("WITH " + ",\n".join(ctes) + " SELECT security_id FROM uv_eligible ORDER BY security_id", parameters=params).result_rows]


def test_size_population_basis_ties_and_combination(universe_db):
    client,t=universe_db
    base={"exchange_codes":["KOSDAQ"]}
    assert eligible(client,t,{**base,"size_percentile":{"side":"top","percent":50}})==["A","C"]
    assert eligible(client,t,{**base,"size_percentile":{"side":"bottom","percent":50}})==["A","B"]
    assert eligible(client,t,{**base,"market_cap_min_mil":100,"market_cap_max_mil":100})==["A","B"]
    assert eligible(client,t,{**base,"size_percentile":{"side":"top","percent":50},"market_cap_max_mil":100})==["A"]
    assert eligible(client,t,base)==list("ABCEF")
    assert eligible(client,t,{**base,"market_cap_min_mil":300},"2026-01-05")==["A","C"]
    assert eligible(client,t,{**base,"size_percentile":{"side":"top","percent":1}})==["C"]
    assert eligible(client,t,{**base,"market_cap_min_mil":350},"2026-01-02")==[]
    assert eligible(client,t,{**base,"market_cap_min_mil":1},"2026-01-06")==[]


def test_latest_null_revision_and_currency_are_not_replaced_by_stale_cap(universe_db):
    c,t=universe_db
    c.insert(t["factors"], [
        ["B",date(2026,1,2),"mcap_mil","annual",None,"KRW",datetime(2099,1,1)],
        ["C",date(2026,1,2),"mcap_mil","annual",300,"USD",datetime(2099,1,1)],
    ], column_names=["security_id","trade_date","factor_id","financial_basis","factor_value","currency","updated_at"])
    assert eligible(c,t,{"exchange_codes":["KOSDAQ"],"market_cap_min_mil":1})==["A"]


def test_zscore_uses_only_eligible_stocks(universe_db):
    c,t=universe_db
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}},
             {"id":"z","type":"zscore","config":{"min_count":2}}],
            [{"source":"roe","target":"z","target_handle":"input"}],"z")
    rows=lab_values(c,t,g)
    assert {r["security_id"] for r in rows}=={"A","B"}
    assert sum(r["value"] for r in rows)==pytest.approx(0)
    assert min(r["value"] for r in rows)<0<max(r["value"] for r in rows)


def test_date_fallback_checks_primary_availability_inside_universe(universe_db):
    c,t=universe_db
    c.insert(t["factors"], [["D",date(2026,1,2),"primary_only","annual",1,"KRW",date(2026,1,2)]],
        column_names=["security_id","trade_date","factor_id","financial_basis","factor_value","currency","source_trade_date"])
    g=graph([{"id":"p","type":"factor_input","config":{"factor_id":"primary_only"}},
             {"id":"f","type":"factor_input","config":{"factor_id":"roe"}},
             {"id":"choice","type":"date_fallback","config":{}}],
            [{"source":"p","target":"choice","target_handle":"primary"},
             {"source":"f","target":"choice","target_handle":"fallback"}],"choice")
    rows=lab_values(c,t,g)
    assert {r["security_id"] for r in rows}=={"A","B"}


def test_blended_sources_are_recomputed_with_the_frozen_parent_universe():
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from api.service.factor_lab_service import FactorLabService
    from api.service.dto import FactorLabBacktestRequestDto
    source=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}}],[],"roe")
    original=deepcopy(source)
    parent=deepcopy(source)
    parent["experiment"]["universe"]={"exchange_codes":["KOSDAQ"],"market_cap_min_mil":200}
    service=FactorLabService(client_factory=Mock)
    request=FactorLabBacktestRequestDto(start_date="2026-01-06",end_date="2026-01-07")
    with patch("api.service.factor_lab_service._load_frozen_graph",return_value=source), patch.object(service,"run_graph",return_value=SimpleNamespace(run_id="new-run")) as run:
        assert service._rerun_blend_source("source",parent,request)=="new-run"
    submitted=run.call_args.args[0]
    assert submitted.mode=="history"
    assert submitted.graph.experiment.universe.market_cap_min_mil==200
    assert submitted.graph.experiment.market=="KR"
    assert submitted.history_start_date==date(2026,1,6)
    assert source==original


def graph(nodes, edges, final):
    return {"version":2,"experiment":{"market":"KR","start_date":"2026-01-02","end_date":"2026-01-05",
        "universe":{"exchange_codes":["KOSDAQ"],"market_cap_min_mil":100,"market_cap_max_mil":300}},
        "nodes":nodes,"edges":edges,"outputs":{"final_node_id":final}}


def lab_values(client,t,g):
    compiled=compile_factor_lab_graph(g,factor_table=t["factors"],cap_table=t["factors"],security_table=t["securities"],issuer_table=t["issuers"],price_table=t["prices"])
    return client.query_df(compiled.query,parameters=compiled.parameters).to_dict("records")


def test_graph_ranks_only_eligible_stocks(universe_db):
    c,t=universe_db
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}},
             {"id":"rank","type":"rank","config":{"direction":"desc"}}],
            [{"source":"roe","target":"rank","target_handle":"input"}],"rank")
    rows=lab_values(c,t,g)
    assert {r["security_id"] for r in rows}=={"A","B"}
    assert sorted(r["value"] for r in rows)==[1,2]


def test_lag_keeps_history_before_entering_universe(universe_db):
    c,t=universe_db
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}},
             {"id":"lag","type":"lag","version":2,"config":{"period":1,"unit":"row"}}],
            [{"source":"roe","target":"lag","target_handle":"input"}],"lag")
    g["experiment"]["universe"]={"exchange_codes":["KOSDAQ"],"market_cap_min_mil":350}
    rows=lab_values(c,t,g)
    assert len(rows)==1 and rows[0]["security_id"]=="A" and rows[0]["value"]==10


@pytest.mark.parametrize("unit", ["row", "trading_day"])
def test_rank_then_lag_preserves_excluded_date_on_reentry(universe_db, unit):
    c,t=universe_db
    day=date(2026,1,6)
    c.insert(t["prices"], [["A",day]],column_names=["security_id","trade_date"])
    c.insert(t["factors"], [["A",day,f,"annual",v,"KRW",day] for f,v in [("roe",30),("mcap_mil",100)]],
        column_names=["security_id","trade_date","factor_id","financial_basis","factor_value","currency","source_trade_date"])
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}},
             {"id":"rank","type":"rank","config":{"direction":"desc"}},
             {"id":"lag","type":"lag","version":2,"config":{"period":1,"unit":unit}}],
            [{"source":"roe","target":"rank","target_handle":"input"},
             {"source":"rank","target":"lag","target_handle":"input"}],"lag")
    g["experiment"]["end_date"]="2026-01-06"
    rows=lab_values(c,t,g)
    # A is excluded on Jan 5; Jan 6 must not reuse its valid Jan 2 rank.
    assert not any(r["security_id"]=="A" and str(r["trade_date"])[:10]=="2026-01-06" for r in rows)


@pytest.mark.parametrize("builder", [build_factor_raw_batch_query, build_factor_snapshot_batch_query, build_factor_snapshot_query, build_factor_screen_query])
def test_screen_and_backtest_use_same_universe_before_ranking(universe_db,builder):
    c,t=universe_db
    kwargs={"market":"KR","universe":{"exchange_codes":["KOSDAQ"],"market_cap_max_mil":100},
        "factor_table":t["factors"],"cap_table":t["factors"],"security_table":t["securities"],
        "issuer_table":t["issuers"],"identifier_table":t["identifiers"],"catalog_table":t["catalog"]}
    if "batch" in builder.__name__: kwargs["signal_dates"]=["2026-01-02"]
    elif "screen" in builder.__name__: kwargs["as_of_date"]="2026-01-02"
    else: kwargs["signal_date"]="2026-01-02"
    query,params=builder([FactorCondition.top("roe",50)],**kwargs)
    if "screen" in builder.__name__: params["market_security_prefix"] = ""
    rows=c.query_df(query,parameters=params).to_dict("records")
    assert {r["security_id"] for r in rows}.issubset({"A","B"})
    assert any(r["security_id"]=="B" for r in rows)


def test_exact_lab_signal_does_not_carry_previous_score(universe_db):
    c,t=universe_db
    query,params=build_factor_raw_batch_query([FactorCondition.top("roe",100)],
        signal_dates=["2026-01-02","2026-01-05"],market="KR",
        universe={"exchange_codes":["KOSDAQ"],"market_cap_max_mil":100},exact_signal_values=True,
        factor_table=t["factors"],cap_table=t["factors"],security_table=t["securities"],
        issuer_table=t["issuers"],identifier_table=t["identifiers"],catalog_table=t["catalog"])
    rows=c.query_df(query,parameters=params).to_dict("records")
    assert rows and all(str(r["signal_date"])[:10]=="2026-01-02" for r in rows)


def test_median_uses_eligible_cross_section(universe_db):
    c,t=universe_db
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe", "missing_policy":"cross_sectional_median"}}],[],"roe")
    rows=lab_values(c,t,g)
    missing = [r for r in rows if r["security_id"] == "C" and str(r["trade_date"])[:10] == "2026-01-02"]
    assert len(missing) == 1 and missing[0]["value"] in (10,20)
    assert missing[0]["value"] < 100  # Ineligible E/F/D must not inflate the median.


def test_empty_additions_preserve_old_graph_hash():
    from copy import deepcopy
    from api.repository.factor_lab_query import validate_factor_lab_graph
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe"}}],[],"roe")
    g["experiment"]["universe"]={"type":"market","sector_codes":[],"industry_group_codes":[]}
    old=validate_factor_lab_graph(g).graph_hash
    new=deepcopy(g)
    new["experiment"]["universe"].update(UniverseFiltersDto().model_dump())
    assert validate_factor_lab_graph(new).graph_hash==old
    new["experiment"]["universe"]["market_cap_min_mil"]=1
    assert validate_factor_lab_graph(new).graph_hash!=old


@pytest.mark.parametrize("minimum", [100, 1000])
def test_filtered_run_freezes_settings_and_returns_metadata(isolated_lab, minimum):
    from api.service.factor_lab_service import FactorLabService
    from api.service.dto import FactorLabRunRequestDto, FactorLabGraphDto, FactorLabBacktestRequestDto
    factory=isolated_lab
    c=factory()
    try:
        c.command("ALTER TABLE security_master UPDATE exchange_code = 'NASDAQ' WHERE security_id = 'SEC_US_A' SETTINGS mutations_sync=1")
        c.command("CREATE TABLE benchmark_price_daily AS arcana.benchmark_price_daily")
        c.command("ALTER TABLE price_daily UPDATE close = 100, adj_close = 100 WHERE security_id = 'SEC_US_A' SETTINGS mutations_sync=1")
        for day in [2,5,6,7]:
            c.insert("fact_daily_factors", [["SEC_US_A",date(2026,1,day),"mcap_mil","annual",200,"USD"]],
                column_names=["security_id","trade_date","factor_id","financial_basis","factor_value","currency"])
    finally: c.close()
    g=graph([{"id":"roe","type":"factor_input","config":{"factor_id":"roe","financial_basis":"annual"}}],[],"roe")
    g["experiment"].update(market="US",end_date="2026-01-07",factor_data_mode="point_in_time_snapshot",
        universe={"exchange_codes":["NASDAQ"],"market_cap_min_mil":minimum})
    service=FactorLabService(client_factory=factory)
    run=service.run_graph(FactorLabRunRequestDto(graph=FactorLabGraphDto(**g),mode="history",
        history_start_date="2026-01-06",history_end_date="2026-01-07",history_rebalance_frequency="monthly"))
    if minimum==100:
        assert run.rows and run.rows[0].exchange_code=="NASDAQ" and run.rows[0].market_cap==200
    else:
        assert not run.rows
    assert run.universe_summary["dates"][0]["after_count"]==(1 if minimum==100 else 0)
    assert service.get_run(run.run_id).universe_summary==run.universe_summary
    result=service.run_backtest(run.run_id,FactorLabBacktestRequestDto(start_date="2026-01-06",
        end_date="2026-01-07",rebalance_frequency="monthly",market="KR",benchmarks=[]))
    assert result.universe_summary["market"]=="US"
    if minimum==100:
        assert result.rebalance_history[0].positions[0].security_id=="SEC_US_A"
    else:
        assert all(not r.positions for r in result.rebalance_history)
        assert all(p.strategy_nav==1 for p in result.equity_curve)
