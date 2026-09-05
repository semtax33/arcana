from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.factor_drift import DriftSeverity, classify_factor_drift, percentile_ranks
from engine.semantic.factor_graph import FactorDependencyGraph


INPUT_DIR = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
SHARES_CSV = PROJECT_ROOT / "data-lake" / "silver" / "krx" / "shares" / "kr_normalized_shares.csv"
FACTOR_SOURCE = PROJECT_ROOT / "scripts" / "calculate_factor_coverage.js"
OUTPUT = PROJECT_ROOT / "deliverables" / "capex_factor_drift_v5.json"
TARGET_OUTPUT = PROJECT_ROOT / "data-lake" / "meta" / "kr_capex_semantic_v5_factor_targets.csv"
CAPEX_IDS = frozenset({"CAPEX_PPE", "CAPEX_INTANG"})
AFFECTED_FACTORS = (
    "capex_growth_2y_pct",
    "capx",
    "fc_to_ndr",
    "fcf",
    "fcf_yoy_pct",
    "fcfe",
    "fcff",
    "fcfpr",
)
REQUIRED_IDS = frozenset(
    {
        "CAPEX_PPE", "CAPEX_INTANG", "CFO", "PPE", "CURRENT_ASSETS",
        "CURRENT_LIABILITIES", "LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK",
        "SHORT_TERM_DEBT", "CASH_AND_EQUIVALENTS", "SHORT_TERM_FINANCIAL_ASSETS",
        "DEBT_ISSUE", "DEBT_REPAY", "EQ_ISSUE", "BUYBACK", "OPERATING_INCOME",
        "TAX_EXPENSE", "PBT", "DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION",
    }
)


