from __future__ import annotations

from pathlib import Path
import json
import subprocess

import pandas as pd
import pytest

from engine.semantic import (
    AccountingInvariantAuditor,
    CompanyYearCompletenessAuditor,
    FactorDependencyGraph,
    GoldenCorpusEvaluator,
    MissingFactCause,
    build_kr_financial_availability_dataframe,
    classify_missing_fact,
    cluster_narrative_candidates,
    audit_portfolio_factor_drift,
    resolve_rule_bundle,
    validate_rule_manifest,
)
from engine.transformers.filings import RuleEngine
from engine.workflows._internal.normalize_workflow import (
    CANONICAL_CSV_PATH,
    SEMANTIC_MAPPING_RULE_PATH,
    SEMANTIC_SIGN_POLICY_PATH,
)
from engine.transformers.dividends import build_kr_dividend_pit_events_dataframe
from engine.transformers.factors import (
    read_annual_financials,
    read_quarterly_financials,
    read_ttm_financials,
)


pytestmark = pytest.mark.semantic


def test_factor_dependency_coverage_accepts_real_executable_fallback_paths() -> None:
    graph = FactorDependencyGraph.from_javascript(
        Path("scripts/calculate_factor_coverage.js")
    )
    every_observed_concept = {
        dependency
        for factor in graph.factors
        for dependency in graph.canonical_dependencies(factor)
    }
    fallback_only_concepts = {
        "LONG_TERM_DEBT_FALLBACK",
        "RETAINED_EARNINGS_FALLBACK",
        "INTEREST_EXPENSE_FALLBACK",
        "INTEREST_PAID_FALLBACK",
        "FINANCE_COST_FALLBACK",
        "EBITDA",
        "DEBT_NET_BORROWING",
    }

    coverage = graph.dependency_coverage(
        every_observed_concept - fallback_only_concepts
    )

    assert coverage["strict_union_covered_factor_count"] == 82
    assert coverage["covered_factor_count"] == 107
    assert coverage["factor_input_coverage_pct"] == 100.0
    by_factor = {row["factor"]: row for row in coverage["factors"]}
    assert by_factor["dltt"]["selected_dependency_paths"] == [
        ["LONG_TERM_DEBT"]
    ]
    assert by_factor["xint"]["selected_dependency_paths"] == [
        ["INTEREST_EXPENSE"]
    ]


def test_kr_dividend_events_are_publication_time_safe_and_common_stock_only() -> None:
    by_kind = pd.DataFrame(
        [
            {
                "stock_code": "000020",
                "bsns_year": 2015,
                "rcept_no": "20160330001826",
                "report_name": "annual",
                "stock_knd": "보통주",
                "per_share_cash_dividend_krw": 80,
            },
            {
                "stock_code": "000020",
                "bsns_year": 2015,
                "rcept_no": "20160330001826",
                "report_name": "annual",
                "stock_knd": "우선주",
                "per_share_cash_dividend_krw": 100,
            },
        ]
    )
    company = pd.DataFrame(
        [
            {
                "stock_code": "000020",
                "bsns_year": 2015,
                "rcept_no": "20160330001826",
                "report_name": "annual",
                "dividend_payout_ratio_pct": 39.46,
                "dividend_payment_amount_krw": 2_213_000_000,
            }
        ]
    )

    events = build_kr_dividend_pit_events_dataframe(by_kind, company)

    assert len(events) == 1
    event = events.iloc[0]
    assert event["security_id"] == "SEC_KR_000020"
    assert event["trade_date"] == "2016-03-30"
    assert event["fiscal_year"] == 2015
    assert event["dividend"] == 80
    assert event["payout_ratio"] == pytest.approx(0.3946)
    assert event["pit_safe"] is True or bool(event["pit_safe"])


def test_financial_availability_uses_latest_publication_and_rejects_preperiod_date() -> None:
    metadata = pd.DataFrame(
        [
            {
                "security_id": "SEC_KR_005930",
                "stock_code": "005930",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "period_end_date": "2020-12-31",
                "report_date": "2021-03-30",
                "rcept_no": "20210330000001",
                "source_type": "statement",
            },
            {
                "security_id": "SEC_KR_005930",
                "stock_code": "005930",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "period_end_date": "2020-12-31",
                "report_date": "2021-04-15",
                "rcept_no": "20210415000002",
                "source_type": "statement",
            },
            {
                "security_id": "SEC_KR_000020",
                "stock_code": "000020",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "period_end_date": "2020-12-31",
                "report_date": "2020-12-01",
                "rcept_no": "20201201000003",
                "source_type": "statement",
            },
        ]
    )

    availability, audit = build_kr_financial_availability_dataframe(metadata)

    assert availability.to_dict("records") == [
        {
            "security_id": "SEC_KR_005930",
            "fiscal_year": 2020,
            "financial_period": "2020-12-31",
            "trade_date": "2021-04-15",
            "rcept_no": "20210415000002",
            "pit_safe": True,
        }
    ]
    assert audit["preperiod_rejected_count"] == 1


