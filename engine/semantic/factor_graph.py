from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
import re
from typing import Iterable, Mapping


CORE_ECONOMIC_CONCEPTS = frozenset(
    {
        "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY", "CURRENT_ASSETS",
        "CURRENT_LIABILITIES", "CASH_AND_EQUIVALENTS", "INVENTORIES", "PPE",
        "INTANGIBLE_ASSETS", "TRADE_RECEIVABLES", "TRADE_PAYABLES",
        "SHORT_TERM_DEBT", "LONG_TERM_DEBT", "REVENUE", "COGS", "GROSS_PROFIT",
        "OPERATING_INCOME", "NET_INCOME", "NET_INCOME_PARENT", "PBT", "TAX_EXPENSE",
        "CFO", "CFI", "CFF", "CAPEX_PPE", "CAPEX_INTANG", "DIV_PAID",
        "DEBT_ISSUE", "DEBT_REPAY", "BASIC_EPS", "DILUTED_EPS",
    }
)


@dataclass(frozen=True)
class FactorImpact:
    canonical_id: str
    affected_factor_count: int
    affected_factors: tuple[str, ...]
    affected_companies: int = 0
    affected_periods: int = 0
    materiality: float = 1.0

    @property
    def impact_score(self) -> float:
        return self.affected_companies * self.affected_periods * self.affected_factor_count * self.materiality