def _number(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _pick_largest(current: tuple[Decimal, str] | None, candidate: tuple[Decimal, str]) -> tuple[Decimal, str]:
    return candidate if current is None or abs(candidate[0]) > abs(current[0]) else current


def _first(row: Mapping[str, Decimal | None], *keys: str) -> Decimal | None:
    return next((row.get(key) for key in keys if row.get(key) is not None), None)


def _fill(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal(0)


def _sub(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return left - right if left is not None and right is not None else None


def _div(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return left / right if left is not None and right not in {None, Decimal(0)} else None


def _pct(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    ratio = _div(current, previous)
    return (ratio - 1) * 100 if ratio is not None else None


def _annual_factors(
    annual: Mapping[int, Mapping[str, Decimal]],
    market_caps: Mapping[int, Decimal] | None = None,
) -> dict[int, dict[str, Decimal | None]]:
    output: dict[int, dict[str, Decimal | None]] = {}
    valid_tax_rates: list[Decimal] = []
    for index, year in enumerate(sorted(annual)):
        source = annual[year]
        years = sorted(annual)
        previous = output.get(years[index - 1], {}) if index else {}
        previous2 = output.get(years[index - 2], {}) if index > 1 else {}
        capex_ppe = abs(source["CAPEX_PPE"]) if source.get("CAPEX_PPE") is not None else None
        capex_intang = abs(source["CAPEX_INTANG"]) if source.get("CAPEX_INTANG") is not None else None
        capx = (
            _fill(capex_ppe) + _fill(capex_intang)
            if capex_ppe is not None or capex_intang is not None
            else None
        )
        cfo = source.get("CFO")
        fcf = _sub(cfo, capx)
        cash_assets_available = (
            source.get("CASH_AND_EQUIVALENTS") is not None
            or source.get("SHORT_TERM_FINANCIAL_ASSETS") is not None
        )
        cash_assets = (
            _fill(source.get("CASH_AND_EQUIVALENTS"))
            + _fill(source.get("SHORT_TERM_FINANCIAL_ASSETS"))
            if cash_assets_available else Decimal(0)
        )
        debt = _fill(_first(source, "LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK")) + _fill(source.get("SHORT_TERM_DEBT"))
        net_debt = debt - cash_assets
        working_capital = _sub(source.get("CURRENT_ASSETS"), source.get("CURRENT_LIABILITIES"))
        previous_wc = previous.get("_working_capital")
        wc_change = working_capital - previous_wc if working_capital is not None and previous_wc is not None else Decimal(0)
        tax_rate = _div(source.get("TAX_EXPENSE"), source.get("PBT"))
        if tax_rate is not None and not (Decimal(0) <= tax_rate <= Decimal(1)):
            tax_rate = None
        if tax_rate is not None:
            nopat_tax_rate = tax_rate
        elif valid_tax_rates:
            nopat_tax_rate = Decimal(str(statistics.median(valid_tax_rates)))
        else:
            nopat_tax_rate = Decimal(0) if (source.get("OPERATING_INCOME") or 0) < 0 else Decimal("0.24")
        if tax_rate is not None:
            valid_tax_rates.append(tax_rate)
        operating_income = source.get("OPERATING_INCOME")
        nopat = operating_income * (1 - nopat_tax_rate) if operating_income is not None else None
        depreciation = _first(source, "DNA_IS")
        if depreciation is None and (
            source.get("DEPRECIATION_EXPENSE") is not None
            or source.get("AMORTIZATION") is not None
        ):
            depreciation = _fill(source.get("DEPRECIATION_EXPENSE")) + _fill(source.get("AMORTIZATION"))
        fcff = nopat + _fill(depreciation) - _fill(capx) - wc_change if nopat is not None else None
        fcfe = (
            fcf
            + _fill(source.get("DEBT_ISSUE")) - _fill(source.get("DEBT_REPAY"))
            + _fill(source.get("EQ_ISSUE")) - _fill(source.get("BUYBACK"))
            if fcf is not None else None
        )
        previous_ppe = previous.get("_ppe")
        annual_ppe_change = _sub(source.get("PPE"), previous_ppe)
        oap_capex = capex_ppe if capex_ppe is not None else annual_ppe_change
        previous2_oap = previous2.get("_oap_capex")
        capex_growth = (
            _pct(oap_capex, previous2_oap)
            if oap_capex is not None and oap_capex >= 0 and previous2_oap is not None and previous2_oap > 0
            else None
        )
        market_cap = (market_caps or {}).get(year)
        output[year] = {
            "capx": capx,
            "fcf": fcf,
            "fcfe": fcfe,
            "fcff": fcff,
            "fc_to_ndr": _div(fcf, net_debt),
            "fcf_yoy_pct": _pct(fcf, previous.get("fcf")),
            "capex_growth_2y_pct": capex_growth,
            "fcfpr": _div(fcfe, market_cap),
            "_working_capital": working_capital,
            "_ppe": source.get("PPE"),
            "_oap_capex": oap_capex,
        }
    return output


def _load_market_caps(path: Path | None, *, progress: bool = False) -> dict[str, dict[int, Decimal]]:
    if path is None or not path.exists():
        return {}
    latest: dict[tuple[str, int], tuple[str, Decimal]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_index, row in enumerate(csv.DictReader(stream), start=1):
            security_id = str(row.get("security_id") or "")
            code = security_id.removeprefix("SEC_KR_").zfill(6)
            date = str(row.get("trade_date") or "")
            year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 0
            market_cap = _number(row.get("market_cap"))
            if not year or market_cap is None:
                continue
            key = (code, year)
            if key not in latest or date > latest[key][0]:
                latest[key] = (date, market_cap)
            if progress and row_index % 2_000_000 == 0:
                print(f"[capex-drift] market-cap rows={row_index:,}", flush=True)
    result: dict[str, dict[int, Decimal]] = defaultdict(dict)
    for (code, year), (_, market_cap) in latest.items():
        result[code][year] = market_cap
    return dict(result)


def build_capex_factor_drift_report(
    input_dir: str | Path,
    *,
    shares_csv: str | Path | None = None,
    factor_source: str | Path = FACTOR_SOURCE,
    progress: bool = False,
) -> dict[str, Any]:
    root = Path(input_dir)
    market_caps = _load_market_caps(Path(shares_csv) if shares_csv else None, progress=progress)
    old_by_stock: dict[str, dict[int, dict[str, Decimal]]] = {}
    new_by_stock: dict[str, dict[int, dict[str, Decimal]]] = {}
    corrected_facts: list[dict[str, Any]] = []
    input_rows = 0
    annual_rows = 0
    input_paths = sorted(root.glob("kr_normalized_*.csv"))
    for file_index, path in enumerate(input_paths, start=1):
        if ".debug." in path.name or ".validation." in path.name:
            continue
        match = re.fullmatch(r"kr_normalized_(\d{6})\.csv", path.name)
        if not match:
            continue
        stock_code = match.group(1)
        old_selected: dict[tuple[int, str], tuple[Decimal, str]] = {}
        new_selected: dict[tuple[int, str], tuple[Decimal, str]] = {}
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                input_rows += 1
                if str(row.get("fiscal_month") or "") != "12":
                    continue
                canonical_id = str(row.get("canonical_account_id") or "")
                if canonical_id not in REQUIRED_IDS:
                    continue
                year_text = str(row.get("fiscal_year") or "")
                value = _number(row.get("normalized_amount"))
                if not year_text.isdigit() or value is None:
                    continue
                annual_rows += 1
                key = (int(year_text), canonical_id)
                direction = str(row.get("cash_direction") or "").lower()
                candidate = (value, direction)
                old_selected[key] = _pick_largest(old_selected.get(key), candidate)
                if not (canonical_id in CAPEX_IDS and direction == "inflow"):
                    new_selected[key] = _pick_largest(new_selected.get(key), candidate)
        years = sorted({year for year, _ in old_selected} | {year for year, _ in new_selected})
        old_annual = {year: {} for year in years}
        new_annual = {year: {} for year in years}
        for (year, canonical_id), (value, _) in old_selected.items():
            old_annual[year][canonical_id] = value
        for (year, canonical_id), (value, _) in new_selected.items():
            new_annual[year][canonical_id] = value
        for (year, canonical_id), (old_value, old_direction) in old_selected.items():
            if canonical_id not in CAPEX_IDS or old_direction != "inflow":
                continue
            new_value = new_selected.get((year, canonical_id), (None, ""))[0]
            corrected_facts.append(
                {
                    "stock_code": stock_code,
                    "fiscal_year": year,
                    "canonical_id": canonical_id,
                    "old_value": str(old_value),
                    "new_value": str(new_value) if new_value is not None else None,
                }
            )
        old_by_stock[stock_code] = old_annual
        new_by_stock[stock_code] = new_annual
        if progress and file_index % 250 == 0:
            print(
                f"[capex-drift] files={file_index:,}/{len(input_paths):,} rows={input_rows:,} corrected_annual={len(corrected_facts):,}",
                flush=True,
            )

    old_factors: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    new_factors: dict[tuple[str, int], dict[str, Decimal | None]] = {}
    for stock_code in sorted(old_by_stock):
        old_rows = _annual_factors(old_by_stock[stock_code], market_caps.get(stock_code))
        new_rows = _annual_factors(new_by_stock[stock_code], market_caps.get(stock_code))
        for year in sorted(set(old_rows) | set(new_rows)):
            old_factors[(stock_code, year)] = old_rows.get(year, {})
            new_factors[(stock_code, year)] = new_rows.get(year, {})

    summaries = []
    for factor in AFFECTED_FACTORS:
        old_ranks: dict[tuple[str, int], float | None] = {}
        new_ranks: dict[tuple[str, int], float | None] = {}
        years = sorted({year for _, year in set(old_factors) | set(new_factors)})
        for year in years:
            keys = sorted(key for key in set(old_factors) | set(new_factors) if key[1] == year)
            old_values = {code: old_factors.get((code, year), {}).get(factor) for code, _ in keys}
            new_values = {code: new_factors.get((code, year), {}).get(factor) for code, _ in keys}
            for code, rank in percentile_ranks(old_values).items():
                old_ranks[(code, year)] = rank
            for code, rank in percentile_ranks(new_values).items():
                new_ranks[(code, year)] = rank
        severity_counts: Counter[str] = Counter()
        examples = []
        available = 0
        for key in sorted(set(old_factors) | set(new_factors)):
            old_value = old_factors.get(key, {}).get(factor)
            new_value = new_factors.get(key, {}).get(factor)
            if old_value is None and new_value is None:
                continue
            available += 1
            drift = classify_factor_drift(
                old_value,
                new_value,
                old_percentile=old_ranks.get(key),
                new_percentile=new_ranks.get(key),
            )
            severity_counts[drift.severity.value] += 1
            if drift.severity is not DriftSeverity.UNCHANGED and len(examples) < 20:
                examples.append(
                    {
                        "stock_code": key[0],
                        "fiscal_year": key[1],
                        "old_value": str(old_value) if old_value is not None else None,
                        "new_value": str(new_value) if new_value is not None else None,
                        "old_percentile": drift.old_percentile,
                        "new_percentile": drift.new_percentile,
                        "severity": drift.severity.value,
                    }
                )
        summaries.append(
            {
                "factor": factor,
                "available_cell_count": available,
                "changed_cell_count": available - severity_counts[DriftSeverity.UNCHANGED.value],
                "severity_counts": dict(sorted(severity_counts.items())),
                "examples": examples,
            }
        )

    graph = FactorDependencyGraph.from_javascript(factor_source)
    affected = graph.affected_factors("CAPEX_PPE")
    financially_dependent = [factor for factor in graph.factors if graph.canonical_dependencies(factor)]
    impacted_stocks = {row["stock_code"] for row in corrected_facts}
    impacted_periods = {(row["stock_code"], row["fiscal_year"]) for row in corrected_facts}
    return {
        "semantic_engine_version": 5,
        "input_dir": str(root.resolve()),
        "input_file_count": len(old_by_stock),
        "input_row_count": input_rows,
        "annual_dependency_row_count": annual_rows,
        "corrected_annual_fact_count": len(corrected_facts),
        "affected_stock_count": len(impacted_stocks),
        "affected_stock_year_count": len(impacted_periods),
        "affected_stock_codes": sorted(impacted_stocks),
        "affected_stock_years": [
            {"stock_code": stock_code, "fiscal_year": fiscal_year}
            for stock_code, fiscal_year in sorted(impacted_periods)
        ],
        "corrected_annual_fact_examples": corrected_facts[:100],
        "structural_impact": {
            "published_factor_count": len(graph.factors),
            "financial_dependency_factor_count": len(financially_dependent),
            "affected_factor_count": len(affected),
            "affected_factors": list(affected),
            "financial_factors_proven_unaffected_count": len(financially_dependent) - len(affected),
        },
        "factor_summaries": summaries,
        "ranking_policy": "Average-tie percentile within fiscal year; ranking_flip means an absolute percentile move of at least 0.20.",
        "materiality_policy": "sign flip first, then >=0.20 percentile move, then >=5% relative value move.",
        "market_cap_policy": (
            "Last available market capitalization in each calendar year."
            if shares_csv else "Not supplied; fcfpr value/ranking drift is NOT_TESTABLE in this run."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit CAPEX semantic corrections through factor values and ranks.")
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--shares-csv", type=Path, default=SHARES_CSV)
    parser.add_argument("--factor-source", type=Path, default=FACTOR_SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--targets-output", type=Path, default=TARGET_OUTPUT)
    args = parser.parse_args()
    report = build_capex_factor_drift_report(
        args.input_dir,
        shares_csv=args.shares_csv,
        factor_source=args.factor_source,
        progress=True,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.targets_output.parent.mkdir(parents=True, exist_ok=True)
    with args.targets_output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["symbol", "security_id"])
        writer.writeheader()
        writer.writerows(
            {
                "symbol": stock_code,
                "security_id": f"SEC_KR_{stock_code}",
            }
            for stock_code in report["affected_stock_codes"]
        )
    print(json.dumps({
        "output": str(args.output),
        "corrected_annual_fact_count": report["corrected_annual_fact_count"],
        "affected_stock_count": report["affected_stock_count"],
        "affected_factor_count": report["structural_impact"]["affected_factor_count"],
        "targets_output": str(args.targets_output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