def test_financial_availability_missing_columns_abstains_instead_of_crashing() -> None:
    availability, audit = build_kr_financial_availability_dataframe(
        pd.DataFrame([{"report_date": "2021-03-30"}])
    )

    assert availability.empty
    assert audit["output_count"] == 0


def test_company_year_completeness_separates_statement_core_factor_and_invariant_readiness() -> None:
    facts = pd.DataFrame(
        [
            {"statement_type": "BS", "canonical_account_id": "TOTAL_ASSETS"},
            {"statement_type": "BS", "canonical_account_id": "TOTAL_LIABILITIES"},
            {"statement_type": "BS", "canonical_account_id": "TOTAL_EQUITY"},
            {"statement_type": "IS", "canonical_account_id": "REVENUE"},
            {"statement_type": "IS", "canonical_account_id": "OPERATING_INCOME"},
            {"statement_type": "IS", "canonical_account_id": "NET_INCOME"},
        ]
    )

    result = CompanyYearCompletenessAuditor().assess("SEC_KR_005930", 2020, facts)

    assert result["statement_presence"] == {"BS": True, "IS": True, "CF": False}
    assert result["statement_core_complete"] == {"BS": True, "IS": True, "CF": False}
    assert result["core_fact_present_count"] == 6
    assert result["core_fact_expected_count"] == 9
    assert result["invariant_testable_count"] == 1
    assert result["company_year_complete"] is False


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"source_ingested": False}, MissingFactCause.SOURCE_NOT_INGESTED),
        ({"scope_known": False}, MissingFactCause.SCOPE_MISSING),
        ({"period_known": False}, MissingFactCause.PERIOD_MISSING),
        ({"dialect_supported": False}, MissingFactCause.HISTORICAL_DIALECT_GAP),
        ({"raw_candidate_present": True}, MissingFactCause.CONCEPT_NOT_MAPPED),
        ({"statement_complete": False}, MissingFactCause.STATEMENT_INCOMPLETE),
        ({}, MissingFactCause.CONCEPT_NOT_REPORTED),
        (
            {"canonical_present": True, "transformation_available": False},
            MissingFactCause.TRANSFORMATION_GAP,
        ),
    ],
)
def test_missing_fact_taxonomy_is_mutually_exclusive(kwargs, expected) -> None:
    assert classify_missing_fact(**kwargs) is expected


def test_narrative_clusters_report_company_year_syntax_relation_and_amount_ambiguity() -> None:
    report = cluster_narrative_candidates(
        [
            {
                "source_type": "FINANCIAL_NOTES",
                "source_uri": "C:/dart/005930/finance_statement_comment_(2012.12).html",
                "canonical_ids": ("CAPEX_PPE",),
                "matched_alias": "유형자산",
                "relation": "ACQUIRED",
                "period_role": "AMBIGUOUS",
                "scope": "UNKNOWN",
                "table_kind": "NARRATIVE",
                "context_eligible": True,
                "review_required": True,
                "auto_emit_eligible": False,
                "reasons": ("ambiguous_period", "multiple_nearby_amounts"),
                "source_text": "유형자산 취득액은 당기 10억원, 전기 20억원입니다.",
            }
        ]
    )

    cluster = report["clusters"][0]
    assert cluster["company_count"] == 1
    assert cluster["years"] == [2012]
    assert cluster["relation_counts"] == {"ACQUIRED": 1}
    assert cluster["amount_count_distribution"] == {"2": 1}
    assert cluster["period_ambiguity_count"] == 1
    assert cluster["scope_ambiguity_count"] == 1
    assert "<ACCOUNT>" in cluster["syntax_patterns"][0]["pattern"]
    assert cluster["syntax_patterns"][0]["pattern"].count("<AMOUNT>") == 2


