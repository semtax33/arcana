import unittest
from datetime import date

from api.main import app
from api.service.valuation_service import (
    MultipleValuationService,
    _build_industry_cross_section_stats_query,
    _build_central_band_summary,
    _build_listing_market_stats_query,
    _build_market_stats_query,
    _financial_basis_order,
    _normalize_financial_basis,
)
from api.model.valuation import ValuationBand, ValuationMetric, ValuationStockMetadata


class FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        if orient != "records":
            raise ValueError("FakeFrame only supports records orient")
        return self._rows


class FakeClickHouseClient:
    def __init__(self, industry_rows=None, cross_section_rows=None):
        self.queries = []
        self.closed = False
        self.industry_rows = industry_rows
        self.cross_section_rows = cross_section_rows

    def query_df(self, query, parameters=None):
        params = parameters or {}
        self.queries.append((query, params))
        if "FROM security_master AS sm" in query and "LEFT JOIN identifiers" in query:
            return FakeFrame(
                [
                    {
                        "security_id": "SEC_KR_036640",
                        "ticker": "036640",
                        "stock_name": "Test Co",
                        "stock_name_en": "Test Co",
                        "country": "KR",
                        "currency": "KRW",
                        "primary_market_mic": "KOSPI",
                        "industry_schema": "GICS",
                        "sector_code": "45",
                        "industry_group_code": "4530",
                        "industry_group_name": "Semiconductors",
                        "security_currency": "KRW",
                        "price_trade_date": "2026-05-22",
                        "close": 100.0,
                        "price_currency": "KRW",
                    }
                ]
            )
        if "FROM price_daily" in query and "ORDER BY trade_date DESC" in query:
            return FakeFrame(
                [{"trade_date": "2026-05-22", "close": 100.0, "currency": "KRW"}]
            )
        if (
            "FROM fact_daily_factors" in query
            and "argMax(f.factor_value" in query
            and "GROUP BY f.factor_id" in query
            and "INNER JOIN security_master AS sm" not in query
            and "latest_factors AS" not in query
        ):
            return FakeFrame(
                [
                    {"factor_id": "per", "value": 10.0, "latest_trade_date": "2026-05-22"},
                    {"factor_id": "pbr", "value": 1.0, "latest_trade_date": "2026-05-22"},
                    {"factor_id": "fcf_to_ev_yield", "value": 5.0, "latest_trade_date": "2026-05-22"},
                    {"factor_id": "eps_yoy_pct", "value": -3.0, "latest_trade_date": "2026-05-22"},
                ]
            )
        if "latest_factors AS" in query and "sm.primary_market_mic = {primary_market_mic:String}" in query:
            return FakeFrame(
                [
                    _stats("per", avg_value=19, median_value=17, p25_value=11, p75_value=23, n=90),
                    _stats("pbr", avg_value=1.9, median_value=1.7, p25_value=1, p75_value=3, n=90),
                    _stats("fcf_to_ev_yield", avg_value=3.2, median_value=3.7, p25_value=2, p75_value=5, n=70),
                    _stats("eps_yoy_pct", avg_value=9, median_value=8, p25_value=2, p75_value=14, n=80),
                ]
            )
        if "latest_factors AS" in query and "INNER JOIN security_master AS sm" in query:
            return FakeFrame(
                [
                    _stats("per", avg_value=20, median_value=18, p25_value=12, p75_value=24, n=100),
                    _stats("pbr", avg_value=2, median_value=1.8, p25_value=1, p75_value=3, n=100),
                    _stats("fcf_to_ev_yield", avg_value=3, median_value=3.5, p25_value=2, p75_value=5, n=80),
                    _stats("eps_yoy_pct", avg_value=10, median_value=9, p25_value=2, p75_value=15, n=90),
                ]
            )
        if "industry_universe AS" in query:
            return FakeFrame(
                self.cross_section_rows
                if self.cross_section_rows is not None
                else [
                    _stats("per", avg_value=16, median_value=14, p25_value=10, p75_value=18, n=20),
                    _stats("pbr", avg_value=1.6, median_value=1.4, p25_value=1, p75_value=2, n=20),
                ]
            )
        if "monthly_values AS" in query:
            return FakeFrame(
                [
                    _stats(
                        "per",
                        avg_value=12,
                        median_value=15,
                        p25_value=8,
                        p75_value=20,
                        history_points=[
                            ("2026-04-01", 11.0),
                            ("2026-05-01", 10.0),
                        ]
                        if "history_points" in query
                        else None,
                    ),
                    _stats("pbr", avg_value=1.1, median_value=1.2, p25_value=0.8, p75_value=1.5),
                    _stats(
                        "fcf_to_ev_yield",
                        avg_value=4.5,
                        median_value=4.0,
                        p25_value=3.0,
                        p75_value=6.0,
                    ),
                    _stats("eps_yoy_pct", avg_value=7, median_value=8, p25_value=2, p75_value=12),
                ]
            )
        if "FROM fact_daily_factors" in query and "quantileExact(0.5)(factor_value)" in query:
            return FakeFrame(
                [
                    _stats("per", avg_value=12, median_value=15, p25_value=8, p75_value=20),
                    _stats("pbr", avg_value=1.1, median_value=1.2, p25_value=0.8, p75_value=1.5),
                    _stats(
                        "fcf_to_ev_yield",
                        avg_value=4.5,
                        median_value=4.0,
                        p25_value=3.0,
                        p75_value=6.0,
                    ),
                    _stats("eps_yoy_pct", avg_value=7, median_value=8, p25_value=2, p75_value=12),
                ]
            )
        if "FROM industry_factor_daily_snapshot" in query:
            return FakeFrame(
                self.industry_rows
                if self.industry_rows is not None
                else [
                    _stats("per", avg_value=14, median_value=13, p25_value=9, p75_value=17, n=20),
                    _stats("pbr", avg_value=1.4, median_value=1.3, p25_value=0.9, p75_value=2, n=20),
                    _stats("fcf_to_ev_yield", avg_value=4, median_value=4, p25_value=2, p75_value=6, n=20),
                    _stats("eps_yoy_pct", avg_value=11, median_value=10, p25_value=3, p75_value=16, n=20),
                ]
            )
        if "toDate(toStartOfMonth(trade_date)) AS period" in query:
            return FakeFrame(
                [
                    {"factor_id": "per", "period": "2026-04-01", "value": 11.0},
                    {"factor_id": "per", "period": "2026-05-01", "value": 10.0},
                ]
            )
        return FakeFrame([])

    def close(self):
        self.closed = True


