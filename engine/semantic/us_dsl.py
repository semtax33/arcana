from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterable

from engine.semantic.manifest import RuleBundleIntegrityError


US_RULE_GROUPS = (
    "companyfacts_rules",
    "notes_rules",
    "edgartools_fallback_rules",
)


@dataclass(frozen=True)
class UsSemanticRule:
    rule_id: str
    version: int
    sources: tuple[str, ...]
    forms: tuple[str, ...]
    accounting: str
    statement_type: str
    primary_concepts: tuple[str, ...] = ()
    alternate_concepts: tuple[str, ...] = ()
    component_sets: tuple[tuple[str, ...], ...] = ()
    exact_concepts: tuple[str, ...] = ()
    concept_patterns: tuple[str, ...] = ()
    concept_excludes: tuple[str, ...] = ()
    label_patterns: tuple[str, ...] = ()
    label_excludes: tuple[str, ...] = ()
    report_patterns: tuple[str, ...] = ()
    report_excludes: tuple[str, ...] = ()
    captures: dict[str, str] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    canonical_id: str = "UNMAPPED"
    amount_policy: str = "as_reported"
    cash_direction: str = ""
    legacy_group: str = ""
    legacy_index: int = -1
    legacy_id: str = ""
    legacy_fields: tuple[str, ...] = ()

    def to_legacy_rule(self) -> dict[str, Any]:
        if self.legacy_group not in US_RULE_GROUPS:
            raise ValueError(
                f"rule {self.rule_id!r} has invalid legacy group {self.legacy_group!r}"
            )
        rule: dict[str, Any] = {
            "canonical_id": self.canonical_id,
            "fs_type": self.statement_type,
        }
        explicit = set(self.legacy_fields)

        def include(key: str, value: Any) -> bool:
            return key in explicit if explicit else value not in (None, "", (), [])

        if include("id", self.legacy_id):
            rule["id"] = self.legacy_id

        if self.legacy_group == "companyfacts_rules":
            _put_list(rule, "primary_tags", self.primary_concepts, explicit)
            _put_list(rule, "alternate_tags", self.alternate_concepts, explicit)
            if self.component_sets:
                rule["component_sets"] = [list(group) for group in self.component_sets]
            _put_list(rule, "label_patterns", self.label_patterns, explicit)
            _put_list(rule, "label_exclude_patterns", self.label_excludes, explicit)
            _put_list(rule, "report_name_patterns", self.report_patterns, explicit)
            _put_list(rule, "report_name_exclude_patterns", self.report_excludes, explicit)
            if self.constraints.get("presentation_required"):
                rule["require_statement_scope"] = True
                rule["statement_anchor_tags"] = list(self.constraints.get("presentation_anchor", []))
        elif self.legacy_group == "notes_rules":
            _put_list(rule, "tags", self.exact_concepts, explicit)
            _put_list(rule, "tag_patterns", self.concept_patterns, explicit)
            _put_list(rule, "tag_exclude_patterns", self.concept_excludes, explicit)
            _put_list(rule, "label_patterns", self.label_patterns, explicit)
            _put_list(rule, "label_exclude_patterns", self.label_excludes, explicit)
            _put_list(rule, "report_name_patterns", self.report_patterns, explicit)
            _put_list(rule, "report_name_exclude_patterns", self.report_excludes, explicit)
        else:
            _put_list(rule, "tags", self.exact_concepts, explicit)

        if include("amount_policy", self.amount_policy) and (
            explicit or self.amount_policy != "as_reported"
        ):
            rule["amount_policy"] = self.amount_policy
        if include("cash_direction", self.cash_direction):
            rule["cash_direction"] = self.cash_direction
        return rule


@dataclass(frozen=True)
class UsSemanticRuleSet:
    name: str
    schema: str
    version: int
    authority: tuple[str, ...]
    rules: tuple[UsSemanticRule, ...]

    def to_legacy_rule_groups(self) -> dict[str, list[dict[str, Any]]]:
        groups = {name: [] for name in US_RULE_GROUPS}
        ordered = sorted(
            enumerate(self.rules),
            key=lambda item: (
                US_RULE_GROUPS.index(item[1].legacy_group),
                item[1].legacy_index if item[1].legacy_index >= 0 else item[0],
            ),
        )
        for _, rule in ordered:
            groups[rule.legacy_group].append(rule.to_legacy_rule())
        return groups


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    offset: int


