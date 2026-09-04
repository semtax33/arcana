from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .models import (
    AccountingRegimeFamily,
    Comparability,
    DisclosureSourceType,
    DocumentDialect,
    RelationType,
)


class RulePhase(str, Enum):
    PARSE = "parse"
    CONTEXT = "context"
    NORMALIZE = "normalize"
    VALIDATE = "validate"
    HARMONIZE = "harmonize"


@dataclass(frozen=True)
class TextPredicate:
    exact_any: tuple[str, ...] = ()
    contains_all: tuple[str, ...] = ()
    contains_any_groups: tuple[tuple[str, ...], ...] = ()
    excludes_any: tuple[str, ...] = ()
    regex_any: tuple[str, ...] = ()

    @property
    def phrases(self) -> tuple[str, ...]:
        values = [*self.contains_all, *self.excludes_any]
        for group in self.contains_any_groups:
            values.extend(group)
        return tuple(values)


@dataclass(frozen=True)
class StructuralConstraint:
    has_children: bool | None = None
    amount_is_zero_or_blank: bool | None = None
    required_relations: tuple[RelationType, ...] = ()


@dataclass(frozen=True)
class RuleApplicability:
    statement_types: tuple[str, ...] = ("ANY",)
    accounting_regimes: tuple[AccountingRegimeFamily, ...] = ()
    document_dialects: tuple[DocumentDialect, ...] = ()
    source_types: tuple[DisclosureSourceType, ...] = ()
    sector_codes: tuple[str, ...] = ()
    industry_group_codes: tuple[str, ...] = ()
    table_kinds: tuple[str, ...] = ()
    effective_from: date | None = None
    effective_to: date | None = None


@dataclass(frozen=True)
class EmitAction:
    canonical_id: str = "UNMAPPED"
    amount_policy: str = ""
    cash_direction: str = ""
    fallback_if_missing: str = "UNMAPPED"
    comparability: Comparability = Comparability.EXACT
    context_action: str = ""
    context_label: str = ""
    relations: tuple[RelationType, ...] = ()


@dataclass(frozen=True)
class SemanticRule:
    rule_id: str
    version: int
    phase: RulePhase
    priority: int
    applies: RuleApplicability
    label: TextPredicate = field(default_factory=TextPredicate)
    context: TextPredicate = field(default_factory=TextPredicate)
    constraints: StructuralConstraint = field(default_factory=StructuralConstraint)
    emit: EmitAction = field(default_factory=EmitAction)
    reason: str = ""
    source_file: str = ""
    source_index: int | None = None
    captures: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    extensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleSource:
    path: str
    sha256: str
    rule_kind: str
    source_rule_count: int


@dataclass(frozen=True)
class SemanticRuleSet:
    schema_version: int
    profile: str
    rules: tuple[SemanticRule, ...]
    sources: tuple[RuleSource, ...] = ()
    declared_source_rule_count: int | None = None

    @property
    def source_rule_count(self) -> int:
        return sum(source.source_rule_count for source in self.sources)

    @property
    def normalization_rules(self) -> tuple[SemanticRule, ...]:
        return tuple(rule for rule in self.rules if rule.phase == RulePhase.NORMALIZE)


LEGACY_MAPPING_KEYS = {
    "id",
    "fs_type",
    "priority",
    "exact_any",
    "include_all",
    "include_any",
    "exclude_any",
    "context_include_all",
    "context_include_any",
    "context_exclude_any",
    "conditions",
    "canonical_id",
    "fallback_if_missing",
    "amount_policy",
    "cash_direction",
    "cash_effect",
    "comparability",
    "relations",
    "accounting_regimes",
    "document_dialects",
    "effective_from",
    "effective_to",
    "reason",
    "_source",
}

V2_RULE_KEYS = {
    "id",
    "version",
    "phase",
    "priority",
    "applies",
    "match",
    "captures",
    "emit",
    "reason",
    "migration",
    "extensions",
}
V2_APPLIES_KEYS = {
    "statement_types",
    "accounting_regimes",
    "document_dialects",
    "source_types",
    "sector_codes",
    "industry_group_codes",
    "table_kinds",
    "effective_from",
    "effective_to",
}
V2_MATCH_KEYS = {"label", "context", "constraints"}
V2_PREDICATE_KEYS = {
    "exact_any",
    "contains_all",
    "contains_any_groups",
    "excludes_any",
    "regex_any",
}
V2_CONSTRAINT_KEYS = {
    "has_children",
    "amount_is_zero_or_blank",
    "required_relations",
}
V2_EMIT_KEYS = {
    "canonical_id",
    "amount_policy",
    "cash_direction",
    "fallback_if_missing",
    "comparability",
    "context_action",
    "context_label",
    "relations",
}