@dataclass(frozen=True)
class FactorDependencyGraph:
    direct_canonical: Mapping[str, frozenset[str]]
    direct_factors: Mapping[str, frozenset[str]]
    published_factors: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_javascript(cls, path: str | Path) -> "FactorDependencyGraph":
        text = Path(path).read_text(encoding="utf-8")
        direct_canonical: dict[str, frozenset[str]] = {}
        direct_factors: dict[str, frozenset[str]] = {}
        function_dependencies: dict[str, tuple[set[str], set[str]]] = {}
        for name, body in re.findall(r"function\s+(\w+)\s*\([^)]*\)\s*\{(.*?)\n\}", text, flags=re.S):
            function_dependencies[name] = (
                set(re.findall(r"\bsource\.([A-Z][A-Z0-9_]*)", body)),
                set(re.findall(r"\b(?:r|prev|prev2)\.([a-z][A-Za-z0-9_]*)", body)),
            )

        variable_dependencies: dict[str, tuple[set[str], set[str], set[str]]] = {}
        for variable, expression in re.findall(
            r"\b(?:const|let)\s+([a-zA-Z_$][\w$]*)\s*=\s*(.*?);", text, flags=re.S
        ):
            variable_dependencies[variable] = (
                set(re.findall(r"\bsource\.([A-Z][A-Z0-9_]*)", expression)),
                set(re.findall(r"\b(?:r|prev|prev2)\.([a-z][A-Za-z0-9_]*)", expression)),
                set(re.findall(r"\b([a-zA-Z_$][\w$]*)\b", expression)),
            )

        def expand_variables(expression: str) -> tuple[set[str], set[str]]:
            canonical: set[str] = set()
            factors: set[str] = set()
            pending = list(set(re.findall(r"\b([a-zA-Z_$][\w$]*)\b", expression)) & set(variable_dependencies))
            visited: set[str] = set()
            while pending:
                variable = pending.pop()
                if variable in visited:
                    continue
                visited.add(variable)
                variable_canonical, variable_factors, nested = variable_dependencies[variable]
                canonical.update(variable_canonical)
                factors.update(variable_factors)
                pending.extend((nested & set(variable_dependencies)) - visited)
            return canonical, factors

        assignments = re.findall(r"\br\.([a-z][A-Za-z0-9_]*)\s*=\s*(.*?);", text, flags=re.S)
        for factor, expression in assignments:
            canonical = set(re.findall(r"\bsource\.([A-Z][A-Z0-9_]*)", expression))
            canonical.update(
                value
                for args in re.findall(r"\bfirst(?:PositiveMagnitude)?\s*\(\s*source\s*,(.*?)\)", expression, flags=re.S)
                for value in re.findall(r'["\']([A-Z][A-Z0-9_]*)["\']', args)
            )
            factors = set(re.findall(r"\b(?:r|prev|prev2)\.([a-z][A-Za-z0-9_]*)", expression))
            variable_canonical, variable_factors = expand_variables(expression)
            canonical.update(variable_canonical)
            factors.update(variable_factors)
            factors.discard(factor)
            for function_name, (fn_canonical, fn_factors) in function_dependencies.items():
                if re.search(rf"\b{re.escape(function_name)}\s*\(", expression):
                    canonical.update(fn_canonical)
                    factors.update(fn_factors)
            direct_canonical[factor] = frozenset(set(direct_canonical.get(factor, ())) | canonical)
            direct_factors[factor] = frozenset(set(direct_factors.get(factor, ())) | factors)
        factor_array = re.search(r"\bconst\s+FACTORS\s*=\s*\[(.*?)\];", text, flags=re.S)
        published = frozenset(re.findall(r'["\']([a-z][A-Za-z0-9_]*)["\']', factor_array.group(1))) if factor_array else frozenset()
        return cls(direct_canonical, direct_factors, published)

    @property
    def factors(self) -> frozenset[str]:
        return self.published_factors or (frozenset(self.direct_canonical) | frozenset(self.direct_factors))

    def canonical_dependencies(self, factor: str) -> frozenset[str]:
        found: set[str] = set()
        visited: set[str] = set()

        def visit(current: str) -> None:
            if current in visited:
                return
            visited.add(current)
            found.update(self.direct_canonical.get(current, ()))
            for dependency in self.direct_factors.get(current, ()):
                visit(dependency)

        visit(factor)
        return frozenset(found)

    def affected_factors(self, canonical_id: str) -> tuple[str, ...]:
        return tuple(sorted(factor for factor in self.factors if canonical_id in self.canonical_dependencies(factor)))

    def dependency_coverage(self, available_canonical_ids: Iterable[str]) -> dict[str, object]:
        available = set(available_canonical_ids)
        factor_rows: list[dict[str, object]] = []
        covered = 0
        for factor in sorted(self.factors):
            required = self.canonical_dependencies(factor)
            present = required & available
            is_covered = required.issubset(available)
            if required:
                covered += int(is_covered)
            factor_rows.append(
                {
                    "factor": factor,
                    "dependency_count": len(required),
                    "present_dependency_count": len(present),
                    "dependency_coverage_pct": 100.0 * len(present) / len(required) if required else 100.0,
                    "factor_input_available": is_covered,
                    "missing_dependencies": sorted(required - available),
                }
            )
        financially_dependent = [row for row in factor_rows if row["dependency_count"]]
        return {
            "factor_count": len(factor_rows),
            "financial_dependency_factor_count": len(financially_dependent),
            "covered_factor_count": covered,
            "factor_input_coverage_pct": 100.0 * covered / len(financially_dependent) if financially_dependent else 100.0,
            "factors": factor_rows,
        }

    def impact(
        self,
        canonical_id: str,
        *,
        affected_companies: int = 0,
        affected_periods: int = 0,
        materiality: float = 1.0,
    ) -> FactorImpact:
        factors = self.affected_factors(canonical_id)
        return FactorImpact(canonical_id, len(factors), factors, affected_companies, affected_periods, materiality)


def core_concept_coverage(available_canonical_ids: Iterable[str]) -> dict[str, object]:
    available = set(available_canonical_ids)
    covered = CORE_ECONOMIC_CONCEPTS & available
    return {
        "core_concept_count": len(CORE_ECONOMIC_CONCEPTS),
        "covered_core_concept_count": len(covered),
        "core_economic_concept_coverage_pct": 100.0 * len(covered) / len(CORE_ECONOMIC_CONCEPTS),
        "missing_core_concepts": sorted(CORE_ECONOMIC_CONCEPTS - available),
    }
