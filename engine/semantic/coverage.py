from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
import yaml

from engine.semantic.rules import load_semantic_mapping_rules


@dataclass(frozen=True)
class MigrationCoverage:
    mapping_source_rules: int
    mapping_migrated_rules: int
    context_source_rules: int
    context_migrated_rules: int
    comment_source_rules: int
    comment_migrated_rules: int
    sign_policy_source_entries: int
    sign_policy_migrated_entries: int
    native_k_gaap_rules: int
    native_common_rules: int

    @property
    def migrated_source_rules(self) -> int:
        return (
            self.mapping_migrated_rules
            + self.context_migrated_rules
            + self.comment_migrated_rules
            + self.sign_policy_migrated_entries
        )

    @property
    def total_source_rules(self) -> int:
        return (
            self.mapping_source_rules
            + self.context_source_rules
            + self.comment_source_rules
            + self.sign_policy_source_entries
        )

    @property
    def coverage_pct(self) -> float:
        return (
            100.0 * self.migrated_source_rules / self.total_source_rules
            if self.total_source_rules
            else 0.0
        )


def migration_coverage(bundle_path: str | Path) -> MigrationCoverage:
    data = yaml.safe_load(Path(bundle_path).read_text(encoding="utf-8")) or {}
    migration = data.get("migration", {}) or {}
    sign_source = next(
        (
            int(source.get("source_rule_count", 0))
            for source in migration.get("sources", []) or []
            if source.get("rule_kind") == "sign_policy"
        ),
        0,
    )
    sign_migrated = len(
        ((data.get("sign_policy", {}) or {}).get("canonical_policies", {}) or {})
    )
    return MigrationCoverage(
        mapping_source_rules=int(migration.get("mapping_source_rule_count", 0)),
        mapping_migrated_rules=int(migration.get("mapping_migrated_rule_count", 0)),
        context_source_rules=int(migration.get("context_source_rule_count", 0)),
        context_migrated_rules=int(migration.get("context_migrated_rule_count", 0)),
        comment_source_rules=int(migration.get("comment_source_rule_count", 0)),
        comment_migrated_rules=int(migration.get("comment_migrated_rule_count", 0)),
        sign_policy_source_entries=sign_source,
        sign_policy_migrated_entries=sign_migrated,
        native_k_gaap_rules=int(migration.get("native_k_gaap_rule_count", 0)),
        native_common_rules=int(migration.get("native_common_rule_count", 0)),
    )


