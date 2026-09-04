from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import re
from typing import Any

import yaml


VALID_AMOUNT_POLICIES = {"as_reported", "abs", "neg_abs"}
VALID_CASH_DIRECTIONS = {"", "inflow", "outflow"}
VALID_UNIT_FACTORS = {
    Decimal("0.001"),  # explicitly recorded 1,000x unit-scale repair
    Decimal(1),
    Decimal(10),
    Decimal(100),
    Decimal(1_000),
    Decimal(10_000),
    Decimal(100_000),
    Decimal(1_000_000),
    Decimal(10_000_000),
    Decimal(100_000_000),
    Decimal(1_000_000_000),
    Decimal(1_000_000_000_000),
}
EPS_CANONICAL_IDS = {"BASIC_EPS", "DILUTED_EPS"}
EXPECTED_CF_DIRECTIONS = {
    "CAPEX_PPE": "outflow",
    "CAPEX_INTANG": "outflow",
    "PPE_DISPOSAL_PROCEEDS": "inflow",
    "INTANGIBLE_DISPOSAL_PROCEEDS": "inflow",
    "INT_PAID": "outflow",
    "TAX_PAID": "outflow",
    "DIV_PAID": "outflow",
    "DEBT_ISSUE": "inflow",
    "DEBT_REPAY": "outflow",
    "EQ_ISSUE": "inflow",
    "BUYBACK": "outflow",
    "LEASE_REPAYMENT": "outflow",
}
V3_EXPECTED_CF_DIRECTIONS = {
    "CFI_GROSS_INFLOW": "inflow",
    "CFI_GROSS_OUTFLOW": "outflow",
    "CFF_GROSS_INFLOW": "inflow",
    "CFF_GROSS_OUTFLOW": "outflow",
    "PPE_ACQUISITION_COMPONENT": "outflow",
    "PPE_DISPOSAL_COMPONENT_PROCEEDS": "inflow",
}
ALL_EXPECTED_CF_DIRECTIONS = {**EXPECTED_CF_DIRECTIONS, **V3_EXPECTED_CF_DIRECTIONS}
_SCALAR_AMOUNT_RE = re.compile(
    r"^\s*(?:\(\s*)?[△▲+\-－]?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*$"
)


