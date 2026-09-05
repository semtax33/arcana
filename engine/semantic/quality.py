from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


DEFAULT_COVERAGE_DIMENSIONS = (
    "year",
    "accounting_regime",
    "statement_type",
    "scope",
    "sector_code",
    "document_dialect",
)


@dataclass
class _CoverageCounter:
    row_count: int = 0
    mapped_row_count: int = 0
    absolute_amount: Decimal = Decimal(0)
    mapped_absolute_amount: Decimal = Decimal(0)
    invalid_amount_row_count: int = 0

    def add(self, *, mapped: bool, amount: Decimal | None) -> None:
        self.row_count += 1
        self.mapped_row_count += int(mapped)
        if amount is None:
            self.invalid_amount_row_count += 1
            return
        magnitude = abs(amount)
        self.absolute_amount += magnitude
        if mapped:
            self.mapped_absolute_amount += magnitude

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "mapped_row_count": self.mapped_row_count,
            "row_coverage_pct": 100.0 * self.mapped_row_count / self.row_count if self.row_count else 0.0,
            "absolute_amount": str(self.absolute_amount),
            "mapped_absolute_amount": str(self.mapped_absolute_amount),
            "amount_coverage_pct": (
                float(Decimal(100) * self.mapped_absolute_amount / self.absolute_amount)
                if self.absolute_amount
                else 0.0
            ),
            "invalid_amount_row_count": self.invalid_amount_row_count,
        }


class CoverageStratifier:
    """Accumulate row and amount coverage over stable semantic dimensions."""

    def __init__(self, dimensions: Iterable[str] = DEFAULT_COVERAGE_DIMENSIONS) -> None:
        self.dimensions = tuple(dimensions)
        self._dimensions: dict[str, dict[str, _CoverageCounter]] = {
            dimension: defaultdict(_CoverageCounter) for dimension in self.dimensions
        }
        self._joint: dict[tuple[str, ...], _CoverageCounter] = defaultdict(_CoverageCounter)

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        if value is None or str(value).strip() == "":
            return Decimal(0)
        try:
            parsed = Decimal(str(value).replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None

    def add(self, row: Mapping[str, Any], *, mapped: bool, amount: Any) -> None:
        parsed_amount = self._decimal(amount)
        values = tuple(str(row.get(dimension) or "UNKNOWN") for dimension in self.dimensions)
        for dimension, value in zip(self.dimensions, values):
            self._dimensions[dimension][value].add(mapped=mapped, amount=parsed_amount)
        self._joint[values].add(mapped=mapped, amount=parsed_amount)

    def report(self) -> dict[str, Any]:
        by_dimension: dict[str, list[dict[str, Any]]] = {}
        for dimension in self.dimensions:
            by_dimension[dimension] = [
                {"value": value, **counter.as_dict()}
                for value, counter in sorted(self._dimensions[dimension].items())
            ]
        joint = [
            {
                "dimensions": dict(zip(self.dimensions, values)),
                **counter.as_dict(),
            }
            for values, counter in sorted(self._joint.items())
        ]
        return {
            "dimensions": list(self.dimensions),
            "by_dimension": by_dimension,
            "joint": joint,
        }


class CoverageWaterfall:
    """A heterogeneous-unit funnel whose denominator is explicit at every stage."""

    def __init__(self) -> None:
        self._stages: list[dict[str, Any]] = []

    def add_stage(
        self,
        stage: str,
        *,
        usable: int,
        total: int,
        unit: str,
        loss_reasons: Mapping[str, int] | None = None,
        evidence: str = "measured",
    ) -> None:
        if usable < 0 or total < 0 or usable > total:
            raise ValueError(f"invalid coverage stage {stage}: {usable}/{total}")
        reasons = {str(key): int(value) for key, value in (loss_reasons or {}).items()}
        if any(value < 0 for value in reasons.values()):
            raise ValueError(f"negative loss reason count in {stage}")
        self._stages.append(
            {
                "stage": str(stage),
                "usable_count": int(usable),
                "total_count": int(total),
                "lost_count": int(total - usable),
                "unit": str(unit),
                "coverage_pct": 100.0 * usable / total if total else 0.0,
                "loss_reasons": reasons,
                "evidence": str(evidence),
            }
        )

    def report(self) -> dict[str, Any]:
        bottleneck = min(self._stages, key=lambda item: item["coverage_pct"]) if self._stages else None
        return {
            "stages": [dict(stage) for stage in self._stages],
            "bottleneck": dict(bottleneck) if bottleneck else None,
            "denominator_policy": "Each stage carries its own explicit unit; percentages across unlike units are not chained.",
        }
