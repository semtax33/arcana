"""Lifecycle behavior through the actual FactorLab backtest service and test DB."""
from datetime import date
from uuid import uuid4

import pytest

from api.config.clickhouse import get_clickhouse_client
from api.service.backtest_service import BacktestService
from api.service.dto import FactorBacktestRequestDto, FactorConditionDto

pytestmark = pytest.mark.integration


@pytest.fixture
def lifecycle_database():
    # Session-local Memory tables shadow production names without writing there.
    client = get_clickhouse_client(connect_timeout=2,
        session_id="arcana_test_survivorship_" + uuid4().hex)
    schemas = {
        "security_master": "security_id String, issuer_id String, country String, is_active Bool, exchange_code String DEFAULT '', updated_at UInt64 DEFAULT 1",
        "issuers": "issuer_id String, legal_name_en String, legal_name_ko String DEFAULT '', sector_code String DEFAULT '', industry_group_code String DEFAULT '', updated_at UInt64 DEFAULT 1",
        "identifiers": "security_id String, id_type String, id_value String, is_primary Bool",
        "factor_catalog": "factor_id String, factor_name String, value_direction String, is_active Bool DEFAULT true",
        "fact_daily_factors": "security_id String, trade_date Date, factor_id String, factor_value Float64, financial_basis String DEFAULT 'annual', updated_at UInt64 DEFAULT 1, currency String DEFAULT 'KRW'",
        "benchmark_price_daily": "benchmark_id String, trade_date Date, close Float64",
        "price_daily": "security_id String, trade_date Date, close Float64, adj_close Float64, volume UInt64, currency String, updated_at UInt64 DEFAULT 1",
        "security_lifecycle_events": """event_id String, security_id String, event_type String,
            effective_date Date, cash_per_share Nullable(Float64), cash_payment_date Nullable(Date),
            currency String, status String, published_date Date, source_url String, source_sha256 String,
            entitlements_complete Bool""",
        "security_lifecycle_entitlements": """event_id String, component_id String,
            component_type String, recipient_security_id String, units_per_share Float64,
            delivery_date Nullable(Date), currency String""",
        "security_trading_halts": """halt_id String, security_id String,
            start_date Date, end_date Nullable(Date), status String,
            published_date Date, source_url String, source_sha256 String""",
    }
    created = []
    try:
        for table, schema in schemas.items():
            client.command(f"CREATE TEMPORARY TABLE {table} ({schema}) ENGINE = Memory")
            created.append(table)
        yield client, lambda: client
    finally:
        client.command("DROP TEMPORARY TABLE IF EXISTS security_listing_episodes")
        for table in reversed(created):
            client.command(f"DROP TEMPORARY TABLE {table}")
        client.close()


def test_confirmed_halt_blocks_sale_and_ignores_positive_volume_vendor_quotes_until_resumption(lifecycle_database):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_KR_OLD", "OLD", "KR", True), ("SEC_KR_NEW", "NEW", "KR", True)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [
        ("SEC_KR_OLD", date(2026, 1, 2), "roe", 10.), ("SEC_KR_NEW", date(2026, 1, 2), "roe", 1.),
        ("SEC_KR_OLD", date(2026, 1, 30), "roe", 1.), ("SEC_KR_NEW", date(2026, 1, 30), "roe", 10.),
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 30), date(2026, 2, 2),
            date(2026, 2, 3), date(2026, 2, 4), date(2026, 2, 5)]
    old = [100., 100., 100., 500., 900., 110., 110.]
    new = [100., 100., 100., 100., 200., 300., 300.]
    client.insert("price_daily", [(sid, day, price, price, 100, "KRW")
        for sid, values in (("SEC_KR_OLD", old), ("SEC_KR_NEW", new)) for day, price in zip(days, values)],
        column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_trading_halts", [(
        "synthetic-exchange-halt", "SEC_KR_OLD", date(2026, 2, 2), date(2026, 2, 4),
        "confirmed", date(2026, 1, 30), "https://example.test/halt-and-resumption", "a" * 64,
    )])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 2, 5), rebalance_frequency="monthly",
        market="kr", max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors",
    )
    result = BacktestService(client_factory=factory).run_factor_backtest(request)
    # Confirmed cessation overrides spurious positive-volume rows. The held
    # share cannot finance NEW at the February rebalance; it resumes at 110.
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx([1., 1., 1., 1., 1.1, 1.1])
    assert all(state["cash"] == 0 for state in result.raw["portfolio_history"])
    assert {p["security_id"] for p in result.raw["portfolio_history"][-1]["positions"]} == {"SEC_KR_OLD"}


