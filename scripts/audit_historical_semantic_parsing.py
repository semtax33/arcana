from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.core.paths import DATA_LAKE
from engine.semantic import (
    AccountingInvariantAuditor,
    DisclosureSourceType,
    FactorDependencyGraph,
    HistoricalLexiconMiner,
    InvariantContext,
    UnmappedClassifier,
    core_concept_coverage,
    load_semantic_mapping_rules,
)
from engine.semantic.integrity import ALL_EXPECTED_CF_DIRECTIONS, VALID_UNIT_FACTORS
from engine.transformers._internal.dart_filings import (
    ContextEngine,
    RuleEngine,
    extract_rows_from_dart_html,
    normalize_account_name,
)
from engine.transformers._internal.kr_disclosure_normalizer import build_disclosure_parser


RULE_PATH = DATA_LAKE.rules("semantic_kr_v2.yaml")
CANONICAL_PATH = DATA_LAKE.canonical_accounts()
FACTOR_SOURCE = PROJECT_ROOT / "scripts" / "calculate_factor_coverage.js"
OUTPUT = PROJECT_ROOT / "deliverables" / "historical_semantic_audit_2000_2012.json"
_PERIOD_RE = re.compile(r"\((?P<year>\d{4})[._](?P<month>\d{1,2})\)")


def period_of(path: Path) -> tuple[int, int] | None:
    match = _PERIOD_RE.search(path.name)
    return (int(match.group("year")), int(match.group("month"))) if match else None


def inventory(root: Path, start_year: int, end_year: int) -> dict[int, list[Path]]:
    found: dict[int, list[Path]] = defaultdict(list)
    if not root.exists():
        return found
    for path in root.rglob("*.html"):
        period = period_of(path)
        if period and start_year <= period[0] <= end_year:
            found[period[0]].append(path)
    for paths in found.values():
        paths.sort()
    return found


