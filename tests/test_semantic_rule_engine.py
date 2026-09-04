from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from engine.semantic import (
    AccountingRegimeDetector,
    AccountingRegimeFamily,
    Comparability,
    DartHtmlDocumentAdapter,
    DocumentDialect,
    FinancialSemanticNormalizer,
    NarrativeAccountScanner,
    NodeKind,
    RelationType,
    SpacyPatternIndex,
    build_document_ir_from_rows,
    compile_v2_mapping_rule,
    load_semantic_mapping_rules,
    reconstruct_html_table_grid,
)
from engine.semantic.coverage import (
    canonical_rule_coverage,
    migration_coverage,
    migration_integrity,
)
from engine.semantic.integrity import static_sign_policy_audit
from engine.transformers._internal.dart_filings import (
    ContextEngine,
    RuleEngine,
    SignPolicyEngine,
    apply_amount_policy,
    apply_cash_direction,
    detect_indent_level,
    get_statement_type_from_text,
    is_header_paragraph,
    load_comment_extraction_rules,
    normalize_account_name,
    normalize_statement_type,
    parse_amount,
    parse_unit_factor,
)


ROOT = Path(__file__).resolve().parents[1]
RULE_ROOT = ROOT / "data-lake" / "meta" / "rules"
LEGACY = RULE_ROOT / "kr_mapping.yaml"
V2 = RULE_ROOT / "semantic_kr_v2.yaml"
CANONICAL = ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
SIGN = RULE_ROOT / "sign_policy_common.yaml"


def _engine(path: Path = V2) -> RuleEngine:
    return RuleEngine.from_files(
        canonical_csv_path=CANONICAL,
        rule_paths=[path],
        sign_policy_path=SIGN,
    )


def test_legacy_mapping_rules_are_migrated_losslessly_by_source_index():
    legacy = load_semantic_mapping_rules([LEGACY], text_normalizer=normalize_account_name)
    migrated = load_semantic_mapping_rules([V2], text_normalizer=normalize_account_name)
    migrated_legacy = {
        rule.source_index: rule
        for rule in migrated.normalization_rules
        if rule.source_file.endswith("kr_mapping.yaml")
    }

    assert len(legacy.normalization_rules) == 126
    assert len(migrated_legacy) == 126
    for index, expected in enumerate(legacy.normalization_rules):
        actual = migrated_legacy[index]
        assert actual.priority == expected.priority
        assert actual.applies == expected.applies
        assert actual.label == expected.label
        assert actual.context == expected.context
        assert actual.constraints == expected.constraints
        assert actual.emit == expected.emit
        assert actual.reason == expected.reason


def test_all_rule_families_have_full_migration_coverage():
    coverage = migration_coverage(V2)

    assert coverage.mapping_source_rules == coverage.mapping_migrated_rules == 126
    assert coverage.context_source_rules == coverage.context_migrated_rules == 21
    assert coverage.comment_source_rules == coverage.comment_migrated_rules == 9
    assert coverage.sign_policy_source_entries == coverage.sign_policy_migrated_entries
    assert coverage.native_k_gaap_rules == 26
    assert coverage.native_common_rules == 3
    assert coverage.coverage_pct == 100.0
    assert migration_integrity(V2, project_root=ROOT)["all_sources_match"] is True


def test_v2_context_comment_and_sign_rules_execute_like_legacy_bundle():
    legacy_context = ContextEngine.from_yaml(RULE_ROOT / "context_kr.yaml")
    v2_context = ContextEngine.from_yaml(V2)
    rows = [
        {
            "statement_type": "IS",
            "original_account_name": "당기순이익",
            "amount": "0",
            "indent_level": 0,
        },
        {
            "statement_type": "IS",
            "original_account_name": "지배기업 소유주지분",
            "amount": "100",
            "indent_level": 1,
        },
    ]
    assert v2_context.enrich_context(rows) == legacy_context.enrich_context(rows)

    legacy_comments = load_comment_extraction_rules([RULE_ROOT / "comment_kr.yaml"])
    v2_comments = load_comment_extraction_rules([V2])
    assert len(v2_comments) == len(legacy_comments) == 9
    assert [rule["target_patterns"] for rule in v2_comments] == [
        rule["target_patterns"] for rule in legacy_comments
    ]

    legacy_sign = SignPolicyEngine.from_yaml(SIGN)
    v2_sign = SignPolicyEngine.from_yaml(V2)
    assert v2_sign.decide("CF", "CAPEX_PPE") == legacy_sign.decide("CF", "CAPEX_PPE")