@pytest.mark.parametrize(("event_type", "cash_amount", "expected_nav", "complete"), [
    ("cash_merger", 120.0, [1, 1.2, 1.2], True),
    ("cash_exchange", 120.0, [1, 1.2, 1.2], True),
    ("cancellation", 0.0, [1, 0, 0], True),
    ("cash_merger", 120.0, None, False),
])
def test_terminal_event_uses_entitlement_instead_of_post_delisting_vendor_price(
        lifecycle_database, event_type, cash_amount, expected_nav, complete):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_US_OLD", "OLD", "US", False)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("issuers", [("OLD", "Historical company")],
                  column_names=["issuer_id", "legal_name_en"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [("SEC_US_OLD", date(2026, 1, 2), "roe", 10.0)],
                  column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    client.insert("price_daily", [
        ("SEC_US_OLD", date(2026, 1, 2), 100.0, 100.0, 100, "USD"),
        ("SEC_US_OLD", date(2026, 1, 5), 100.0, 100.0, 100, "USD"),
        # Vendor rows after the actual merger are not executable old shares.
        ("SEC_US_OLD", date(2026, 1, 6), 500.0, 500.0, 1, "USD"),
        ("SEC_US_OLD", date(2026, 1, 7), 500.0, 500.0, 1, "USD"),
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [(
        "synthetic-terminal-event", "SEC_US_OLD", event_type, date(2026, 1, 6),
        cash_amount, date(2026, 1, 6), "USD", "confirmed", date(2026, 1, 2),
        "https://example.test/confirmed-terminal-event", "a" * 64, complete,
    )])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 7),
        rebalance_frequency="monthly", market="us", max_positions=1,
        transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors",
    )

    if not complete:
        with pytest.raises(ValueError, match="entitlements.*complete"):
            BacktestService(client_factory=factory).run_factor_backtest(request)
        return
    result = BacktestService(client_factory=factory).run_factor_backtest(request)

    # A $120 offer pays $1.20 per original dollar; explicit no-consideration
    # cancellation pays nothing. Neither uses the vendor's post-event $500.
    assert [point.strategy_nav for point in result.equity_curve] == pytest.approx(expected_nav)


@pytest.mark.parametrize("exit_is_tradeable", [True, False])
def test_unknown_delisting_rights_matter_only_while_the_position_remains_owned(lifecycle_database, exit_is_tradeable):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_US_OLD", "OLD", "US", False), ("SEC_US_NEW", "NEW", "US", True)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [
        ("SEC_US_OLD", date(2026, 1, 2), "roe", 10.), ("SEC_US_NEW", date(2026, 1, 2), "roe", 1.),
        ("SEC_US_OLD", date(2026, 1, 30), "roe", 1.), ("SEC_US_NEW", date(2026, 1, 30), "roe", 10.),
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3)]
    client.insert("price_daily", [
        (sid, day, 110. if sid == "SEC_US_NEW" and day == days[-1] else 100.,
         110. if sid == "SEC_US_NEW" and day == days[-1] else 100.,
         0 if sid == "SEC_US_OLD" and day == date(2026, 2, 2) and not exit_is_tradeable else 100, "USD")
        for sid in ["SEC_US_OLD", "SEC_US_NEW"] for day in days
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [(
        "unknown-delisting", "SEC_US_OLD", "delisting", date(2026, 2, 3), None, None,
        "USD", "confirmed", date(2026, 1, 2), "https://example.test/delisting", "a" * 64, False)])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 2, 3), rebalance_frequency="monthly",
        market="us", max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors")
    if not exit_is_tradeable:
        with pytest.raises(ValueError, match="entitlements.*complete"):
            BacktestService(client_factory=factory).run_factor_backtest(request)
        return
    result = BacktestService(client_factory=factory).run_factor_backtest(request)
    assert result.equity_curve[-1].strategy_nav == pytest.approx(1.1)
    assert {p["security_id"] for p in result.raw["portfolio_history"][-1]["positions"]} == {"SEC_US_NEW"}