def _ensure_only_keys(
    data: Mapping[str, Any], allowed: set[str], *, location: str
) -> None:
    unknown = sorted(str(key) for key in data if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unsupported fields at {location}: {', '.join(unknown)}")


def _tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        return (values,)
    return tuple(str(value) for value in values if str(value).strip())


def _enum_tuple(enum_type, values: Any) -> tuple:
    output = []
    for value in _tuple(values):
        try:
            output.append(enum_type(value))
        except ValueError as exc:
            raise ValueError(f"invalid {enum_type.__name__}: {value}") from exc
    return tuple(output)


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _numbered_groups(rule: Mapping[str, Any], prefix: str) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[int, tuple[str, ...]]] = []
    if prefix in rule:
        groups.append((0, _tuple(rule[prefix])))
    marker = f"{prefix}_"
    for key, value in rule.items():
        if not str(key).startswith(marker):
            continue
        suffix = str(key)[len(marker):]
        if suffix.isdigit():
            groups.append((int(suffix), _tuple(value)))
    return tuple(group for _, group in sorted(groups) if group)


def _predicate_from_legacy(rule: Mapping[str, Any], prefix: str = "") -> TextPredicate:
    key = lambda value: f"{prefix}{value}"
    return TextPredicate(
        exact_any=_tuple(rule.get(key("exact_any"))),
        contains_all=_tuple(rule.get(key("include_all"))),
        contains_any_groups=_numbered_groups(rule, key("include_any")),
        excludes_any=_tuple(rule.get(key("exclude_any"))),
        regex_any=_tuple(rule.get(key("regex_any"))),
    )


def _predicate_from_v2(data: Mapping[str, Any] | None) -> TextPredicate:
    data = data or {}
    groups = data.get("contains_any_groups", ())
    return TextPredicate(
        exact_any=_tuple(data.get("exact_any")),
        contains_all=_tuple(data.get("contains_all")),
        contains_any_groups=tuple(_tuple(group) for group in groups if _tuple(group)),
        excludes_any=_tuple(data.get("excludes_any")),
        regex_any=_tuple(data.get("regex_any")),
    )


def _comparability(value: Any) -> Comparability:
    try:
        return Comparability(str(value or Comparability.EXACT.value))
    except ValueError as exc:
        raise ValueError(f"invalid comparability: {value}") from exc


def _relations(values: Any) -> tuple[RelationType, ...]:
    return _enum_tuple(RelationType, values)


def compile_legacy_mapping_rule(
    rule: Mapping[str, Any],
    *,
    source_file: str = "",
    source_index: int | None = None,
    text_normalizer: Callable[[Any], str] = str,
) -> SemanticRule:
    unknown = {
        str(key)
        for key in rule
        if str(key) not in LEGACY_MAPPING_KEYS
        and not str(key).startswith("include_any_")
        and not str(key).startswith("context_include_any_")
        and not str(key).startswith("_")
    }
    extensions = {key: rule[key] for key in sorted(unknown)}
    conditions = rule.get("conditions", {}) or {}
    fs_type = str(rule.get("fs_type", "ANY") or "ANY")
    emit_relations = _relations(rule.get("relations"))

    def normalize_predicate(predicate: TextPredicate) -> TextPredicate:
        return TextPredicate(
            exact_any=tuple(text_normalizer(value) for value in predicate.exact_any),
            contains_all=tuple(text_normalizer(value) for value in predicate.contains_all),
            contains_any_groups=tuple(
                tuple(text_normalizer(value) for value in group)
                for group in predicate.contains_any_groups
            ),
            excludes_any=tuple(text_normalizer(value) for value in predicate.excludes_any),
            regex_any=predicate.regex_any,
        )

    return SemanticRule(
        rule_id=str(rule.get("id") or "").strip(),
        version=2,
        phase=RulePhase.NORMALIZE,
        priority=int(rule.get("priority", 0) or 0),
        applies=RuleApplicability(
            statement_types=(fs_type,),
            accounting_regimes=_enum_tuple(
                AccountingRegimeFamily, rule.get("accounting_regimes")
            ),
            document_dialects=_enum_tuple(
                DocumentDialect, rule.get("document_dialects")
            ),
            effective_from=_date(rule.get("effective_from")),
            effective_to=_date(rule.get("effective_to")),
        ),
        label=normalize_predicate(_predicate_from_legacy(rule)),
        context=normalize_predicate(_predicate_from_legacy(rule, "context_")),
        constraints=StructuralConstraint(
            has_children=(
                bool(conditions["has_children"])
                if "has_children" in conditions
                else None
            ),
            amount_is_zero_or_blank=(
                bool(conditions["amount_is_zero_or_blank"])
                if "amount_is_zero_or_blank" in conditions
                else None
            ),
        ),
        emit=EmitAction(
            canonical_id=str(rule.get("canonical_id", "UNMAPPED") or "UNMAPPED"),
            amount_policy=str(rule.get("amount_policy", "") or ""),
            cash_direction=str(
                rule.get("cash_direction", rule.get("cash_effect", "")) or ""
            ),
            fallback_if_missing=str(
                rule.get("fallback_if_missing", "UNMAPPED") or "UNMAPPED"
            ),
            comparability=_comparability(rule.get("comparability")),
            relations=emit_relations,
        ),
        reason=str(rule.get("reason", "") or ""),
        source_file=source_file or str(rule.get("_source", "") or ""),
        source_index=source_index,
        extensions=extensions,
    )


