from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.transformers._internal.kr_business_extractor import (
    DEFAULT_RULE_PATH,
    BusinessInfoRuleError,
    document_to_cell_records,
    document_to_row_records,
    document_to_section_records,
    document_to_table_records,
    load_business_info_rules,
    parse_business_info_files,
    parse_business_info_html,
    write_business_info_csvs,
)


SAMPLE_ROOT = Path("data-lake/bronze/dart/business-info")
SAMPLE_CODES = [
    "096770",
    "010950",
    "078930",
    "005490",
    "051910",
    "010130",
    "000720",
    "028260",
    "034020",
    "005380",
    "000270",
    "161390",
    "097950",
    "004370",
    "271560",
    "068270",
    "207940",
    "128940",
    "105560",
    "055550",
    "086790",
    "005930",
    "000660",
    "034220",
    "017670",
    "030200",
    "035420",
    "015760",
    "036460",
    "051600",
    "088260",
    "330590",
    "357120",
]


def _sample_path(stock_code: str, period: str = "2026.03") -> Path:
    path = SAMPLE_ROOT / stock_code / f"business_info_({period}).html"
    if path.exists():
        return path
    files = sorted((SAMPLE_ROOT / stock_code).glob("business_info_(*).html"))
    if not files:
        raise FileNotFoundError(stock_code)
    return files[-1]