class TtmFallbackClickHouseClient(FakeClickHouseClient):
    def query_df(self, query, parameters=None):
        params = parameters or {}
        if (
            "FROM fact_daily_factors" in query
            and "argMax(f.factor_value" in query
            and "GROUP BY f.factor_id" in query
            and "INNER JOIN security_master AS sm" not in query
        ):
            self.queries.append((query, params))
            if params.get("financial_basis") == "ttm":
                return FakeFrame(
                    _filter_requested(
                        [{"factor_id": "per", "value": 8.0, "latest_trade_date": "2026-05-22"}],
                        params,
                    )
                )
            if params.get("financial_basis") == "annual":
                return FakeFrame(
                    _filter_requested(
                        [{"factor_id": "pbr", "value": 2.0, "latest_trade_date": "2026-05-22"}],
                        params,
                    )
                )
        if "monthly_values AS" in query:
            self.queries.append((query, params))
            if params.get("financial_basis") == "ttm":
                return FakeFrame(
                    _filter_requested(
                        [
                            _stats(
                                "per",
                                avg_value=10,
                                median_value=12,
                                p25_value=8,
                                p75_value=14,
                                history_points=[("2026-05-01", 8.0)],
                            )
                        ],
                        params,
                    )
                )
            if params.get("financial_basis") == "annual":
                return FakeFrame(
                    _filter_requested(
                        [
                            _stats(
                                "per",
                                avg_value=13,
                                median_value=12,
                                p25_value=8,
                                p75_value=14,
                                history_points=[("2026-05-01", 12.0)],
                            ),
                            _stats(
                                "pbr",
                                avg_value=2.5,
                                median_value=3,
                                p25_value=2,
                                p75_value=4,
                                history_points=[("2026-05-01", 2.0)],
                            )
                        ],
                        params,
                    )
                )
        return super().query_df(query, parameters=params)


class TtmHistoricalFallbackClickHouseClient(FakeClickHouseClient):
    def query_df(self, query, parameters=None):
        params = parameters or {}
        if "monthly_values AS" in query:
            self.queries.append((query, params))
            if params.get("financial_basis") == "ttm":
                return FakeFrame(
                    [
                        _stats(
                            "per",
                            avg_value=-2,
                            median_value=-3,
                            p25_value=-8,
                            p75_value=4,
                            history_points=[("2026-05-01", -3.0)],
                        )
                    ]
                )
            if params.get("financial_basis") == "annual":
                return FakeFrame(
                    [
                        _stats(
                            "per",
                            avg_value=13,
                            median_value=12,
                            p25_value=9,
                            p75_value=18,
                            history_points=[("2026-05-01", 12.0)],
                        )
                    ]
                )
        return super().query_df(query, parameters=params)