def test_golden_corpus_evaluator_reports_precision_recall_and_abstention_separately() -> None:
    cases = [
        {"case_id": "tp", "expected_canonical_id": "A"},
        {"case_id": "fn_abstain", "expected_canonical_id": "B"},
        {"case_id": "fp", "expected_canonical_id": None},
        {"case_id": "wrong", "expected_canonical_id": "D"},
    ]
    predictions = {"tp": "A", "fn_abstain": None, "fp": "C", "wrong": "E"}

    report = GoldenCorpusEvaluator().evaluate(
        cases, lambda case: predictions[case["case_id"]]
    )

    assert report["true_positive_count"] == 1
    assert report["false_positive_count"] == 2
    assert report["false_negative_count"] == 2
    assert report["abstain_count"] == 1
    assert report["precision_pct"] == pytest.approx(100 / 3)
    assert report["recall_pct"] == pytest.approx(100 / 3)
    assert report["abstain_pct"] == 25.0


def test_portfolio_drift_reports_value_rank_decile_membership_ic_spread_and_turnover() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-02"] * 10 + ["2024-01-03"] * 10,
            "security_id": [f"S{i}" for i in range(10)] * 2,
            "old_value": list(range(10)) + list(range(10)),
            "new_value": list(range(10)) + list(range(9)) + [-1],
            "forward_return": [i / 100 for i in range(10)] * 2,
        }
    )

    report = audit_portfolio_factor_drift(frame, higher_is_better=True)

    assert report["value_changed_cell_count"] == 1
    assert report["decile_changed_cell_count"] >= 1
    assert report["top_decile_membership_changed_cell_count"] == 2
    assert report["ic"]["old_mean"] == pytest.approx(1.0)
    assert report["ic"]["new_mean"] < report["ic"]["old_mean"]
    assert report["long_short_return"]["old_mean"] == pytest.approx(0.09)
    assert report["cross_version_membership_turnover_mean"] > 0
    assert "old_mean" in report["time_series_turnover"]


def test_portfolio_drift_handles_asymmetric_missing_deciles() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-01-31"] * 4,
            "security_id": ["S1", "S2", "S3", "S4"],
            "old_value": [1.0, 2.0, None, 4.0],
            "new_value": [1.0, None, 3.0, 4.0],
            "forward_return": [0.0, 0.0, 0.0, 0.0],
        }
    )

    report = audit_portfolio_factor_drift(frame)

    assert report["date_count"] == 1
    assert report["value_changed_cell_count"] == 0
    assert report["decile_changed_cell_count"] == 0
    assert report["ic"] == {"old_mean": None, "new_mean": None, "delta": None}


def test_actual_annual_factor_input_excludes_capex_inflow(tmp_path: Path) -> None:
    financial_dir = tmp_path / "financials"
    financial_dir.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_account_id": "CFO",
                "canonical_account_name": "영업활동현금흐름",
                "original_account_name": "영업활동현금흐름",
                "statement_type": "CF",
                "period": "2020.12",
                "normalized_amount": 200,
                "cash_direction": "",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            },
            {
                "canonical_account_id": "CAPEX_PPE",
                "canonical_account_name": "유형자산취득",
                "original_account_name": "유형자산 처분",
                "statement_type": "CF",
                "period": "2020.12",
                "normalized_amount": 100,
                "cash_direction": "inflow",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            },
            {
                "canonical_account_id": "CAPEX_PPE",
                "canonical_account_name": "유형자산취득",
                "original_account_name": "유형자산 취득",
                "statement_type": "CF",
                "period": "2020.12",
                "normalized_amount": -40,
                "cash_direction": "outflow",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            },
        ]
    ).to_csv(financial_dir / "kr_normalized_005930.csv", index=False)

    result = read_annual_financials(
        "005930",
        financial_dir=financial_dir,
        report_metadata_path=tmp_path / "missing.csv",
    )

    assert result["CAPEX_PPE"].iat[0] == -40
    assert result["capx"].iat[0] == 40
    assert result["fcf"].iat[0] == 160


def test_strict_point_in_time_annual_input_abstains_without_report_metadata(
    tmp_path: Path,
) -> None:
    financial_dir = tmp_path / "financials"
    financial_dir.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_account_id": "TOTAL_ASSETS",
                "canonical_account_name": "자산총계",
                "original_account_name": "자산총계",
                "statement_type": "BS",
                "period": "2020.12",
                "normalized_amount": 100,
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            }
        ]
    ).to_csv(financial_dir / "kr_normalized_005930.csv", index=False)

    result = read_annual_financials(
        "005930",
        financial_dir=financial_dir,
        report_metadata_path=tmp_path / "missing.csv",
        require_report_metadata=True,
    )

    assert result.empty