def test_unpaid_merger_cash_is_not_reinvested_at_next_rebalance(lifecycle_database):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_US_OLD", "OLD", "US", False), ("SEC_US_NEW", "NEW", "US", True)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [
        ("SEC_US_OLD", date(2025, 12, 31), "roe", 10.0),
        ("SEC_US_NEW", date(2026, 1, 30), "roe", 20.0),
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    client.insert("price_daily", [
        ("SEC_US_OLD", date(2025, 12, 31), 100.0, 100.0, 100, "USD"),
        ("SEC_US_OLD", date(2026, 1, 2), 100.0, 100.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 1, 15), 100.0, 100.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 1, 30), 100.0, 100.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 2, 2), 100.0, 100.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 2, 3), 200.0, 200.0, 100, "USD"),
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [(
        "synthetic-delayed-cash", "SEC_US_OLD", "cash_merger", date(2026, 1, 15),
        120.0, date(2026, 2, 3), "USD", "confirmed", date(2025, 12, 31),
        "https://example.test/delayed-cash", "b" * 64, True,
    )])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 2), end_date=date(2026, 2, 3),
        rebalance_frequency="monthly", market="us", max_positions=1,
        transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors",
    )

    result = BacktestService(client_factory=factory).run_factor_backtest(request)

    # $1.20 is owed from Jan 15 but not spendable on Feb 2. NEW's doubling
    # cannot be earned using cash that is paid only on Feb 3.
    assert [p.strategy_nav for p in result.equity_curve] == pytest.approx([1, 1.2, 1.2, 1.2, 1.2])
    assert result.rebalance_history[-1].positions == []


def test_delivered_exchange_shares_cannot_fund_rebalance_before_their_tradable_date(lifecycle_database):
    client, factory = lifecycle_database
    client.command("DROP TEMPORARY TABLE security_lifecycle_entitlements")
    client.command("""CREATE TEMPORARY TABLE security_lifecycle_entitlements (
        event_id String, component_id String, component_type String, recipient_security_id String,
        units_per_share Float64, delivery_date Nullable(Date), currency String,
        tradable_date Nullable(Date)) ENGINE = Memory""")
    client.insert("security_master", [("SEC_US_"+sid, sid, "US", sid != "OLD") for sid in ["OLD", "RECIPIENT", "TARGET"]],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [
        ("SEC_US_OLD", date(2025, 12, 31), "roe", 10.),
        ("SEC_US_TARGET", date(2026, 1, 30), "roe", 20.)],
        column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    days = [date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 15), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3)]
    client.insert("price_daily", [
        ("SEC_US_"+sid, day, 200. if sid == "TARGET" and day == days[-1] else 100.,
         200. if sid == "TARGET" and day == days[-1] else 100., 100, "USD")
        for sid in ["OLD", "RECIPIENT", "TARGET"] for day in days],
        column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [(
        "delivered-but-restricted", "SEC_US_OLD", "share_exchange", date(2026, 1, 15), 0., None,
        "USD", "confirmed", date(2025, 12, 31), "https://example.test/confirmed-exchange", "d"*64, True)])
    client.insert("security_lifecycle_entitlements", [(
        "delivered-but-restricted", "common", "security", "SEC_US_RECIPIENT",
        1., date(2026, 2, 2), "USD", date(2026, 2, 3))])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 2), end_date=date(2026, 2, 3), rebalance_frequency="monthly",
        market="us", max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors")
    result = BacktestService(client_factory=factory).run_factor_backtest(request)
    assert result.equity_curve[-1].strategy_nav == pytest.approx(1.)
    states = {str(row["trade_date"]): row for row in result.raw["portfolio_history"]}
    assert states["2026-02-02"]["positions"] == []
    claim = states["2026-02-02"]["security_receivables"][0]
    assert str(claim["delivery_date"]) == "2026-02-02"
    assert str(claim["tradable_date"]) == "2026-02-03"
    assert states["2026-02-03"]["positions"][0]["security_id"] == "SEC_US_RECIPIENT"


