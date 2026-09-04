from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from engine.semantic import (
    AccountingInvariantAuditor,
    DisclosureHtmlParser,
    DisclosureSourceType,
    FactorDependencyGraph,
    NarrativeAccountScanner,
    NarrativeRelation,
    NarrativeRelationExtractor,
    Scope,
    UnmappedCategory,
    UnmappedClassifier,
    capex_direction_correction,
)
from engine.semantic import InvariantContext
from engine.transformers.filings import normalize_account_name
from engine.transformers.filings import RuleEngine
from scripts.audit_historical_semantic_parsing import (
    has_explicit_source_amount,
    load_security_context,
)


class SemanticRuleEngineV3Test(unittest.TestCase):
    @staticmethod
    def mapping_engine() -> RuleEngine:
        return RuleEngine.from_files(
            canonical_csv_path=Path("data-lake/meta/CanonicalAccount.csv"),
            rule_paths=[Path("data-lake/meta/rules/semantic_kr_v2.yaml")],
            sign_policy_path=Path("data-lake/meta/rules/semantic_kr_v2.yaml"),
        )

    def test_historical_financial_sector_and_gross_flow_concepts_stay_distinct(self):
        engine = self.mapping_engine()
        cases = (
            ("BS", "대출채권", "LOAN_RECEIVABLES_FINANCIAL"),
            ("BS", "예수부채", "DEPOSIT_LIABILITIES_FINANCIAL"),
            ("BS", "매도가능증권", "AVAILABLE_FOR_SALE_SECURITIES"),
            ("IS", "이자수익", "INTEREST_INCOME"),
            ("CF", "재무활동으로인한현금유입액", "CFF_GROSS_INFLOW"),
            ("CF", "기초의현금", "CF_CASH_BEGIN"),
            ("BS", "연결자본잉여금", "CAPITAL_SURPLUS"),
        )
        for statement_type, label, expected in cases:
            result = engine.map_row(
                {
                    "statement_type": statement_type,
                    "original_account_name": label,
                    "raw_amount": 100,
                    "period": "2008.12",
                    "accounting_regime": "K_GAAP",
                    "document_dialect": "DART_LEGACY_HTML",
                }
            )
            self.assertEqual(result.canonical_account_id, expected, label)

    def test_security_context_registry_accepts_gics_column_names(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "security_context.csv"
            path.write_text(
                "stock_code,gics_sector_code,gics_industry_group_code\n"
                "5930,45,4530\n",
                encoding="utf-8",
            )

            sectors, industry_groups = load_security_context(path)

        self.assertEqual(sectors["005930"], "45")
        self.assertEqual(industry_groups["005930"], "4530")

    def test_accounting_invariants_only_consume_explicit_source_numbers(self):
        self.assertTrue(has_explicit_source_amount("0"))
        self.assertTrue(has_explicit_source_amount("(1,000)"))
        self.assertFalse(has_explicit_source_amount(""))
        self.assertFalse(has_explicit_source_amount("-"))

    def test_sector_and_document_area_jointly_disambiguate_generic_account(self):
        engine = self.mapping_engine()
        base = {
            "statement_type": "BS",
            "original_account_name": "예수금",
            "raw_amount": 100,
            "period": "2008.12",
            "accounting_regime": "K_GAAP",
            "document_dialect": "DART_LEGACY_HTML",
            "source_type": "FINANCIAL_STATEMENT",
        }

        financial = engine.map_row({**base, "sector_code": "40"})
        industrial = engine.map_row({**base, "sector_code": "15"})
        business_section = engine.map_row(
            {
                **base,
                "sector_code": "40",
                "source_type": "BUSINESS_CONTENT",
            }
        )

        self.assertEqual(financial.canonical_account_id, "DEPOSIT_LIABILITIES_FINANCIAL")
        self.assertEqual(industrial.canonical_account_id, "UNMAPPED")
        self.assertEqual(business_section.canonical_account_id, "UNMAPPED")
        self.assertEqual(
            financial.semantic_provenance["normalized_inputs"]["sector_code"], "40"
        )
        self.assertEqual(
            financial.semantic_provenance["normalized_inputs"]["source_type"],
            "FINANCIAL_STATEMENT",
        )
        debug = engine.map_rows(
            [
                {
                    **base,
                    "sector_code": "40",
                    "industry_group_code": "4010",
                    "table_kind": "PRIMARY_STATEMENT",
                }
            ]
        ).iloc[0]
        self.assertEqual(debug["source_type"], "FINANCIAL_STATEMENT")
        self.assertEqual(debug["sector_code"], "40")
        self.assertEqual(debug["industry_group_code"], "4010")
        self.assertEqual(debug["table_kind"], "PRIMARY_STATEMENT")

    def test_narrative_candidate_keeps_but_downgrades_context_mismatch(self):
        engine = self.mapping_engine()
        extractor = NarrativeRelationExtractor(
            NarrativeAccountScanner.from_ruleset(engine.semantic_ruleset)
        )

        candidates = extractor.extract(
            "고객 예수금은 100억원입니다.",
            source_type=DisclosureSourceType.BUSINESS_CONTENT,
            document_id="doc",
            section_path=("사업의 내용",),
            source_uri="business.html",
            source_hash="abc",
            sector_code="40",
        )
        deposit = next(
            candidate for candidate in candidates if candidate.matched_alias == "예수금"
        )

        self.assertFalse(deposit.context_eligible)
        self.assertTrue(deposit.review_required)
        self.assertIn("rule_context_not_satisfied", deposit.reasons)

    def test_legacy_ordinal_prefix_is_removed_without_losing_maturity_semantics(self):
        self.assertEqual(normalize_account_name("ⅰ영업활동으로 인한 현금흐름"), "영업활동으로인한현금흐름")
        self.assertEqual(normalize_account_name("1당기순이익"), "당기순이익")
        self.assertEqual(normalize_account_name("1년 이내 만기도래분"), "1년이내만기도래분")

    def test_composite_korean_money_is_one_amount(self):
        scanner = NarrativeAccountScanner({"매출액": ["REVENUE"]})

        mentions = scanner.extract_money_mentions("당기 매출액은 133조 8,734억원입니다.")

        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0].value_krw, Decimal("133873400000000"))

    def test_relation_extraction_preserves_direction_period_and_qualifier(self):
        scanner = NarrativeAccountScanner({"유형자산": ["CAPEX_PPE"]})
        extractor = NarrativeRelationExtractor(scanner)

        candidates = extractor.extract(
            "당기 유형자산 취득에 약 120억원을 지급하였습니다.",
            source_type=DisclosureSourceType.FINANCIAL_NOTES,
            document_id="doc",
            section_path=("유형자산",),
            source_uri="note.html",
            source_hash="abc",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].relation, NarrativeRelation.ACQUIRED)
        self.assertEqual(candidates[0].cash_direction, "outflow")
        self.assertEqual(candidates[0].period_role, "CURRENT")
        self.assertFalse(candidates[0].auto_emit_eligible)

    def test_ambiguous_period_never_auto_emits(self):
        scanner = NarrativeAccountScanner({"매출액": ["REVENUE"]})
        extractor = NarrativeRelationExtractor(scanner)

        candidates = extractor.extract(
            "전기 매출액은 100억원이고 당기 매출액은 120억원입니다.",
            source_type=DisclosureSourceType.BUSINESS_CONTENT,
            document_id="doc",
            section_path=(),
            source_uri="business.html",
            source_hash="abc",
        )

        self.assertTrue(candidates)
        self.assertTrue(all(not candidate.auto_emit_eligible for candidate in candidates))

    def test_unmapped_taxonomy_is_closed_and_deterministic(self):
        classifier = UnmappedClassifier()
        self.assertEqual(classifier.classify("합 계").category, UnmappedCategory.SUBTOTAL_OR_PRESENTATION_ONLY)
        self.assertEqual(classifier.classify("자산총계").category, UnmappedCategory.SUBTOTAL_OR_PRESENTATION_ONLY)
        self.assertEqual(classifier.classify("생산능력").category, UnmappedCategory.NON_FINANCIAL)
        self.assertEqual(classifier.classify("").category, UnmappedCategory.LOW_INFORMATION)

    def test_factor_graph_resolves_transitive_source_dependencies(self):
        graph = FactorDependencyGraph.from_javascript(Path("scripts/calculate_factor_coverage.js"))

        self.assertEqual(len(graph.factors), 129)
        self.assertIn("capx", graph.factors)
        self.assertIn("CAPEX_PPE", graph.canonical_dependencies("fcf"))
        self.assertIn("CAPEX_INTANG", graph.canonical_dependencies("fcf"))
        self.assertIn("fcff", graph.affected_factors("CAPEX_PPE"))

    def test_factor_input_coverage_requires_every_dependency(self):
        graph = FactorDependencyGraph(
            direct_canonical={"fcf": frozenset({"CFO", "CAPEX_PPE"})},
            direct_factors={"fcf": frozenset()},
        )

        coverage = graph.dependency_coverage({"CFO"})

        self.assertEqual(coverage["covered_factor_count"], 0)
        self.assertFalse(coverage["factors"][0]["factor_input_available"])

    def test_invariants_are_candidate_evidence_only(self):
        result = AccountingInvariantAuditor().audit(
            {"TOTAL_ASSETS": 100, "TOTAL_LIABILITIES": 40, "TOTAL_EQUITY": 60}
        )

        self.assertEqual(result[0].status, "PASS")
        self.assertTrue(result[0].candidate_only)

    def test_invariants_fail_closed_when_semantic_dimensions_are_mixed(self):
        result = AccountingInvariantAuditor().audit(
            {"TOTAL_ASSETS": 100, "TOTAL_LIABILITIES": 40, "TOTAL_EQUITY": 60},
            context=InvariantContext(scope_consistent=False),
        )

        balance_sheet = next(
            item
            for item in result
            if item.invariant_id == "BS_ASSETS_EQUALS_LIABILITIES_PLUS_EQUITY"
        )
        self.assertEqual(balance_sheet.status, "NOT_TESTABLE")
        self.assertEqual(
            balance_sheet.reason, "semantic_dimensions_are_not_comparable"
        )

    def test_cash_invariant_uses_canonical_cash_ids_and_keeps_zero_end_balance(self):
        result = AccountingInvariantAuditor().audit(
            {
                "CF_CASH_BEGIN": 100,
                "CFO": 30,
                "CFI": -80,
                "CFF": -50,
                "FX_EFFECT_CASH": 0,
                "CF_CASH_END": 0,
                "CASH_AND_EQUIVALENTS": 999,
            }
        )

        cash = next(item for item in result if item.invariant_id == "CF_BEGIN_PLUS_FLOWS_EQUALS_END")
        self.assertEqual(cash.status, "PASS")
        self.assertEqual(cash.right_value, Decimal(0))

    def test_cash_invariant_does_not_assume_missing_fx_effect_is_zero(self):
        result = AccountingInvariantAuditor().audit(
            {
                "CF_CASH_BEGIN": 100,
                "CFO": 30,
                "CFI": -20,
                "CFF": -5,
                "CF_CASH_END": 105,
            }
        )

        cash = next(
            item
            for item in result
            if item.invariant_id == "CF_BEGIN_PLUS_FLOWS_EQUALS_END"
        )
        self.assertEqual(cash.status, "NOT_TESTABLE")
        self.assertEqual(cash.reason, "required_fact_missing")

    def test_capex_semantic_correction_has_distinct_pit_identity(self):
        base = {
                "company_name": "005930", "period": "2020.12", "statement_type": "CF",
                "original_account_name": "유형자산의 처분", "raw_amount": "100",
                "canonical_account_id": "CAPEX_PPE", "cash_direction": "inflow",
                "debug_file": "kr_normalized_005930.debug.csv",
            }
        correction = capex_direction_correction(
            {**base, "source_row_ordinal": 10},
            source_hash="abc",
        )
        repeated = capex_direction_correction(
            {**base, "source_row_ordinal": 11},
            source_hash="abc",
        )

        self.assertIsNotNone(correction)
        self.assertNotEqual(correction.old_fact_id, correction.new_fact_id)
        self.assertNotEqual(correction.old_fact_id, repeated.old_fact_id)
        self.assertIn("PPE_DISPOSAL_PROCEEDS", correction.new_semantics)

    def test_full_disclosure_html_is_preserved_and_normalized(self):
        html = """
        <html><body>
          <p class="section-2">10. 유형자산</p>
          <p>당기 유형자산 취득에 120억원을 지급하였습니다.</p>
          <table><tr><th>구분</th><th>당기</th></tr><tr><td>유형자산</td><td>120억원</td></tr></table>
        </body></html>
        """
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "note.html"
            path.write_text(html, encoding="utf-8")
            parser = DisclosureHtmlParser(
                NarrativeRelationExtractor(NarrativeAccountScanner({"유형자산": ["CAPEX_PPE"]}))
            )
            document = parser.parse(
                path,
                source_type=DisclosureSourceType.FINANCIAL_NOTES,
                sector_code="40",
                industry_group_code="4010",
            )

        self.assertGreaterEqual(len(document.sections), 2)
        self.assertEqual(len(document.tables), 1)
        self.assertTrue(document.candidates)
        self.assertTrue(any(candidate.scope == Scope.UNKNOWN for candidate in document.candidates))
        self.assertTrue(all(candidate.sector_code == "40" for candidate in document.candidates))
        self.assertTrue(all(candidate.industry_group_code == "4010" for candidate in document.candidates))
        self.assertEqual(
            {candidate.table_kind for candidate in document.candidates},
            {"NARRATIVE", "TABLE"},
        )


if __name__ == "__main__":
    unittest.main()