def test_strict_point_in_time_annual_input_rejects_pre_period_report_date(
    tmp_path: Path,
) -> None:
    financial_dir = tmp_path / "financials"
    financial_dir.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_account_id": "TOTAL_ASSETS",
                "canonical_account_name": "자산총계",
                "original_account_name": "자산총계",
                "statement_type": "BS",
                "period": "2020.12",
                "normalized_amount": 100,
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "fiscal_quarter": 4,
            }
        ]
    ).to_csv(financial_dir / "kr_normalized_005930.csv", index=False)
    metadata_path = tmp_path / "report_metadata.csv"
    pd.DataFrame(
        [
            {
                "stock_code": "005930",
                "fiscal_year": 2020,
                "fiscal_month": 12,
                "report_date": "2020-01-01",
                "source_type": "statement",
            }
        ]
    ).to_csv(metadata_path, index=False)

    result = read_annual_financials(
        "005930",
        financial_dir=financial_dir,
        report_metadata_path=metadata_path,
        require_report_metadata=True,
    )

    assert result.empty


@pytest.mark.parametrize("reader", [read_quarterly_financials, read_ttm_financials])
def test_strict_point_in_time_periodic_input_abstains_without_report_metadata(
    tmp_path: Path,
    reader,
) -> None:
    financial_dir = tmp_path / "financials"
    financial_dir.mkdir()
    pd.DataFrame(
        [
            {
                "canonical_account_id": "REVENUE",
                "canonical_account_name": "매출액",
                "original_account_name": "매출액",
                "statement_type": "IS",
                "period": "2020.3",
                "normalized_amount": 100,
                "fiscal_year": 2020,
                "fiscal_month": 3,
                "fiscal_quarter": 1,
            }
        ]
    ).to_csv(financial_dir / "kr_normalized_005930.csv", index=False)

    result = reader(
        "005930",
        financial_dir=financial_dir,
        report_metadata_path=tmp_path / "missing.csv",
        require_report_metadata=True,
    )

    assert result.empty


def test_v6_manifest_carries_reproducibility_hashes_and_resolves_bundle() -> None:
    manifest_path = Path("data-lake/meta/rules/semantic_rule_manifest.json")
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))

    validate_rule_manifest(manifest, path=manifest_path)

    assert manifest["bundle_id"] == "arcana.semantic.kr.v6"
    assert manifest["schema"] == "arcana.semantic-rules/v4"
    assert manifest["engine"] == "arcana-financial-semantic-v6"
    assert manifest["golden_corpus_hash"].startswith("sha256:")
    assert manifest["test_suite_hash"].startswith("sha256:")
    assert resolve_rule_bundle(manifest_path).name == "semantic_kr_v6.yaml"


def test_v6_preserves_signed_tax_benefit_and_identity_passes() -> None:
    engine = RuleEngine.from_files(
        canonical_csv_path=CANONICAL_CSV_PATH,
        rule_paths=[SEMANTIC_MAPPING_RULE_PATH],
        sign_policy_path=SEMANTIC_SIGN_POLICY_PATH,
    )
    mapped = engine.map_rows(
        [
            {
                "company_name": "006920",
                "statement_type": "IS",
                "period": "2008.12",
                "original_account_name": "법인세비용(수익)",
                "raw_account_name": "법인세비용(수익)",
                "raw_amount": "-157234785",
                "amount_raw": "(-)157,234,785",
                "unit_factor": "1",
            }
        ],
        include_debug_cols=True,
    )

    assert mapped["canonical_account_id"].iat[0] == "TAX_EXPENSE"
    assert mapped["normalized_amount"].iat[0] == "-157234785"
    assert mapped["semantic_engine_version"].iat[0] == "6"
    evidence = AccountingInvariantAuditor().audit(
        {
            "PBT": 241_878_238,
            "TAX_EXPENSE": -157_234_785,
            "NET_INCOME": 399_113_023,
        }
    )
    income_identity = next(
        item
        for item in evidence
        if item.invariant_id == "IS_PBT_MINUS_TAX_EQUALS_NET_INCOME"
    )
    assert income_identity.status == "PASS"
    assert income_identity.residual == 0


