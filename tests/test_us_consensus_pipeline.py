from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal.us_consensus import RollingRateLimiter, download_us_consensus
from engine.transformers._internal.us_consensus import build_us_consensus_frames, normalize_us_consensus
from engine.transformers.factors import add_us_consensus_factors
from engine.workflows._internal.score_workflow import calculate_style_scores


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TestUSConsensusPipeline(unittest.TestCase):
    def test_alpha_collector_reads_environment_key_and_writes_all_three_datasets(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "runtime-secret"}):
            root = Path(temp)
            calls = []

            def fake_get(url, *, params, timeout):
                calls.append((url, params.copy(), timeout))
                return _Response({"symbol": params["symbol"], "data": []})

            counts = download_us_consensus(
                symbols=["AAPL"], sources=["alpha-vantage"], snapshot_date="2026-07-26",
                output_root=root, http_get=fake_get, sleeper=lambda _: None,
            )

            self.assertEqual(counts, {"symbols": 1, "written": 3, "skipped": 0, "failed": 0})
            self.assertEqual({call[1]["function"] for call in calls}, {"EARNINGS_ESTIMATES", "EARNINGS", "SPLITS"})
            self.assertTrue(all(call[1]["apikey"] == "runtime-secret" for call in calls))
            self.assertTrue((root / "alpha-vantage" / "earnings-estimates" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())
            self.assertTrue((root / "alpha-vantage" / "earnings" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())
            self.assertTrue((root / "alpha-vantage" / "splits" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())

    def test_alpha_collector_requires_environment_key(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "ALPHA_VANTAGE_API_KEY"):
                download_us_consensus(symbols=["AAPL"], sources=["alpha-vantage"], output_root=temp)

    def test_rate_limiter_never_exceeds_75_requests_in_a_rolling_minute(self):
        with TemporaryDirectory() as temp:
            now = [0.0]
            limiter = RollingRateLimiter(
                max_calls_per_minute=75, state_path=Path(temp) / "rate.json",
                clock=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
            for _ in range(75):
                limiter.acquire()
            limiter.acquire()
            self.assertGreaterEqual(now[0], 60.0)
            persisted = json.loads((Path(temp) / "rate.json").read_text(encoding="utf-8"))
            self.assertLessEqual(len(persisted["request_timestamps"]), 75)

    def test_normalizer_adjusts_pre_split_eps_and_preserves_provider_boundary(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_json(root / "alpha-vantage" / "splits" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"symbol": "AAPL", "data": [{"effective_date": "2020-08-31", "split_factor": "4.0"}]})
            _write_json(root / "alpha-vantage" / "earnings" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"quarterlyEarnings": [{"fiscalDateEnding": "2020-09-30", "reportedDate": "2020-10-29", "reportedEPS": "0.97", "estimatedEPS": "0.95", "surprisePercentage": "2.11"}]})
            _write_json(root / "alpha-vantage" / "earnings-estimates" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"quarterlyEstimates": [{"date": "2020-09-30", "eps_estimate_average": "0.70", "eps_estimate_average_30_days_ago": "0.60", "eps_estimate_average_60_days_ago": "2.80", "eps_estimate_average_90_days_ago": "2.40", "eps_estimate_high": "0.80", "eps_estimate_low": "0.60", "eps_estimate_number_of_analysts": "10", "eps_estimate_revision_up": "4", "eps_estimate_revision_down": "1"}]})
            _write_json(root / "yahoo" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"data": {"earnings_estimate": _frame([{"period": "0y", "avg": 10, "low": 9, "high": 11, "numberOfAnalysts": 12, "currency": "USD"}]), "revenue_estimate": _frame([{"period": "0y", "avg": 100, "low": 90, "high": 110}]), "eps_trend": _frame([{"period": "0y", "current": 10, "7daysAgo": 9, "30daysAgo": 8, "60daysAgo": 7, "90daysAgo": 6}]), "eps_revisions": _frame([{"period": "0y", "upLast30days": 4, "downLast30Days": 1}]), "earnings_history": _frame([{"index": "2026-07-20", "epsActual": 10.5, "epsEstimate": 10, "surprisePercent": 0.05}])}})

            observations, events, factors = build_us_consensus_frames(root)
            alpha_60 = observations.loc[(observations["provider"] == "ALPHA_VANTAGE") & (observations["lookback_days"] == 60), "value"].iloc[0]
            self.assertEqual(alpha_60, 0.70)
            self.assertEqual(events.loc[events["provider"] == "ALPHA_VANTAGE", "estimated_eps"].iloc[0], 0.95)
            alpha_factor = factors.loc[factors["provider"] == "ALPHA_VANTAGE"].iloc[0]
            self.assertAlmostEqual(alpha_factor["us_eps_revision_30d_pct"], (0.7 - 0.6) / 0.6 * 100)
            self.assertEqual(alpha_factor["us_eps_surprise_pct"], 2.11)
            yahoo_factor = factors.loc[(factors["provider"] == "YAHOO_FINANCE") & (factors["horizon"] == "FY1")].iloc[0]
            self.assertEqual(yahoo_factor["us_eps_revision_30d_pct"], 25.0)
            self.assertEqual(yahoo_factor["us_eps_surprise_pct"], 5.0)
            result = normalize_us_consensus(bronze_dir=root, output_dir=root / "silver")
            self.assertEqual(pd.read_csv(result["factors_path"]).shape[0], len(factors))

    def test_normalizer_reads_actual_alpha_estimates_as_historical_fq1(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_json(root / "alpha-vantage" / "splits" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"symbol": "AAPL", "data": [{"effective_date": "2020-08-31", "split_factor": "4.0"}]})
            _write_json(root / "alpha-vantage" / "earnings" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"quarterlyEarnings": [{"fiscalDateEnding": "2020-09-30", "reportedDate": "2020-10-29", "reportedEPS": "0.97", "estimatedEPS": "0.95", "surprisePercentage": "2.11"}]})
            _write_json(root / "alpha-vantage" / "earnings-estimates" / "snapshot_date=2026-07-26" / "ticker=AAPL.json", {"symbol": "AAPL", "estimates": [
                {"date": "2020-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "0.70", "eps_estimate_average_30_days_ago": "0.60", "eps_estimate_average_60_days_ago": "2.80", "eps_estimate_average_90_days_ago": "2.40", "eps_estimate_high": "0.80", "eps_estimate_low": "0.60", "eps_estimate_analyst_count": "10", "eps_estimate_revision_up_trailing_30_days": "4", "eps_estimate_revision_down_trailing_30_days": "1"},
                {"date": "2021-03-31", "horizon": "fiscal quarter", "eps_estimate_average": "1.00", "eps_estimate_analyst_count": "10"}
            ]})

            observations, events, factors = build_us_consensus_frames(root)
            alpha = factors.loc[factors["provider"] == "ALPHA_VANTAGE"].iloc[0]
            self.assertEqual(len(events), 1)
            self.assertEqual(len(factors.loc[factors["provider"] == "ALPHA_VANTAGE"]), 1)
            self.assertEqual(alpha["horizon"], "FQ1")
            self.assertEqual(alpha["factor_date"], "2020-10-30")
            self.assertEqual(alpha["analyst_count"], 10.0)
            self.assertAlmostEqual(alpha["us_eps_revision_30d_pct"], (0.7 - 0.6) / 0.6 * 100)
            self.assertAlmostEqual(alpha["us_eps_revision_breadth_30d_pct"], 0.6)
            self.assertEqual(alpha["us_eps_surprise_pct"], 2.11)
            alpha_60 = observations.loc[(observations["provider"] == "ALPHA_VANTAGE") & (observations["lookback_days"] == 60), "value"].iloc[0]
            self.assertEqual(alpha_60, 0.70)

    def test_us_style_score_uses_us_consensus_weights_and_requires_core_factors(self):
        values = {
            "us_eps_revision_30d_pct": 90.0, "us_eps_revision_breadth_30d_pct": 80.0,
            "us_eps_revision_acceleration_30d_pct": 70.0, "us_eps_dispersion_pct": 60.0,
            "us_revenue_dispersion_pct": 50.0, "us_eps_surprise_pct": 40.0,
        }
        rows = [{"security_id": "SEC_US_AAPL", "country": "US", "factor_id": factor_id, "percentile_score": score, "is_valid": True} for factor_id, score in values.items()]
        result = calculate_style_scores(pd.DataFrame(rows), trade_date="2026-07-26")
        self.assertAlmostEqual(result.loc[0, "consensus_score"], 72.5)
        result = calculate_style_scores(pd.DataFrame(rows[:-1]), trade_date="2026-07-26")
        self.assertAlmostEqual(result.loc[0, "consensus_score"], (90 * .35 + 80 * .20 + 70 * .15 + 60 * .10 + 50 * .05) / .85)
        result = calculate_style_scores(pd.DataFrame(rows[1:]), trade_date="2026-07-26")
        self.assertTrue(pd.isna(result.loc[0, "consensus_score"]))

    def test_us_factor_merge_uses_fy1_and_enforces_analyst_threshold(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame([
                {"symbol": "AAPL", "factor_date": "2026-07-20", "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "horizon": "FY1", "analyst_count": 4, "us_eps_revision_30d_pct": 12, "us_eps_revision_breadth_30d_pct": .5, "us_eps_revision_acceleration_30d_pct": 4, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 3, "us_eps_consensus": 10, "us_revenue_consensus": 100, "us_eps_revision_7d_pct": 1, "us_eps_revision_60d_pct": 8, "us_eps_revision_90d_pct": 5, "raw_path": "a"},
                {"symbol": "AAPL", "factor_date": "2026-07-25", "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "horizon": "FY1", "analyst_count": 2, "us_eps_revision_30d_pct": 99, "us_eps_revision_breadth_30d_pct": .9, "us_eps_revision_acceleration_30d_pct": 9, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 3, "us_eps_consensus": 11, "us_revenue_consensus": 110, "us_eps_revision_7d_pct": 2, "us_eps_revision_60d_pct": 9, "us_eps_revision_90d_pct": 6, "raw_path": "b"},
            ]).to_csv(path, index=False)
            daily = pd.DataFrame({"trade_date": pd.to_datetime(["2026-07-24", "2026-07-26"])})
            result = add_us_consensus_factors(daily, "AAPL", market="us", us_consensus_factors_path=path)
            self.assertEqual(result.loc[0, "us_eps_revision_30d_pct"], 12)
            self.assertTrue(pd.isna(result.loc[1, "us_eps_revision_30d_pct"]))
            self.assertEqual(result.loc[1, "us_eps_consensus"], 11)
            self.assertEqual(result.loc[0, "us_consensus_source_regime"], "YAHOO_CURRENT")

    def test_us_factor_merge_uses_alpha_fq1_until_yahoo_fy1_handoff(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame([
                {"symbol": "AAPL", "factor_date": "2020-10-30", "provider": "ALPHA_VANTAGE", "source_regime": "ALPHA_VANTAGE_HISTORICAL", "horizon": "FQ1", "analyst_count": 4, "us_eps_revision_30d_pct": 12, "us_eps_revision_breadth_30d_pct": .5, "us_eps_revision_acceleration_30d_pct": 4, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 3, "us_eps_consensus": .7, "us_revenue_consensus": 100, "us_eps_revision_7d_pct": 1, "us_eps_revision_60d_pct": 8, "us_eps_revision_90d_pct": 5, "raw_path": "alpha"},
                {"symbol": "AAPL", "factor_date": "2020-11-05", "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "horizon": "FY1", "analyst_count": 4, "us_eps_revision_30d_pct": 22, "us_eps_revision_breadth_30d_pct": .7, "us_eps_revision_acceleration_30d_pct": 6, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 4, "us_eps_consensus": 10, "us_revenue_consensus": 100, "us_eps_revision_7d_pct": 2, "us_eps_revision_60d_pct": 9, "us_eps_revision_90d_pct": 6, "raw_path": "yahoo"},
                {"symbol": "AAPL", "factor_date": "2020-11-06", "provider": "ALPHA_VANTAGE", "source_regime": "ALPHA_VANTAGE_HISTORICAL", "horizon": "FQ1", "analyst_count": 4, "us_eps_revision_30d_pct": 99, "us_eps_revision_breadth_30d_pct": .9, "us_eps_revision_acceleration_30d_pct": 9, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 9, "us_eps_consensus": 9, "us_revenue_consensus": 90, "us_eps_revision_7d_pct": 9, "us_eps_revision_60d_pct": 9, "us_eps_revision_90d_pct": 9, "raw_path": "late-alpha"},
            ]).to_csv(path, index=False)
            daily = pd.DataFrame({"trade_date": pd.to_datetime(["2020-10-29", "2020-10-30", "2020-11-04", "2020-11-05", "2020-11-06"])})

            result = add_us_consensus_factors(daily, "AAPL", market="us", us_consensus_factors_path=path)
            self.assertTrue(pd.isna(result.loc[0, "us_eps_revision_30d_pct"]))
            self.assertEqual(result.loc[1, "us_eps_revision_30d_pct"], 12)
            self.assertEqual(result.loc[2, "us_consensus_source_regime"], "ALPHA_VANTAGE_HISTORICAL")
            self.assertEqual(result.loc[3, "us_eps_revision_30d_pct"], 22)
            self.assertEqual(result.loc[4, "us_eps_revision_30d_pct"], 22)
            self.assertEqual(result.loc[4, "us_consensus_source_regime"], "YAHOO_CURRENT")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _frame(records):
    return {"kind": "dataframe", "records": records}
