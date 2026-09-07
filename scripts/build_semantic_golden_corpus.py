from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

import pandas as pd

from engine.semantic import (
    AccountingRegimeFamily,
    DisclosureSourceType,
    DocumentDialect,
    GoldenCorpusEvaluator,
    RelationType,
    SemanticRuleExecutor,
    load_semantic_mapping_rules,
    resolve_rule_bundle,
)
from engine.transformers.filings import normalize_account_name


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_current.yaml"
DEFAULT_CANONICAL = ROOT / "data-lake" / "meta" / "CanonicalAccount.csv"
DEFAULT_CORPUS = ROOT / "data-lake" / "meta" / "rules" / "semantic_golden_contract_v6.jsonl"
DEFAULT_REPORT = ROOT / "deliverables" / "semantic_golden_contract_v6_evaluation.json"


def _context_text(predicate) -> str:
    if predicate.exact_any:
        return predicate.exact_any[0]
    parts = list(predicate.contains_all)
    parts.extend(group[0] for group in predicate.contains_any_groups if group)
    return " ".join(parts)


def _first(values, default):
    return values[0] if values else default


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _test_suite_hash() -> str:
    digest = sha256()
    for path in sorted((ROOT / "tests").glob("test_semantic_rule_engine_v*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_corpus(rule_path: Path, canonical_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved_rule_path = resolve_rule_bundle(rule_path)
    version_match = re.fullmatch(r"semantic_kr_v(\d+)\.yaml", resolved_rule_path.name)
    semantic_engine_version = int(version_match.group(1)) if version_match else 4
    catalog = pd.read_csv(canonical_path, dtype=str).fillna("")
    canonical_names = dict(zip(catalog["canonical_id"], catalog["canonical_nm"]))
    statement_by_id = dict(zip(catalog["canonical_id"], catalog["fs_type"]))
    ruleset = load_semantic_mapping_rules(
        [rule_path], text_normalizer=normalize_account_name
    )
    executor = SemanticRuleExecutor(
        ruleset,
        canonical_names,
        text_normalizer=normalize_account_name,
    )
    cases = []
    for rule in ruleset.normalization_rules:
        for alias_index, label in enumerate(rule.label.exact_any):
            statement_type = _first(
                tuple(value for value in rule.applies.statement_types if value != "ANY"),
                statement_by_id.get(rule.emit.canonical_id, "UNKNOWN") or "UNKNOWN",
            )
            effective_at = rule.applies.effective_from or rule.applies.effective_to
            cases.append(
                {
                    "case_id": f"{rule.rule_id}:{alias_index}",
                    "label": label,
                    "context": _context_text(rule.context),
                    "statement_type": statement_type,
                    "accounting_regime": _enum_value(
                        _first(
                            rule.applies.accounting_regimes,
                            AccountingRegimeFamily.UNKNOWN,
                        )
                    ),
                    "document_dialect": _enum_value(
                        _first(
                            rule.applies.document_dialects,
                            DocumentDialect.UNKNOWN,
                        )
                    ),
                    "source_type": _enum_value(
                        _first(
                            rule.applies.source_types,
                            DisclosureSourceType.FINANCIAL_STATEMENT,
                        )
                    ),
                    "sector_code": _first(rule.applies.sector_codes, ""),
                    "industry_group_code": _first(
                        rule.applies.industry_group_codes, ""
                    ),
                    "table_kind": _first(rule.applies.table_kinds, ""),
                    "effective_at": effective_at.isoformat() if effective_at else None,
                    "has_children": bool(rule.constraints.has_children),
                    "amount_is_zero_or_blank": bool(
                        rule.constraints.amount_is_zero_or_blank
                    ),
                    "relations": [
                        _enum_value(value)
                        for value in rule.constraints.required_relations
                    ],
                    "expected_canonical_id": (
                        None
                        if rule.emit.canonical_id == "UNMAPPED"
                        else rule.emit.canonical_id
                    ),
                    "expected_rule_id": rule.rule_id,
                    "label_origin": "migrated_rule_contract",
                    "review_status": "source_rule_authoritative_not_independently_relabeled",
                    "source_rule_file": rule.source_file,
                    "source_rule_index": rule.source_index,
                }
            )

    regime_values = {value.value: value for value in AccountingRegimeFamily}
    dialect_values = {value.value: value for value in DocumentDialect}
    source_values = {value.value: value for value in DisclosureSourceType}
    relation_values = {value.value: value for value in RelationType}

    def predict(case: dict[str, Any]) -> str | None:
        matched = executor.match(
            statement_type=case["statement_type"],
            label=case["label"],
            context=case["context"],
            has_children=case["has_children"],
            amount_is_zero_or_blank=case["amount_is_zero_or_blank"],
            regime=regime_values[case["accounting_regime"]],
            dialect=dialect_values[case["document_dialect"]],
            effective_at=(
                date.fromisoformat(case["effective_at"])
                if case["effective_at"]
                else None
            ),
            relations=[relation_values[value] for value in case["relations"]],
            source_type=source_values[case["source_type"]],
            sector_code=case["sector_code"],
            industry_group_code=case["industry_group_code"],
            table_kind=case["table_kind"],
        )
        return None if matched.canonical_id == "UNMAPPED" else matched.canonical_id

    evaluation = GoldenCorpusEvaluator().evaluate(cases, predict)
    report = {
        "semantic_engine_version": semantic_engine_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_kind": "deterministic rule-contract regression corpus",
        "independently_human_labelled": False,
        "statistical_accuracy_claim_allowed": False,
        "limitation": "These labels come from authoritative migrated rules. They prove executable migration conformance, not out-of-sample semantic accuracy.",
        "rule_bundle_sha256": sha256(resolved_rule_path.read_bytes()).hexdigest(),
        "test_suite_hash": _test_suite_hash(),
        **evaluation,
    }
    return cases, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and evaluate the active semantic rule-contract golden corpus."
    )
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    cases, report = build_corpus(args.rules, args.canonical)
    args.corpus.parent.mkdir(parents=True, exist_ok=True)
    args.corpus.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    report["golden_corpus_hash"] = sha256(args.corpus.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"corpus": str(args.corpus), "output": str(args.output), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