def test_factor_coverage_javascript_applies_dividend_only_after_disclosure_date() -> None:
    script = r"""
const engine = require('./scripts/calculate_factor_coverage.js');
const ts = (d) => Date.parse(`${d}T00:00:00+09:00`);
const prices = [
  {security_id:'SEC_KR_000020',trade_date:'2021-03-29',_ts:ts('2021-03-29'),close:8000,volume:1},
  {security_id:'SEC_KR_000020',trade_date:'2021-03-30',_ts:ts('2021-03-30'),close:8000,volume:1},
];
const dividends = engine.prepareDividendRows([
  {security_id:'SEC_KR_000020',trade_date:'2021-03-30',dividend:'80',payout_ratio:'0.3946'}
]);
const rows = engine.mergeStockRows('000020', prices, [], dividends, []);
process.stdout.write(JSON.stringify(rows.map((r) => ({date:r.trade_date,dps:r.dvpsx,yield:r.dividend_yield}))));
"""

    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=True,
    )
    rows = json.loads(result.stdout)

    assert rows[0] == {"date": "2021-03-29", "dps": None, "yield": None}
    assert rows[1]["dps"] == 80
    assert rows[1]["yield"] == pytest.approx(1.0)


def test_daily_portfolio_drift_sql_preserves_missing_sides_and_tail_returns() -> None:
    from scripts.audit_capex_daily_portfolio_drift_v5 import (
        DRIFT_QUERY_SETTINGS,
        build_daily_cell_audit_query,
        build_factor_drift_query,
    )

    query = build_factor_drift_query(
        "fact_daily_factors_semantic_v5_capex_backup_0123456789ab",
        start_date="2015-01-01",
        end_date="2025-12-31",
    )

    assert DRIFT_QUERY_SETTINGS["join_use_nulls"] == 1
    assert "leadInFrame(toNullable(close), 21)" in query
    assert query.count("trade_date >= {start_date:Date}") >= 2
    assert query.count("trade_date <= {end_date:Date}") >= 2
    assert query.count("startsWith(security_id, 'SEC_KR_')") >= 2
    assert "security_id NOT IN {security_ids:Array(String)}" in query
    assert "calendar_month_ends" in query and "toYYYYMM" in query
    assert "max(trade_date) AS month_end_date" in query
    assert "FULL OUTER JOIN affected_old_rows" in query
    assert "WHERE (merged.factor_id, merged.trade_date)" not in query
    assert query.count("factor_id IN {factor_ids:Array(String)}") == 2
    assert "factor_id = {factor_id:String}" not in query
    assert query.count(" FINAL") == 1  # price series only; factor rows use argMax(version)

    daily_query = build_daily_cell_audit_query(
        "fact_daily_factors_semantic_v5_capex_backup_0123456789ab",
        start_date="2015-01-01",
        end_date="2025-12-31",
    )
    assert daily_query.count("factor_id IN {factor_ids:Array(String)}") == 2
    assert "GROUP BY factor_id" in daily_query
    assert " FINAL" not in daily_query


def test_capex_rebuild_count_uses_the_same_date_scope_as_rebuild() -> None:
    from scripts.rebuild_capex_daily_factors_v5 import (
        REBUILD_SPLIT_INSERT_BY_PARTITION,
        _backup_years,
        _source_count_for_run,
        _scoped_count,
    )

    assert REBUILD_SPLIT_INSERT_BY_PARTITION is True
    assert _backup_years("2015-01-01", "2017-12-31") == [2015, 2016, 2017]

    class _Result:
        result_rows = [(7,)]

    class _Client:
        def __init__(self) -> None:
            self.query_text = ""
            self.parameters = {}

        def query(self, query_text, *, parameters):
            self.query_text = query_text
            self.parameters = parameters
            return _Result()

    client = _Client()
    count = _scoped_count(
        client,
        "fact_daily_factors",
        ["SEC_KR_005930"],
        start_date="2015-01-01",
        end_date="2025-12-31",
    )

    assert count == 7
    assert "trade_date >= {start_date:Date}" in client.query_text
    assert "trade_date <= {end_date:Date}" in client.query_text
    assert client.parameters["start_date"] == "2015-01-01"
    assert client.parameters["end_date"] == "2025-12-31"
    assert _source_count_for_run(
        client,
        {"delete_complete": True, "source_row_count": 123},
        ["SEC_KR_005930"],
        start_date="2015-01-01",
        end_date=None,
    ) == 123