def load_security_context(
    path: str | Path | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load optional PIT security context without requiring a live data service."""

    if path is None:
        return {}, {}
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"security context registry not found: {source}")
    sectors: dict[str, str] = {}
    industry_groups: dict[str, str] = {}
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            raw_code = next(
                (
                    str(row.get(key) or "").strip()
                    for key in ("stock_code", "security_code", "symbol", "ticker")
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            if not raw_code:
                continue
            stock_code = raw_code.zfill(6) if raw_code.isdigit() else raw_code.upper()
            sector = next(
                (
                    str(row.get(key) or "").strip()
                    for key in ("sector_code", "gics_sector_code")
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            industry_group = next(
                (
                    str(row.get(key) or "").strip()
                    for key in ("industry_group_code", "gics_industry_group_code")
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            if sector:
                sectors[stock_code] = sector
            if industry_group:
                industry_groups[stock_code] = industry_group
    return sectors, industry_groups


def stratified_sample(paths: list[Path], limit: int, *, min_bytes: int = 0) -> list[Path]:
    if limit <= 0 or not paths:
        return []
    viable = [path for path in paths if path.stat().st_size >= min_bytes]
    if not viable:
        viable = paths
    annual = [path for path in viable if period_of(path) and period_of(path)[1] == 12]
    annual_set = set(annual)
    nonannual = [path for path in viable if path not in annual_set]

    def one_per_entity(sequence: list[Path], excluded: set[str] | None = None) -> list[Path]:
        by_entity: dict[str, Path] = {}
        excluded = excluded or set()
        for path in sequence:
            if path.parent.name not in excluded:
                by_entity.setdefault(path.parent.name, path)
        return list(by_entity.values())

    annual_unique = one_per_entity(annual)
    if len(annual_unique) >= limit:
        return [annual_unique[round(index * (len(annual_unique) - 1) / (limit - 1))] for index in range(limit)] if limit > 1 else [annual_unique[len(annual_unique) // 2]]
    selected = annual_unique
    selected.extend(one_per_entity(nonannual, {path.parent.name for path in selected}))
    return selected[:limit]


def number(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value or "0").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and abs(result) <= Decimal("1e18") else None


def has_explicit_source_amount(value: object) -> bool:
    """Distinguish a reported zero from a blank coerced to zero for compatibility."""

    return bool(re.search(r"\d", str(value or "")))


def coverage_record(row_count: int, mapped_count: int, monetary_total: Decimal, monetary_mapped: Decimal) -> dict[str, object]:
    return {
        "row_count": row_count,
        "mapped_row_count": mapped_count,
        "row_coverage_pct": 100.0 * mapped_count / row_count if row_count else 0.0,
        "absolute_monetary_total": str(monetary_total),
        "absolute_monetary_mapped": str(monetary_mapped),
        "monetary_coverage_pct": float(Decimal(100) * monetary_mapped / monetary_total) if monetary_total else 0.0,
    }


def audit_statements(
    files_by_year: dict[int, list[Path]],
    *,
    start_year: int,
    end_year: int,
    files_per_year: int,
    sector_codes: dict[str, str] | None = None,
    industry_group_codes: dict[str, str] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], set[str]]:
    sector_codes = sector_codes or {}
    industry_group_codes = industry_group_codes or {}
    context_engine = ContextEngine.from_yaml(RULE_PATH)
    mapping_engine = RuleEngine.from_files(CANONICAL_PATH, [RULE_PATH], sign_policy_path=RULE_PATH)
    ruleset = load_semantic_mapping_rules([RULE_PATH], text_normalizer=normalize_account_name)
    alias_map: dict[str, set[str]] = defaultdict(set)
    for rule in ruleset.normalization_rules:
        if rule.emit.canonical_id != "UNMAPPED":
            for alias in rule.label.exact_any:
                alias_map[alias].add(rule.emit.canonical_id)
    miner = HistoricalLexiconMiner(alias_map)
    classifier = UnmappedClassifier()
    invariant_auditor = AccountingInvariantAuditor(relative_tolerance=0.005)
    totals = Counter()
    monetary_total = Decimal(0)
    monetary_mapped = Decimal(0)
    mapped_ids: set[str] = set()
    taxonomy = Counter()
    regimes = Counter()
    dialects = Counter()
    yearly: dict[str, Counter] = {}
    unmapped_rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    invariant_counts = Counter()
    invariant_review_examples: list[dict[str, object]] = []
    direction_mismatches = Counter()
    invalid_unit_count = 0

    for year in range(start_year, end_year + 1):
        population = files_by_year.get(year, [])
        eligible = [path for path in population if path.stat().st_size >= 30_000]
        selected = stratified_sample(population, files_per_year, min_bytes=30_000)
        stats = Counter(population_file_count=len(population), population_entity_count=len({p.parent.name for p in population}), data_bearing_size_candidate_count=len(eligible), sample_file_count=len(selected))
        for path in selected:
            period = period_of(path)
            period_text = f"{period[0]}.{period[1]}" if period else str(year)
            stock_code = path.parent.name.zfill(6)
            sector_code = sector_codes.get(stock_code, "")
            industry_group_code = industry_group_codes.get(stock_code, "")
            try:
                rows = extract_rows_from_dart_html(path, path.parent.name, period_text)
                for row in rows:
                    row.update(
                        {
                            "source_type": DisclosureSourceType.FINANCIAL_STATEMENT.value,
                            "sector_code": sector_code,
                            "industry_group_code": industry_group_code,
                        }
                    )
                mapped = mapping_engine.map_rows(context_engine.enrich_context(rows), include_debug_cols=True)
            except Exception as exc:
                failures.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                stats["parse_failure_count"] += 1
                continue
            stats["parsed_file_count"] += 1
            stats["sector_context_file_count"] += int(bool(sector_code))
            stats["industry_group_context_file_count"] += int(bool(industry_group_code))
            totals["sector_context_file_count"] += int(bool(sector_code))
            totals["industry_group_context_file_count"] += int(
                bool(industry_group_code)
            )
            regime = str(mapped.iloc[0].get("accounting_regime", "UNKNOWN")) if not mapped.empty else "UNKNOWN"
            dialect = str(mapped.iloc[0].get("document_dialect", "UNKNOWN")) if not mapped.empty else "UNKNOWN"
            regimes[regime] += 1
            dialects[dialect] += 1
            stats[f"regime_{regime}"] += 1
            facts_by_id: dict[str, list[Decimal]] = defaultdict(list)
            file_invalid_unit_count = 0
            for _, row in mapped.iterrows():
                totals["row_count"] += 1
                stats["row_count"] += 1
                canonical_id = str(row.get("canonical_account_id") or "")
                is_mapped = canonical_id not in {"", "UNMAPPED"}
                amount = number(row.get("raw_amount"))
                if amount is not None:
                    monetary_total += abs(amount)
                    stats["monetary_total"] += abs(amount)
                    if is_mapped:
                        monetary_mapped += abs(amount)
                        stats["monetary_mapped"] += abs(amount)
                if is_mapped:
                    totals["mapped_row_count"] += 1
                    stats["mapped_row_count"] += 1
                    mapped_ids.add(canonical_id)
                    if amount is not None and has_explicit_source_amount(
                        row.get("amount_raw")
                    ):
                        facts_by_id[canonical_id].append(amount)
                    if str(row.get("rule_id") or "") and str(row.get("semantic_provenance") or "") not in {"", "{}"}:
                        totals["provenance_complete_count"] += 1
                    expected_direction = ALL_EXPECTED_CF_DIRECTIONS.get(canonical_id)
                    actual_direction = str(row.get("cash_direction") or "")
                    if expected_direction and actual_direction != expected_direction:
                        direction_mismatches[f"{canonical_id}:{actual_direction or '<blank>'}->{expected_direction}"] += 1
                else:
                    suggestions = miner.suggest(row.get("original_account_name"))
                    assessment = classifier.classify(
                        row.get("original_account_name"),
                        statement_type=str(row.get("statement_type") or "UNKNOWN"),
                        raw_value=row.get("amount_raw"),
                        period_label=str(row.get("period") or ""),
                        scope=str(row.get("scope") or "UNKNOWN"),
                        parent_context=str(row.get("parent_context") or ""),
                        suggestions=suggestions,
                    )
                    taxonomy[assessment.category.value] += 1
                    if len(unmapped_rows) < 50_000:
                        unmapped_rows.append(
                            {
                                "label": row.get("original_account_name"), "statement_type": row.get("statement_type"),
                                "period": row.get("period"), "year": year, "entity_id": path.parent.name,
                                "parent_context": row.get("parent_context"), "category": assessment.category.value,
                                "absolute_amount": str(abs(amount)) if amount is not None else "0",
                            }
                        )
                try:
                    unit = Decimal(str(row.get("unit_factor") or "1"))
                    if unit not in VALID_UNIT_FACTORS:
                        invalid_unit_count += 1
                        file_invalid_unit_count += 1
                except Exception:
                    invalid_unit_count += 1
                    file_invalid_unit_count += 1

            unique_facts = {canonical_id: values[0] for canonical_id, values in facts_by_id.items() if len(values) == 1}
            scopes = {str(value) for value in mapped.get("scope", []) if str(value)}
            periods = {str(value) for value in mapped.get("period", []) if str(value)}
            currencies = {
                str(value) for value in mapped.get("currency", []) if str(value)
            }
            accounting_bases = {
                str(value)
                for value in mapped.get("accounting_regime", [])
                if str(value)
            }
            evidence = invariant_auditor.audit(
                unique_facts,
                context=InvariantContext(
                    # UNKNOWN is acceptable when every row still has the same
                    # unresolved scope. Mixed resolved scopes are not.
                    scope_consistent=len(scopes) <= 1,
                    period_consistent=len(periods) <= 1,
                    currency_consistent=len(currencies) <= 1,
                    unit_normalized=file_invalid_unit_count == 0,
                    accounting_basis_consistent=len(accounting_bases) <= 1,
                    # Duplicated canonical IDs are removed from unique_facts;
                    # only equations needing those IDs become NOT_TESTABLE.
                    has_dimensional_duplicates=False,
                ),
            )
            for item in evidence:
                invariant_counts[item.status] += 1
                if item.status == "REVIEW" and len(invariant_review_examples) < 200:
                    invariant_review_examples.append(
                        {
                            "source_path": str(path),
                            "entity_id": path.parent.name,
                            "period": period_text,
                            "invariant_id": item.invariant_id,
                            "left_value": str(item.left_value),
                            "right_value": str(item.right_value),
                            "residual": str(item.residual),
                            "relative_residual": item.relative_residual,
                            "involved_canonical_ids": list(
                                item.involved_canonical_ids
                            ),
                            "candidate_only": item.candidate_only,
                        }
                    )
        yearly[str(year)] = stats

    yearly_report: dict[str, object] = {}
    for year, stats in yearly.items():
        monetary_year = Decimal(stats["monetary_total"])
        mapped_year = Decimal(stats["monetary_mapped"])
        yearly_report[year] = {
            "population_file_count": stats["population_file_count"],
            "population_entity_count": stats["population_entity_count"],
            "data_bearing_size_candidate_count": stats["data_bearing_size_candidate_count"],
            "sample_file_count": stats["sample_file_count"],
            "parsed_file_count": stats["parsed_file_count"],
            "parse_failure_count": stats["parse_failure_count"],
            "sector_context_file_count": stats["sector_context_file_count"],
            "industry_group_context_file_count": stats["industry_group_context_file_count"],
            **coverage_record(stats["row_count"], stats["mapped_row_count"], monetary_year, mapped_year),
            "regime_file_counts": {key.removeprefix("regime_"): value for key, value in stats.items() if key.startswith("regime_")},
        }

    lexicon = miner.aggregate(unmapped_rows)
    cluster_stats: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in unmapped_rows:
        label = normalize_account_name(row.get("label"))
        key = (label, str(row.get("statement_type") or "UNKNOWN"), str(row.get("category") or "UNKNOWN_LABEL"))
        cluster = cluster_stats.setdefault(
            key,
            {"normalized_label": label, "statement_type": key[1], "category": key[2], "occurrence_count": 0, "absolute_monetary_total": Decimal(0), "entities": set(), "years": set()},
        )
        cluster["occurrence_count"] = int(cluster["occurrence_count"]) + 1
        cluster["absolute_monetary_total"] = Decimal(cluster["absolute_monetary_total"]) + Decimal(str(row.get("absolute_amount") or "0"))
        cluster["entities"].add(str(row.get("entity_id") or ""))
        cluster["years"].add(str(row.get("year") or ""))
    monetary_clusters = []
    for cluster in sorted(cluster_stats.values(), key=lambda item: (-Decimal(item["absolute_monetary_total"]), str(item["normalized_label"])))[:200]:
        suggestions = miner.suggest(cluster["normalized_label"])
        monetary_clusters.append(
            {
                "normalized_label": cluster["normalized_label"],
                "statement_type": cluster["statement_type"],
                "category": cluster["category"],
                "occurrence_count": cluster["occurrence_count"],
                "absolute_monetary_total": str(cluster["absolute_monetary_total"]),
                "entity_count": len(cluster["entities"]),
                "year_count": len(cluster["years"]),
                "suggestions": [asdict(suggestion) for suggestion in suggestions],
            }
        )
    report = {
        "sample_method": "annual-first, one-filing-per-issuer, evenly-spaced issuer sample",
        "coverage": coverage_record(totals["row_count"], totals["mapped_row_count"], monetary_total, monetary_mapped),
        "provenance_completeness_pct": 100.0 * totals["provenance_complete_count"] / totals["mapped_row_count"] if totals["mapped_row_count"] else 0.0,
        "regime_file_counts": dict(regimes),
        "document_dialect_file_counts": dict(dialects),
        "years": yearly_report,
        "unmapped_taxonomy": dict(taxonomy),
        "invariant_status_counts": dict(invariant_counts),
        "wrong_semantic_mapping_candidate_count": invariant_counts["REVIEW"],
        "invariant_review_examples": invariant_review_examples,
        "invalid_unit_factor_row_count": invalid_unit_count,
        "canonical_direction_mismatch_count": sum(direction_mismatches.values()),
        "canonical_direction_mismatches": dict(direction_mismatches),
        "ambiguous_auto_emit_count": 0,
        "hierarchical_context": {
            "source_type": DisclosureSourceType.FINANCIAL_STATEMENT.value,
            "sector_context_file_count": totals["sector_context_file_count"],
            "industry_group_context_file_count": totals["industry_group_context_file_count"],
        },
        "historical_lexicon_candidates": [
            {**asdict(item), "suggestions": [asdict(suggestion) for suggestion in item.suggestions]}
            for item in lexicon[:200]
        ],
        "unmapped_monetary_clusters": monetary_clusters,
        "failures": failures[:100],
    }
    return report, unmapped_rows, mapped_ids


def audit_disclosures(
    source_name: str,
    source_type: DisclosureSourceType,
    files_by_year: dict[int, list[Path]],
    *,
    start_year: int,
    end_year: int,
    files_per_year: int,
    sector_codes: dict[str, str] | None = None,
    industry_group_codes: dict[str, str] | None = None,
) -> dict[str, object]:
    sector_codes = sector_codes or {}
    industry_group_codes = industry_group_codes or {}
    parser = build_disclosure_parser()
    totals = Counter()
    yearly: dict[str, object] = {}
    failures: list[dict[str, str]] = []
    for year in range(start_year, end_year + 1):
        population = files_by_year.get(year, [])
        selected = stratified_sample(population, files_per_year, min_bytes=1_000)
        stats = Counter(population_file_count=len(population), population_entity_count=len({path.parent.name for path in population}), sample_file_count=len(selected))
        for path in selected:
            stock_code = path.parent.name.zfill(6)
            sector_code = sector_codes.get(stock_code, "")
            industry_group_code = industry_group_codes.get(stock_code, "")
            try:
                document = parser.parse(
                    path,
                    source_type=source_type,
                    sector_code=sector_code,
                    industry_group_code=industry_group_code,
                )
            except Exception as exc:
                failures.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                stats["parse_failure_count"] += 1
                continue
            stats["parsed_file_count"] += 1
            stats["sector_context_file_count"] += int(bool(sector_code))
            stats["industry_group_context_file_count"] += int(bool(industry_group_code))
            stats["section_count"] += len(document.sections)
            stats["table_count"] += len(document.tables)
            stats["candidate_count"] += len(document.candidates)
            stats["review_required_count"] += sum(candidate.review_required for candidate in document.candidates)
            stats["ambiguous_auto_emit_count"] += sum(candidate.auto_emit_eligible for candidate in document.candidates if candidate.period_role == "AMBIGUOUS")
        yearly[str(year)] = dict(stats)
        totals.update(stats)
    return {
        "source": source_name,
        "totals": dict(totals),
        "years": yearly,
        "hierarchical_context": {
            "source_type": source_type.value,
            "sector_context_file_count": totals["sector_context_file_count"],
            "industry_group_context_file_count": totals["industry_group_context_file_count"],
        },
        "failures": failures[:100],
    }


def availability(files_by_year: dict[int, list[Path]], start_year: int, end_year: int) -> dict[str, object]:
    return {
        str(year): {"file_count": len(files_by_year.get(year, [])), "entity_count": len({path.parent.name for path in files_by_year.get(year, [])})}
        for year in range(start_year, end_year + 1)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit deterministic semantic parsing over historical DART filings")
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2012)
    parser.add_argument("--statement-files-per-year", type=int, default=12)
    parser.add_argument("--disclosure-files-per-year", type=int, default=12)
    parser.add_argument(
        "--security-context-csv",
        type=Path,
        help="Optional PIT CSV with stock_code and sector/industry-group columns.",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    statements = inventory(DATA_LAKE.bronze("dart", "finance-statement"), args.start_year, args.end_year)
    notes = inventory(DATA_LAKE.bronze("dart", "finance-comment"), args.start_year, args.end_year)
    business = inventory(DATA_LAKE.bronze("dart", "business-info"), args.start_year, args.end_year)
    sector_codes, industry_group_codes = load_security_context(
        args.security_context_csv
    )
    statement_report, _, mapped_ids = audit_statements(
        statements,
        start_year=args.start_year,
        end_year=args.end_year,
        files_per_year=args.statement_files_per_year,
        sector_codes=sector_codes,
        industry_group_codes=industry_group_codes,
    )
    graph = FactorDependencyGraph.from_javascript(FACTOR_SOURCE)
    report = {
        "semantic_engine_version": 3,
        "period": {"start_year": args.start_year, "end_year": args.end_year},
        "accounting_regime_policy": "evidence-detected per filing; 2009-2012 are not date-forced and may contain K_GAAP or K_IFRS",
        "hierarchical_context_policy": {
            "dimensions": ["source_type", "statement_type", "section_path", "parent_account_path", "table_kind", "sector_code", "industry_group_code", "scope", "accounting_regime", "period"],
            "security_context_registry": str(args.security_context_csv) if args.security_context_csv else "not_provided",
            "unknown_sector_is_not_guessed": True,
        },
        "availability": {
            "financial_statements": availability(statements, args.start_year, args.end_year),
            "financial_notes": availability(notes, args.start_year, args.end_year),
            "business_content": availability(business, args.start_year, args.end_year),
        },
        "financial_statements": statement_report,
        "financial_notes": audit_disclosures("financial_notes", DisclosureSourceType.FINANCIAL_NOTES, notes, start_year=args.start_year, end_year=args.end_year, files_per_year=args.disclosure_files_per_year, sector_codes=sector_codes, industry_group_codes=industry_group_codes),
        "business_content": audit_disclosures("business_content", DisclosureSourceType.BUSINESS_CONTENT, business, start_year=args.start_year, end_year=args.end_year, files_per_year=args.disclosure_files_per_year, sector_codes=sector_codes, industry_group_codes=industry_group_codes),
        "core_economic_concepts": core_concept_coverage(mapped_ids),
        "factor_dependencies": graph.dependency_coverage(mapped_ids),
        "limitations": [
            "Coverage is measured only over locally available bronze files.",
            "Invariant failures are review candidates, not confirmed mapping errors.",
            "Narrative candidates are not production-emitted until a rule and golden test are approved.",
            "No local PIT security-context registry was supplied; sector-gated rules are tested synthetically but not applied to historical files with unknown sectors." if not args.security_context_csv else "Sector context is supplied by the user-provided PIT registry and is not inferred from the filing date.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "statement_row_coverage_pct": statement_report["coverage"]["row_coverage_pct"],
        "statement_monetary_coverage_pct": statement_report["coverage"]["monetary_coverage_pct"],
        "core_economic_concept_coverage_pct": report["core_economic_concepts"]["core_economic_concept_coverage_pct"],
        "factor_input_coverage_pct": report["factor_dependencies"]["factor_input_coverage_pct"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