def _decimal_value(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return 0
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def _source_amount(value: Any, unit_factor: Decimal) -> Decimal | None:
    text = str(value or "").replace("\u3000", "").strip()
    if text in {"", "-", "－", "—", "–"}:
        return 0
    if not _SCALAR_AMOUNT_RE.fullmatch(text):
        return None
    sign_probe = re.sub(r"\s", "", text)
    negative = (sign_probe.startswith("(") and sign_probe.endswith(")")) or sign_probe.startswith(
        ("△", "▲", "-", "－")
    )
    digits = re.sub(r"[^0-9.]", "", text.replace(",", ""))
    if not digits:
        return 0
    try:
        with localcontext() as context:
            context.prec = max(50, len(digits) + 30)
            scaled = Decimal(digits) * unit_factor
        # The table parser stores integer won for ordinary display units.  A
        # fractional factor is an explicit post-parse scale repair and must
        # retain its decimal remainder.
        number = Decimal(int(scaled)) if unit_factor >= 1 else scaled
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return -number if negative else number


def _expected_normalized(raw_amount: Decimal, policy: str) -> Decimal:
    if policy == "abs":
        return raw_amount.copy_abs()
    if policy == "neg_abs":
        return raw_amount.copy_abs().copy_negate()
    return raw_amount


def _expected_cash_effect(normalized_amount: Decimal, direction: str) -> Decimal:
    if direction == "inflow":
        return normalized_amount.copy_abs()
    if direction == "outflow":
        return normalized_amount.copy_abs().copy_negate()
    return normalized_amount


def static_sign_policy_audit(path: str | Path) -> dict[str, Any]:
    bundle = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    is_v2 = int(bundle.get("schema_version", 1) or 1) >= 2
    data = bundle.get("sign_policy", {}) or {} if is_v2 else bundle
    defaults = data.get("defaults", {}) or {}
    policies = data.get("canonical_policies", {}) or {}
    errors: list[dict[str, str]] = []
    for fs_type, policy in defaults.items():
        amount_policy = str((policy or {}).get("amount_policy", "as_reported"))
        if amount_policy not in VALID_AMOUNT_POLICIES:
            errors.append(
                {"location": f"defaults.{fs_type}", "error": f"invalid {amount_policy}"}
            )
    for canonical_id, policy in policies.items():
        policy = policy or {}
        amount_policy = str(policy.get("amount_policy", "as_reported"))
        direction = str(policy.get("cash_direction", "") or "")
        if amount_policy not in VALID_AMOUNT_POLICIES:
            errors.append(
                {"location": str(canonical_id), "error": f"invalid {amount_policy}"}
            )
        if direction not in VALID_CASH_DIRECTIONS:
            errors.append(
                {"location": str(canonical_id), "error": f"invalid {direction}"}
            )
    for canonical_id, expected in ALL_EXPECTED_CF_DIRECTIONS.items():
        actual = str((policies.get(canonical_id, {}) or {}).get("cash_direction", ""))
        if actual != expected:
            errors.append(
                {
                    "location": canonical_id,
                    "error": f"expected {expected}, got {actual or '<blank>'}",
                }
            )
    rule_direction_check_count = 0
    if is_v2:
        for rule in ((bundle.get("rule_sets", {}) or {}).get("mapping", []) or []):
            emit = rule.get("emit", {}) or {}
            canonical_id = str(emit.get("canonical_id", ""))
            expected = ALL_EXPECTED_CF_DIRECTIONS.get(canonical_id)
            if expected is None:
                continue
            rule_direction_check_count += 1
            emitted = str(emit.get("cash_direction", "") or "")
            actual = emitted or str(
                (policies.get(canonical_id, {}) or {}).get("cash_direction", "") or ""
            )
            if actual != expected:
                errors.append(
                    {
                        "location": f"rule_sets.mapping.{rule.get('id', '<missing-id>')}",
                        "error": f"expected {expected}, got {actual or '<blank>'}",
                    }
                )
    return {
        "policy_path": str(Path(path).resolve()),
        "canonical_policy_count": len(policies),
        "expected_direction_count": len(EXPECTED_CF_DIRECTIONS),
        "v3_expected_direction_count": len(V3_EXPECTED_CF_DIRECTIONS),
        "rule_direction_check_count": rule_direction_check_count,
        "error_count": len(errors),
        "errors": errors,
    }


def audit_debug_corpus(
    input_dir: str | Path,
    *,
    max_files: int | None = None,
    max_examples: int = 30,
) -> dict[str, Any]:
    paths = sorted(Path(input_dir).glob("kr_normalized_*.debug.csv"))
    if max_files is not None:
        paths = paths[:max_files]
    counts = {
        "row_count": 0,
        "mapped_row_count": 0,
        "scalar_source_amount_row_count": 0,
        "non_scalar_source_amount_row_count": 0,
        "invalid_unit_factor_row_count": 0,
        "eps_scaled_row_count": 0,
        "source_scale_mismatch_row_count": 0,
        "invalid_amount_policy_row_count": 0,
        "invalid_cash_direction_row_count": 0,
        "normalized_amount_mismatch_row_count": 0,
        "amount_alias_mismatch_row_count": 0,
        "cash_effect_mismatch_row_count": 0,
        "canonical_direction_mismatch_row_count": 0,
        "inflow_negative_row_count": 0,
        "outflow_positive_row_count": 0,
    }
    unit_factor_counts: dict[str, int] = {}
    direction_counts: dict[str, int] = {}
    direction_mismatch_counts: dict[str, int] = {}
    examples: list[dict[str, str]] = []
    examples_by_kind: dict[str, list[dict[str, str]]] = {}

    def issue(kind: str, path: Path, row: dict[str, str]) -> None:
        counts[kind] += 1
        example = {
            "kind": kind,
            "file": path.name,
            "period": str(row.get("period", "")),
            "canonical_id": str(row.get("canonical_account_id", "")),
            "label": str(row.get("original_account_name", ""))[:300],
            "raw_amount": str(row.get("raw_amount", ""))[:100],
            "unit_factor": str(row.get("unit_factor", "")),
            "amount_policy": str(row.get("amount_policy", "")),
            "cash_direction": str(row.get("cash_direction", "")),
        }
        if len(examples) < max_examples:
            examples.append(example)
        kind_examples = examples_by_kind.setdefault(kind, [])
        if len(kind_examples) < min(max_examples, 10):
            kind_examples.append(example)

    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                counts["row_count"] += 1
                canonical_id = str(row.get("canonical_account_id", "") or "")
                if canonical_id not in {"", "UNMAPPED"}:
                    counts["mapped_row_count"] += 1
                unit_text = str(row.get("unit_factor", "") or "1").strip()
                unit = _decimal_value(unit_text)
                unit_factor_counts[unit_text] = unit_factor_counts.get(unit_text, 0) + 1
                if unit is None or unit not in VALID_UNIT_FACTORS:
                    issue("invalid_unit_factor_row_count", path, row)
                    unit = Decimal(1)
                if canonical_id in EPS_CANONICAL_IDS and unit != Decimal(1):
                    issue("eps_scaled_row_count", path, row)

                raw_amount = _decimal_value(row.get("raw_amount"))
                source_amount = _source_amount(row.get("amount_raw"), unit)
                if source_amount is None:
                    counts["non_scalar_source_amount_row_count"] += 1
                else:
                    counts["scalar_source_amount_row_count"] += 1
                    if raw_amount is not None and source_amount != raw_amount:
                        issue("source_scale_mismatch_row_count", path, row)

                policy = str(row.get("amount_policy", "") or "as_reported")
                direction = str(row.get("cash_direction", "") or "")
                direction_counts[direction or "none"] = (
                    direction_counts.get(direction or "none", 0) + 1
                )
                if policy not in VALID_AMOUNT_POLICIES:
                    issue("invalid_amount_policy_row_count", path, row)
                    policy = "as_reported"
                if direction not in VALID_CASH_DIRECTIONS:
                    issue("invalid_cash_direction_row_count", path, row)
                    direction = ""

                normalized = _decimal_value(row.get("normalized_amount"))
                amount_alias = _decimal_value(row.get("amount"))
                cash_effect = _decimal_value(row.get("cash_effect_amount"))
                if raw_amount is not None:
                    expected_normalized = _expected_normalized(raw_amount, policy)
                    if normalized is None or normalized != expected_normalized:
                        issue("normalized_amount_mismatch_row_count", path, row)
                    if amount_alias is None or amount_alias != expected_normalized:
                        issue("amount_alias_mismatch_row_count", path, row)
                    expected_cash = _expected_cash_effect(expected_normalized, direction)
                    if cash_effect is None or cash_effect != expected_cash:
                        issue("cash_effect_mismatch_row_count", path, row)
                    if direction == "inflow" and cash_effect is not None and cash_effect < 0:
                        issue("inflow_negative_row_count", path, row)
                    if direction == "outflow" and cash_effect is not None and cash_effect > 0:
                        issue("outflow_positive_row_count", path, row)

                expected_direction = EXPECTED_CF_DIRECTIONS.get(canonical_id)
                if expected_direction is not None and direction != expected_direction:
                    mismatch_key = (
                        f"{canonical_id}:expected={expected_direction}:"
                        f"stored={direction or '<blank>'}"
                    )
                    direction_mismatch_counts[mismatch_key] = (
                        direction_mismatch_counts.get(mismatch_key, 0) + 1
                    )
                    issue("canonical_direction_mismatch_row_count", path, row)

    return {
        "input_dir": str(Path(input_dir).resolve()),
        "file_count": len(paths),
        **counts,
        "unit_factor_row_counts": dict(
            sorted(unit_factor_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "cash_direction_row_counts": dict(sorted(direction_counts.items())),
        "canonical_direction_mismatch_breakdown": dict(
            sorted(
                direction_mismatch_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        "examples": examples,
        "examples_by_kind": examples_by_kind,
        "interpretation": {
            "observed_scope": "existing persisted debug outputs",
            "direction_mismatch_action": "re-normalize with semantic engine v2; do not mutate source facts",
        },
    }