class MultipleValuationServiceTest(unittest.TestCase):
    def test_multiple_valuation_builds_comparisons_and_margin_price_bands(self):
        client = FakeClickHouseClient()

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per", "pbr", "fcf_to_ev_yield", "eps_yoy_pct"],
            band_basis="historical",
            buy_margin_pct=20,
            sell_margin_pct=10,
        )

        self.assertTrue(client.closed)
        self.assertEqual(result.stock.stock_code, "036640")
        self.assertEqual(result.price_date, date(2026, 5, 22))
        self.assertEqual(result.factor_source, "fact_daily_factors")

        bands = {band.factor_id: band for band in result.bands}
        self.assertEqual(bands["per"].target_multiple.value, 15)
        self.assertEqual(bands["per"].fair_price.value, 150)
        self.assertEqual(bands["per"].buy_below_price.value, 120)
        self.assertEqual(bands["per"].sell_above_price.value, 165)
        self.assertEqual(bands["per"].signal, "discount")

        self.assertEqual(bands["fcf_to_ev_yield"].target_multiple.value, 4)
        self.assertEqual(bands["fcf_to_ev_yield"].fair_price.value, 125)
        self.assertEqual(bands["fcf_to_ev_yield"].signal, "discount")

        self.assertIsNone(bands["eps_yoy_pct"].fair_price.value)
        self.assertIn("not price-derivable", bands["eps_yoy_pct"].warning)
        self.assertIn("eps_yoy_pct", bands)

        self.assertIsNotNone(result.central_band)
        self.assertEqual(result.central_band.valid_factor_count, 3)
        self.assertEqual(result.central_band.excluded_factor_ids, ["eps_yoy_pct"])
        self.assertEqual(result.central_band.fair_price.value, 125)
        self.assertEqual(result.central_band.buy_below_price.value, 100)
        self.assertEqual(result.central_band.sell_above_price.value, 137.5)

        per_comparison = next(row for row in result.comparisons if row.factor_id == "per")
        by_key = {item.benchmark_key: item for item in per_comparison.comparisons}
        self.assertEqual(by_key["market_avg"].benchmark_name, "Market Avg")
        self.assertEqual(by_key["market_avg"].value.value, 20)
        self.assertEqual(by_key["listing_market_avg"].benchmark_name, "KOSPI Avg")
        self.assertEqual(by_key["listing_market_avg"].value.value, 19)
        self.assertEqual(by_key["industry_avg"].benchmark_name, "Industry Avg")
        self.assertEqual(by_key["industry_avg"].value.value, 16)
        self.assertEqual(by_key["historical_median"].signal, "discount")
        self.assertEqual(result.history[0].period, date(2026, 4, 1))

    def test_multiple_valuation_can_skip_history_query_for_fast_band_load(self):
        client = FakeClickHouseClient()

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per"],
            include_history=False,
        )

        self.assertEqual(result.history, [])
        historical_queries = [query for query, _ in client.queries if "monthly_values AS" in query]
        self.assertEqual(len(historical_queries), 1)
        self.assertNotIn("history_points", historical_queries[0])

    def test_multiple_valuation_prefers_ttm_and_falls_back_to_annual_by_factor(self):
        client = TtmFallbackClickHouseClient()

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per", "pbr"],
            band_basis="historical",
            buy_margin_pct=20,
            sell_margin_pct=10,
        )

        self.assertEqual(result.financial_basis, "ttm")
        bands = {band.factor_id: band for band in result.bands}
        self.assertEqual(bands["per"].current_multiple.value, 8)
        self.assertEqual(bands["per"].target_multiple.value, 12)
        self.assertEqual(bands["per"].fair_price.value, 150)
        self.assertEqual(bands["pbr"].current_multiple.value, 2)
        self.assertEqual(bands["pbr"].target_multiple.value, 3)
        self.assertEqual(bands["pbr"].fair_price.value, 150)

        annual_fallback_params = [
            params
            for _, params in client.queries
            if params.get("financial_basis") == "annual"
        ]
        self.assertTrue(
            any(params.get("factor_ids") == ["pbr"] for params in annual_fallback_params)
        )

    def test_ttm_request_uses_annual_history_for_historical_band(self):
        client = TtmHistoricalFallbackClickHouseClient()

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per"],
            band_basis="historical",
            buy_margin_pct=20,
            sell_margin_pct=10,
        )

        band = result.bands[0]
        self.assertEqual(result.financial_basis, "ttm")
        self.assertEqual(band.current_multiple.value, 10)
        self.assertEqual(band.target_multiple.value, 12)
        self.assertEqual(band.fair_price.value, 120)
        self.assertEqual(result.history[0].value, 12)

    def test_industry_band_uses_available_positive_average_when_median_is_missing(self):
        client = FakeClickHouseClient(
            cross_section_rows=[],
            industry_rows=[
                _stats("per", avg_value=14, median_value=None, p25_value=None, p75_value=None, n=20),
                _stats("pbr", avg_value=4.7, median_value=None, p25_value=None, p75_value=None, n=20),
                _stats("eps_yoy_pct", avg_value=-12, median_value=None, p25_value=None, p75_value=None, n=20),
            ]
        )

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per", "pbr", "eps_yoy_pct"],
            financial_basis="annual",
            band_basis="industry",
            buy_margin_pct=20,
            sell_margin_pct=15,
            include_history=False,
        )

        bands = {band.factor_id: band for band in result.bands}
        self.assertEqual(bands["per"].target_multiple.value, 14)
        self.assertEqual(bands["per"].fair_price.value, 140)
        self.assertEqual(bands["pbr"].target_multiple.value, 4.7)
        self.assertEqual(bands["pbr"].fair_price.value, 470)
        self.assertIsNone(bands["eps_yoy_pct"].fair_price.value)
        self.assertEqual(result.central_band.valid_factor_count, 2)
        self.assertEqual(result.central_band.fair_price.value, 305)

        per_comparison = next(row for row in result.comparisons if row.factor_id == "per")
        industry_comparison = {
            item.benchmark_key: item for item in per_comparison.comparisons
        }["industry_avg"]
        self.assertEqual(industry_comparison.value.value, 14)

    def test_industry_band_fills_snapshot_gaps_from_cross_section_stats(self):
        client = FakeClickHouseClient(
            industry_rows=[
                _stats("eps_yoy_pct", avg_value=1.4, median_value=-20, p25_value=-60, p75_value=-5, n=30),
            ]
        )

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per", "pbr", "eps_yoy_pct"],
            financial_basis="annual",
            band_basis="industry",
            buy_margin_pct=20,
            sell_margin_pct=10,
            include_history=False,
        )

        bands = {band.factor_id: band for band in result.bands}
        self.assertEqual(bands["eps_yoy_pct"].target_multiple.value, 1.4)

        per_comparison = next(row for row in result.comparisons if row.factor_id == "per")
        industry_comparison = {
            item.benchmark_key: item for item in per_comparison.comparisons
        }["industry_avg"]
        self.assertEqual(bands["per"].target_multiple.value, 16)
        self.assertEqual(bands["per"].fair_price.value, 160)
        self.assertEqual(bands["pbr"].target_multiple.value, 1.6)
        self.assertEqual(bands["pbr"].fair_price.value, 160)
        self.assertEqual(industry_comparison.value.value, 16)
        self.assertIn("industry_universe AS", "\n".join(query for query, _ in client.queries))

    def test_query_uses_cross_section_latest_values_for_market_average(self):
        query = _build_market_stats_query("fact_daily_factors", "factor_value")

        self.assertIn("latest_factors AS", query)
        self.assertIn("latest_factor_dates AS", query)
        self.assertIn("INNER JOIN security_master AS sm", query)
        self.assertIn("sm.country = {market_country:String}", query)
        self.assertIn("INTERVAL 45 DAY", query)
        self.assertIn("d.latest_trade_date = f.trade_date", query)
        self.assertIn("argMax(f.factor_value, f.updated_at)", query)
        self.assertIn("quantileExact(0.10)(value)", query)
        self.assertIn("quantileExact(0.90)(value)", query)
        self.assertIn("avg(if(lf.value < b.p10_value", query)
        self.assertIn("GROUP BY factor_id", query)

    def test_industry_cross_section_query_separates_peers_by_country_and_winsorizes(self):
        query = _build_industry_cross_section_stats_query("fact_daily_factors", "factor_value")

        self.assertIn("iss.industry_group_code = {industry_group_code:String}", query)
        self.assertIn("sm.country = {market_country:String}", query)
        self.assertIn("quantileExact(0.10)(value)", query)
        self.assertIn("quantileExact(0.90)(value)", query)
        self.assertIn("avg(if(lf.value < b.p10_value", query)

    def test_listing_market_query_filters_by_primary_market_mic(self):
        query = _build_listing_market_stats_query("fact_daily_factors", "factor_value")

        self.assertIn("sm.country = {market_country:String}", query)
        self.assertIn("sm.primary_market_mic = {primary_market_mic:String}", query)
        self.assertIn("quantileExact(0.10)(value)", query)
        self.assertIn("avg(if(lf.value < b.p10_value", query)

    def test_listing_market_band_uses_average_multiple(self):
        client = FakeClickHouseClient()

        result = MultipleValuationService(
            client_factory=lambda: client,
            today_factory=lambda: date(2026, 5, 29),
        ).get_multiple_valuation(
            "36640",
            as_of_date=date(2026, 5, 22),
            factor_ids=["per"],
            financial_basis="annual",
            band_basis="listing_market",
            buy_margin_pct=20,
            sell_margin_pct=10,
            include_history=False,
        )

        band = result.bands[0]
        self.assertEqual(band.target_source, "listing_market")
        self.assertEqual(band.target_multiple.value, 19)
        self.assertEqual(band.fair_price.value, 190)

    def test_semiconductor_central_band_uses_core_cashflow_multiples(self):
        bands = [
            _band("per", 600),
            _band("pbr", 700),
            _band("ev_to_nopat", 2500),
            _band("ev_to_ebitda", 1400),
            _band("fcfpr", 2800),
            _band("fcf_to_ev_yield", 1500),
            _band("pcr", 900),
            _band("peg", 10),
        ]
        summary = _build_central_band_summary(
            bands,
            metadata=ValuationStockMetadata(
                stock_code="000660",
                security_id="SEC_KR_000660",
                sector_code="45",
                industry_group_code="4530",
            ),
            buy_margin_pct=20,
            sell_margin_pct=10,
        )

        self.assertEqual(summary.fair_price.value, 1500)
        self.assertEqual(summary.buy_below_price.value, 1200)
        self.assertEqual(summary.valid_factor_count, 5)
        self.assertIn("per", summary.excluded_factor_ids)
        self.assertIn("pbr", summary.excluded_factor_ids)
        self.assertIn("peg", summary.excluded_factor_ids)

    def test_financial_central_band_uses_per_pbr_pcr(self):
        bands = [
            _band("per", 600),
            _band("pbr", 700),
            _band("pcr", 900),
            _band("ev_to_ebitda", 2000),
            _band("fcf_to_ev_yield", 1800),
        ]
        summary = _build_central_band_summary(
            bands,
            metadata=ValuationStockMetadata(
                stock_code="000001",
                security_id="SEC_KR_000001",
                sector_code="40",
                industry_group_code="4010",
            ),
            buy_margin_pct=20,
            sell_margin_pct=10,
        )

        self.assertEqual(summary.fair_price.value, 700)
        self.assertEqual(summary.valid_factor_count, 3)
        self.assertIn("ev_to_ebitda", summary.excluded_factor_ids)

    def test_forward_financial_basis_is_supported_with_ttm_fallback(self):
        self.assertEqual(_normalize_financial_basis("ntm"), "forward")
        self.assertEqual(_financial_basis_order("forward"), ["forward", "ttm", "annual"])
        self.assertEqual(_financial_basis_order("ttm"), ["ttm", "annual"])

    def test_app_registers_multiple_valuation_route(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/api/valuations/{stock_code}/multiple-bands", paths)


def _stats(
    factor_id,
    *,
    avg_value,
    median_value,
    p25_value,
    p75_value,
    n=30,
    history_points=None,
):
    row = {
        "factor_id": factor_id,
        "avg_value": avg_value,
        "median_value": median_value,
        "p25_value": p25_value,
        "p75_value": p75_value,
        "n": n,
    }
    if history_points is not None:
        row["history_points"] = history_points
    return row


def _filter_requested(rows, params):
    factor_ids = set(params.get("factor_ids") or [])
    if not factor_ids:
        return rows
    return [row for row in rows if row.get("factor_id") in factor_ids]


def _band(factor_id, fair_price):
    return ValuationBand(
        factor_id=factor_id,
        factor_name=factor_id,
        current_multiple=ValuationMetric(),
        target_multiple=ValuationMetric(),
        target_source="industry",
        fair_price=ValuationMetric(value=fair_price, display_value=str(fair_price)),
        buy_below_price=ValuationMetric(),
        sell_above_price=ValuationMetric(),
    )


if __name__ == "__main__":
    unittest.main()