def compile_v2_mapping_rule(
    data: Mapping[str, Any],
    *,
    text_normalizer: Callable[[Any], str] = str,
) -> SemanticRule:
    rule_id = str(data.get("id", "") or "")
    if not rule_id:
        raise ValueError("semantic rule id is required")
    version = int(data.get("version", 2) or 2)
    if version < 1:
        raise ValueError(f"rule[{rule_id}].version must be >= 1")
    _ensure_only_keys(data, V2_RULE_KEYS, location=f"rule[{rule_id or '?'}]")
    applies = data.get("applies", {}) or {}
    match = data.get("match", {}) or {}
    constraints = match.get("constraints", {}) or {}
    emit = data.get("emit", {}) or {}
    migration = data.get("migration", {}) or {}
    _ensure_only_keys(applies, V2_APPLIES_KEYS, location=f"rule[{rule_id}].applies")
    _ensure_only_keys(match, V2_MATCH_KEYS, location=f"rule[{rule_id}].match")
    _ensure_only_keys(
        match.get("label", {}) or {},
        V2_PREDICATE_KEYS,
        location=f"rule[{rule_id}].match.label",
    )
    _ensure_only_keys(
        match.get("context", {}) or {},
        V2_PREDICATE_KEYS,
        location=f"rule[{rule_id}].match.context",
    )
    _ensure_only_keys(
        constraints,
        V2_CONSTRAINT_KEYS,
        location=f"rule[{rule_id}].match.constraints",
    )
    _ensure_only_keys(emit, V2_EMIT_KEYS, location=f"rule[{rule_id}].emit")

    def normalize_predicate(predicate: TextPredicate) -> TextPredicate:
        return TextPredicate(
            exact_any=tuple(text_normalizer(value) for value in predicate.exact_any),
            contains_all=tuple(text_normalizer(value) for value in predicate.contains_all),
            contains_any_groups=tuple(
                tuple(text_normalizer(value) for value in group)
                for group in predicate.contains_any_groups
            ),
            excludes_any=tuple(text_normalizer(value) for value in predicate.excludes_any),
            regex_any=predicate.regex_any,
        )

    return SemanticRule(
        rule_id=rule_id,
        version=version,
        phase=RulePhase(str(data.get("phase", RulePhase.NORMALIZE.value))),
        priority=int(data.get("priority", 0) or 0),
        applies=RuleApplicability(
            statement_types=_tuple(applies.get("statement_types", ("ANY",))),
            accounting_regimes=_enum_tuple(
                AccountingRegimeFamily, applies.get("accounting_regimes")
            ),
            document_dialects=_enum_tuple(
                DocumentDialect, applies.get("document_dialects")
            ),
            source_types=_enum_tuple(
                DisclosureSourceType, applies.get("source_types")
            ),
            sector_codes=_tuple(applies.get("sector_codes")),
            industry_group_codes=_tuple(applies.get("industry_group_codes")),
            table_kinds=_tuple(applies.get("table_kinds")),
            effective_from=_date(applies.get("effective_from")),
            effective_to=_date(applies.get("effective_to")),
        ),
        label=normalize_predicate(_predicate_from_v2(match.get("label"))),
        context=normalize_predicate(_predicate_from_v2(match.get("context"))),
        constraints=StructuralConstraint(
            has_children=constraints.get("has_children"),
            amount_is_zero_or_blank=constraints.get("amount_is_zero_or_blank"),
            required_relations=_relations(constraints.get("required_relations")),
        ),
        emit=EmitAction(
            canonical_id=str(emit.get("canonical_id", "UNMAPPED") or "UNMAPPED"),
            amount_policy=str(emit.get("amount_policy", "") or ""),
            cash_direction=str(emit.get("cash_direction", "") or ""),
            fallback_if_missing=str(
                emit.get("fallback_if_missing", "UNMAPPED") or "UNMAPPED"
            ),
            comparability=_comparability(emit.get("comparability")),
            context_action=str(emit.get("context_action", "") or ""),
            context_label=str(emit.get("context_label", "") or ""),
            relations=_relations(emit.get("relations")),
        ),
        reason=str(data.get("reason", "") or ""),
        source_file=str(migration.get("source_file", "") or ""),
        source_index=(
            int(migration["source_index"])
            if migration.get("source_index") is not None
            else None
        ),
        captures={
            str(key): _tuple(value)
            for key, value in (data.get("captures", {}) or {}).items()
        },
        extensions=data.get("extensions", {}) or {},
    )


