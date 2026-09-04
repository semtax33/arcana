from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULE_ROOT = PROJECT_ROOT / "data-lake" / "meta" / "rules"
DEFAULT_MAPPING = RULE_ROOT / "kr_mapping.yaml"
DEFAULT_CONTEXT = RULE_ROOT / "context_kr.yaml"
DEFAULT_COMMENT = RULE_ROOT / "comment_kr.yaml"
DEFAULT_SIGN = RULE_ROOT / "sign_policy_common.yaml"
DEFAULT_K_GAAP = RULE_ROOT / "k_gaap_historical_v2.yaml"
DEFAULT_COMMON = RULE_ROOT / "semantic_common_v2.yaml"
DEFAULT_OUTPUT = RULE_ROOT / "semantic_kr_v2.yaml"


def _read(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.read_bytes()
    return raw, yaml.safe_load(raw) or {}


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _groups(rule: dict[str, Any], prefix: str) -> list[list[Any]]:
    items: list[tuple[int, list[Any]]] = []
    if prefix in rule:
        items.append((0, _values(rule[prefix])))
    marker = f"{prefix}_"
    for key, value in rule.items():
        if key.startswith(marker) and key[len(marker):].isdigit():
            items.append((int(key[len(marker):]), _values(value)))
    return [group for _, group in sorted(items) if group]


def _predicate(rule: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    mapping = {
        "exact_any": "exact_any",
        "include_all": "contains_all",
        "exclude_any": "excludes_any",
    }
    for legacy_key, v2_key in mapping.items():
        values = _values(rule.get(f"{prefix}{legacy_key}"))
        if values:
            result[v2_key] = values
    groups = _groups(rule, f"{prefix}include_any")
    if groups:
        result["contains_any_groups"] = groups
    return result


def migrate_mapping_rule(rule: dict[str, Any], source: Path, index: int) -> dict[str, Any]:
    match: dict[str, Any] = {}
    label = _predicate(rule)
    context = _predicate(rule, "context_")
    if label:
        match["label"] = label
    if context:
        match["context"] = context
    if rule.get("conditions"):
        match["constraints"] = dict(rule["conditions"])
    emit = {"canonical_id": str(rule.get("canonical_id", "UNMAPPED"))}
    for key in (
        "fallback_if_missing",
        "amount_policy",
        "cash_direction",
        "comparability",
        "relations",
    ):
        if key in rule:
            emit[key] = rule[key]
    known = {
        "id", "fs_type", "priority", "exact_any", "include_all", "include_any",
        "exclude_any", "context_include_all", "context_include_any",
        "context_exclude_any", "conditions", "canonical_id", "fallback_if_missing",
        "amount_policy", "cash_direction", "cash_effect", "comparability", "relations",
        "reason",
    }
    known.update(key for key in rule if key.startswith("include_any_") or key.startswith("context_include_any_"))
    result: dict[str, Any] = {
        "id": rule["id"],
        "version": 2,
        "phase": "normalize",
        "priority": int(rule.get("priority", 0)),
        "applies": {"statement_types": [rule.get("fs_type", "ANY")]},
        "match": match,
        "emit": emit,
    }
    if rule.get("reason"):
        result["reason"] = rule["reason"]
    extensions = {key: value for key, value in rule.items() if key not in known}
    if extensions:
        result["extensions"] = extensions
    result["migration"] = {"source_file": str(source.relative_to(PROJECT_ROOT)).replace("\\", "/"), "source_index": index}
    return result


def migrate_context_rule(rule: dict[str, Any], source: Path, index: int) -> dict[str, Any]:
    action = rule.get("action", {}) or {}
    match: dict[str, Any] = {"label": _predicate(rule)}
    if rule.get("conditions"):
        match["constraints"] = dict(rule["conditions"])
    return {
        "id": rule["id"],
        "version": 2,
        "phase": "context",
        "priority": int(rule.get("priority", 0)),
        "applies": {"statement_types": [rule.get("fs_type", "ANY")]},
        "match": match,
        "emit": {
            "context_action": action.get("type", "IGNORE_CONTEXT"),
            "context_label": action.get("context_label", ""),
        },
        "reason": rule.get("reason", ""),
        "migration": {"source_file": str(source.relative_to(PROJECT_ROOT)).replace("\\", "/"), "source_index": index},
    }


def migrate_comment_rule(rule: dict[str, Any], source: Path, index: int) -> dict[str, Any]:
    captures = dict(rule.get("target_patterns", {}) or {})
    return {
        "id": rule["id"],
        "version": 2,
        "phase": "parse",
        "priority": int(rule.get("priority", 0)),
        "confidence": rule.get("confidence", "medium"),
        "match": {"section": {"contains_any": [rule.get("section_name", "")] }},
        "captures": captures,
        "emit": {"fact_layer": "REPORTED"},
        "migration": {"source_file": str(source.relative_to(PROJECT_ROOT)).replace("\\", "/"), "source_index": index},
    }


def source_record(path: Path, raw: bytes, kind: str, count: int) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "sha256": sha256(raw).hexdigest(),
        "rule_kind": kind,
        "source_rule_count": count,
    }


def migrate(
    *,
    mapping_path: Path = DEFAULT_MAPPING,
    context_path: Path = DEFAULT_CONTEXT,
    comment_path: Path = DEFAULT_COMMENT,
    sign_path: Path = DEFAULT_SIGN,
    k_gaap_path: Path = DEFAULT_K_GAAP,
    common_path: Path = DEFAULT_COMMON,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    mapping_raw, mapping = _read(mapping_path)
    context_raw, context = _read(context_path)
    comment_raw, comment = _read(comment_path)
    sign_raw, sign = _read(sign_path)
    _, k_gaap = _read(k_gaap_path)
    _, common = _read(common_path)
    mapping_rules = mapping.get("rules", [])
    context_rules = context.get("context_rules", [])
    comment_rules = comment.get("comment_rules", [])
    historical_rules = (k_gaap.get("rule_sets", {}) or {}).get("mapping", [])
    common_rules = (common.get("rule_sets", {}) or {}).get("mapping", [])
    migrated_mapping = [
        migrate_mapping_rule(rule, mapping_path, index)
        for index, rule in enumerate(mapping_rules)
    ]
    occurrences: dict[str, int] = {}
    for rule in migrated_mapping:
        legacy_id = str(rule["id"])
        occurrences[legacy_id] = occurrences.get(legacy_id, 0) + 1
        if occurrences[legacy_id] <= 1:
            continue
        rule["id"] = f"{legacy_id}__legacy_dup_{occurrences[legacy_id]}"
        rule.setdefault("extensions", {})["legacy_rule_id"] = legacy_id
    all_mapping = [*migrated_mapping, *historical_rules, *common_rules]
    ids = [rule["id"] for rule in all_mapping]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate mapping rule ids in v2 bundle")
    bundle = {
        "schema_version": 2,
        "engine": "arcana-financial-semantic",
        "profile": "kr_all_regimes",
        "description": "Deterministic typed semantic rules for K-GAAP, general K-GAAP and K-IFRS filings.",
        "migration": {
            "mapping_source_rule_count": len(mapping_rules),
            "mapping_migrated_rule_count": len(migrated_mapping),
            "context_source_rule_count": len(context_rules),
            "context_migrated_rule_count": len(context_rules),
            "comment_source_rule_count": len(comment_rules),
            "comment_migrated_rule_count": len(comment_rules),
            "native_k_gaap_rule_count": len(historical_rules),
            "native_common_rule_count": len(common_rules),
            "sources": [
                source_record(mapping_path, mapping_raw, "mapping", len(mapping_rules)),
                source_record(context_path, context_raw, "context", len(context_rules)),
                source_record(comment_path, comment_raw, "comment", len(comment_rules)),
                source_record(sign_path, sign_raw, "sign_policy", len(sign.get("canonical_policies", {}))),
            ],
        },
        "rule_sets": {
            "mapping": all_mapping,
            "context": [migrate_context_rule(rule, context_path, index) for index, rule in enumerate(context_rules)],
            "comments": [migrate_comment_rule(rule, comment_path, index) for index, rule in enumerate(comment_rules)],
        },
        "sign_policy": sign,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Arcana legacy YAML rules to semantic v2")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    bundle = migrate(output_path=args.output)
    print(yaml.safe_dump(bundle["migration"], allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    main()