class KrBusinessInfoParserTest(unittest.TestCase):
    def test_rule_file_loads_and_validates_required_keys(self):
        rules = load_business_info_rules()

        self.assertIn("section_aliases", rules)
        self.assertIn("template_rules", rules)
        self.assertIn("table_kind_rules", rules)
        self.assertIn("header_detection_rules", rules)

        with TemporaryDirectory() as temp_dir:
            invalid_rule_path = Path(temp_dir) / "invalid.yaml"
            invalid_rule_path.write_text("section_aliases: {}\n", encoding="utf-8")

            with self.assertRaises(BusinessInfoRuleError):
                load_business_info_rules(invalid_rule_path)

    def test_standard_it_sample_detects_seven_sections_and_tables(self):
        doc = parse_business_info_html(_sample_path("005930"))

        self.assertEqual(doc.template_type, "standard")
        keys = [section.canonical_key for section in doc.sections]
        self.assertIn("overview", keys)
        self.assertIn("products_services", keys)
        self.assertIn("raw_materials_facilities", keys)
        self.assertIn("sales_orders", keys)
        self.assertGreaterEqual(len(doc.sections), 7)
        self.assertGreater(
            sum(len(section.tables) for section in doc.sections if section.canonical_key == "sales_orders"),
            0,
        )

    def test_financial_sample_detects_financial_template(self):
        doc = parse_business_info_html(_sample_path("105560"))

        self.assertEqual(doc.template_type, "financial")
        keys = {section.canonical_key for section in doc.sections}
        self.assertIn("business_status", keys)
        self.assertIn("derivatives", keys)
        self.assertIn("business_facilities", keys)
        self.assertIn("financial_soundness", keys)

    def test_mixed_sample_preserves_business_domains(self):
        doc = parse_business_info_html(_sample_path("005380"))

        self.assertEqual(doc.template_type, "mixed")
        domains = {section.business_domain for section in doc.sections if section.business_domain}
        self.assertIn("manufacturing_service", domains)
        self.assertIn("financial", domains)

    def test_real_estate_sample_keeps_not_applicable_sections(self):
        doc = parse_business_info_html(_sample_path("088260"))

        self.assertEqual(doc.template_type, "real_estate_light")
        not_applicable_sections = [
            section for section in doc.sections if section.is_not_applicable
        ]
        self.assertGreaterEqual(len(not_applicable_sections), 2)
        self.assertTrue(any("해당사항" in section.text for section in not_applicable_sections))

    def test_legacy_sample_uses_fallback_when_section_two_is_missing(self):
        doc = parse_business_info_html(_sample_path("005930", "2016.12"))

        self.assertEqual(doc.template_type, "legacy")
        self.assertGreaterEqual(len(doc.sections), 1)
        self.assertTrue(any(section.text for section in doc.sections))

    def test_rule_alias_change_changes_canonical_section_result(self):
        html = """
        <html><body>
          <p class="section-1"><a name="toc1">II. 사업의 내용</a></p>
          <p class="section-2"><a name="toc2">1. 회사 설명</a></p>
          <p>새 별칭으로만 매핑되는 설명입니다.</p>
        </body></html>
        """
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample_dir = root / "123456"
            sample_dir.mkdir()
            sample_path = sample_dir / "business_info_(2026.03).html"
            sample_path.write_text(html, encoding="utf-8")

            default_doc = parse_business_info_html(sample_path)
            self.assertEqual(default_doc.sections[0].canonical_key, "other")

            rule_text = DEFAULT_RULE_PATH.read_text(encoding="utf-8")
            rule_text = rule_text.replace('    - "사업의 개요"\n', '    - "회사 설명"\n', 1)
            custom_rule_path = root / "kr_business_info.yaml"
            custom_rule_path.write_text(rule_text, encoding="utf-8")

            custom_doc = parse_business_info_html(sample_path, rule_path=custom_rule_path)
            self.assertEqual(custom_doc.sections[0].canonical_key, "overview")

    def test_sample_set_batch_parse_has_no_fatal_errors(self):
        parsed = [parse_business_info_html(_sample_path(code)) for code in SAMPLE_CODES]

        self.assertEqual(len(parsed), len(SAMPLE_CODES))
        self.assertTrue(all(document.sections for document in parsed))
        self.assertTrue(all(document.template_type for document in parsed))

    def test_multithreaded_parse_preserves_order_and_results(self):
        paths = [_sample_path(code) for code in ["005930", "105560", "005380", "088260"]]

        sequential = parse_business_info_files(paths, max_workers=1)
        parallel = parse_business_info_files(paths, max_workers=4)

        self.assertEqual([document.stock_code for document in parallel], ["005930", "105560", "005380", "088260"])
        self.assertEqual(
            [(document.stock_code, document.period, document.template_type) for document in parallel],
            [(document.stock_code, document.period, document.template_type) for document in sequential],
        )

    def test_additional_legacy_samples_use_expanded_rules(self):
        doc = parse_business_info_html(_sample_path("000100", "2016.12"))
        keys = {section.canonical_key for section in doc.sections}

        self.assertEqual(doc.template_type, "legacy")
        self.assertIn("overview", keys)
        self.assertIn("products_services", keys)
        self.assertIn("raw_materials_facilities", keys)
        self.assertIn("contracts_rd", keys)

    def test_record_serializers_match_expected_columns(self):
        doc = parse_business_info_html(_sample_path("005930"))

        section_row = document_to_section_records(doc)[0]
        for key in [
            "market",
            "stock_code",
            "period",
            "report_code",
            "report_type",
            "parser_version",
            "source_uri",
            "source_html_hash",
            "source_path",
            "security_id",
            "section_key",
            "parsed_at",
        ]:
            self.assertIn(key, section_row)

        table_rows = document_to_table_records(doc)
        self.assertTrue(table_rows)
        for key in [
            "table_id",
            "html_table_hash",
            "table_kind",
            "section_title",
            "table_title",
            "context_before",
            "context_after",
            "header_paths_json",
        ]:
            self.assertIn(key, table_rows[0])
        self.assertIn("headers_json", table_rows[0])
        self.assertIn("rows_json", table_rows[0])

    def test_table_lineage_and_hash_fields_are_stable(self):
        doc = parse_business_info_html(_sample_path("005930"))
        table_rows = document_to_table_records(doc)

        self.assertTrue(table_rows)
        first_table = table_rows[0]
        self.assertRegex(first_table["table_id"], r"^KR_005930_202603_[A-Za-z0-9_]+_\d{3}_[0-9a-f]{8}$")
        self.assertRegex(first_table["html_table_hash"], r"^[0-9a-f]{16}$")
        self.assertRegex(first_table["source_html_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(first_table["source_uri"].startswith("data-lake/"))

        reparsed = parse_business_info_html(_sample_path("005930"))
        self.assertEqual(
            [row["table_id"] for row in table_rows[:5]],
            [row["table_id"] for row in document_to_table_records(reparsed)[:5]],
        )

    def test_cells_and_rows_preserve_header_paths_and_spans(self):
        html = """
        <html><body>
          <p class="section-2"><a name="toc2">2. 주요 제품</a></p>
          <table>
            <tr><th rowspan="2">부문</th><th colspan="2">제58기 1분기</th></tr>
            <tr><th>매출액</th><th>비중</th></tr>
            <tr><td>DX</td><td>526,547</td><td>39.3%</td></tr>
          </table>
        </body></html>
        """
        with TemporaryDirectory() as temp_dir:
            sample_dir = Path(temp_dir) / "005930"
            sample_dir.mkdir()
            sample_path = sample_dir / "business_info_(2026.03).html"
            sample_path.write_text(html, encoding="utf-8")

            doc = parse_business_info_html(sample_path)

        table = doc.sections[0].tables[0]
        self.assertEqual(table.headers, ["부문", "매출액", "비중"])
        self.assertEqual(table.header_paths[1], ["제58기 1분기", "매출액"])

        cell_rows = document_to_cell_records(doc)
        self.assertTrue(any(row["rowspan"] == 2 for row in cell_rows))
        self.assertTrue(any(row["colspan"] == 2 for row in cell_rows))
        self.assertTrue(any(row["data_type"] == "percent" for row in cell_rows))

        row_records = document_to_row_records(doc)
        self.assertEqual(len(row_records), 1)
        self.assertIn("제58기 1분기 > 매출액", row_records[0]["header_value_map_json"])

    def test_rule_table_kind_change_changes_parser_result(self):
        html = """
        <html><body>
          <p class="section-2"><a name="toc2">2. 주요 제품</a></p>
          <table><tr><td>[연구개발비용]</td></tr></table>
        </body></html>
        """
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample_dir = root / "005930"
            sample_dir.mkdir()
            sample_path = sample_dir / "business_info_(2026.03).html"
            sample_path.write_text(html, encoding="utf-8")

            default_doc = parse_business_info_html(sample_path)
            self.assertEqual(default_doc.sections[0].tables[0].table_kind, "title_block")

            rule_text = DEFAULT_RULE_PATH.read_text(encoding="utf-8")
            rule_text = rule_text.replace('  max_title_rows: 1\n', '  max_title_rows: 0\n', 1)
            custom_rule_path = root / "kr_business_info.yaml"
            custom_rule_path.write_text(rule_text, encoding="utf-8")

            custom_doc = parse_business_info_html(sample_path, rule_path=custom_rule_path)
            self.assertNotEqual(custom_doc.sections[0].tables[0].table_kind, "title_block")

    def test_write_business_info_csvs_creates_four_outputs(self):
        doc = parse_business_info_html(_sample_path("005930"))
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = write_business_info_csvs(
                [doc],
                section_output_path=root / "sections.csv",
                table_output_path=root / "tables.csv",
                cell_output_path=root / "cells.csv",
                row_output_path=root / "rows.csv",
            )

            self.assertEqual(len(paths), 4)
            for path in paths:
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 0)

    def test_write_business_info_csvs_partitions_by_stock_code_by_default(self):
        docs = [
            parse_business_info_html(_sample_path("005930", "2026.03")),
            parse_business_info_html(_sample_path("005930", "2016.12")),
            parse_business_info_html(_sample_path("105560", "2026.03")),
        ]
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path_groups = write_business_info_csvs(docs, output_dir=root)

            self.assertEqual(len(path_groups), 2)
            self.assertTrue((root / "005930" / "kr_business_info_sections.csv").exists())
            self.assertTrue((root / "105560" / "kr_business_info_sections.csv").exists())
            samsung_sections = (root / "005930" / "kr_business_info_sections.csv").read_text(encoding="utf-8-sig")
            self.assertIn("2026.03", samsung_sections)
            self.assertIn("2016.12", samsung_sections)


if __name__ == "__main__":
    unittest.main()
