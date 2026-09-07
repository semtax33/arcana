from __future__ import annotations

from decimal import Decimal
import math
from typing import Any

import pandas as pd

from engine.transformers._internal.statement_files import statement_output_columns


BASE_EXPECTED_HEADER = [
    "canonical_account_id",
    "canonical_account_name",
    "original_account_name",
    "statement_type",
    "period",
    "amount",
    "raw_amount",
    "normalized_amount",
    "cash_effect_amount",
    "amount_policy",
    "cash_direction",
]
EXPECTED_HEADER = statement_output_columns(BASE_EXPECTED_HEADER)

DEBUG_COLUMNS = [
    "rule_id",
    "reason",
    "raw_account_name",
    "normalized_name",
    "indent_level",
    "has_children",
    "section_context",
    "parent_context",
    "context_path",
    "context_rule_id",
    "context_reason",
    "amount_raw",
    "unit_factor",
    "parse_alignment_complete",
    "semantic_engine_version",
    "accounting_regime",
    "accounting_regime_confidence",
    "accounting_regime_evidence",
    "document_dialect",
    "source_type",
    "sector_code",
    "industry_group_code",
    "table_kind",
    "scope",
    "currency",
    "comparability",
    "semantic_provenance",
]


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def apply_cash_direction(normalized_amount: int, cash_direction: str) -> int:
    direction = safe_str(cash_direction).strip()
    if direction == "inflow":
        return abs(normalized_amount)
    if direction == "outflow":
        return -abs(normalized_amount)
    return normalized_amount