_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>\#[^\r\n]*|//[^\r\n]*)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.:/-]*)
  | (?P<symbol>[{}\[\]=,])
  | (?P<invalid>.)
    """,
    re.VERBOSE,
)


class _DslParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tuple(self._tokenize(text))
        self.index = 0

    @staticmethod
    def _tokenize(text: str) -> Iterable[_Token]:
        for match in _TOKEN_RE.finditer(text):
            kind = match.lastgroup or "invalid"
            if kind in {"space", "comment"}:
                continue
            if kind == "invalid":
                raise ValueError(
                    f"invalid US semantic DSL character {match.group()!r} at offset {match.start()}"
                )
            yield _Token(kind, match.group(), match.start())

    def parse(self) -> tuple[tuple[str, str, dict[str, Any]], ...]:
        entries: list[tuple[str, str, dict[str, Any]]] = []
        while not self._at_end:
            kind = self._take("ident").value
            name = self._parse_name()
            entries.append((kind, name, self._parse_block()))
        return tuple(entries)

    def _parse_name(self) -> str:
        token = self._peek()
        if token.kind == "string":
            self.index += 1
            return str(json.loads(token.value))
        if token.kind == "ident":
            self.index += 1
            return token.value
        self._fail("expected block name", token)

    def _parse_block(self) -> dict[str, Any]:
        self._take_value("{")
        output: dict[str, Any] = {}
        while self._peek().value != "}":
            key = self._take("ident").value
            if self._peek().value == "=":
                self.index += 1
                output[key] = self._parse_value()
                continue

            qualifier = ""
            if self._peek().value != "{":
                qualifier = self._parse_name()
            nested_key = f"{key} {qualifier}".strip()
            if nested_key in output:
                self._fail(f"duplicate block {nested_key!r}", self._peek())
            output[nested_key] = self._parse_block()
        self._take_value("}")
        return output

    def _parse_value(self) -> Any:
        token = self._peek()
        if token.value == "[":
            self.index += 1
            values: list[Any] = []
            while self._peek().value != "]":
                values.append(self._parse_value())
                if self._peek().value == ",":
                    self.index += 1
                elif self._peek().value != "]":
                    self._fail("expected ',' or ']'", self._peek())
            self._take_value("]")
            return values
        if token.kind == "string":
            self.index += 1
            return json.loads(token.value)
        if token.kind == "number":
            self.index += 1
            return float(token.value) if "." in token.value else int(token.value)
        if token.kind == "ident":
            self.index += 1
            lowered = token.value.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered == "null":
                return None
            return token.value
        self._fail("expected value", token)

    @property
    def _at_end(self) -> bool:
        return self.index >= len(self.tokens)

    def _peek(self) -> _Token:
        if self._at_end:
            return _Token("eof", "", len(self.text))
        return self.tokens[self.index]

    def _take(self, kind: str) -> _Token:
        token = self._peek()
        if token.kind != kind:
            self._fail(f"expected {kind}", token)
        self.index += 1
        return token

    def _take_value(self, value: str) -> _Token:
        token = self._peek()
        if token.value != value:
            self._fail(f"expected {value!r}", token)
        self.index += 1
        return token

    def _fail(self, message: str, token: _Token) -> None:
        line = self.text.count("\n", 0, token.offset) + 1
        raise ValueError(f"{message} at line {line}")


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _put_list(
    target: dict[str, Any],
    key: str,
    values: tuple[str, ...],
    explicit: set[str] | None = None,
) -> None:
    if values or (explicit and key in explicit):
        target[key] = list(values)


def _component_sets(value: Any) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(group, list) or len(group) < 2
        or any(not isinstance(tag, str) or not tag.strip() for tag in group)
        or len(set(group)) != len(group)
        for group in value
    ):
        raise ValueError("concept.component_sets requires lists of at least two distinct concepts")
    return tuple(tuple(group) for group in value)


def _compile_rule(rule_id: str, data: dict[str, Any]) -> UsSemanticRule:
    applies = data.get("applies", {})
    match = data.get("match fact", {})
    capture = data.get("capture", {})
    constraint = data.get("constraint", {})
    emit = data.get("emit", {})
    legacy = data.get("legacy", {})
    required_blocks = {
        "applies": applies,
        "match fact": match,
        "capture": capture,
        "constraint": constraint,
        "emit": emit,
        "legacy": legacy,
    }
    missing = [name for name, value in required_blocks.items() if not isinstance(value, dict)]
    if missing:
        raise ValueError(f"rule {rule_id!r} missing blocks: {', '.join(missing)}")
    if "presentation_required" in constraint and type(constraint["presentation_required"]) is not bool:
        raise ValueError("presentation_required must be a boolean")
    if constraint.get("presentation_required"):
        anchors = constraint.get("presentation_anchor")
        if not isinstance(anchors, list) or not anchors or any(not isinstance(v, str) or not v for v in anchors):
            raise ValueError("presentation_required needs nonempty presentation_anchor concepts")
        if not match.get("report.matches") or not match.get("label.matches"):
            raise ValueError("presentation_required needs report and statement label patterns")

    return UsSemanticRule(
        rule_id=rule_id,
        version=int(data.get("version", 2)),
        sources=_tuple_of_strings(applies.get("source")),
        forms=_tuple_of_strings(applies.get("form")),
        accounting=str(applies.get("accounting", "US_GAAP")),
        statement_type=str(constraint.get("statement", "UNKNOWN")),
        primary_concepts=_tuple_of_strings(match.get("concept.primary")),
        alternate_concepts=_tuple_of_strings(match.get("concept.alternate")),
        component_sets=_component_sets(match.get("concept.component_sets")),
        exact_concepts=_tuple_of_strings(match.get("concept.exact")),
        concept_patterns=_tuple_of_strings(match.get("concept.matches")),
        concept_excludes=_tuple_of_strings(match.get("concept.excludes")),
        label_patterns=_tuple_of_strings(match.get("label.matches")),
        label_excludes=_tuple_of_strings(match.get("label.excludes")),
        report_patterns=_tuple_of_strings(match.get("report.matches")),
        report_excludes=_tuple_of_strings(match.get("report.excludes")),
        captures={str(key): str(value) for key, value in capture.items()},
        constraints=dict(constraint),
        canonical_id=str(emit.get("canonical", "UNMAPPED")),
        amount_policy=str(emit.get("amount", "as_reported")),
        cash_direction=str(emit.get("cash_direction", "")),
        legacy_group=str(legacy.get("group", "")),
        legacy_index=int(legacy.get("index", -1)),
        legacy_id=str(legacy.get("id", "")),
        legacy_fields=_tuple_of_strings(legacy.get("fields")),
    )


def parse_us_semantic_rules(text: str) -> UsSemanticRuleSet:
    entries = _DslParser(text).parse()
    rulesets = [(name, data) for kind, name, data in entries if kind == "ruleset"]
    if len(rulesets) != 1:
        raise ValueError("US semantic DSL must contain exactly one ruleset block")
    unexpected = sorted({kind for kind, _, _ in entries if kind not in {"ruleset", "rule"}})
    if unexpected:
        raise ValueError(f"unsupported top-level blocks: {', '.join(unexpected)}")

    name, header = rulesets[0]
    rules = tuple(
        _compile_rule(rule_id, data)
        for kind, rule_id, data in entries
        if kind == "rule"
    )
    ids = [rule.rule_id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("US semantic DSL rule ids must be unique")
    return UsSemanticRuleSet(
        name=name,
        schema=str(header.get("schema", "")),
        version=int(header.get("version", 2)),
        authority=_tuple_of_strings(header.get("authority")),
        rules=rules,
    )


def load_us_semantic_rules(path: str | Path) -> UsSemanticRuleSet:
    source_path = Path(path).resolve()
    if source_path.suffix.lower() != ".json":
        return parse_us_semantic_rules(source_path.read_text(encoding="utf-8"))

    manifest = json.loads(source_path.read_text(encoding="utf-8"))
    required = {
        "bundle_id",
        "schema",
        "version",
        "active_bundle",
        "hash",
        "sha256",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise RuleBundleIntegrityError(
            f"US semantic rule manifest missing fields {missing}: {source_path}"
        )
    expected_hash = str(manifest["sha256"])
    if str(manifest["hash"]) != f"sha256:{expected_hash}":
        raise RuleBundleIntegrityError("US semantic rule manifest hash fields disagree")
    bundle_path = (source_path.parent / str(manifest["active_bundle"])).resolve()
    if bundle_path.parent != source_path.parent or not bundle_path.is_file():
        raise RuleBundleIntegrityError(f"invalid US semantic rule bundle path: {bundle_path}")
    actual_hash = sha256(bundle_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise RuleBundleIntegrityError(
            "US semantic rule bundle checksum mismatch: "
            f"expected={expected_hash} actual={actual_hash}"
        )
    ruleset = parse_us_semantic_rules(bundle_path.read_text(encoding="utf-8"))
    if ruleset.schema != str(manifest["schema"]):
        raise RuleBundleIntegrityError(
            f"US semantic rule schema mismatch: expected={manifest['schema']} "
            f"actual={ruleset.schema}"
        )
    if ruleset.version != int(manifest["version"]):
        raise RuleBundleIntegrityError(
            f"US semantic rule version mismatch: expected={manifest['version']} "
            f"actual={ruleset.version}"
        )
    expected_count = manifest.get("rule_count")
    if expected_count is not None and len(ruleset.rules) != int(expected_count):
        raise RuleBundleIntegrityError(
            f"US semantic rule count mismatch: expected={expected_count} "
            f"actual={len(ruleset.rules)}"
        )
    return ruleset


def render_us_semantic_v2(legacy_groups: dict[str, Any]) -> str:
    unknown = sorted(set(legacy_groups) - set(US_RULE_GROUPS))
    if unknown:
        raise ValueError(f"unsupported legacy US rule groups: {', '.join(unknown)}")

    lines = [
        'ruleset "semantic_us_v2" {',
        '  schema = "arcana.sec-semantic/v2"',
        '  version = 2',
        '  authority = ["FILING_XBRL", "COMPANYFACTS", "FINANCIAL_STATEMENT_NOTES"]',
        '}',
        '',
    ]
    for group in US_RULE_GROUPS:
        rules = legacy_groups.get(group, [])
        if not isinstance(rules, list):
            raise ValueError(f"{group} must be a list")
        for index, rule in enumerate(rules):
            lines.extend(_render_legacy_rule(group, index, rule))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_legacy_rule(group: str, index: int, rule: dict[str, Any]) -> list[str]:
    canonical_id = str(rule.get("canonical_id", "UNMAPPED"))
    prefix = {
        "companyfacts_rules": "concept",
        "notes_rules": "notes",
        "edgartools_fallback_rules": "legacy_edgartools",
    }[group]
    rule_id = str(rule.get("id") or f"{prefix}_{index:03d}_{canonical_id.lower()}")
    if group == "companyfacts_rules":
        sources = ["FILING_XBRL", "COMPANYFACTS"]
    elif group == "notes_rules":
        sources = ["FINANCIAL_STATEMENT_NOTES"]
    else:
        sources = ["EDGARTOOLS_COMPANY_FACTS"]

    def literal(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    output = [
        f"rule {literal(rule_id)} {{",
        "  version = 2",
        "  applies {",
        f"    source = {literal(sources)}",
        '    form = ["10-K","10-Q","10-K/A","10-Q/A"]',
        '    accounting = "US_GAAP"',
        "  }",
        "  match fact {",
    ]
    match_fields = {
        "primary_tags": "concept.primary",
        "alternate_tags": "concept.alternate",
        "component_sets": "concept.component_sets",
        "tags": "concept.exact",
        "tag_patterns": "concept.matches",
        "tag_exclude_patterns": "concept.excludes",
        "label_patterns": "label.matches",
        "label_exclude_patterns": "label.excludes",
        "report_name_patterns": "report.matches",
        "report_name_exclude_patterns": "report.excludes",
    }
    for legacy_key, dsl_key in match_fields.items():
        if legacy_key in rule:
            output.append(f"    {dsl_key} = {literal(rule[legacy_key])}")
    output.extend(
        [
            "  }",
            "  capture {",
            '    value = "fact.numeric_value"',
            '    period = "context.period"',
            '    dimensions = "context.dimensions"',
            "  }",
            "  constraint {",
            f"    statement = {literal(str(rule.get('fs_type', 'UNKNOWN')))}",
            '    entity = "filing.entity"',
            '    dimensions = "CONSOLIDATED_OR_NONE"',
            *([f"    presentation_required = {literal(rule['require_statement_scope'])}",
               f"    presentation_anchor = {literal(rule.get('statement_anchor_tags', []))}"]
              if rule.get("require_statement_scope") else []),
            "  }",
            "  emit {",
            f"    canonical = {literal(canonical_id)}",
            f"    amount = {literal(str(rule.get('amount_policy', 'as_reported')))}",
            f"    cash_direction = {literal(str(rule.get('cash_direction', '')))}",
            "  }",
            "  legacy {",
            f"    group = {literal(group)}",
            f"    index = {index}",
        ]
    )
    if "id" in rule:
        output.append(f"    id = {literal(str(rule['id']))}")
    output.extend(
        [
            f"    fields = {literal(list(rule))}",
            "  }",
            "}",
        ]
    )
    return output
