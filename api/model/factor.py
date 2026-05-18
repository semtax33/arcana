from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Factor:
    factor_id: str
    factor_name: str
    factor_type: str
    factor_group: str
    unit: str | None
    value_direction: str
    description: str | None
    is_active: bool

