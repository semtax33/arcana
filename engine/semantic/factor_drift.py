from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping


class DriftSeverity(str, Enum):
    UNCHANGED = "unchanged"
    MINOR = "minor"
    MATERIAL = "material"
    SIGN_FLIP = "sign_flip"
    RANKING_FLIP = "ranking_flip"
    COVERAGE_GAIN = "coverage_gain"
    COVERAGE_LOSS = "coverage_loss"


@dataclass(frozen=True)
class FactorDrift:
    severity: DriftSeverity
    old_value: Decimal | None
    new_value: Decimal | None
    absolute_change: Decimal | None
    relative_change: Decimal | None
    old_percentile: float | None = None
    new_percentile: float | None = None


def classify_factor_drift(
    old_value: Decimal | int | float | str | None,
    new_value: Decimal | int | float | str | None,
    *,
    old_percentile: float | None = None,
    new_percentile: float | None = None,
    material_threshold: Decimal = Decimal("0.05"),
    ranking_threshold: float = 0.20,
) -> FactorDrift:
    """Classify a value change without hiding sign or cross-sectional rank risk."""

    old = Decimal(str(old_value)) if old_value is not None else None
    new = Decimal(str(new_value)) if new_value is not None else None
    if old is None and new is None:
        return FactorDrift(DriftSeverity.UNCHANGED, old, new, None, None, old_percentile, new_percentile)
    if old is None:
        return FactorDrift(DriftSeverity.COVERAGE_GAIN, old, new, None, None, old_percentile, new_percentile)
    if new is None:
        return FactorDrift(DriftSeverity.COVERAGE_LOSS, old, new, None, None, old_percentile, new_percentile)

    absolute_change = abs(new - old)
    denominator = max(abs(old), Decimal("1e-30"))
    relative_change = absolute_change / denominator
    if old != 0 and new != 0 and (old < 0) != (new < 0):
        severity = DriftSeverity.SIGN_FLIP
    elif (
        old_percentile is not None
        and new_percentile is not None
        and abs(new_percentile - old_percentile) >= ranking_threshold
    ):
        severity = DriftSeverity.RANKING_FLIP
    elif absolute_change == 0:
        severity = DriftSeverity.UNCHANGED
    elif relative_change >= material_threshold:
        severity = DriftSeverity.MATERIAL
    else:
        severity = DriftSeverity.MINOR
    return FactorDrift(
        severity,
        old,
        new,
        absolute_change,
        relative_change,
        old_percentile,
        new_percentile,
    )


def percentile_ranks(
    values: Mapping[str, Decimal | int | float | str | None],
) -> dict[str, float | None]:
    """Return deterministic average-tie percentiles in [0, 1]."""

    present = sorted(
        (Decimal(str(value)), str(key))
        for key, value in values.items()
        if value is not None
    )
    if not present:
        return {str(key): None for key in values}
    by_value: dict[Decimal, list[str]] = {}
    for value, key in present:
        by_value.setdefault(value, []).append(key)
    result: dict[str, float | None] = {str(key): None for key in values}
    cursor = 1
    denominator = max(len(present) - 1, 1)
    for value in sorted(by_value):
        keys = sorted(by_value[value])
        average_rank = (cursor + cursor + len(keys) - 1) / 2
        percentile = 1.0 if len(present) == 1 else (average_rank - 1) / denominator
        for key in keys:
            result[key] = percentile
        cursor += len(keys)
    return result
