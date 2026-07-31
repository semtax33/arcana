from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.extractors._internal.us_consensus import (
    FinnworldsRollingRateLimiter,
    FmpRollingRateLimiter,
    RollingRateLimiter,
    download_us_consensus,
)
from engine.loaders._internal.clickhouse_consensus import (
    _delete_legacy_zero_key_rows,
    _insert_us_consensus_frame,
    _prepare_insert_frame,
    _with_consensus_row_key,
    load_us_consensus,
)
from engine.transformers._internal.us_consensus import (
    build_finnworlds_target_price_frames,
    build_us_consensus_frames,
    normalize_us_consensus,
)
from engine.transformers.factors import add_us_consensus_factors
from engine.workflows._internal import download_workflow
from engine.workflows._internal.score_workflow import calculate_style_scores


class _Response:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class TestUSConsensusPipeline(unittest.TestCase):
    def test_download_cli_exposes_finnworlds_backfill_controls(self):
        captured = {}

        def capture(args):
            captured.update(vars(args))

        argv = [
            "download",
            "--market",
            "us",
            "--us-consensus-sources",
            "finnworlds",
            "--finnworlds-date-from",
            "2000-01-01",
            "--finnworlds-date-to",
            "2026-07-31",
            "--finnworlds-max-calls-per-minute",
            "120",
            "--finnworlds-retries",
            "5",
            "consensus",
        ]
        with (
            patch("sys.argv", argv),
            patch.dict(
                download_workflow.US_DOWNLOAD_ACTIONS,
                {"consensus": capture},
                clear=True,
            ),
        ):
            download_workflow.main()

        self.assertEqual(captured["us_consensus_sources"], "finnworlds")
        self.assertEqual(captured["finnworlds_date_from"], "20000101")
        self.assertEqual(captured["finnworlds_date_to"], "20260731")
        self.assertEqual(captured["finnworlds_max_calls_per_minute"], 120)
        self.assertEqual(captured["finnworlds_retries"], 5)

    def test_us_consensus_loader_inserts_each_month_separately(self):
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def insert_df(self, table_name, frame, *, column_names):
                self.calls.append((table_name, frame.copy(), column_names))

        client = RecordingClient()
        frame = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT", "NVDA"],
                "factor_date": pd.to_datetime(
                    ["2026-01-30", "2026-02-02", "2026-02-27"]
                ),
            }
        )

        _insert_us_consensus_frame(client, "us_consensus_factors", frame)

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(
            all("_partition" not in columns for _, _, columns in client.calls)
        )
        self.assertTrue(
            all("consensus_row_key" in columns for _, _, columns in client.calls)
        )
        self.assertEqual(sum(len(batch) for _, batch, _ in client.calls), 3)

    def test_us_consensus_loader_converts_fiscal_period_end_to_date(self):
        prepared = _prepare_insert_frame(
            pd.DataFrame(
                {
                    "fiscal_period_end": ["2026-12-31", ""],
                    "availability_date": ["2026-07-28", ""],
                }
            )
        )

        self.assertEqual(prepared.loc[0, "fiscal_period_end"].isoformat(), "2026-12-31")
        self.assertIsNone(prepared.loc[1, "fiscal_period_end"])
        self.assertEqual(prepared.loc[0, "availability_date"].isoformat(), "2026-07-28")

    def test_us_consensus_internal_row_key_preserves_distinct_normalized_rows(self):
        observations = pd.DataFrame(
            [
                {
                    "dataset": "ANALYST_ESTIMATES",
                    "source_regime": "FMP_CURRENT",
                    "availability_date": "2026-07-28",
                    "period_type": "fiscal_year",
                    "fiscal_period_end": "2027-09-30",
                    "forecast_slot": "2027-09-30",
                    "lookback_days": lookback,
                }
                for lookback in (0, 30)
            ]
        )
        factors = pd.DataFrame(
            [{"raw_path": "estimates.json"}, {"raw_path": "target.json"}]
        )

        observation_keys = _with_consensus_row_key(
            "us_consensus_observations",
            observations,
        )["consensus_row_key"]
        factor_keys = _with_consensus_row_key(
            "us_consensus_factors",
            factors,
        )["consensus_row_key"]

        self.assertEqual(observation_keys.nunique(), 2)
        self.assertEqual(factor_keys.nunique(), 2)

    def test_us_consensus_loader_cleans_only_legacy_zero_key_rows(self):
        class RecordingClient:
            def __init__(self):
                self.commands = []

            def command(self, query):
                self.commands.append(query)

        client = RecordingClient()
        _delete_legacy_zero_key_rows(client, "us_consensus_observations")
        _delete_legacy_zero_key_rows(client, "us_consensus_events")

        self.assertEqual(len(client.commands), 1)
        self.assertIn(
            "DELETE WHERE consensus_row_key = 0",
            client.commands[0],
        )
        self.assertIn("mutations_sync = 2", client.commands[0])

    def test_us_consensus_load_applies_operating_income_schema_migration(self):
        class RecordingClient:
            def __init__(self):
                self.commands = []

            def command(self, query):
                self.commands.append(query)

            def close(self):
                pass

        client = RecordingClient()
        with TemporaryDirectory() as temp:
            load_us_consensus(silver_dir=temp, client=client)

        commands = "\n".join(client.commands)
        self.assertIn("CREATE TABLE IF NOT EXISTS us_target_price_ratings", commands)
        self.assertIn("CREATE TABLE IF NOT EXISTS us_target_price_consensus", commands)
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS us_operating_income_consensus Nullable(Float64)",
            commands,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS us_target_price Nullable(Float64)",
            commands,
        )
        self.assertIn("ADD COLUMN IF NOT EXISTS publishers_json String", commands)
        self.assertIn(
            "ALTER TABLE us_consensus_observations",
            commands,
        )
        self.assertIn("MODIFY ORDER BY", commands)
        self.assertIn(
            "symbol, snapshot_date, provider, horizon, metric, statistic",
            commands,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS consensus_row_key UInt64",
            commands,
        )
        self.assertIn(
            "ALTER TABLE us_consensus_factors",
            commands,
        )
        self.assertIn("MODIFY ORDER BY", commands)
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS consensus_row_key UInt64",
            commands,
        )
        self.assertIn("horizon, consensus_row_key", commands)

    def test_alpha_collector_reads_environment_key_and_writes_all_four_datasets(self):
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

            self.assertEqual(counts["symbols"], 1)
            self.assertEqual(counts["written"], 4)
            self.assertEqual(counts["failed"], 0)
            self.assertEqual(counts["providers"]["alpha-vantage"]["written"], 4)
            self.assertEqual({call[1]["function"] for call in calls}, {"EARNINGS_ESTIMATES", "EARNINGS", "OVERVIEW", "SPLITS"})
            self.assertTrue(all(call[1]["apikey"] == "runtime-secret" for call in calls))
            self.assertTrue((root / "alpha-vantage" / "earnings-estimates" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())
            self.assertTrue((root / "alpha-vantage" / "earnings" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())
            self.assertTrue((root / "alpha-vantage" / "overview" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())
            self.assertTrue((root / "alpha-vantage" / "splits" / "snapshot_date=2026-07-26" / "ticker=AAPL.json").exists())

    def test_alpha_collector_missing_environment_key_disables_provider(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            counts = download_us_consensus(
                symbols=["AAPL"],
                sources=["alpha-vantage"],
                output_root=temp,
            )

        self.assertTrue(counts["providers"]["alpha-vantage"]["auth_disabled"])
        self.assertEqual(counts["fallback_symbols"], 1)

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

    def test_fmp_rate_limiter_defaults_to_headroom_below_vendor_limit(self):
        with TemporaryDirectory() as temp:
            now = [0.0]
            limiter = FmpRollingRateLimiter(
                max_calls_per_minute=720,
                state_path=Path(temp) / "rate.json",
                clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
            for _ in range(720):
                limiter.acquire()
            limiter.acquire()

            self.assertGreaterEqual(now[0], 60.0)
            persisted = json.loads(
                (Path(temp) / "rate.json").read_text(encoding="utf-8")
            )
            self.assertLessEqual(len(persisted["request_timestamps"]), 720)

    def test_finnworlds_rate_limiter_persists_120_call_window_across_restart(self):
        with TemporaryDirectory() as temp:
            now = [0.0]
            state_path = Path(temp) / "rate.json"
            first = FinnworldsRollingRateLimiter(
                max_calls_per_minute=120,
                state_path=state_path,
                clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
            for _ in range(120):
                first.acquire()

            restarted = FinnworldsRollingRateLimiter(
                max_calls_per_minute=120,
                state_path=state_path,
                clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )
            restarted.acquire()

            self.assertGreaterEqual(now[0], 60.0)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(persisted["request_timestamps"]), 120)

    def test_finnworlds_interruption_resumes_from_verified_bronze(self):
        secret = "finnworlds-never-persist-this"
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FINNWORLDS_API_KEY": secret},
            clear=True,
        ):
            root = Path(temp)
            first_calls = []

            def interrupted_get(url, *, params, headers, timeout):
                first_calls.append(params.copy())
                if params["ticker"] == "MSFT":
                    raise KeyboardInterrupt
                return _Response(_finnworlds_payload(params["ticker"]))

            output = StringIO()
            with self.assertRaises(KeyboardInterrupt), redirect_stdout(output):
                download_us_consensus(
                    symbols=["AAPL", "MSFT"],
                    sources=["finnworlds"],
                    snapshot_date="2026-07-31",
                    finnworlds_date_from="2000-01-01",
                    finnworlds_date_to="2026-07-31",
                    output_root=root,
                    http_get=interrupted_get,
                    sleeper=lambda _: None,
                )

            self.assertEqual(
                [call["ticker"] for call in first_calls],
                ["AAPL", "MSFT"],
            )
            self.assertTrue(
                (
                    root
                    / "finnworlds"
                    / "company-ratings"
                    / "snapshot_date=2026-07-31"
                    / "ticker=AAPL.json"
                ).exists()
            )

            resumed_calls = []

            def resumed_get(url, *, params, headers, timeout):
                resumed_calls.append(params.copy())
                return _Response(_finnworlds_payload(params["ticker"]))

            resumed = download_us_consensus(
                symbols=["AAPL", "MSFT"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                finnworlds_date_from="2000-01-01",
                finnworlds_date_to="2026-07-31",
                output_root=root,
                http_get=resumed_get,
                sleeper=lambda _: None,
            )

            self.assertEqual([call["ticker"] for call in resumed_calls], ["MSFT"])
            self.assertEqual(resumed["providers"]["finnworlds"]["skipped"], 1)
            self.assertEqual(resumed["providers"]["finnworlds"]["written"], 1)
            self.assertTrue(
                all(call["date_from"] == "2000-01-01" for call in resumed_calls)
            )
            self.assertTrue(
                all(call["date_to"] == "2026-07-31" for call in resumed_calls)
            )
            checkpoint_path = next(
                (root / "meta" / "consensus").glob(
                    "finnworlds_backfill_*.json"
                )
            )
            checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "complete")
            self.assertEqual(checkpoint["completed"], ["AAPL", "MSFT"])
            self.assertEqual(checkpoint["pending"], [])
            self.assertEqual(checkpoint["http_requests"], 3)
            self.assertEqual(checkpoint["billed_calls"], 30)
            self.assertNotIn(secret, output.getvalue())
            for path in root.rglob("*.json"):
                self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_finnworlds_resume_uses_bronze_truth_and_force_redownloads(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FINNWORLDS_API_KEY": "runtime-secret"},
            clear=True,
        ):
            root = Path(temp)
            calls = []

            def fake_get(url, *, params, headers, timeout):
                calls.append(params["ticker"])
                return _Response(_finnworlds_payload(params["ticker"]))

            first = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                http_get=fake_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(first["written"], 1)
            checkpoint_path = next(
                (root / "meta" / "consensus").glob(
                    "finnworlds_backfill_*.json"
                )
            )
            checkpoint_path.unlink()

            cached = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                http_get=lambda *args, **kwargs: self.fail(
                    "verified bronze must be used without a checkpoint"
                ),
                sleeper=lambda _: None,
            )
            self.assertEqual(cached["skipped"], 1)

            bronze_path = (
                root
                / "finnworlds"
                / "company-ratings"
                / "snapshot_date=2026-07-31"
                / "ticker=AAPL.json"
            )
            bronze_path.write_text("{}", encoding="utf-8")
            orphan = bronze_path.parent / ".consensus-orphan.json"
            orphan.write_text("{", encoding="utf-8")
            repaired = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                http_get=fake_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(repaired["written"], 1)
            self.assertFalse(orphan.exists())

            forced = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                force=True,
                http_get=fake_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(forced["written"], 1)
            self.assertEqual(calls, ["AAPL", "AAPL", "AAPL"])

    def test_finnworlds_429_and_5xx_retry_without_leaking_key(self):
        secret = "finnworlds-runtime-secret"
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FINNWORLDS_API_KEY": secret},
            clear=True,
        ):
            calls = []
            sleeps = []

            def fake_get(url, *, params, headers, timeout):
                calls.append(params.copy())
                if len(calls) == 1:
                    return _Response(
                        {"status": {"code": 429, "message": "limited"}},
                        status_code=429,
                        headers={"Retry-After": "7"},
                    )
                if len(calls) == 2:
                    return _Response(
                        {"status": {"code": 503, "message": "unavailable"}},
                        status_code=503,
                    )
                return _Response(_finnworlds_payload(params["ticker"]))

            output = StringIO()
            with redirect_stdout(output):
                result = download_us_consensus(
                    symbols=["AAPL"],
                    sources=["finnworlds"],
                    snapshot_date="2026-07-31",
                    finnworlds_retries=2,
                    output_root=temp,
                    http_get=fake_get,
                    sleeper=sleeps.append,
                )

            self.assertEqual(result["providers"]["finnworlds"]["requests"], 3)
            self.assertEqual(result["providers"]["finnworlds"]["billed_calls"], 30)
            self.assertEqual(sleeps, [7.0, 4.0])
            self.assertNotIn(secret, output.getvalue())
            self.assertTrue(all(call["key"] == secret for call in calls))

    def test_finnworlds_auth_failure_saves_interrupted_checkpoint(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FINNWORLDS_API_KEY": "bad-secret"},
            clear=True,
        ):
            calls = []

            def fake_get(url, *, params, headers, timeout):
                calls.append(params["ticker"])
                return _Response(
                    {"status": {"code": 401, "message": "invalid key"}},
                    status_code=401,
                )

            result = download_us_consensus(
                symbols=["AAPL", "MSFT"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=temp,
                http_get=fake_get,
                sleeper=lambda _: None,
            )

            self.assertEqual(calls, ["AAPL"])
            self.assertTrue(result["providers"]["finnworlds"]["auth_disabled"])
            checkpoint_path = next(
                (Path(temp) / "meta" / "consensus").glob(
                    "finnworlds_backfill_*.json"
                )
            )
            checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "interrupted_auth")
            self.assertEqual(checkpoint["pending"], ["AAPL", "MSFT"])

    def test_finnworlds_failed_and_no_data_symbols_resume_without_duplicates(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FINNWORLDS_API_KEY": "runtime-secret"},
            clear=True,
        ):
            root = Path(temp)

            failed = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                finnworlds_retries=0,
                output_root=root,
                http_get=lambda *args, **kwargs: _Response(
                    {"status": {"code": 503, "message": "unavailable"}},
                    status_code=503,
                ),
                sleeper=lambda _: None,
            )
            self.assertEqual(failed["failed"], 1)
            checkpoint_path = next(
                (root / "meta" / "consensus").glob(
                    "finnworlds_backfill_*.json"
                )
            )
            failed_checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertEqual(failed_checkpoint["failed"], ["AAPL"])

            calls = []

            def retry_get(url, *, params, headers, timeout):
                calls.append(params["ticker"])
                return _Response(
                    {
                        "status": {
                            "code": 200,
                            "message": "OK",
                            "details": "",
                        },
                        "result": {
                            "basics": {"company_ticker": params["ticker"]},
                            "output": {
                                "analyst_consensus": {},
                                "analysts": [],
                            },
                        },
                    }
                )

            resumed = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                http_get=retry_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(calls, ["AAPL"])
            self.assertEqual(resumed["no_data"], 1)
            checkpoint = json.loads(
                checkpoint_path.read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], "complete")
            self.assertEqual(checkpoint["failed"], [])
            self.assertEqual(checkpoint["no_data"], ["AAPL"])

            repeated = download_us_consensus(
                symbols=["AAPL"],
                sources=["finnworlds"],
                snapshot_date="2026-07-31",
                output_root=root,
                http_get=lambda *args, **kwargs: self.fail(
                    "verified no-data bronze must be skipped"
                ),
                sleeper=lambda _: None,
            )
            self.assertEqual(repeated["skipped"], 1)

    def test_fmp_collector_uses_environment_header_and_paginates_all_estimates(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FMP_API_KEY": "fmp-runtime-secret"},
            clear=True,
        ):
            root = Path(temp)
            calls = []

            def fake_get(url, *, params, headers, timeout):
                calls.append((url, params.copy(), headers.copy(), timeout))
                if url.endswith("/price-target-summary"):
                    return _Response(
                        [{"symbol": "AAPL", "lastMonthCount": 4, "lastMonthAvgPriceTarget": 200}]
                    )
                if params["period"] == "annual" and params["page"] == 0:
                    return _Response(
                        [
                            {"symbol": "AAPL", "date": f"2030-12-{(index % 28) + 1:02d}"}
                            for index in range(1000)
                        ]
                    )
                return _Response([{"symbol": "AAPL", "date": "2031-12-31"}])

            counts = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=root,
                http_get=fake_get,
                sleeper=lambda _: None,
            )

            self.assertEqual(counts["written"], 3)
            self.assertEqual(len(calls), 4)
            self.assertTrue(
                all(call[2] == {"apikey": "fmp-runtime-secret"} for call in calls)
            )
            self.assertTrue(all("apikey" not in call[1] for call in calls))
            annual = json.loads(
                (
                    root
                    / "fmp"
                    / "analyst-estimates"
                    / "period=annual"
                    / "snapshot_date=2026-07-26"
                    / "ticker=AAPL.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(annual["pages"], 2)
            self.assertEqual(len(annual["data"]), 1001)
            self.assertTrue(annual["complete"])
            self.assertNotIn("fmp-runtime-secret", json.dumps(annual))

    def test_fmp_restart_skips_only_complete_cached_payloads(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FMP_API_KEY": "runtime-secret"},
            clear=True,
        ):
            root = Path(temp)

            def fake_get(url, *, params, headers, timeout):
                if url.endswith("/price-target-summary"):
                    return _Response([{"symbol": "AAPL"}])
                return _Response([{"symbol": "AAPL", "date": "2030-12-31"}])

            first = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=root,
                http_get=fake_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(first["written"], 3)

            def unexpected_get(*args, **kwargs):
                raise AssertionError("complete cache must not be downloaded again")

            repeated = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=root,
                http_get=unexpected_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(repeated["skipped"], 3)
            self.assertEqual(repeated["written"], 0)

            annual_path = (
                root
                / "fmp"
                / "analyst-estimates"
                / "period=annual"
                / "snapshot_date=2026-07-26"
                / "ticker=AAPL.json"
            )
            annual_path.write_text("{}", encoding="utf-8")
            calls = []

            def repair_get(url, *, params, headers, timeout):
                calls.append((url, params.copy()))
                return _Response([{"symbol": "AAPL", "date": "2030-12-31"}])

            repaired = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=root,
                http_get=repair_get,
                sleeper=lambda _: None,
            )
            self.assertEqual(repaired["written"], 1)
            self.assertEqual(repaired["skipped"], 2)
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                json.loads(annual_path.read_text(encoding="utf-8"))["complete"]
            )

    def test_fmp_does_not_publish_partial_pagination(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FMP_API_KEY": "runtime-secret"},
            clear=True,
        ):
            root = Path(temp)

            def fake_get(url, *, params, headers, timeout):
                if (
                    url.endswith("/analyst-estimates")
                    and params["period"] == "annual"
                    and params["page"] == 0
                ):
                    return _Response(
                        [
                            {"symbol": "AAPL", "date": "2030-12-31"}
                            for _ in range(1000)
                        ]
                    )
                if (
                    url.endswith("/analyst-estimates")
                    and params["period"] == "annual"
                ):
                    raise OSError("connection interrupted")
                if url.endswith("/price-target-summary"):
                    return _Response([{"symbol": "AAPL"}])
                return _Response([{"symbol": "AAPL", "date": "2030-12-31"}])

            counts = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=root,
                fmp_retries=0,
                http_get=fake_get,
                sleeper=lambda _: None,
            )

            annual_path = (
                root
                / "fmp"
                / "analyst-estimates"
                / "period=annual"
                / "snapshot_date=2026-07-26"
                / "ticker=AAPL.json"
            )
            self.assertFalse(annual_path.exists())
            self.assertEqual(counts["failed"], 1)
            self.assertEqual(counts["written"], 2)

    def test_fmp_auth_failure_stops_fmp_and_falls_back_without_logging_key(self):
        class EmptyYahooTicker:
            pass

        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FMP_API_KEY": "never-log-this"},
            clear=True,
        ):
            calls = []

            def fake_get(url, *, params, headers, timeout):
                calls.append((url, params.copy(), headers.copy(), timeout))
                return _Response(
                    {"message": "Invalid API Key"},
                    status_code=403,
                )

            output = StringIO()
            with redirect_stdout(output):
                counts = download_us_consensus(
                    symbols=["AAPL", "MSFT"],
                    sources=["fmp", "yfinance"],
                    snapshot_date="2026-07-26",
                    output_root=temp,
                    http_get=fake_get,
                    yahoo_ticker_factory=lambda _: EmptyYahooTicker(),
                    sleeper=lambda _: None,
                )

            self.assertEqual(len(calls), 1)
            self.assertNotIn("never-log-this", output.getvalue())
            self.assertTrue(counts["providers"]["fmp"]["auth_disabled"])
            self.assertEqual(counts["fallback_symbols"], 2)
            self.assertEqual(counts["providers"]["yfinance"]["written"], 2)
            self.assertTrue(
                (
                    Path(temp)
                    / "yahoo"
                    / "snapshot_date=2026-07-26"
                    / "ticker=MSFT.json"
                ).exists()
            )

    def test_fmp_429_retries_same_provider_instead_of_falling_back(self):
        with TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"FMP_API_KEY": "runtime-secret"},
            clear=True,
        ):
            calls = []

            def fake_get(url, *, params, headers, timeout):
                calls.append((url, params.copy()))
                if len(calls) == 1:
                    return _Response([], status_code=429, headers={"Retry-After": "0"})
                if url.endswith("/price-target-summary"):
                    return _Response(
                        [{"symbol": "AAPL", "lastMonthCount": 3, "lastMonthAvgPriceTarget": 200}]
                    )
                return _Response([{"symbol": "AAPL", "date": "2030-12-31"}])

            counts = download_us_consensus(
                symbols=["AAPL"],
                sources=["fmp"],
                snapshot_date="2026-07-26",
                output_root=temp,
                http_get=fake_get,
                sleeper=lambda _: None,
            )

            self.assertEqual(len(calls), 4)
            self.assertEqual(counts["providers"]["fmp"]["written"], 3)
            self.assertFalse(counts["providers"]["fmp"]["auth_disabled"])
            self.assertEqual(counts["fallback_symbols"], 0)

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

    def test_us_normalizer_reads_yahoo_mean_target_price(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_json(
                root / "yahoo" / "snapshot_date=2026-07-26" / "ticker=AAPL.json",
                {
                    "data": {
                        "earnings_estimate": _frame(
                            [
                                {
                                    "period": "0y",
                                    "numberOfAnalysts": 4,
                                    "currency": "USD",
                                }
                            ]
                        ),
                        "analyst_price_targets": {"mean": 125.0, "median": 120.0},
                    }
                },
            )

            observations, _, factors = build_us_consensus_frames(root)
            target_row = factors.loc[
                (factors["provider"] == "YAHOO_FINANCE")
                & (factors["horizon"] == "FY1")
            ].iloc[0]

            self.assertEqual(target_row["us_target_price"], 125.0)
            self.assertEqual(
                observations.loc[observations["metric"] == "target_price", "value"].iloc[0],
                125.0,
            )

    def test_us_normalizer_reads_alpha_overview_target_price(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_json(
                root / "alpha-vantage" / "overview" / "snapshot_date=2026-07-26" / "ticker=AAPL.json",
                {
                    "Symbol": "AAPL",
                    "Currency": "USD",
                    "AnalystTargetPrice": "225.50",
                    "AnalystRatingStrongBuy": "8",
                    "AnalystRatingBuy": "12",
                    "AnalystRatingHold": "5",
                    "AnalystRatingSell": "1",
                    "AnalystRatingStrongSell": "0",
                },
            )

            observations, _, factors = build_us_consensus_frames(root)
            target_row = factors.iloc[0]

            self.assertEqual(target_row["provider"], "ALPHA_VANTAGE")
            self.assertEqual(target_row["source_regime"], "ALPHA_VANTAGE_CURRENT")
            self.assertEqual(target_row["horizon"], "FY1")
            self.assertEqual(target_row["analyst_count"], 26.0)
            self.assertEqual(target_row["us_target_price"], 225.50)
            self.assertEqual(
                observations.loc[observations["metric"] == "target_price", "value"].iloc[0],
                225.50,
            )

    def test_finnworlds_normalizer_preserves_ratings_and_builds_pit_120d(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for snapshot in ("2026-07-30", "2026-07-31"):
                data = _finnworlds_payload("AAPL")
                if snapshot == "2026-07-31":
                    data["result"]["output"]["analysts"][0]["rating"][
                        "price_target"
                    ] = 130
                _write_json(
                    root
                    / "finnworlds"
                    / "company-ratings"
                    / f"snapshot_date={snapshot}"
                    / "ticker=AAPL.json",
                    {
                        "symbol": "AAPL",
                        "provider": "FINNWORLDS",
                        "dataset": "COMPANY_RATINGS",
                        "schema_version": 1,
                        "date_from": "2000-01-01",
                        "date_to": snapshot,
                        "snapshot_date": snapshot,
                        "complete": True,
                        "data": data,
                    },
                )

            ratings, consensus, observations, factors = (
                build_finnworlds_target_price_frames(root)
            )

            self.assertEqual(len(ratings), 4)
            self.assertEqual(ratings["rating_key"].nunique(), 4)
            self.assertEqual(ratings["snapshot_date"].unique().tolist(), ["2026-07-31"])
            self.assertEqual(ratings["price_target"].notna().sum(), 3)
            self.assertEqual(
                ratings.loc[
                    ratings["analyst_name"] == "Alice",
                    "price_target",
                ].iloc[0],
                130.0,
            )
            self.assertEqual(
                ratings.loc[
                    ratings["analyst_name"] == "No Target",
                    "rating",
                ].iloc[0],
                "Hold",
            )
            pit = consensus.loc[consensus["consensus_kind"] == "pit_120d"]
            active = pit.loc[pit["event_date"] == "2026-01-02"].iloc[0]
            expired = pit.loc[pit["event_date"] == "2026-05-02"].iloc[0]
            self.assertEqual(active["analyst_count"], 3)
            self.assertEqual(active["target_price_mean"], 120.0)
            self.assertEqual(active["target_price_median"], 120.0)
            self.assertEqual(active["target_price_low"], 110.0)
            self.assertEqual(active["target_price_high"], 130.0)
            self.assertEqual(active["availability_date"], "2026-01-05")
            self.assertEqual(expired["analyst_count"], 0)

            official = consensus.loc[
                consensus["consensus_kind"] == "official"
            ].iloc[0]
            self.assertEqual(official["target_price_mean"], 180.0)
            self.assertEqual(official["analyst_count"], 4)
            self.assertEqual(official["availability_date"], "2026-08-03")
            self.assertTrue(
                any(
                    row["source_regime"] == "FINNWORLDS_PIT_HISTORICAL"
                    and row["us_target_price"] == 120.0
                    for row in factors
                )
            )
            self.assertTrue(
                any(
                    row["source_regime"] == "FINNWORLDS_OFFICIAL_CURRENT"
                    and row["us_target_price"] == 180.0
                    for row in factors
                )
            )
            self.assertTrue(
                any(
                    row["source_regime"] == "FINNWORLDS_OFFICIAL_EXPIRED"
                    and row["us_target_price"] is None
                    for row in factors
                )
            )
            self.assertTrue(
                any(
                    row["metric"] == "target_price"
                    and row["statistic"] == "median"
                    for row in observations
                )
            )

            normalized = normalize_us_consensus(
                bronze_dir=root,
                output_dir=root / "silver",
            )
            self.assertEqual(normalized["target_price_ratings"], 4)
            self.assertGreater(normalized["target_price_consensus"], 0)
            self.assertTrue(normalized["target_price_ratings_path"].exists())
            self.assertTrue(normalized["target_price_consensus_path"].exists())

    def test_new_finnworlds_official_value_suppresses_superseded_expiry(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for snapshot, consensus_date, target in (
                ("2026-01-02", "2026-01-01", 100.0),
                ("2026-03-02", "2026-03-01", 200.0),
            ):
                data = _finnworlds_payload("AAPL")
                output = data["result"]["output"]
                output["analysts"] = []
                output["analyst_consensus"]["consensus_date"] = consensus_date
                output["analyst_consensus"]["analyst_average"] = target
                _write_json(
                    root
                    / "finnworlds"
                    / "company-ratings"
                    / f"snapshot_date={snapshot}"
                    / "ticker=AAPL.json",
                    {
                        "symbol": "AAPL",
                        "provider": "FINNWORLDS",
                        "dataset": "COMPANY_RATINGS",
                        "schema_version": 1,
                        "date_from": "2000-01-01",
                        "date_to": snapshot,
                        "snapshot_date": snapshot,
                        "complete": True,
                        "data": data,
                    },
                )

            _, _, _, factors = build_finnworlds_target_price_frames(root)
            official = [
                row
                for row in factors
                if row["source_regime"].startswith("FINNWORLDS_OFFICIAL")
            ]
            current = [
                row
                for row in official
                if row["source_regime"] == "FINNWORLDS_OFFICIAL_CURRENT"
            ]
            expired = [
                row
                for row in official
                if row["source_regime"] == "FINNWORLDS_OFFICIAL_EXPIRED"
            ]

            self.assertEqual(
                [row["us_target_price"] for row in current],
                [100.0, 200.0],
            )
            self.assertEqual(len(expired), 1)
            self.assertGreater(
                expired[0]["factor_date"],
                current[-1]["factor_date"],
            )

    def test_us_normalizer_maps_all_fmp_fields_revisions_and_target_windows(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = {
                "symbol": "AAPL",
                "provider": "FMP",
                "dataset": "ANALYST_ESTIMATES",
                "period": "annual",
                "pages": 1,
                "data": [
                    {
                        "symbol": "AAPL",
                        "date": "2027-09-30",
                        "revenueLow": 90,
                        "revenueHigh": 110,
                        "revenueAvg": 100,
                        "ebitdaLow": 25,
                        "ebitdaHigh": 35,
                        "ebitdaAvg": 30,
                        "ebitLow": 20,
                        "ebitHigh": 30,
                        "ebitAvg": 25,
                        "netIncomeLow": 15,
                        "netIncomeHigh": 25,
                        "netIncomeAvg": 20,
                        "sgaExpenseLow": 8,
                        "sgaExpenseHigh": 12,
                        "sgaExpenseAvg": 10,
                        "epsLow": 4,
                        "epsHigh": 6,
                        "epsAvg": 5,
                        "numAnalystsRevenue": 8,
                        "numAnalystsEps": 7,
                    }
                ],
            }
            second = json.loads(json.dumps(first))
            second["data"][0]["epsAvg"] = 6
            second["data"][0]["epsHigh"] = 7
            second["data"][0]["revenueAvg"] = 120
            for snapshot, payload in (
                ("2026-06-01", first),
                ("2026-07-01", second),
            ):
                _write_json(
                    root
                    / "fmp"
                    / "analyst-estimates"
                    / "period=annual"
                    / f"snapshot_date={snapshot}"
                    / "ticker=AAPL.json",
                    payload,
                )
            _write_json(
                root
                / "fmp"
                / "price-target-summary"
                / "snapshot_date=2026-07-01"
                / "ticker=AAPL.json",
                {
                    "symbol": "AAPL",
                    "provider": "FMP",
                    "dataset": "PRICE_TARGET_SUMMARY",
                    "data": [
                        {
                            "symbol": "AAPL",
                            "lastMonthCount": 2,
                            "lastMonthAvgPriceTarget": 210,
                            "lastQuarterCount": 5,
                            "lastQuarterAvgPriceTarget": 200,
                            "lastYearCount": 12,
                            "lastYearAvgPriceTarget": 190,
                            "allTimeCount": 40,
                            "allTimeAvgPriceTarget": 150,
                            "publishers": "[\"Benzinga\",\"MarketWatch\",\"Benzinga\"]",
                        }
                    ],
                },
            )

            observations, _, factors = build_us_consensus_frames(root)

            self.assertTrue(
                {
                    "revenue",
                    "ebitda",
                    "ebit",
                    "net_income",
                    "sga_expense",
                    "eps",
                    "target_price",
                }.issubset(set(observations["metric"]))
            )
            latest = factors.loc[
                (factors["provider"] == "FMP")
                & (factors["factor_date"] == "2026-07-01")
                & factors["us_eps_consensus"].notna()
            ].iloc[0]
            self.assertEqual(latest["horizon"], "FY1")
            self.assertEqual(latest["us_operating_income_consensus"], 25)
            self.assertAlmostEqual(latest["us_eps_revision_30d_pct"], 20)
            target = factors.loc[
                (factors["provider"] == "FMP")
                & factors["us_target_price"].notna()
            ].iloc[0]
            self.assertEqual(target["us_target_price"], 200)
            target_observations = observations.loc[
                observations["dataset"] == "PRICE_TARGET_SUMMARY"
            ]
            self.assertEqual(set(target_observations["lookback_days"]), {0, 30, 90, 365})
            self.assertEqual(
                json.loads(target_observations["publishers_json"].iloc[0]),
                ["Benzinga", "MarketWatch"],
            )

    def test_us_factor_merge_coalesces_each_field_in_fmp_alpha_yahoo_order(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "FMP",
                        "source_regime": "FMP_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 8,
                        "us_eps_consensus": 12,
                        "us_revenue_consensus": 120,
                        "us_eps_revision_30d_pct": 10,
                        "us_eps_dispersion_pct": 0.1,
                        "raw_path": "fmp",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 9,
                        "us_eps_consensus": 11,
                        "us_revenue_consensus": 110,
                        "us_eps_revision_30d_pct": 20,
                        "us_eps_revision_breadth_30d_pct": 0.6,
                        "us_eps_revision_acceleration_30d_pct": 3,
                        "us_eps_dispersion_pct": 0.2,
                        "us_revenue_dispersion_pct": 0.3,
                        "us_eps_surprise_pct": 4,
                        "raw_path": "yahoo",
                    },
                ]
            ).to_csv(path, index=False)
            daily = pd.DataFrame(
                {"trade_date": pd.to_datetime(["2026-07-21"])}
            )

            result = add_us_consensus_factors(
                daily,
                "AAPL",
                market="us",
                us_consensus_factors_path=path,
            )

            self.assertEqual(result.loc[0, "us_eps_consensus"], 12)
            self.assertEqual(result.loc[0, "us_eps_revision_30d_pct"], 10)
            self.assertEqual(result.loc[0, "us_eps_revision_breadth_30d_pct"], 0.6)
            self.assertEqual(result.loc[0, "us_eps_surprise_pct"], 4)
            self.assertEqual(result.loc[0, "us_consensus_source_regime"], "FMP_CURRENT")

    def test_us_factor_merge_uses_fresher_fallback_after_fmp_stops(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "FMP",
                        "source_regime": "FMP_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 8,
                        "us_eps_consensus": 12,
                        "us_eps_revision_30d_pct": 10,
                        "raw_path": "fmp",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-25",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 9,
                        "us_eps_consensus": 11,
                        "us_eps_revision_30d_pct": 20,
                        "raw_path": "yahoo",
                    },
                ]
            ).to_csv(path, index=False)
            daily = pd.DataFrame(
                {"trade_date": pd.to_datetime(["2026-07-24", "2026-07-26"])}
            )

            result = add_us_consensus_factors(
                daily,
                "AAPL",
                market="us",
                us_consensus_factors_path=path,
            )

            self.assertEqual(result.loc[0, "us_eps_consensus"], 12)
            self.assertEqual(result.loc[0, "us_consensus_source_regime"], "FMP_CURRENT")
            self.assertEqual(result.loc[1, "us_eps_consensus"], 11)
            self.assertEqual(result.loc[1, "us_consensus_source_regime"], "YAHOO_CURRENT")

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
                {"symbol": "AAPL", "factor_date": "2026-07-20", "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "horizon": "FY1", "analyst_count": 4, "us_eps_revision_30d_pct": 12, "us_eps_revision_breadth_30d_pct": .5, "us_eps_revision_acceleration_30d_pct": 4, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 3, "us_eps_consensus": 10, "us_revenue_consensus": 100, "us_operating_income_consensus": 150, "us_eps_revision_7d_pct": 1, "us_eps_revision_60d_pct": 8, "us_eps_revision_90d_pct": 5, "raw_path": "a"},
                {"symbol": "AAPL", "factor_date": "2026-07-25", "provider": "YAHOO_FINANCE", "source_regime": "YAHOO_CURRENT", "horizon": "FY1", "analyst_count": 2, "us_eps_revision_30d_pct": 99, "us_eps_revision_breadth_30d_pct": .9, "us_eps_revision_acceleration_30d_pct": 9, "us_eps_dispersion_pct": .1, "us_revenue_dispersion_pct": .2, "us_eps_surprise_pct": 3, "us_eps_consensus": 11, "us_revenue_consensus": 110, "us_operating_income_consensus": 160, "us_eps_revision_7d_pct": 2, "us_eps_revision_60d_pct": 9, "us_eps_revision_90d_pct": 6, "raw_path": "b"},
            ]).to_csv(path, index=False)
            daily = pd.DataFrame({"trade_date": pd.to_datetime(["2026-07-24", "2026-07-26"])})
            result = add_us_consensus_factors(daily, "AAPL", market="us", us_consensus_factors_path=path)
            self.assertEqual(result.loc[0, "us_eps_revision_30d_pct"], 12)
            self.assertTrue(pd.isna(result.loc[1, "us_eps_revision_30d_pct"]))
            self.assertEqual(result.loc[1, "us_eps_consensus"], 11)
            self.assertEqual(result.loc[0, "us_operating_income_consensus"], 150)
            self.assertEqual(result.loc[0, "us_consensus_source_regime"], "YAHOO_CURRENT")
            self.assertEqual(result.loc[0, "us_consensus_horizon"], "FY1")

    def test_us_price_to_target_price_uses_eligible_mean_target_price(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 4,
                        "us_target_price": 125.0,
                        "raw_path": "a",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-25",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 2,
                        "us_target_price": 250.0,
                        "raw_path": "b",
                    },
                ]
            ).to_csv(path, index=False)
            daily = pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-07-24", "2026-07-26"]),
                    "close": [100.0, 100.0],
                }
            )

            result = add_us_consensus_factors(
                daily,
                "AAPL",
                market="us",
                us_consensus_factors_path=path,
            )

            self.assertEqual(result.loc[0, "us_target_price"], 125.0)
            self.assertAlmostEqual(result.loc[0, "us_price_to_target_price"], 0.8)
            self.assertTrue(pd.isna(result.loc[1, "us_target_price"]))
            self.assertTrue(pd.isna(result.loc[1, "us_price_to_target_price"]))

    def test_us_price_to_target_price_prefers_alpha_and_falls_back_to_yahoo(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 8,
                        "us_target_price": 125.0,
                        "raw_path": "yahoo-1",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "ALPHA_VANTAGE",
                        "source_regime": "ALPHA_VANTAGE_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 10,
                        "us_target_price": 200.0,
                        "raw_path": "alpha-1",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-25",
                        "provider": "ALPHA_VANTAGE",
                        "source_regime": "ALPHA_VANTAGE_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 2,
                        "us_target_price": 300.0,
                        "raw_path": "alpha-2",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-25",
                        "provider": "YAHOO_FINANCE",
                        "source_regime": "YAHOO_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 6,
                        "us_target_price": 160.0,
                        "raw_path": "yahoo-2",
                    },
                ]
            ).to_csv(path, index=False)
            daily = pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-07-24", "2026-07-26"]),
                    "close": [100.0, 120.0],
                }
            )

            result = add_us_consensus_factors(
                daily,
                "AAPL",
                market="us",
                us_consensus_factors_path=path,
            )

            self.assertEqual(result.loc[0, "us_target_price"], 200.0)
            self.assertAlmostEqual(result.loc[0, "us_price_to_target_price"], 0.5)
            self.assertEqual(result.loc[1, "us_target_price"], 160.0)
            self.assertAlmostEqual(result.loc[1, "us_price_to_target_price"], 0.75)

    def test_us_target_price_strictly_prefers_finnworlds_until_expiry(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "us_consensus_factors.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-20",
                        "provider": "FINNWORLDS",
                        "source_regime": "FINNWORLDS_OFFICIAL_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 4,
                        "us_target_price": 220.0,
                        "raw_path": "finn-current",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-07-25",
                        "provider": "FMP",
                        "source_regime": "FMP_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 8,
                        "us_target_price": 200.0,
                        "raw_path": "fmp-1",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-11-18",
                        "provider": "FINNWORLDS",
                        "source_regime": "FINNWORLDS_OFFICIAL_EXPIRED",
                        "horizon": "FY1",
                        "analyst_count": 0,
                        "us_target_price": None,
                        "raw_path": "finn-expired",
                    },
                    {
                        "symbol": "AAPL",
                        "factor_date": "2026-11-20",
                        "provider": "FMP",
                        "source_regime": "FMP_CURRENT",
                        "horizon": "FY1",
                        "analyst_count": 7,
                        "us_target_price": 180.0,
                        "raw_path": "fmp-2",
                    },
                ]
            ).to_csv(path, index=False)
            daily = pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(
                        ["2026-07-24", "2026-07-26", "2026-11-19", "2026-11-21"]
                    ),
                    "close": [110.0, 110.0, 100.0, 90.0],
                }
            )

            result = add_us_consensus_factors(
                daily,
                "AAPL",
                market="us",
                us_consensus_factors_path=path,
            )

            self.assertEqual(result.loc[0, "us_target_price"], 220.0)
            self.assertEqual(result.loc[1, "us_target_price"], 220.0)
            self.assertEqual(
                result.loc[1, "us_target_price_provider"],
                "FINNWORLDS",
            )
            self.assertEqual(
                result.loc[1, "us_target_price_source_regime"],
                "FINNWORLDS_OFFICIAL_CURRENT",
            )
            self.assertEqual(result.loc[1, "us_target_price_analyst_count"], 4.0)
            self.assertAlmostEqual(
                result.loc[1, "us_price_to_target_price"],
                0.5,
            )
            self.assertEqual(result.loc[2, "us_target_price"], 200.0)
            self.assertEqual(result.loc[2, "us_target_price_provider"], "FMP")
            self.assertEqual(result.loc[3, "us_target_price"], 180.0)
            self.assertAlmostEqual(
                result.loc[3, "us_price_to_target_price"],
                0.5,
            )

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