def migration_integrity(
    bundle_path: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    data = yaml.safe_load(Path(bundle_path).read_text(encoding="utf-8")) or {}
    root = Path(project_root)
    rows: list[dict[str, Any]] = []
    for source in (data.get("migration", {}) or {}).get("sources", []) or []:
        path = root / str(source.get("path", ""))
        expected = str(source.get("sha256", ""))
        actual = sha256(path.read_bytes()).hexdigest() if path.exists() else ""
        rows.append(
            {
                "path": str(path),
                "rule_kind": source.get("rule_kind", ""),
                "exists": path.exists(),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches": bool(expected) and expected == actual,
            }
        )
    return {
        "source_count": len(rows),
        "matching_source_count": sum(bool(row["matches"]) for row in rows),
        "all_sources_match": bool(rows) and all(row["matches"] for row in rows),
        "sources": rows,
    }


def canonical_rule_coverage(
    bundle_path: str | Path,
    canonical_path: str | Path,
    *,
    text_normalizer=str,
) -> dict[str, Any]:
    catalog = pd.read_csv(canonical_path, dtype=str).fillna("")
    eligible = catalog.loc[
        catalog["is_derived"].astype(str).str.upper().ne("TRUE"), "canonical_id"
    ].astype(str)
    eligible_ids = set(eligible)
    ruleset = load_semantic_mapping_rules(
        [bundle_path], text_normalizer=text_normalizer
    )
    target_ids = {
        rule.emit.canonical_id
        for rule in ruleset.normalization_rules
        if rule.emit.canonical_id != "UNMAPPED"
    }
    covered = eligible_ids & target_ids
    missing = eligible_ids - target_ids
    unknown_targets = target_ids - eligible_ids
    by_statement: dict[str, dict[str, Any]] = {}
    for statement_type, group in catalog.groupby("fs_type"):
        ids = set(group.loc[group["is_derived"].astype(str).str.upper().ne("TRUE"), "canonical_id"])
        hits = ids & target_ids
        by_statement[str(statement_type)] = {
            "catalog_count": len(ids),
            "covered_count": len(hits),
            "coverage_pct": 100.0 * len(hits) / len(ids) if ids else 0.0,
            "missing_ids": sorted(ids - target_ids),
        }
    return {
        "catalog_count": len(eligible_ids),
        "rule_target_count": len(target_ids),
        "covered_catalog_count": len(covered),
        "coverage_pct": 100.0 * len(covered) / len(eligible_ids) if eligible_ids else 0.0,
        "missing_ids": sorted(missing),
        "unknown_target_ids": sorted(unknown_targets),
        "by_statement_type": by_statement,
    }


# Normalized DART values are KRW-scaled.  One quintillion KRW is already far
# above a plausible single Korean filing fact and safely catches legacy rows
# where every number in a table was concatenated into one numeric string.
MAX_COVERAGE_AMOUNT_MAGNITUDE = Decimal("1e18")


def _amount(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return Decimal(0)
    try:
        amount = abs(Decimal(text))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount > MAX_COVERAGE_AMOUNT_MAGNITUDE:
        return None
    return amount


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def observed_mapping_coverage(
    input_dir: str | Path,
    mapping_engine,
    *,
    legacy_mapping_engine=None,
    max_files: int | None = None,
) -> dict[str, Any]:
    paths = sorted(Path(input_dir).glob("kr_normalized_*.debug.csv"))
    if max_files is not None:
        paths = paths[:max_files]
    totals = {
        "row_count": 0,
        "rows_with_semantic_metadata": 0,
        "valid_amount_row_count": 0,
        "excluded_invalid_amount_row_count": 0,
        "baseline_mapped_row_count": 0,
        "legacy_replay_mapped_row_count": 0,
        "v2_mapped_row_count": 0,
        "changed_mapping_count": 0,
        "newly_mapped_row_count": 0,
    }
    total_amount = Decimal(0)
    baseline_mapped_amount = Decimal(0)
    legacy_replay_mapped_amount = Decimal(0)
    v2_mapped_amount = Decimal(0)
    v2_rule_hits: dict[str, int] = {}
    statement_type_counts: dict[str, int] = {}
    regime_counts: dict[str, int] = {}
    dialect_counts: dict[str, int] = {}
    newly_mapped_examples: list[dict[str, str]] = []
    result_cache: dict[tuple[Any, ...], Any] = {}
    legacy_result_cache: dict[tuple[Any, ...], Any] = {}
    semantic_rules = tuple(mapping_engine.semantic_ruleset.normalization_rules)
    context_tokens = tuple(
        sorted(
            {
                token
                for rule in semantic_rules
                for token in (
                    *rule.context.contains_all,
                    *rule.context.excludes_any,
                    *(
                        value
                        for group in rule.context.contains_any_groups
                        for value in group
                    ),
                )
            }
        )
    )
    context_requires_full_text = any(
        rule.context.exact_any or rule.context.regex_any for rule in semantic_rules
    )
    date_sensitive = any(
        rule.applies.effective_from or rule.applies.effective_to
        for rule in semantic_rules
    )

    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                baseline = str(row.get("canonical_account_id", "") or "")
                raw_amount = row.get("raw_amount", row.get("amount", ""))
                parsed_amount = _amount(raw_amount)
                amount_is_valid = parsed_amount is not None
                magnitude = parsed_amount or Decimal(0)
                # A malformed concatenated amount is not a zero/blank semantic
                # constraint; it is excluded only from amount-weighted coverage.
                zero = amount_is_valid and magnitude == 0
                probe = dict(row)
                probe["raw_amount"] = "0" if zero else "1"
                probe["has_children"] = _bool(row.get("has_children"))
                normalized = mapping_engine._normalize_row_for_matching(probe)
                context_key: Any = normalized["context"]
                if not context_requires_full_text:
                    context_key = tuple(
                        token for token in context_tokens if token in normalized["context"]
                    )
                key = (
                    normalized["fs_type"],
                    normalized["name"],
                    context_key,
                    normalized["has_children"],
                    normalized["amount_is_zero_or_blank"],
                    row.get("accounting_regime", "UNKNOWN") or "UNKNOWN",
                    row.get("document_dialect", "UNKNOWN") or "UNKNOWN",
                    row.get("period", "") if date_sensitive else "",
                )
                result = result_cache.get(key)
                if result is None:
                    result = mapping_engine.map_row(probe)
                    if len(result_cache) >= 400_000:
                        result_cache.clear()
                    result_cache[key] = result
                legacy_result = None
                if legacy_mapping_engine is not None:
                    legacy_result = legacy_result_cache.get(key)
                    if legacy_result is None:
                        legacy_probe = probe
                        legacy_result = legacy_mapping_engine.map_row(legacy_probe)
                        if len(legacy_result_cache) >= 400_000:
                            legacy_result_cache.clear()
                        legacy_result_cache[key] = legacy_result
                mapped = result.canonical_account_id
                totals["row_count"] += 1
                if amount_is_valid:
                    totals["valid_amount_row_count"] += 1
                else:
                    totals["excluded_invalid_amount_row_count"] += 1
                statement_type = normalized["fs_type"]
                regime_value = str(row.get("accounting_regime", "") or "UNKNOWN")
                dialect_value = str(row.get("document_dialect", "") or "UNKNOWN")
                statement_type_counts[statement_type] = (
                    statement_type_counts.get(statement_type, 0) + 1
                )
                regime_counts[regime_value] = regime_counts.get(regime_value, 0) + 1
                dialect_counts[dialect_value] = dialect_counts.get(dialect_value, 0) + 1
                if regime_value != "UNKNOWN" and dialect_value != "UNKNOWN":
                    totals["rows_with_semantic_metadata"] += 1
                if amount_is_valid:
                    total_amount += magnitude
                if baseline not in {"", "UNMAPPED"}:
                    totals["baseline_mapped_row_count"] += 1
                    if amount_is_valid:
                        baseline_mapped_amount += magnitude
                if (
                    legacy_result is not None
                    and legacy_result.canonical_account_id != "UNMAPPED"
                ):
                    totals["legacy_replay_mapped_row_count"] += 1
                    if amount_is_valid:
                        legacy_replay_mapped_amount += magnitude
                if mapped != "UNMAPPED":
                    totals["v2_mapped_row_count"] += 1
                    if amount_is_valid:
                        v2_mapped_amount += magnitude
                    v2_rule_hits[result.rule_id] = v2_rule_hits.get(result.rule_id, 0) + 1
                if baseline != mapped:
                    totals["changed_mapping_count"] += 1
                baseline_for_delta = (
                    legacy_result.canonical_account_id
                    if legacy_result is not None
                    else baseline
                )
                if baseline_for_delta == "UNMAPPED" and mapped != "UNMAPPED":
                    totals["newly_mapped_row_count"] += 1
                    if len(newly_mapped_examples) < 50:
                        newly_mapped_examples.append(
                            {
                                "file": path.name,
                                "period": str(row.get("period", "")),
                                "statement_type": str(row.get("statement_type", "")),
                                "label": str(row.get("original_account_name", "")),
                                "canonical_id": mapped,
                                "rule_id": result.rule_id,
                            }
                        )

    row_count = totals["row_count"]
    return {
        "input_dir": str(Path(input_dir).resolve()),
        "file_count": len(paths),
        **totals,
        "statement_type_row_counts": dict(sorted(statement_type_counts.items())),
        "accounting_regime_row_counts": dict(sorted(regime_counts.items())),
        "document_dialect_row_counts": dict(sorted(dialect_counts.items())),
        "semantic_metadata_row_pct": (
            100.0 * totals["rows_with_semantic_metadata"] / row_count
            if row_count
            else 0.0
        ),
        "baseline_mapped_row_pct": (
            100.0 * totals["baseline_mapped_row_count"] / row_count if row_count else 0.0
        ),
        "v2_mapped_row_pct": (
            100.0 * totals["v2_mapped_row_count"] / row_count if row_count else 0.0
        ),
        "legacy_replay_mapped_row_pct": (
            100.0 * totals["legacy_replay_mapped_row_count"] / row_count
            if row_count and legacy_mapping_engine is not None
            else None
        ),
        "mapping_pct_point_delta": (
            100.0
            * (
                totals["v2_mapped_row_count"]
                - (
                    totals["legacy_replay_mapped_row_count"]
                    if legacy_mapping_engine is not None
                    else totals["baseline_mapped_row_count"]
                )
            )
            / row_count
            if row_count
            else 0.0
        ),
        "total_absolute_amount": str(total_amount),
        "baseline_mapped_absolute_amount_pct": (
            float(Decimal(100) * baseline_mapped_amount / total_amount)
            if total_amount
            else 0.0
        ),
        "v2_mapped_absolute_amount_pct": (
            float(Decimal(100) * v2_mapped_amount / total_amount)
            if total_amount
            else 0.0
        ),
        "legacy_replay_mapped_absolute_amount_pct": (
            float(Decimal(100) * legacy_replay_mapped_amount / total_amount)
            if total_amount and legacy_mapping_engine is not None
            else None
        ),
        "top_v2_rule_hits": [
            {"rule_id": rule_id, "row_count": count}
            for rule_id, count in sorted(
                v2_rule_hits.items(), key=lambda item: (-item[1], item[0])
            )[:50]
        ],
        "newly_mapped_examples": newly_mapped_examples,
    }


def load_factor_coverage(
    summary_path: str | Path,
    detail_path: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(summary_path)
    if not path.exists():
        return {"available": False, "summary_path": str(path)}
    summary = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "available": True,
        "summary_path": str(path.resolve()),
        "generated_at": summary.get("generated_at"),
        "as_of_date": summary.get("as_of_date"),
        "row_count": summary.get("row_count", 0),
        "factor_count": summary.get("factor_count", 0),
        "covered_cells": summary.get("covered_cells", 0),
        "total_cells": summary.get("total_cells", 0),
        "coverage_pct": summary.get("coverage_pct", 0.0),
    }
    if detail_path is not None and Path(detail_path).exists():
        rows = list(csv.DictReader(Path(detail_path).open("r", encoding="utf-8-sig")))
        rows.sort(key=lambda row: float(row.get("coverage_pct", 0) or 0))
        result["lowest_factors"] = rows[:15]
        result["highest_factors"] = list(reversed(rows[-15:]))
    return result


def build_coverage_report(
    *,
    bundle_path: str | Path,
    canonical_path: str | Path,
    project_root: str | Path | None = None,
    text_normalizer=str,
    observed: Mapping[str, Any] | None = None,
    factor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    migration = migration_coverage(bundle_path)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "semantic_engine_version": 2,
        "migration": {**asdict(migration), "coverage_pct": migration.coverage_pct},
        "migration_integrity": migration_integrity(
            bundle_path,
            project_root=project_root or Path(bundle_path).resolve().parents[3],
        ),
        "canonical_rule_coverage": canonical_rule_coverage(
            bundle_path, canonical_path, text_normalizer=text_normalizer
        ),
        "observed_mapping_coverage": dict(observed or {}),
        "factor_coverage": dict(factor or {}),
    }


def write_coverage_report(report: Mapping[str, Any], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return output