@pytest.mark.parametrize("recipient_split", [False, True])
def test_share_exchange_values_received_shares_before_and_after_delivery(lifecycle_database, recipient_split):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_US_OLD", "OLD", "US", False), ("SEC_US_NEW", "NEW", "US", True)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [("SEC_US_OLD", date(2026, 1, 2), "roe", 10.0)],
                  column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    client.insert("price_daily", [
        ("SEC_US_OLD", date(2026, 1, 2), 100.0, 100.0, 100, "USD"),
        ("SEC_US_OLD", date(2026, 1, 5), 100.0, 100.0, 100, "USD"),
        ("SEC_US_OLD", date(2026, 1, 6), 500.0, 500.0, 1, "USD"),
        ("SEC_US_NEW", date(2026, 1, 6), 50.0, 25.0 if recipient_split else 50.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 1, 7), 30.0 if recipient_split else 60.0, 30.0 if recipient_split else 60.0, 100, "USD"),
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [(
        "synthetic-share-exchange", "SEC_US_OLD", "share_exchange", date(2026, 1, 6),
        0.0, None, "USD", "confirmed", date(2026, 1, 2),
        "https://example.test/share-exchange", "c" * 64, True,
    )])
    client.insert("security_lifecycle_entitlements", [(
        "synthetic-share-exchange", "new-common-shares", "security", "SEC_US_NEW",
        2.0, date(2026, 1, 7), "USD",
    )])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 7),
        rebalance_frequency="monthly", market="us", max_positions=1,
        transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors",
    )

    result = BacktestService(client_factory=factory).run_factor_backtest(request)

    # .01 OLD becomes .02 NEW. NEW is worth $50 then $60, so NAV is 1 then 1.2.
    assert [p.strategy_nav for p in result.equity_curve] == pytest.approx([1, 1, 1.2])
    states = result.raw["portfolio_history"]
    assert states[1]["positions"] == []
    assert states[1]["receivable_value"] == pytest.approx(1)
    assert states[2]["positions"][0]["security_id"] == "SEC_US_NEW"