def _finnworlds_payload(symbol: str):
    return {
        "status": {"code": 200, "message": "OK", "details": ""},
        "result": {
            "basics": {
                "company_name": f"{symbol} Inc.",
                "company_ticker": symbol,
            },
            "output": {
                "analyst_consensus": {
                    "consensus_conclusion": "Buy",
                    "analyst_average": 180.0,
                    "analyst_highest": 220.0,
                    "analyst_lowest": 140.0,
                    "analysts_number": 4,
                    "buy": 3,
                    "hold": 1,
                    "sell": 0,
                    "consensus_date": "2026-07-30",
                },
                "analysts": [
                    {
                        "analyst_name": "Alice",
                        "analyst_firm": "Firm A",
                        "analyst_role": "Analyst",
                        "rating": {
                            "date_rating": "2026-01-02",
                            "price_target": 100,
                            "target_date": "2026-12-31",
                            "rated": "Buy",
                            "conclusion": "Maintained",
                        },
                    },
                    {
                        "analyst_name": "Bob",
                        "analyst_firm": "Firm B",
                        "analyst_role": "Analyst",
                        "rating": {
                            "date_rating": "2026-01-02",
                            "price_target": 110,
                            "target_date": "2026-12-31",
                            "rated": "Buy",
                            "conclusion": "Raised",
                        },
                    },
                    {
                        "analyst_name": "",
                        "analyst_firm": "",
                        "analyst_role": "",
                        "rating": {
                            "date_rating": "2026-01-02",
                            "price_target": 120,
                            "target_date": "2026-12-31",
                            "rated": "Buy",
                            "conclusion": "Initiated",
                        },
                    },
                    {
                        "analyst_name": "No Target",
                        "analyst_firm": "Firm C",
                        "analyst_role": "Analyst",
                        "rating": {
                            "date_rating": "2026-01-02",
                            "price_target": "None",
                            "target_date": "2026-12-31",
                            "rated": "Hold",
                            "conclusion": "Maintained",
                        },
                    },
                ],
            },
        },
    }
