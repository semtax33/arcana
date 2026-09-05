from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
import re
from typing import Any, Iterable, Mapping


_SOURCE_YEAR_RE = re.compile(r"\((\d{4})[._]\d{1,2}\)")
_AMOUNT_TOKEN_RE = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:조원|십억원|천만원|백만원|십만원|억원|만원|천원|백원|십원|원|%)"
)


def _row(candidate: object) -> dict[str, Any]:
    if isinstance(candidate, Mapping):
        return dict(candidate)
    if is_dataclass(candidate):
        return asdict(candidate)
    return dict(vars(candidate))


def _value(value: object) -> str:
    return str(getattr(value, "value", value or "UNKNOWN"))


def _category(row: Mapping[str, Any]) -> str:
    reasons = {str(reason) for reason in row.get("reasons", ()) or ()}
    canonical_ids = tuple(row.get("canonical_ids", ()) or ())
    if _value(row.get("period_role")) == "AMBIGUOUS" or "ambiguous_period" in reasons:
        return "period_ambiguity"
    if not bool(row.get("context_eligible", True)) or "rule_context_not_satisfied" in reasons:
        return "context_mismatch"
    if "multiple_nearby_amounts" in reasons:
        return "multiple_amount_ambiguity"
    if "multiple_account_mentions" in reasons:
        return "multiple_account_ambiguity"
    if len(canonical_ids) != 1 or "alias_maps_to_multiple_accounts" in reasons:
        return "alias_ambiguity"
    if _value(row.get("scope")) == "UNKNOWN":
        return "scope_unknown"
    if bool(row.get("review_required", True)):
        return "other_review"
    return "stable_relation_template"


def _source_identity(row: Mapping[str, Any]) -> tuple[str, int | None]:
    source_uri = str(row.get("source_uri") or "").replace("\\", "/")
    parts = [part for part in source_uri.split("/") if part]
    company = parts[-2] if len(parts) >= 2 else ""
    match = _SOURCE_YEAR_RE.search(source_uri)
    return company, int(match.group(1)) if match else None


def _syntax_pattern(row: Mapping[str, Any]) -> tuple[str, int]:
    text = " ".join(str(row.get("source_text") or "").split())
    alias = str(row.get("matched_alias") or "").strip()
    if alias:
        text = re.sub(re.escape(alias), "<ACCOUNT>", text, flags=re.IGNORECASE)
    amount_count = len(_AMOUNT_TOKEN_RE.findall(text))
    return _AMOUNT_TOKEN_RE.sub("<AMOUNT>", text)[:500], amount_count


def cluster_narrative_candidates(
    candidates: Iterable[object],
    *,
    promotion_minimum: int = 3,
    sample_limit: int = 3,
) -> dict[str, Any]:
    """Cluster ambiguous prose facts for review; never promote them automatically."""

    rows = [_row(candidate) for candidate in candidates]
    category_counts: Counter[str] = Counter()
    clusters: dict[tuple[str, ...], dict[str, Any]] = {}
    false_emit_count = 0
    for row in rows:
        category = _category(row)
        category_counts[category] += 1
        canonical_ids = tuple(sorted(str(value) for value in row.get("canonical_ids", ()) or ()))
        key = (
            category,
            _value(row.get("source_type")),
            _value(row.get("relation")),
            str(row.get("matched_alias") or ""),
            "|".join(canonical_ids),
            str(row.get("table_kind") or "UNKNOWN"),
        )
        cluster = clusters.setdefault(
            key,
            {
                "category": category,
                "source_type": key[1],
                "relation": key[2],
                "matched_alias": key[3],
                "canonical_ids": list(canonical_ids),
                "table_kind": key[5],
                "candidate_count": 0,
                "samples": [],
                "_companies": set(),
                "_years": set(),
                "_relations": Counter(),
                "_amount_counts": Counter(),
                "_syntax_patterns": Counter(),
                "period_ambiguity_count": 0,
                "scope_ambiguity_count": 0,
            },
        )
        cluster["candidate_count"] += 1
        company, year = _source_identity(row)
        if company:
            cluster["_companies"].add(company)
        if year is not None:
            cluster["_years"].add(year)
        relation = _value(row.get("relation"))
        cluster["_relations"][relation] += 1
        pattern, amount_count = _syntax_pattern(row)
        cluster["_amount_counts"][str(amount_count)] += 1
        if pattern:
            cluster["_syntax_patterns"][pattern] += 1
        cluster["period_ambiguity_count"] += int(
            _value(row.get("period_role")) == "AMBIGUOUS"
        )
        cluster["scope_ambiguity_count"] += int(
            _value(row.get("scope")) == "UNKNOWN"
        )
        text = " ".join(str(row.get("source_text") or "").split())[:500]
        if text and len(cluster["samples"]) < sample_limit and text not in cluster["samples"]:
            cluster["samples"].append(text)
        unsafe_emit = bool(row.get("auto_emit_eligible", False)) and (
            bool(row.get("review_required", True))
            or not bool(row.get("context_eligible", True))
            or _value(row.get("period_role")) == "AMBIGUOUS"
            or len(canonical_ids) != 1
        )
        false_emit_count += int(unsafe_emit)

    materialized_clusters = []
    for cluster in clusters.values():
        companies = cluster.pop("_companies")
        years = cluster.pop("_years")
        relations = cluster.pop("_relations")
        amount_counts = cluster.pop("_amount_counts")
        syntax_patterns = cluster.pop("_syntax_patterns")
        cluster.update(
            {
                "company_count": len(companies),
                "year_count": len(years),
                "years": sorted(years),
                "relation_counts": dict(sorted(relations.items())),
                "amount_count_distribution": dict(
                    sorted(amount_counts.items(), key=lambda item: int(item[0]))
                ),
                "syntax_patterns": [
                    {"pattern": pattern, "candidate_count": count}
                    for pattern, count in syntax_patterns.most_common(10)
                ],
            }
        )
        materialized_clusters.append(cluster)

    ordered = sorted(
        materialized_clusters,
        key=lambda item: (-int(item["candidate_count"]), str(item["category"]), str(item["matched_alias"])),
    )
    promotion_candidates = [
        {**cluster, "promotion_gate": "requires_human_rule_and_golden_test"}
        for cluster in ordered
        if cluster["category"] == "stable_relation_template"
        and int(cluster["candidate_count"]) >= promotion_minimum
    ]
    return {
        "candidate_count": len(rows),
        "category_counts": dict(sorted(category_counts.items())),
        "clusters": ordered,
        "promotion_candidates": promotion_candidates,
        "precision_guard": {
            "promotion_candidate_count": len(promotion_candidates),
            "automatic_promotion_count": 0,
            "promotion_rate_pct": 0.0,
            "false_semantic_emit_count": false_emit_count,
            "policy": "No narrative cluster enters canonical facts without a reviewed rule and independent golden test.",
        },
    }