@pytest.mark.parametrize("surface", ["backtest", "factorlab"])
@pytest.mark.parametrize("retrospective_confirmation", [False, True])
def test_historical_kr_listing_population_is_restored_before_top70_and_factor_ranking(lifecycle_database, surface, retrospective_confirmation):
    client, factory = lifecycle_database
    client.command("""CREATE TEMPORARY TABLE security_listing_episodes (
        episode_id String, security_id String, issuer_id String, country String,
        exchange_code String, security_type String, valid_from Date, valid_until Nullable(Date),
        status String, published_date Date, source_url String, source_sha256 String
    ) ENGINE = Memory""")
    names = ["HIST", "A", "B", "C", "FUTURE", "ENDED"]
    ids = {name: "SEC_KR_" + name for name in names}
    # HIST genuinely has no row in the current master.
    client.insert("security_master", [(ids[n], n, "KR", True) for n in names if n != "HIST"],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("security_listing_episodes", [(
        n + "-episode", ids[n], n, "KR", "KOSPI", "common_stock",
        date(2027, 1, 1) if n == "FUTURE" else date(2020, 1, 1),
        date(2026, 1, 5) if n == "ENDED" else None,
        "confirmed", date(2026, 3, 31) if retrospective_confirmation else date(2020, 1, 1),
        "https://example.test/dart-listing/" + n, "d" * 64,
    ) for n in names])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    caps = {"HIST": 200, "A": 100, "B": 90, "C": 80, "FUTURE": 10000, "ENDED": 9000}
    values = {"HIST": 1, "A": 5, "B": 4, "C": 100, "FUTURE": 1000, "ENDED": 999}
    client.insert("fact_daily_factors", [
        (ids[n], date(2026, 1, 30), factor, float(value))
        for n in names for factor, value in [("mcap_mil", caps[n]), ("roe", values[n])]
    ], column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    client.insert("price_daily", [
        (ids[n], day, 120.0 if n == "HIST" and day.day == 3 else 100.0,
         120.0 if n == "HIST" and day.day == 3 else 100.0, 100, "KRW")
        for n in names for day in [date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3)]
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 2, 2), end_date=date(2026, 2, 3),
        rebalance_frequency="monthly", market="kr", max_positions=6,
        universe={"size_percentile": {"side": "top", "percent": 70}},
        transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors",
    )

    if surface == "factorlab":
        from api.service.factor_lab_service import FactorLabService
        from api.service.dto import FactorLabGraphDto
        from scripts.research_cross_market_factorlab import recipe_graph
        graph = recipe_graph(
            {"id": "historical_listing_contract", "weights": {"roe": 1.0}, "gate": None},
            "KR", {"roe": {"direction": "higher"}}, {}, start="2026-01-30", end="2026-01-30",
        )
        compiled = FactorLabService(client_factory=factory).compile_graph(FactorLabGraphDto(**graph))
        scores = client.query_df(compiled.query, parameters=compiled.parameters)
        assert set(scores.loc[scores.is_valid, "security_id"]) == {ids["HIST"], ids["A"], ids["B"]}
        return
    result = BacktestService(client_factory=factory).run_factor_backtest(request)

    assert {p.security_id for p in result.rebalance_history[0].positions} == {ids["HIST"], ids["A"], ids["B"]}
    assert result.universe_summary["dates"][0]["before_count"] == 4
    assert result.universe_summary["listing_basis"] == "dated_with_current_fallback"
    assert result.universe_summary["dates"][0]["listing_verified_count"] == 4
    assert result.universe_summary["dates"][0]["listing_unverified_count"] == 0
    assert result.universe_summary["dates"][0]["after_count"] == 3
    assert result.equity_curve[-1].strategy_nav == pytest.approx(1.0666666666666667)


@pytest.mark.parametrize("first_delivery_day", [6, 8])
@pytest.mark.parametrize("second_effective_day", [6, 7])
def test_received_stock_later_merger_is_applied_even_when_it_was_not_a_factor_selection(lifecycle_database, first_delivery_day, second_effective_day):
    client, factory = lifecycle_database
    client.insert("security_master", [("SEC_US_OLD", "OLD", "US", False), ("SEC_US_NEW", "NEW", "US", False)],
                  column_names=["security_id", "issuer_id", "country", "is_active"])
    client.insert("factor_catalog", [("roe", "ROE", "HIGHER_BETTER")],
                  column_names=["factor_id", "factor_name", "value_direction"])
    client.insert("fact_daily_factors", [("SEC_US_OLD", date(2026, 1, 2), "roe", 10.0)],
                  column_names=["security_id", "trade_date", "factor_id", "factor_value"])
    client.insert("price_daily", [
        ("SEC_US_OLD", date(2026, 1, 2), 100.0, 100.0, 100, "USD"),
        ("SEC_US_OLD", date(2026, 1, 5), 100.0, 100.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 1, 6), 50.0, 50.0, 100, "USD"),
        ("SEC_US_NEW", date(2026, 1, 7), 500.0, 500.0, 1, "USD"),
        ("SEC_US_NEW", date(2026, 1, 8), 500.0, 500.0, 1, "USD"),
    ], column_names=["security_id", "trade_date", "close", "adj_close", "volume", "currency"])
    client.insert("security_lifecycle_events", [
        ("first-exchange", "SEC_US_OLD", "share_exchange", date(2026, 1, 6), 0.0, None,
         "USD", "confirmed", date(2026, 1, 2), "https://example.test/exchange", "e" * 64, True),
        ("second-merger", "SEC_US_NEW", "cash_merger", date(2026, 1, second_effective_day), 30.0, date(2026, 1, 8),
         "USD", "confirmed", date(2026, 1, 2), "https://example.test/merger", "f" * 64, True),
    ])
    client.insert("security_lifecycle_entitlements", [(
        "first-exchange", "new-stock", "security", "SEC_US_NEW", 2.0, date(2026, 1, first_delivery_day), "USD")])
    request = FactorBacktestRequestDto(
        conditions=[FactorConditionDto(factor_id="roe", mode="top_percent", top_percent=100)],
        start_date=date(2026, 1, 5), end_date=date(2026, 1, 8), rebalance_frequency="monthly",
        market="us", max_positions=1, transaction_cost_bps=0, benchmarks=[], factor_table="fact_daily_factors")
    if second_effective_day == 6:
        with pytest.raises(ValueError, match="explicit event ordering"):
            BacktestService(client_factory=factory).run_factor_backtest(request)
        return
    result = BacktestService(client_factory=factory).run_factor_backtest(request)
    # .01 OLD -> .02 NEW -> $0.60 cash. NEW's subsequent vendor quotes are invalid.
    assert [row.strategy_nav for row in result.equity_curve] == pytest.approx([1, 1, .6, .6])
    assert result.raw["portfolio_history"][-1]["cash"] == pytest.approx(.6)
