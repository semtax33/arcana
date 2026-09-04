from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class SemanticCorrection:
    old_fact_id: str
    old_semantics: str
    new_fact_id: str
    new_semantics: str
    reason: str
    source_hash: str
    migration_version: str
    affected_factors: tuple[str, ...] = ()


def stable_fact_id(row: Mapping[str, object]) -> str:
    identity = "|".join(
        str(row.get(key) or "")
        for key in (
            "company_name", "stock_code", "period", "statement_type", "table_index",
            "row_index", "original_account_name", "raw_amount", "source_uri",
            "canonical_id", "canonical_account_id", "cash_direction",
            "rule_id", "section_context", "parent_context", "context_path",
            "debug_file", "source_row_ordinal",
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def source_sha256(path: str | Path) -> str:
    path = Path(path)
    return sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def capex_direction_correction(
    row: Mapping[str, object],
    *,
    source_hash: str,
    migration_version: str = "semantic-v3",
    affected_factors: Iterable[str] = (),
) -> SemanticCorrection | None:
    canonical_id = str(row.get("canonical_id") or row.get("canonical_account_id") or "")
    direction = str(row.get("cash_direction") or row.get("direction") or "").lower()
    if direction != "inflow" or canonical_id not in {"CAPEX_PPE", "CAPEX_INTANG"}:
        return None
    new_id = "PPE_DISPOSAL_PROCEEDS" if canonical_id == "CAPEX_PPE" else "INTANGIBLE_DISPOSAL_PROCEEDS"
    old_id = stable_fact_id(row)
    new_identity = {
        **row,
        "canonical_id": new_id,
        "canonical_account_id": new_id,
        "cash_direction": "inflow",
    }
    return SemanticCorrection(
        old_fact_id=old_id,
        old_semantics=json.dumps({"canonical_id": canonical_id, "cash_direction": direction}, ensure_ascii=False, sort_keys=True),
        new_fact_id=stable_fact_id(new_identity),
        new_semantics=json.dumps({"canonical_id": new_id, "cash_direction": "inflow"}, ensure_ascii=False, sort_keys=True),
        reason="disposal proceeds were previously conflated with capital expenditure",
        source_hash=source_hash,
        migration_version=migration_version,
        affected_factors=tuple(sorted(set(affected_factors))),
    )


def correction_record(correction: SemanticCorrection) -> dict[str, object]:
    record = asdict(correction)
    record["affected_factors"] = "|".join(correction.affected_factors)
    return record