def test_k_gaap_rules_preserve_historical_semantics_and_comparability():
    engine = _engine()
    cases = [
        ("BS", "외상매출금", "TRADE_RECEIVABLES", "AGGREGATION_DIFFERENCE"),
        ("BS", "감가상각누계액", "ACCUMULATED_DEPRECIATION", "EXACT"),
        ("BS", "이연자산", "DEFERRED_ASSETS_K_GAAP", "ACCOUNTING_POLICY_BREAK"),
        ("IS", "경상이익", "ORDINARY_INCOME", "ACCOUNTING_POLICY_BREAK"),
    ]
    for fs_type, label, canonical_id, comparability in cases:
        result = engine.map_row(
            {
                "statement_type": fs_type,
                "original_account_name": label,
                "raw_amount": 100,
                "period": "2008.12",
                "accounting_regime": "K_GAAP",
                "document_dialect": "DART_LEGACY_HTML",
            }
        )
        assert result.canonical_account_id == canonical_id
        assert result.comparability == comparability
        assert result.semantic_provenance["rule_version"] == 2


def test_regime_detector_uses_document_evidence_over_year_hint():
    detector = AccountingRegimeDetector()
    result = detector.detect(
        "본 재무제표는 한국채택국제회계기준에 따라 작성되었습니다. 연결재무상태표",
        filing_date=date(2010, 12, 31),
    )

    assert result.regime.family == AccountingRegimeFamily.K_IFRS
    assert result.scores[AccountingRegimeFamily.K_IFRS] > result.scores[AccountingRegimeFamily.K_GAAP]
    assert any(item.kind == "filing_year_hint" for item in result.evidence)

    historical = detector.detect(
        "연결재무제표기준 및 일반적으로 인정된 회계원칙에 따라 작성했습니다.",
        filing_date=date(2012, 12, 31),
    )
    assert historical.regime.family == AccountingRegimeFamily.K_GAAP
    assert any(
        item.kind == "explicit_accounting_policy" for item in historical.evidence
    )


def test_spacy_pattern_index_matches_korean_substrings_without_mecab():
    index = SpacyPatternIndex(["매출채권", "대손충당금"])

    analysis = index.analyze("유동매출채권순액")

    assert "매출채권" in analysis.phrases
    assert "대손충당금" not in analysis.phrases


def test_document_ir_recovers_parent_child_accounting_graph():
    rows = [
        {
            "statement_type": "BS",
            "table_index": 0,
            "row_index": 0,
            "original_account_name": "유형자산",
            "indent_level": 0,
            "has_children": True,
        },
        {
            "statement_type": "BS",
            "table_index": 0,
            "row_index": 1,
            "original_account_name": "감가상각누계액",
            "indent_level": 1,
            "has_children": False,
        },
    ]
    document = build_document_ir_from_rows(
        rows,
        document_id="test",
        text_normalizer=normalize_account_name,
    )
    child = document.nodes["test:table:BS:0:row:1"]

    assert child.semantic_address.row_header_path == ("유형자산", "감가상각누계액")
    parents = document.related(child.node_id, RelationType.PARENT_OF, outgoing=False)
    assert any(parent.raw_text == "유형자산" for parent in parents)


def test_html_grid_expands_rowspan_and_colspan():
    soup = BeautifulSoup(
        "<table><tr><th rowspan='2'>계정</th><th colspan='2'>당기</th></tr>"
        "<tr><th>분기</th><th>누적</th></tr><tr><td>매출액</td><td>1</td><td>3</td></tr></table>",
        "lxml",
    )
    grid = reconstruct_html_table_grid(soup.table)

    assert len(grid) == 3
    assert len(grid[0]) == 3
    assert grid[0][0] is grid[1][0]
    assert grid[0][1] is grid[0][2]

    malformed = BeautifulSoup(
        "<table><tr><td rowspan='not-a-number'>값</td></tr></table>", "lxml"
    )
    assert len(reconstruct_html_table_grid(malformed.table)) == 1


def test_legacy_k_gaap_statement_and_label_conventions_are_normalized():
    assert normalize_statement_type("연결대차대조표") == "BS"
    assert get_statement_type_from_text("대 차 대 조 표") == "BS"
    assert normalize_account_name("【유동자산】") == "유동자산"
    assert detect_indent_level("\xa0\xa0감가상각누계액") == 2

    primary = BeautifulSoup(
        "<p class='table-group'>연 결 대 차 대 조 표</p><table class='nb'></table>",
        "lxml",
    )
    note = BeautifulSoup(
        "<p>22. 포괄손익계산서</p><p>당기 포괄손익 내역은 다음과 같습니다.</p>",
        "lxml",
    )
    assert is_header_paragraph(primary.p) is True
    assert is_header_paragraph(note.p) is False


def test_reported_canonical_layers_keep_raw_fact_and_pit_identity():
    engine = _engine()
    normalizer = FinancialSemanticNormalizer(engine.semantic_engine)
    reported = normalizer.reported_fact_from_row(
        {
            "statement_type": "BS",
            "original_account_name": "외상매출금",
            "amount_raw": "1,234",
            "unit": "백만원",
            "period": "2008.12",
        },
        entity_id="001",
        filing_id="F-1",
        revision_id="R-1",
        regime=AccountingRegimeFamily.K_GAAP,
        dialect=DocumentDialect.DART_LEGACY_HTML,
    )
    canonical = normalizer.normalize(reported)

    assert reported.raw_label == "외상매출금"
    assert reported.numeric_value == Decimal(1_234_000_000)
    assert canonical.canonical_id == "TRADE_RECEIVABLES"
    assert canonical.reported_fact is reported
    assert canonical.identity.filing_id == "F-1"
    assert canonical.identity.revision_id == "R-1"