def load_semantic_mapping_rules(
    paths: Sequence[str | Path],
    *,
    text_normalizer: Callable[[Any], str] = str,
) -> SemanticRuleSet:
    compiled: list[SemanticRule] = []
    sources: list[RuleSource] = []
    schema_version = 2
    profile = "mixed"
    declared_source_count = 0

    for raw_path in paths:
        path = Path(raw_path)
        raw = path.read_bytes()
        data = yaml.safe_load(raw) or {}
        source_hash = sha256(raw).hexdigest()

        if int(data.get("schema_version", 1) or 1) >= 2:
            schema_version = max(schema_version, int(data["schema_version"]))
            profile = str(data.get("profile", profile))
            mapping_rules = (
                (data.get("rule_sets", {}) or {}).get("mapping", []) or []
            )
            for rule in mapping_rules:
                compiled.append(
                    compile_v2_mapping_rule(rule, text_normalizer=text_normalizer)
                )
            migration = data.get("migration", {}) or {}
            declared_source_count += int(
                migration.get("mapping_source_rule_count", len(mapping_rules))
            )
            for source in migration.get("sources", []) or []:
                if str(source.get("rule_kind")) != "mapping":
                    continue
                sources.append(
                    RuleSource(
                        path=str(source.get("path", "")),
                        sha256=str(source.get("sha256", "")),
                        rule_kind="mapping",
                        source_rule_count=int(source.get("source_rule_count", 0)),
                    )
                )
        else:
            rules = data.get("rules", [])
            if not isinstance(rules, list):
                raise ValueError(f"rules must be a list: {path}")
            for index, rule in enumerate(rules):
                compiled.append(
                    compile_legacy_mapping_rule(
                        rule,
                        source_file=str(path),
                        source_index=index,
                        text_normalizer=text_normalizer,
                    )
                )
            declared_source_count += len(rules)
            sources.append(
                RuleSource(
                    path=str(path),
                    sha256=source_hash,
                    rule_kind="mapping",
                    source_rule_count=len(rules),
                )
            )

    ids = [rule.rule_id for rule in compiled]
    if any(not rule_id for rule_id in ids):
        raise ValueError("all semantic mapping rules must have an id")
    # Historical YAML contains a few duplicated ids.  Rule identity is the
    # (source file, source index) pair, so retaining both is lossless and keeps
    # the legacy stable-order/first-match behavior.

    return SemanticRuleSet(
        schema_version=schema_version,
        profile=profile,
        rules=tuple(compiled),
        sources=tuple(sources),
        declared_source_rule_count=declared_source_count,
    )


def phrases_from_rules(rules: Iterable[SemanticRule]) -> tuple[str, ...]:
    values: set[str] = set()
    for rule in rules:
        values.update(rule.label.phrases)
        values.update(rule.context.phrases)
    values.discard("")
    return tuple(sorted(values))
