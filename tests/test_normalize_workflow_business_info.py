from pathlib import Path
import importlib
import sys
import types
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd


def _load_workflow_with_stubs():
    filings = types.ModuleType("engine.transformers.filings")

    class ContextEngine:
        pass

    class RuleEngine:
        pass

    filings.ContextEngine = ContextEngine
    filings.EXPECTED_HEADER = []
    filings.RuleEngine = RuleEngine
    filings.infer_comment_html_path = lambda **kwargs: Path("missing_comment.html")
    filings.load_canonical_accounts = lambda *args, **kwargs: None
    filings.normalize_financial_statement_rule_based = lambda *args, **kwargs: None

    statement_files = types.ModuleType("engine.transformers._internal.statement_files")
    statement_files.consolidate_statement_debug_snapshots = lambda *args, **kwargs: False
    statement_files.consolidate_statement_snapshots = lambda *args, **kwargs: False

    market_universe = types.ModuleType("engine.extractors.market_universe")
    market_universe.kospi_kosdaq_corp_list = lambda: None

    module_name = "engine.workflows._internal.normalize_workflow"
    sys.modules.pop(module_name, None)
    with patch.dict(
        sys.modules,
        {
            "engine.transformers.filings": filings,
            "engine.transformers._internal.statement_files": statement_files,
            "engine.extractors.market_universe": market_universe,
        },
    ):
        return importlib.import_module(module_name)


class NormalizeWorkflowBusinessInfoTest(unittest.TestCase):
    def test_iter_business_info_paths_filters_symbols_and_years_inclusive(self):
        workflow = _load_workflow_with_stubs()
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for stock_code in ["005930", "105560"]:
                stock_dir = root / stock_code
                stock_dir.mkdir()
                for period in ["2025.12", "2026.03", "2027.03"]:
                    (stock_dir / f"business_info_({period}).html").write_text("", encoding="utf-8")

            paths = workflow.iter_business_info_paths(
                ["5930"],
                start_year=2026,
                end_year=2026,
                bronze_root=root,
            )

        self.assertEqual([path.name for path in paths], ["business_info_(2026.03).html"])
        self.assertEqual(paths[0].parent.name, "005930")

    def test_normalize_business_infos_writes_parser_outputs(self):
        workflow = _load_workflow_with_stubs()
        section_path = Path("sections.csv")
        table_path = Path("tables.csv")
        cell_path = Path("cells.csv")
        row_path = Path("rows.csv")
        written_paths = [(section_path, table_path, cell_path, row_path)]

        with (
            patch.object(workflow, "iter_business_info_paths", return_value=[Path("a.html"), Path("b.html")]),
            patch("engine.transformers._internal.kr_business_extractor.parse_business_info_files", return_value=["doc1", "doc2"]) as parse_mock,
            patch("engine.transformers._internal.kr_business_extractor.document_to_section_records") as sections_mock,
            patch("engine.transformers._internal.kr_business_extractor.document_to_table_records") as tables_mock,
            patch("engine.transformers._internal.kr_business_extractor.document_to_cell_records") as cells_mock,
            patch("engine.transformers._internal.kr_business_extractor.document_to_row_records") as rows_mock,
            patch(
                "engine.transformers._internal.kr_business_extractor.write_business_info_csvs",
                return_value=written_paths,
            ) as write_mock,
        ):
            sections_mock.return_value = [{"section": 1}]
            tables_mock.return_value = [{"table": 1}, {"table": 2}]
            cells_mock.return_value = [{"cell": 1}, {"cell": 2}, {"cell": 3}]
            rows_mock.return_value = [{"row": 1}]

            result = workflow.normalize_business_infos(
                symbols=["005930"],
                start_year=2026,
                end_year=2026,
                workers=4,
            )

        self.assertEqual(result, written_paths)
        parse_mock.assert_called_once_with([Path("a.html"), Path("b.html")], max_workers=4)
        write_mock.assert_called_once_with(["doc1", "doc2"])

    def test_main_dispatches_business_info_target_for_kr(self):
        workflow = _load_workflow_with_stubs()
        argv = [
            "normalize",
            "--target",
            "business-info",
            "--symbols",
            "005930,105560",
            "--start-year",
            "2026",
            "--end-year",
            "2026",
            "--workers",
            "4",
        ]

        with (
            patch.object(sys, "argv", argv),
            patch.object(workflow, "normalize_business_infos") as business_mock,
            patch.object(workflow, "normalize_all_statements") as statements_mock,
        ):
            workflow.main()

        statements_mock.assert_not_called()
        business_mock.assert_called_once_with(
            symbols=["005930", "105560"],
            start_year=2026,
            end_year=2026,
            workers=4,
        )

    def test_main_default_target_preserves_statement_behavior(self):
        workflow = _load_workflow_with_stubs()

        with (
            patch.object(sys, "argv", ["normalize"]),
            patch.object(workflow, "normalize_business_infos") as business_mock,
            patch.object(workflow, "normalize_all_statements") as statements_mock,
        ):
            workflow.main()

        statements_mock.assert_called_once_with()
        business_mock.assert_not_called()

    def test_main_passes_inclusive_year_range_to_kr_statements(self):
        workflow = _load_workflow_with_stubs()

        with (
            patch.object(
                sys,
                "argv",
                [
                    "normalize",
                    "--market",
                    "kr",
                    "--target",
                    "statements",
                    "--start-year",
                    "2021",
                    "--end-year",
                    "2026",
                ],
            ),
            patch.object(workflow, "normalize_all_statements") as statements_mock,
        ):
            workflow.main()

        statements_mock.assert_called_once_with(start_year=2021, end_year=2026)

    def test_normalize_all_statements_uses_inclusive_year_bounds(self):
        workflow = _load_workflow_with_stubs()
        dependency = Path(__file__)

        with (
            patch.object(
                workflow,
                "kospi_kosdaq_corp_list",
                return_value=pd.DataFrame({"stock_code": ["005930"]}),
            ),
            patch.object(workflow, "normalization_dependency_paths", return_value=[dependency]),
            patch.object(
                workflow,
                "build_normalization_tasks",
                return_value=([], 0, 0),
            ) as build_tasks_mock,
            patch.object(workflow, "consolidate_statement_snapshots", return_value=None),
            patch.object(workflow, "consolidate_statement_debug_snapshots", return_value=None),
            patch.object(workflow, "remove_legacy_statement_snapshots", return_value=0),
        ):
            workflow.normalize_all_statements(start_year=2021, end_year=2026)

        self.assertEqual(build_tasks_mock.call_args.kwargs["start_year"], 2021)
        self.assertEqual(build_tasks_mock.call_args.kwargs["end_year"], 2027)

    def test_business_info_target_rejects_us_market(self):
        workflow = _load_workflow_with_stubs()

        with patch.object(sys, "argv", ["normalize", "--market", "us", "--target", "business-info"]):
            with self.assertRaisesRegex(ValueError, "only supported for --market kr"):
                workflow.main()


if __name__ == "__main__":
    unittest.main()