def test_pre_scaled_raw_amount_is_not_scaled_twice():
    normalizer = FinancialSemanticNormalizer(_engine().semantic_engine)
    reported = normalizer.reported_fact_from_row(
        {
            "statement_type": "BS",
            "original_account_name": "외상매출금",
            "raw_amount": "1234000000",
            "unit_factor": "1000000",
            "period": "2008.12",
        },
        regime=AccountingRegimeFamily.K_GAAP,
    )

    assert reported.numeric_value == Decimal(1_234_000_000)


def test_eps_source_value_ignores_surrounding_statement_display_unit():
    normalizer = FinancialSemanticNormalizer(_engine().semantic_engine)
    reported = normalizer.reported_fact_from_row(
        {
            "statement_type": "IS",
            "original_account_name": "기본주당이익",
            "amount_raw": "1,234",
            "unit": "백만원",
            "unit_factor": "1000000",
            "period": "2024.12",
        }
    )
    canonical = normalizer.normalize(reported)

    assert reported.numeric_value == Decimal(1_234)
    assert reported.unit_multiplier == Decimal(1)
    assert canonical.canonical_id == "BASIC_EPS"


def test_every_v2_target_exists_in_canonical_catalog():
    coverage = canonical_rule_coverage(V2, CANONICAL, text_normalizer=normalize_account_name)

    assert coverage["unknown_target_ids"] == []
    assert coverage["covered_catalog_count"] > 0


def test_v2_rule_ir_fails_closed_on_unknown_dsl_operations():
    with pytest.raises(ValueError, match="unsupported fields"):
        compile_v2_mapping_rule(
            {
                "id": "unsafe_rule",
                "version": 2,
                "phase": "normalize",
                "applies": {"statement_types": ["BS"]},
                "match": {"label": {"exact_any": ["현금"]}},
                "emit": {"canonical_id": "CASH_AND_EQUIVALENTS"},
                "python_callback": "read_filesystem",
            },
            text_normalizer=normalize_account_name,
        )


def test_narrative_scanner_captures_only_unambiguous_unit_bearing_fact():
    scanner = NarrativeAccountScanner(
        {
            "연구개발비": ["RND"],
            "매출채권": ["TRADE_RECEIVABLES"],
            "재고자산": ["INVENTORIES"],
        }
    )
    candidates = scanner.scan_text("당기 연구개발비는 1,234백만원입니다.")

    assert len(candidates) == 1
    assert candidates[0].canonical_ids == ("RND",)
    assert candidates[0].value_krw == Decimal(1_234_000_000)
    assert candidates[0].review_required is False
    assert scanner.confirmed(candidates) == candidates

    negative = scanner.scan_text("당기 연구개발비는 (-)1.5억원입니다.")
    assert len(negative) == 1
    assert negative[0].value_krw == Decimal(-150_000_000)
    assert scanner.scan_text("당기 연구개발비는 1.5억원)입니다.") == ()


def test_narrative_scanner_fail_closes_multiple_accounts_and_amounts():
    scanner = NarrativeAccountScanner(
        {
            "매출채권": ["TRADE_RECEIVABLES"],
            "재고자산": ["INVENTORIES"],
        }
    )
    candidates = scanner.scan_text(
        "매출채권과 재고자산은 각각 100백만원 및 200백만원입니다."
    )

    assert candidates
    assert all(candidate.review_required for candidate in candidates)
    assert scanner.confirmed(candidates) == ()
    assert scanner.scan_text("2024년 매출채권 회수 정책을 변경했습니다.") == ()


@pytest.mark.parametrize(
    ("unit_text", "factor"),
    [
        ("(단위: 조원)", 1_000_000_000_000),
        ("(단위: 십억원)", 1_000_000_000),
        ("(단위: 억원)", 100_000_000),
        ("(단위: 백만원)", 1_000_000),
        ("(단위: 천원)", 1_000),
    ],
)
def test_unit_factor_and_fractional_display_unit_conversion(unit_text: str, factor: int):
    assert parse_unit_factor(unit_text) == factor
    assert parse_amount("1.5", factor) == int(Decimal("1.5") * factor)


def test_reported_sign_and_cash_flow_direction_are_separate_axes():
    assert parse_amount("(1,234)", 1_000_000) == -1_234_000_000
    assert parse_amount("△1,234", 1_000) == -1_234_000
    assert apply_amount_policy(-100, "abs") == 100
    assert apply_amount_policy(100, "neg_abs") == -100
    assert apply_cash_direction(-100, "inflow") == 100
    assert apply_cash_direction(100, "outflow") == -100
    assert parse_amount("100 200 300", 1) == 0


def test_static_sign_policy_covers_expected_cash_flow_directions():
    audit = static_sign_policy_audit(V2)

    assert audit["expected_direction_count"] == 12
    assert audit["error_count"] == 0
