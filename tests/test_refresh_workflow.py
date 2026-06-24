from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from engine.transformers._internal import krx_market_data
from engine.workflows._internal import refresh_workflow


class FakeClickHouseClient:
    def __init__(self, overlap_rows: int = 0):
        self.overlap_rows = overlap_rows
        self.queries = []
        self.commands = []
        self.inserted = []

    def query_df(self, query, parameters=None):
        self.queries.append((query, parameters or {}))
        return pd.DataFrame({"rows": [self.overlap_rows]})

    def command(self, query):
        self.commands.append(query)

    def insert_df(self, table_name, dataframe, column_names=None):
        self.inserted.append((table_name, dataframe.copy(), list(column_names or [])))


class RefreshWorkflowTest(unittest.TestCase):
    def test_build_refresh_window_starts_after_latest_date(self):
        window = refresh_workflow.build_refresh_window(
            date(2026, 6, 10),
            end_date="2026-06-22",
        )

        self.assertEqual(window.start_date, "20260611")
        self.assertEqual(window.end_date, "20260622")
        self.assertEqual(window.start_iso, "2026-06-11")
        self.assertTrue(window.has_work)

    def test_build_refresh_window_has_no_work_when_already_fresh(self):
        window = refresh_workflow.build_refresh_window(
            date(2026, 6, 22),
            end_date="20260622",
        )

        self.assertIsNone(window.start_date)
        self.assertFalse(window.has_work)

    def test_latest_date_in_csv_detects_korean_date_column(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kr_005930.csv"
            pd.DataFrame(
                {
                    "?좎쭨": ["2026-06-10", "2026-06-12", "bad"],
                    "醫낃?": [100, 110, 120],
                }
            ).to_csv(path, index=False, encoding="utf-8-sig")

            latest = refresh_workflow.latest_date_in_csv(path)

        self.assertEqual(latest, date(2026, 6, 12))

    def test_merge_krx_symbol_csv_appends_dedupes_and_sorts(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "kr_005930.csv"
            pd.DataFrame(
                {
                    "?좎쭨": ["2026-06-10", "2026-06-11"],
                    "醫낃?": [100, 110],
                }
            ).to_csv(path, index=False, encoding="utf-8-sig")

            merged = refresh_workflow.merge_krx_symbol_csv(
                path,
                pd.DataFrame(
                    {
                        "?좎쭨": ["2026-06-11 00:00:00", "2026-06-12 00:00:00"],
                        "醫낃?": [111, 120],
                    }
                ),
            )

        self.assertEqual(merged["?좎쭨"].tolist(), ["2026-06-10", "2026-06-11", "2026-06-12"])
        self.assertEqual(merged["醫낃?"].tolist(), [100, 111, 120])

    def test_download_and_merge_report_metadata_dedupes_existing_and_incremental(self):
        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "kr_report_metadata.csv"
            existing = pd.DataFrame(
                [
                    {
                        "security_id": "SEC_KR_005930",
                        "stock_code": "005930",
                        "fiscal_year": 2025,
                        "fiscal_month": 12,
                        "period_end_date": "2025-12-31",
                        "report_date": "2026-03-01",
                        "rcept_no": "20260301000001",
                        "report_name": "old",
                        "source_type": "statement",
                        "source_url": "old",
                        "updated_at": "2026-03-01 00:00:00",
                    }
                ]
            )
            existing.to_csv(output_path, index=False)
            incremental = existing.copy()
            incremental.loc[0, "report_date"] = "2026-03-02"
            incremental.loc[0, "rcept_no"] = "20260302000001"
            incremental.loc[0, "report_name"] = "new"

            with patch.object(refresh_workflow, "collect_dart_report_metadata", return_value=incremental):
                merged = refresh_workflow.download_and_merge_report_metadata(
                    ["005930"],
                    start_date="20260302",
                    end_date="20260302",
                    output_csv_path=output_path,
                )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged["report_name"].iat[0], "new")

    def test_overlap_policy_appends_when_no_overlap(self):
        args = argparse.Namespace(clickhouse_mode="overlap-truncate", dry_run=False)
        client = FakeClickHouseClient(overlap_rows=0)
        full = pd.DataFrame({"trade_date": pd.to_datetime(["2026-06-10", "2026-06-11"]), "value": [1, 2]})
        candidate = full.iloc[[1]].copy()

        rows = refresh_workflow.load_dataframe_with_policy(
            args,
            client,
            table_name="price_daily",
            full_frame=full,
            candidate_frame=candidate,
            column_names=list(full.columns),
            window=refresh_workflow.RefreshWindow("20260611", "20260611", date(2026, 6, 10)),
        )

        self.assertEqual(rows, 1)
        self.assertEqual(client.commands, [])
        self.assertEqual(client.inserted[0][1]["value"].tolist(), [2])

    def test_overlap_policy_truncates_and_reloads_when_overlap_exists(self):
        args = argparse.Namespace(clickhouse_mode="overlap-truncate", dry_run=False)
        client = FakeClickHouseClient(overlap_rows=1)
        full = pd.DataFrame({"trade_date": pd.to_datetime(["2026-06-10", "2026-06-11"]), "value": [1, 2]})
        candidate = full.iloc[[1]].copy()

        rows = refresh_workflow.load_dataframe_with_policy(
            args,
            client,
            table_name="price_daily",
            full_frame=full,
            candidate_frame=candidate,
            column_names=list(full.columns),
            window=refresh_workflow.RefreshWindow("20260611", "20260611", date(2026, 6, 10)),
        )

        self.assertEqual(rows, 2)
        self.assertEqual(client.commands, ["TRUNCATE TABLE price_daily"])
        self.assertEqual(client.inserted[0][1]["value"].tolist(), [1, 2])

    def test_append_only_mode_never_truncates(self):
        args = argparse.Namespace(clickhouse_mode="append-only", dry_run=False)
        client = FakeClickHouseClient(overlap_rows=1)

        self.assertFalse(refresh_workflow.should_truncate_table(args, client, "price_daily"))

    def test_always_truncate_mode_skips_overlap_query(self):
        args = argparse.Namespace(clickhouse_mode="always-truncate", dry_run=False)
        client = FakeClickHouseClient(overlap_rows=0)

        self.assertTrue(refresh_workflow.should_truncate_table(args, client, "price_daily"))
        self.assertEqual(client.queries, [])

    def test_table_name_validation_rejects_unknown_tables(self):
        with self.assertRaisesRegex(ValueError, "unsupported refresh table"):
            refresh_workflow.validate_table_name("price_daily; DROP TABLE price_daily")

    def test_cli_defaults_route_market_data_target(self):
        with (
            patch.object(
                sys,
                "argv",
                ["prog", "--targets", "market-data", "--dry-run", "--skip-clickhouse", "--end-date", "2026-06-22"],
            ),
            patch.object(refresh_workflow.download_workflow, "_stock_codes", return_value=["005930"]),
            patch.object(refresh_workflow, "run_market_data_refresh") as run_market_data,
        ):
            refresh_workflow.main()

        run_market_data.assert_called_once()
        args = run_market_data.call_args.args[0]
        self.assertEqual(args.clickhouse_mode, "overlap-truncate")
        self.assertEqual(args.market, "kr")

    def test_progress_tracker_prints_step_status(self):
        stdout = io.StringIO()
        progress = refresh_workflow.ProgressTracker(["market-data", "factors"])

        with redirect_stdout(stdout):
            progress.begin("market-data")
            progress.done("market-data")

        output = stdout.getvalue()
        self.assertIn("[PROGRESS] refresh step=1/2 name=market-data status=start", output)
        self.assertIn("[PROGRESS] refresh step=1/2 name=market-data status=done", output)

    def test_download_incremental_krx_dataset_prints_symbol_progress(self):
        stdout = io.StringIO()
        with (
            patch.object(refresh_workflow, "latest_date_in_csv", return_value=None),
            patch.object(refresh_workflow, "fetch_krx_dataset_frame", return_value=pd.DataFrame({"?좎쭨": ["2026-06-11"]})),
            patch.object(refresh_workflow, "merge_krx_symbol_csv") as merge_csv,
            redirect_stdout(stdout),
        ):
            refresh_workflow.download_incremental_krx_dataset(
                "price",
                ["005930", "000660"],
                end_date="20260611",
                dry_run=False,
                progress_interval=1,
            )

        output = stdout.getvalue()
        self.assertIn("[PROGRESS] bronze price processed=1/2", output)
        self.assertIn("[PROGRESS] bronze price processed=2/2", output)
        self.assertIn("downloaded_symbols=2", output)
        self.assertEqual(merge_csv.call_count, 2)

    def test_refresh_state_persists_completed_steps_and_symbols(self):
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "refresh_state.json"
            signature = {"market": "kr", "targets": ["market-data"], "end_date": "20260622"}
            state = refresh_workflow.RefreshState.open(
                state_path,
                signature=signature,
                resume=False,
                enabled=True,
            )
            state.complete_symbol("price", "005930")
            state.complete_step(
                "market-data",
                refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10)),
            )

            loaded = refresh_workflow.RefreshState.open(
                state_path,
                signature=signature,
                resume=True,
                enabled=True,
            )

        self.assertTrue(loaded.is_symbol_completed("price", "005930"))
        self.assertTrue(loaded.is_step_completed("market-data"))
        self.assertEqual(loaded.step_window("market-data").start_date, "20260611")

    def test_run_refresh_resume_skips_completed_market_data_step(self):
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "refresh_state.json"
            args = argparse.Namespace(
                market="kr",
                targets="market-data",
                end_date="20260622",
                workers=1,
                sleep_seconds=5.0,
                stock_retries=3,
                stock_retry_backoff=30.0,
                financial_basis="annual",
                dry_run=False,
                skip_clickhouse=True,
                force_full=False,
                resume=True,
                resume_state_path=str(state_path),
                progress_interval=100,
                clickhouse_mode="overlap-truncate",
            )
            signature = refresh_workflow.resume_signature(args, "20260622", {"market-data"})
            state = refresh_workflow.RefreshState.open(state_path, signature=signature, resume=False, enabled=True)
            state.complete_step(
                "market-data",
                refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10)),
            )
            stdout = io.StringIO()
            with (
                patch.object(refresh_workflow.download_workflow, "_stock_codes", return_value=["005930"]),
                patch.object(refresh_workflow, "run_market_data_refresh") as run_market_data,
                redirect_stdout(stdout),
            ):
                refresh_workflow.run_refresh(args)

        run_market_data.assert_not_called()
        self.assertIn("[RESUME] skipping completed step: market-data", stdout.getvalue())

    def test_run_market_data_refresh_passes_workers_and_state_to_krx_downloads(self):
        args = argparse.Namespace(
            force_full=False,
            dry_run=True,
            progress_interval=7,
            workers=3,
            skip_clickhouse=True,
        )
        state = object()
        price_window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))
        share_window = refresh_workflow.RefreshWindow("20260612", "20260622", date(2026, 6, 11))

        with patch.object(
            refresh_workflow,
            "download_incremental_krx_dataset",
            side_effect=[price_window, share_window],
        ) as download_dataset:
            window = refresh_workflow.run_market_data_refresh(
                args,
                ["005930"],
                "20260622",
                client=None,
                state=state,
            )

        self.assertEqual(window.start_date, "20260611")
        self.assertEqual(download_dataset.call_count, 2)
        for call in download_dataset.call_args_list:
            self.assertEqual(call.kwargs["workers"], 3)
            self.assertIs(call.kwargs["state"], state)

    def test_filing_refresh_resumes_symbol_and_substep_state(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            state.complete_symbol("filings-statements", "000660")
            state.complete_step(
                "filings-metadata",
                refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10)),
            )
            args = argparse.Namespace(
                dry_run=False,
                workers=1,
                progress_interval=1,
                skip_clickhouse=True,
            )
            window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))

            with (
                patch.object(refresh_workflow, "download_statements") as download_statements,
                patch.object(refresh_workflow, "download_statement_comments") as download_comments,
                patch.object(refresh_workflow, "download_and_merge_report_metadata") as metadata,
                patch.object(refresh_workflow, "normalize_statements_for_years") as normalize,
            ):
                refresh_workflow.run_filing_refresh(
                    args,
                    ["005930", "000660"],
                    window,
                    client=None,
                    state=state,
                )

        download_statements.assert_called_once()
        self.assertEqual(download_statements.call_args.args[0], ["005930"])
        self.assertEqual(download_statements.call_args.kwargs["display_offset_base"], 1)
        self.assertEqual(download_comments.call_count, 2)
        metadata.assert_not_called()
        normalize.assert_called_once_with(2026, 2026)
        self.assertTrue(state.is_symbol_completed("filings-statements", "005930"))
        self.assertTrue(state.is_symbol_completed("filings-comments", "005930"))
        self.assertTrue(state.is_step_completed("filings-normalize"))

    def test_business_info_refresh_resumes_symbols_and_normalize(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            state.complete_symbol("business-info", "000660")
            args = argparse.Namespace(
                dry_run=False,
                workers=1,
                progress_interval=1,
                sleep_seconds=0.0,
                stock_retries=0,
                stock_retry_backoff=0.0,
            )
            window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))
            normalize_workflow = type(
                "NormalizeWorkflow",
                (),
                {"normalize_business_infos": staticmethod(lambda **kwargs: None)},
            )

            with (
                patch.object(refresh_workflow, "download_business_infos") as download_business,
                patch.object(refresh_workflow, "get_normalize_workflow", return_value=normalize_workflow),
            ):
                refresh_workflow.run_business_info_refresh(
                    args,
                    ["005930", "000660"],
                    window,
                    state,
                )

        download_business.assert_called_once()
        self.assertEqual(download_business.call_args.args[0], ["005930"])
        self.assertEqual(download_business.call_args.kwargs["display_offset_base"], 1)
        self.assertTrue(state.is_symbol_completed("business-info", "005930"))
        self.assertTrue(state.is_step_completed("business-info-normalize"))

    def test_dividend_refresh_resumes_symbols_and_silver_file(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            state.complete_symbol("dividends", "000660")
            state.complete_step(
                "dividends-silver",
                refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10)),
            )
            output_path = Path(temp_dir) / "dividend_normalized.csv"
            pd.DataFrame({"security_id": ["SEC_KR_000660"], "trade_date": ["2026-06-12"]}).to_csv(output_path, index=False)
            args = argparse.Namespace(
                dry_run=False,
                workers=1,
                progress_interval=1,
                skip_clickhouse=True,
            )
            window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))

            with (
                patch.object(refresh_workflow, "download_dividend_histories") as download_dividends,
                patch.object(refresh_workflow.dividend_loader, "dividend_output_path", return_value=output_path),
                patch.object(refresh_workflow.dividend_loader, "refresh_silver_dividend_files") as refresh_silver,
            ):
                refresh_workflow.run_dividend_refresh(
                    args,
                    ["005930", "000660"],
                    window,
                    client=None,
                    state=state,
                )

        download_dividends.assert_called_once()
        self.assertEqual(download_dividends.call_args.args[0], ["005930"])
        self.assertEqual(download_dividends.call_args.kwargs["display_offset_base"], 1)
        refresh_silver.assert_not_called()
        self.assertTrue(state.is_symbol_completed("dividends", "005930"))

    def test_factor_refresh_resumes_pending_reload_and_marks_insert(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))
            state.complete_step("factors-reload-required", window)
            args = argparse.Namespace(
                dry_run=False,
                skip_clickhouse=False,
                clickhouse_mode="overlap-truncate",
                financial_basis="annual",
                workers=2,
            )
            client = FakeClickHouseClient(overlap_rows=0)

            with (
                patch.object(refresh_workflow, "load_factor_catalog") as load_catalog,
                patch.object(refresh_workflow.factor_loader, "insert_daily_factors") as insert_factors,
            ):
                refresh_workflow.run_factor_refresh(args, window, client, state)

        load_catalog.assert_called_once()
        self.assertEqual(client.commands, ["TRUNCATE TABLE fact_daily_factors"])
        self.assertIsNone(insert_factors.call_args.kwargs["start_date"])
        self.assertEqual(insert_factors.call_args.kwargs["parallel_workers"], 2)
        self.assertTrue(state.is_step_completed("factors-insert"))
    def test_factor_refresh_skips_completed_insert_before_overlap_truncate(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            window = refresh_workflow.RefreshWindow("20260611", "20260622", date(2026, 6, 10))
            state.complete_step("factors-catalog", window)
            state.complete_step("factors-insert", window)
            args = argparse.Namespace(
                dry_run=False,
                skip_clickhouse=False,
                clickhouse_mode="overlap-truncate",
                financial_basis="annual",
                workers=2,
            )
            client = FakeClickHouseClient(overlap_rows=1)

            with patch.object(refresh_workflow.factor_loader, "insert_daily_factors") as insert_factors:
                refresh_workflow.run_factor_refresh(args, window, client, state)

        self.assertEqual(client.queries, [])
        self.assertEqual(client.commands, [])
        insert_factors.assert_not_called()
    def test_download_incremental_krx_dataset_resume_skips_completed_symbol(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            state.complete_symbol("price", "005930")
            stdout = io.StringIO()
            with (
                patch.object(refresh_workflow, "latest_date_in_csv", return_value=date(2026, 6, 22)),
                patch.object(refresh_workflow, "fetch_krx_dataset_frame") as fetch_frame,
                redirect_stdout(stdout),
            ):
                refresh_workflow.download_incremental_krx_dataset(
                    "price",
                    ["005930"],
                    end_date="20260622",
                    state=state,
                    progress_interval=1,
                )

        fetch_frame.assert_not_called()
        self.assertIn("skipped=1", stdout.getvalue())
    def test_download_incremental_krx_dataset_uses_workers_and_updates_state(self):
        with TemporaryDirectory() as temp_dir:
            state = refresh_workflow.RefreshState.open(
                Path(temp_dir) / "refresh_state.json",
                signature={"market": "kr"},
                resume=False,
                enabled=True,
            )
            stdout = io.StringIO()
            with (
                patch.object(refresh_workflow, "latest_date_in_csv", return_value=None),
                patch.object(refresh_workflow, "_download_and_merge_krx_symbol") as download_one,
                redirect_stdout(stdout),
            ):
                refresh_workflow.download_incremental_krx_dataset(
                    "price",
                    ["005930", "000660", "035420"],
                    end_date="20260622",
                    workers=2,
                    state=state,
                    progress_interval=1,
                )

        self.assertEqual(download_one.call_count, 3)
        self.assertTrue(state.is_symbol_completed("price", "005930"))
        self.assertTrue(state.is_symbol_completed("price", "000660"))
        self.assertTrue(state.is_symbol_completed("price", "035420"))
        self.assertIn("workers=2", stdout.getvalue())

    def test_krx_trade_date_parser_accepts_timestamp_strings(self):
        dates = krx_market_data._parse_trade_dates(
            pd.Series(["2026-06-11", "2026-06-12 00:00:00"])
        )

        self.assertEqual(dates.dt.strftime("%Y-%m-%d").tolist(), ["2026-06-11", "2026-06-12"])

if __name__ == "__main__":
    unittest.main()







