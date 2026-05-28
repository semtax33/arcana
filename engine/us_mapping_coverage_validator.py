from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from engine.core.paths import DATA_LAKE, first_existing_path


DEFAULT_INPUT_DIR = DATA_LAKE.silver("sec", "normalized")
DEFAULT_OUT_DIR = DATA_LAKE.silver("sec", "mapping_coverage")
DEFAULT_RULE_PATH = first_existing_path(
    DATA_LAKE.rules("us_mapping.yaml"),
    DATA_LAKE.rules("mapping_us.yaml"),
)

DEFAULT_REQUIRED_IDS = [
    "REVENUE",
    "COGS",
    "GROSS_PROFIT",
    "SGNA",
    "RND",
    "OPERATING_INCOME",
    "TAX_EXPENSE",
    "PBT",
    "NET_INCOME",
    "TOTAL_ASSETS",
    "TOTAL_LIABILITIES",
    "TOTAL_EQUITY",
    "CASH_AND_EQUIVALENTS",
    "SHORT_TERM_DEBT",
    "LONG_TERM_DEBT",
    "CFO",
    "CAPEX_PPE",
    "INT_PAID",
    "TAX_PAID",
    "DIV_PAID",
    "BUYBACK",
]

RULE_GROUPS = [
    "companyfacts_rules",
    "notes_rules",
    "edgartools_fallback_rules",
]


@dataclass(frozen=True)
class RuleExpectation:
    rule_key: str
    rule_group: str
    source: str
    canonical_id: str
    statement_type: str
    match_kind: str
    matcher: str


@dataclass(frozen=True)
class ValidationReport:
    verdict: str
    score: int
    input_dir: str
    rule_path: str
    output_dir: str
    symbol_count: int
    symbol_year_count: int
    normalized_file_count: int
    debug_file_count: int
    normalized_row_count: int
    debug_row_count: int
    expected_rule_count: int
    observed_rule_count: int
    expected_rule_hit_pct: float
    required_ids: list[str]
    min_required_coverage_pct: float
    min_rule_hit_pct: float
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    canonical_coverage: list[dict[str, Any]]
    factor_readiness: list[dict[str, Any]]
    source_contribution: list[dict[str, Any]]
    rule_coverage: list[dict[str, Any]]
    missing_sample: list[dict[str, Any]]


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_tag_name(value: Any) -> str:
    text = safe_str(value).strip()
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def split_csv_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _emit_progress(message: str, *, progress_interval: int) -> None:
    if progress_interval > 0:
        print(f"[US_MAPPING_COVERAGE] {message}", file=sys.stderr, flush=True)


def _should_emit_progress(index: int, total: int, progress_interval: int) -> bool:
    if progress_interval <= 0:
        return False
    return index == 1 or index == total or index % progress_interval == 0


def _symbol_from_path(path: str | Path) -> str:
    name = Path(path).name
    if name.endswith(".debug.csv"):
        base = name[: -len(".debug.csv")]
    else:
        base = Path(name).stem
    base = base.removeprefix("us_normalized_")
    snapshot_match = re.match(r"(?P<symbol>.+?)_\d{4}[._]\d{2}$", base)
    if snapshot_match:
        return snapshot_match.group("symbol")
    return base


def _normalized_files(input_dir: str | Path, symbols: set[str] | None = None) -> list[Path]:
    files = [
        path
        for path in Path(input_dir).glob("us_normalized_*.csv")
        if not path.name.endswith(".debug.csv")
    ]
    if symbols:
        files = [path for path in files if _symbol_from_path(path).upper() in symbols]
    return sorted(files)


def _debug_files(input_dir: str | Path, symbols: set[str] | None = None) -> list[Path]:
    files = list(Path(input_dir).glob("us_normalized_*.debug.csv"))
    if symbols:
        files = [path for path in files if _symbol_from_path(path).upper() in symbols]
    return sorted(files)


def _read_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                yield from csv.DictReader(f)
            return
        except UnicodeDecodeError:
            continue


def _annual_key(
    symbol: str,
    row: dict[str, str],
    *,
    annual_month: int,
    start_year: int | None,
    end_year: int | None,
) -> str | None:
    fiscal_year_text = safe_str(row.get("fiscal_year")).strip()
    fiscal_month_text = safe_str(row.get("fiscal_month")).strip()
    if not fiscal_year_text or not fiscal_month_text:
        period = safe_str(row.get("period")).replace("_", ".")
        period_match = re.match(r"(?P<year>\d{4})\.(?P<month>\d{2})$", period)
        if period_match:
            fiscal_year_text = period_match.group("year")
            fiscal_month_text = period_match.group("month")
    try:
        fiscal_year = int(float(fiscal_year_text))
        fiscal_month = int(float(fiscal_month_text))
    except ValueError:
        return None
    if fiscal_month != annual_month:
        return None
    if start_year is not None and fiscal_year < start_year:
        return None
    if end_year is not None and fiscal_year > end_year:
        return None
    return f"{symbol}|{fiscal_year}"


def load_mapping_rules(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for key in RULE_GROUPS:
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"{key} must be a list: {path}")
    return data


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [safe_str(item).strip() for item in value if safe_str(item).strip()]
    text = safe_str(value).strip()
    return [text] if text else []


def build_rule_expectations(rules: dict[str, Any]) -> list[RuleExpectation]:
    expectations: list[RuleExpectation] = []

    for rule in rules.get("companyfacts_rules", []) or []:
        canonical_id = safe_str(rule.get("canonical_id")).strip()
        if not canonical_id:
            continue
        statement_type = safe_str(rule.get("fs_type")).strip() or "UNKNOWN"
        for source, field_name, match_kind in [
            ("companyfacts_primary", "primary_tags", "primary_tag"),
            ("companyfacts_alternate", "alternate_tags", "alternate_tag"),
        ]:
            for tag in _as_list(rule.get(field_name)):
                expectations.append(
                    RuleExpectation(
                        rule_key=f"{source}:{canonical_id}:{tag}",
                        rule_group="companyfacts_rules",
                        source=source,
                        canonical_id=canonical_id,
                        statement_type=statement_type,
                        match_kind=match_kind,
                        matcher=tag,
                    )
                )

    for rule in rules.get("notes_rules", []) or []:
        canonical_id = safe_str(rule.get("canonical_id")).strip()
        rule_id = safe_str(rule.get("id")).strip()
        if not canonical_id or not rule_id:
            continue
        statement_type = safe_str(rule.get("fs_type")).strip() or "UNKNOWN"
        for tag in _as_list(rule.get("tags")):
            normalized_tag = normalize_tag_name(tag)
            expectations.append(
                RuleExpectation(
                    rule_key=f"notes:{rule_id}:{canonical_id}:{normalized_tag}",
                    rule_group="notes_rules",
                    source="notes",
                    canonical_id=canonical_id,
                    statement_type=statement_type,
                    match_kind="tag",
                    matcher=normalized_tag,
                )
            )

    for rule in rules.get("edgartools_fallback_rules", []) or []:
        canonical_id = safe_str(rule.get("canonical_id")).strip()
        if not canonical_id:
            continue
        statement_type = safe_str(rule.get("fs_type")).strip() or "UNKNOWN"
        for tag in _as_list(rule.get("tags")):
            normalized_tag = normalize_tag_name(tag)
            expectations.append(
                RuleExpectation(
                    rule_key=f"edgartools:{canonical_id}:{normalized_tag}",
                    rule_group="edgartools_fallback_rules",
                    source="edgartools",
                    canonical_id=canonical_id,
                    statement_type=statement_type,
                    match_kind="tag",
                    matcher=normalized_tag,
                )
            )

    return list({item.rule_key: item for item in expectations}.values())


def _parse_observed_rule_id(rule_id: str, row: dict[str, str]) -> dict[str, str]:
    parts = rule_id.split(":")
    source = parts[0] if parts else safe_str(row.get("source")).strip()
    canonical_id = safe_str(row.get("canonical_account_id")).strip()
    matcher = ""
    rule_group = "observed_only"
    match_kind = "observed"

    if source.startswith("companyfacts") and len(parts) >= 3:
        canonical_id = parts[1]
        matcher = ":".join(parts[2:])
        rule_group = "companyfacts_rules"
        match_kind = "tag_or_label"
    elif source == "notes" and len(parts) >= 4:
        canonical_id = parts[2]
        matcher = parts[3]
        rule_group = "notes_rules"
        match_kind = "tag"
    elif source == "edgartools" and len(parts) >= 3:
        canonical_id = parts[1]
        matcher = parts[2]
        rule_group = "edgartools_fallback_rules"
        match_kind = "tag"

    return {
        "rule_group": rule_group,
        "source": source,
        "canonical_id": canonical_id,
        "statement_type": safe_str(row.get("statement_type")).strip() or "UNKNOWN",
        "match_kind": match_kind,
        "matcher": matcher,
    }


def _annual_key_year(key: str) -> int | None:
    if "|" not in key:
        return None
    try:
        return int(key.rsplit("|", 1)[1])
    except ValueError:
        return None


def _annual_key_symbol(key: str) -> str:
    return key.split("|", 1)[0]


def _build_factor_readiness(
    annual_keys: set[str],
    covered_by_id: dict[str, set[str]],
) -> list[dict[str, Any]]:
    def has(key: str, canonical_id: str) -> bool:
        return key in covered_by_id.get(canonical_id, set())

    def has_any(key: str, canonical_ids: list[str]) -> bool:
        return any(has(key, canonical_id) for canonical_id in canonical_ids)

    def has_all(key: str, canonical_ids: list[str]) -> bool:
        return all(has(key, canonical_id) for canonical_id in canonical_ids)

    specs: list[dict[str, Any]] = [
        {
            "factor_id": "RND",
            "description": "R&D amount is present.",
            "required": ["RND"],
            "predicate": lambda key: has(key, "RND"),
        },
        {
            "factor_id": "RND_MARGIN",
            "description": "R&D margin / R&D to sales readiness.",
            "required": ["RND", "REVENUE"],
            "predicate": lambda key: has_all(key, ["RND", "REVENUE"]),
        },
        {
            "factor_id": "NOPAT_FALLBACK_READY",
            "description": "NOPAT can use statutory or historical tax-rate fallback.",
            "required": ["OPERATING_INCOME"],
            "predicate": lambda key: has(key, "OPERATING_INCOME"),
        },
        {
            "factor_id": "NOPAT_STRICT",
            "description": "NOPAT can use current reported tax rate.",
            "required": ["OPERATING_INCOME", "PBT", "TAX_EXPENSE"],
            "predicate": lambda key: has_all(key, ["OPERATING_INCOME", "PBT", "TAX_EXPENSE"]),
        },
        {
            "factor_id": "D_AND_A_AVAILABLE",
            "description": "Depreciation and amortization is available from IS or CF components.",
            "required": [],
            "any_of": ["DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION"],
            "predicate": lambda key: has_any(key, ["DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION"]),
        },
        {
            "factor_id": "EBITDA_DIRECT",
            "description": "Reported EBITDA tag is present.",
            "required": ["EBITDA"],
            "predicate": lambda key: has(key, "EBITDA"),
        },
        {
            "factor_id": "EBITDA_CALCULATED",
            "description": "EBITDA can be derived as operating income plus D&A.",
            "required": ["OPERATING_INCOME"],
            "any_of": ["DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION"],
            "predicate": lambda key: has(key, "OPERATING_INCOME")
            and has_any(key, ["DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION"]),
        },
        {
            "factor_id": "EV_INPUTS_STRICT",
            "description": "SEC-side EV inputs have cash and at least one debt component.",
            "required": ["CASH_AND_EQUIVALENTS"],
            "any_of": ["SHORT_TERM_DEBT", "LONG_TERM_DEBT", "LEASE_LIABILITY"],
            "predicate": lambda key: has(key, "CASH_AND_EQUIVALENTS")
            and has_any(key, ["SHORT_TERM_DEBT", "LONG_TERM_DEBT", "LEASE_LIABILITY"]),
        },
        {
            "factor_id": "EV_EBITDA_STRICT",
            "description": "EV/EBITDA SEC-side readiness using calculated EBITDA and strict EV inputs.",
            "required": ["OPERATING_INCOME", "CASH_AND_EQUIVALENTS"],
            "any_of": [
                "DNA_IS",
                "DEPRECIATION_EXPENSE",
                "AMORTIZATION",
                "SHORT_TERM_DEBT",
                "LONG_TERM_DEBT",
                "LEASE_LIABILITY",
            ],
            "predicate": lambda key: has(key, "OPERATING_INCOME")
            and has_any(key, ["DNA_IS", "DEPRECIATION_EXPENSE", "AMORTIZATION"])
            and has(key, "CASH_AND_EQUIVALENTS")
            and has_any(key, ["SHORT_TERM_DEBT", "LONG_TERM_DEBT", "LEASE_LIABILITY"]),
        },
        {
            "factor_id": "ROIC_PROXY",
            "description": "ROIC proxy readiness with NOPAT, equity, and cash.",
            "required": ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "TOTAL_EQUITY", "CASH_AND_EQUIVALENTS"],
            "predicate": lambda key: has_all(
                key,
                ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "TOTAL_EQUITY", "CASH_AND_EQUIVALENTS"],
            ),
        },
        {
            "factor_id": "ROIC_FINANCIAL_STRICT",
            "description": "Financial invested-capital ROIC readiness with at least one debt component.",
            "required": ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "TOTAL_EQUITY", "CASH_AND_EQUIVALENTS"],
            "any_of": ["SHORT_TERM_DEBT", "LONG_TERM_DEBT", "LEASE_LIABILITY"],
            "predicate": lambda key: has_all(
                key,
                ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "TOTAL_EQUITY", "CASH_AND_EQUIVALENTS"],
            )
            and has_any(key, ["SHORT_TERM_DEBT", "LONG_TERM_DEBT", "LEASE_LIABILITY"]),
        },
        {
            "factor_id": "ROIC_OPERATIONAL_STRICT",
            "description": "Operational invested-capital ROIC readiness with all working-capital components.",
            "required": [
                "OPERATING_INCOME",
                "PBT",
                "TAX_EXPENSE",
                "TRADE_RECEIVABLES",
                "INVENTORIES",
                "TRADE_PAYABLES",
                "PPE",
                "INTANGIBLE_ASSETS",
            ],
            "predicate": lambda key: has_all(
                key,
                [
                    "OPERATING_INCOME",
                    "PBT",
                    "TAX_EXPENSE",
                    "TRADE_RECEIVABLES",
                    "INVENTORIES",
                    "TRADE_PAYABLES",
                    "PPE",
                    "INTANGIBLE_ASSETS",
                ],
            ),
        },
        {
            "factor_id": "ROIC_OPERATIONAL_PRACTICAL",
            "description": "Operational ROIC readiness with PPE plus at least one operating-capital component.",
            "required": ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "PPE"],
            "any_of": ["TRADE_RECEIVABLES", "INVENTORIES", "TRADE_PAYABLES", "INTANGIBLE_ASSETS"],
            "predicate": lambda key: has_all(key, ["OPERATING_INCOME", "PBT", "TAX_EXPENSE", "PPE"])
            and has_any(key, ["TRADE_RECEIVABLES", "INVENTORIES", "TRADE_PAYABLES", "INTANGIBLE_ASSETS"]),
        },
    ]

    denominator = len(annual_keys)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        covered_count = sum(1 for key in annual_keys if spec["predicate"](key))
        rows.append(
            {
                "factor_id": spec["factor_id"],
                "basis": "current",
                "description": spec["description"],
                "covered_symbol_years": covered_count,
                "missing_symbol_years": max(denominator - covered_count, 0),
                "total_symbol_years": denominator,
                "coverage_pct": round((covered_count / denominator * 100.0) if denominator else 0.0, 4),
                "required_canonical_ids": ",".join(spec.get("required", [])),
                "any_of_canonical_ids": ",".join(spec.get("any_of", [])),
            }
        )

    lag_keys = {
        key
        for key in annual_keys
        if (year := _annual_key_year(key)) is not None
        and f"{_annual_key_symbol(key)}|{year - 1}" in annual_keys
    }
    spec_by_id = {spec["factor_id"]: spec for spec in specs}
    for factor_id in [
        "ROIC_FINANCIAL_STRICT",
        "ROIC_OPERATIONAL_STRICT",
        "ROIC_OPERATIONAL_PRACTICAL",
    ]:
        spec = spec_by_id[factor_id]
        covered_count = 0
        for key in lag_keys:
            year = _annual_key_year(key)
            if year is None:
                continue
            prior_key = f"{_annual_key_symbol(key)}|{year - 1}"
            if spec["predicate"](key) and spec["predicate"](prior_key):
                covered_count += 1
        denominator = len(lag_keys)
        rows.append(
            {
                "factor_id": f"{factor_id}_CURRENT_AND_PRIOR",
                "basis": "current_and_prior",
                "description": f"{spec['description']} Current and prior-year inputs are both present.",
                "covered_symbol_years": covered_count,
                "missing_symbol_years": max(denominator - covered_count, 0),
                "total_symbol_years": denominator,
                "coverage_pct": round((covered_count / denominator * 100.0) if denominator else 0.0, 4),
                "required_canonical_ids": ",".join(spec.get("required", [])),
                "any_of_canonical_ids": ",".join(spec.get("any_of", [])),
            }
        )

    return rows


def build_mapping_coverage_report(
    input_dir: str | Path = DEFAULT_INPUT_DIR,
    *,
    rule_path: str | Path = DEFAULT_RULE_PATH,
    output_dir: str | Path = DEFAULT_OUT_DIR,
    required_ids: list[str] | None = None,
    min_required_coverage_pct: float = 0.0,
    min_rule_hit_pct: float = 0.0,
    annual_month: int = 12,
    start_year: int | None = None,
    end_year: int | None = None,
    symbols: list[str] | None = None,
    progress_interval: int = 0,
) -> ValidationReport:
    input_dir = Path(input_dir)
    rule_path = Path(rule_path)
    output_dir = Path(output_dir)
    symbol_filter = {symbol.upper() for symbol in (symbols or [])}
    required = list(dict.fromkeys(required_ids or DEFAULT_REQUIRED_IDS))

    _emit_progress(
        f"START input_dir={input_dir} rules={rule_path} output_dir={output_dir}",
        progress_interval=progress_interval,
    )
    rules = load_mapping_rules(rule_path)
    expectations = build_rule_expectations(rules)
    expected_by_key = {item.rule_key: item for item in expectations}
    expected_canonical_ids = {
        safe_str(rule.get("canonical_id")).strip()
        for group in RULE_GROUPS
        for rule in (rules.get(group, []) or [])
        if safe_str(rule.get("canonical_id")).strip()
    }

    normalized_files = _normalized_files(input_dir, symbol_filter or None)
    debug_files = _debug_files(input_dir, symbol_filter or None)
    _emit_progress(
        "DISCOVERED "
        f"expected_rules={len(expected_by_key)} "
        f"normalized_files={len(normalized_files)} debug_files={len(debug_files)}",
        progress_interval=progress_interval,
    )

    symbols_seen: set[str] = set()
    annual_keys: set[str] = set()
    covered_by_id: dict[str, set[str]] = {}
    row_count_by_id: dict[str, int] = {}
    normalized_row_count = 0

    for index, path in enumerate(normalized_files, start=1):
        symbol = _symbol_from_path(path)
        symbols_seen.add(symbol)
        file_row_count = 0
        for row in _read_csv_rows(path):
            normalized_row_count += 1
            file_row_count += 1
            annual_key = _annual_key(
                safe_str(row.get("symbol")).strip() or symbol,
                row,
                annual_month=annual_month,
                start_year=start_year,
                end_year=end_year,
            )
            if annual_key is None:
                continue
            annual_keys.add(annual_key)
            canonical_id = safe_str(row.get("canonical_account_id")).strip()
            if not canonical_id:
                continue
            covered_by_id.setdefault(canonical_id, set()).add(annual_key)
            row_count_by_id[canonical_id] = row_count_by_id.get(canonical_id, 0) + 1
        if _should_emit_progress(index, len(normalized_files), progress_interval):
            _emit_progress(
                "NORMALIZED "
                f"{index}/{len(normalized_files)} file={path.name} "
                f"file_rows={file_row_count} total_rows={normalized_row_count} "
                f"symbol_years={len(annual_keys)}",
                progress_interval=progress_interval,
            )

    source_keys: dict[tuple[str, str], set[str]] = {}
    source_rows: dict[tuple[str, str], int] = {}
    observed_rule_keys: dict[str, set[str]] = {}
    observed_rule_rows: dict[str, int] = {}
    observed_rule_meta: dict[str, dict[str, str]] = {}
    debug_row_count = 0

    for index, path in enumerate(debug_files, start=1):
        symbol = _symbol_from_path(path)
        symbols_seen.add(symbol)
        file_row_count = 0
        for row in _read_csv_rows(path):
            debug_row_count += 1
            file_row_count += 1
            canonical_id = safe_str(row.get("canonical_account_id")).strip()
            source = safe_str(row.get("source")).strip()
            rule_id = safe_str(row.get("rule_id")).strip()
            annual_key = _annual_key(
                safe_str(row.get("symbol")).strip() or symbol,
                row,
                annual_month=annual_month,
                start_year=start_year,
                end_year=end_year,
            )

            if canonical_id and source:
                source_key = (canonical_id, source)
                source_rows[source_key] = source_rows.get(source_key, 0) + 1
                if annual_key is not None:
                    source_keys.setdefault(source_key, set()).add(annual_key)

            if rule_id:
                observed_rule_rows[rule_id] = observed_rule_rows.get(rule_id, 0) + 1
                observed_rule_meta.setdefault(rule_id, _parse_observed_rule_id(rule_id, row))
                if annual_key is not None:
                    observed_rule_keys.setdefault(rule_id, set()).add(annual_key)
        if _should_emit_progress(index, len(debug_files), progress_interval):
            _emit_progress(
                "DEBUG "
                f"{index}/{len(debug_files)} file={path.name} "
                f"file_rows={file_row_count} total_rows={debug_row_count} "
                f"observed_rules={len(observed_rule_rows)}",
                progress_interval=progress_interval,
            )

    denominator = len(annual_keys)
    _emit_progress(
        "AGGREGATING "
        f"symbols={len(symbols_seen)} symbol_years={denominator} "
        f"normalized_rows={normalized_row_count} debug_rows={debug_row_count}",
        progress_interval=progress_interval,
    )
    all_canonical_ids = sorted(expected_canonical_ids | set(covered_by_id) | set(required))
    canonical_coverage: list[dict[str, Any]] = []
    for canonical_id in all_canonical_ids:
        covered_keys = covered_by_id.get(canonical_id, set())
        covered_count = len(covered_keys)
        coverage_pct = (covered_count / denominator * 100.0) if denominator else 0.0
        canonical_coverage.append(
            {
                "canonical_id": canonical_id,
                "is_required": canonical_id in required,
                "has_mapping_rule": canonical_id in expected_canonical_ids,
                "covered_symbol_years": covered_count,
                "missing_symbol_years": max(denominator - covered_count, 0),
                "total_symbol_years": denominator,
                "coverage_pct": round(coverage_pct, 4),
                "normalized_row_count": row_count_by_id.get(canonical_id, 0),
            }
        )

    source_contribution = [
        {
            "canonical_id": canonical_id,
            "source": source,
            "covered_symbol_years": len(source_keys.get((canonical_id, source), set())),
            "row_count": source_rows.get((canonical_id, source), 0),
        }
        for canonical_id, source in sorted(source_rows)
    ]
    factor_readiness = _build_factor_readiness(annual_keys, covered_by_id)

    rule_rows: list[dict[str, Any]] = []
    all_rule_keys = set(expected_by_key) | set(observed_rule_rows)
    for rule_key in sorted(all_rule_keys):
        expected = expected_by_key.get(rule_key)
        observed_meta = observed_rule_meta.get(rule_key, {})
        covered_count = len(observed_rule_keys.get(rule_key, set()))
        if expected is not None:
            meta = asdict(expected)
            expected_flag = True
        else:
            meta = {
                "rule_key": rule_key,
                "rule_group": observed_meta.get("rule_group", "observed_only"),
                "source": observed_meta.get("source", ""),
                "canonical_id": observed_meta.get("canonical_id", ""),
                "statement_type": observed_meta.get("statement_type", ""),
                "match_kind": observed_meta.get("match_kind", "observed"),
                "matcher": observed_meta.get("matcher", ""),
            }
            expected_flag = False
        rule_rows.append(
            {
                **meta,
                "is_expected": expected_flag,
                "is_observed": rule_key in observed_rule_rows,
                "covered_symbol_years": covered_count,
                "row_count": observed_rule_rows.get(rule_key, 0),
            }
        )

    expected_hit_count = len(set(expected_by_key) & set(observed_rule_rows))
    expected_rule_hit_pct = (
        expected_hit_count / len(expected_by_key) * 100.0 if expected_by_key else 0.0
    )

    coverage_by_id = {row["canonical_id"]: row for row in canonical_coverage}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not normalized_files:
        warnings.append({"code": "NO_NORMALIZED_FILES", "message": f"No files found under {input_dir}"})
    if denominator == 0:
        warnings.append({"code": "NO_SYMBOL_YEARS", "message": "No annual symbol-year rows were found."})

    for canonical_id in required:
        coverage_row = coverage_by_id.get(canonical_id)
        if coverage_row is None or not coverage_row["has_mapping_rule"]:
            errors.append({"code": "MISSING_REQUIRED_RULE", "canonical_id": canonical_id})
            continue
        if float(coverage_row["coverage_pct"]) < min_required_coverage_pct:
            warnings.append(
                {
                    "code": "LOW_REQUIRED_COVERAGE",
                    "canonical_id": canonical_id,
                    "coverage_pct": coverage_row["coverage_pct"],
                    "threshold_pct": min_required_coverage_pct,
                }
            )

    if expected_rule_hit_pct < min_rule_hit_pct:
        warnings.append(
            {
                "code": "LOW_RULE_HIT_RATE",
                "expected_rule_hit_pct": round(expected_rule_hit_pct, 4),
                "threshold_pct": min_rule_hit_pct,
            }
        )

    missing_sample: list[dict[str, Any]] = []
    for canonical_id in required:
        missing_keys = sorted(annual_keys - covered_by_id.get(canonical_id, set()))
        if missing_keys:
            missing_sample.append(
                {
                    "canonical_id": canonical_id,
                    "missing_count": len(missing_keys),
                    "sample": missing_keys[:20],
                }
            )

    verdict = "FAIL" if errors else "WARN" if warnings else "PASS"
    if denominator == 0:
        score = 0
    else:
        required_coverages = [
            float(coverage_by_id[canonical_id]["coverage_pct"])
            for canonical_id in required
            if canonical_id in coverage_by_id
        ]
        required_avg = sum(required_coverages) / len(required_coverages) if required_coverages else 0.0
        score = round((required_avg * 0.8) + (expected_rule_hit_pct * 0.2))
        score = max(0, min(100, int(score)))
        if errors:
            score = min(score, 59)
        elif warnings:
            score = min(score, 89)

    _emit_progress(
        f"DONE verdict={verdict} score={score} expected_rule_hit_pct={expected_rule_hit_pct:.4f}",
        progress_interval=progress_interval,
    )

    return ValidationReport(
        verdict=verdict,
        score=score,
        input_dir=str(input_dir),
        rule_path=str(rule_path),
        output_dir=str(output_dir),
        symbol_count=len(symbols_seen),
        symbol_year_count=denominator,
        normalized_file_count=len(normalized_files),
        debug_file_count=len(debug_files),
        normalized_row_count=normalized_row_count,
        debug_row_count=debug_row_count,
        expected_rule_count=len(expected_by_key),
        observed_rule_count=len(observed_rule_rows),
        expected_rule_hit_pct=round(expected_rule_hit_pct, 4),
        required_ids=required,
        min_required_coverage_pct=min_required_coverage_pct,
        min_rule_hit_pct=min_rule_hit_pct,
        warnings=warnings,
        errors=errors,
        canonical_coverage=canonical_coverage,
        factor_readiness=factor_readiness,
        source_contribution=source_contribution,
        rule_coverage=rule_rows,
        missing_sample=missing_sample,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_report_files(report: ValidationReport, output_dir: str | Path | None = None) -> list[Path]:
    root = Path(output_dir or report.output_dir)
    payload = asdict(report)
    written = [
        root / "mapping_coverage_validation.json",
        root / "canonical_coverage.csv",
        root / "factor_readiness.csv",
        root / "source_contribution.csv",
        root / "rule_coverage.csv",
    ]

    _write_json(written[0], payload)
    _write_csv(
        written[1],
        report.canonical_coverage,
        [
            "canonical_id",
            "is_required",
            "has_mapping_rule",
            "covered_symbol_years",
            "missing_symbol_years",
            "total_symbol_years",
            "coverage_pct",
            "normalized_row_count",
        ],
    )
    _write_csv(
        written[2],
        report.factor_readiness,
        [
            "factor_id",
            "basis",
            "description",
            "covered_symbol_years",
            "missing_symbol_years",
            "total_symbol_years",
            "coverage_pct",
            "required_canonical_ids",
            "any_of_canonical_ids",
        ],
    )
    _write_csv(
        written[3],
        report.source_contribution,
        ["canonical_id", "source", "covered_symbol_years", "row_count"],
    )
    _write_csv(
        written[4],
        report.rule_coverage,
        [
            "rule_key",
            "rule_group",
            "source",
            "canonical_id",
            "statement_type",
            "match_kind",
            "matcher",
            "is_expected",
            "is_observed",
            "covered_symbol_years",
            "row_count",
        ],
    )
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate US mapping rule coverage.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--rules", default=str(DEFAULT_RULE_PATH))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--symbols", default="", help="Comma-separated US symbols, e.g. AAPL,MSFT.")
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--annual-month", type=int, default=12)
    parser.add_argument("--required-ids", default=",".join(DEFAULT_REQUIRED_IDS))
    parser.add_argument("--min-required-coverage-pct", type=float, default=0.0)
    parser.add_argument("--min-rule-hit-pct", type=float, default=0.0)
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=100,
        help="Print progress every N files to stderr. Use 0 to disable.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when the verdict is WARN or FAIL.",
    )
    args = parser.parse_args()

    report = build_mapping_coverage_report(
        input_dir=args.input_dir,
        rule_path=args.rules,
        output_dir=args.out_dir,
        required_ids=split_csv_arg(args.required_ids),
        min_required_coverage_pct=args.min_required_coverage_pct,
        min_rule_hit_pct=args.min_rule_hit_pct,
        annual_month=args.annual_month,
        start_year=args.start_year,
        end_year=args.end_year,
        symbols=split_csv_arg(args.symbols),
        progress_interval=args.progress_interval,
    )
    written = write_report_files(report, args.out_dir)
    summary = {
        "verdict": report.verdict,
        "score": report.score,
        "symbols": report.symbol_count,
        "symbol_years": report.symbol_year_count,
        "expected_rule_hit_pct": report.expected_rule_hit_pct,
        "written": [str(path) for path in written],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if report.verdict == "FAIL" or (args.strict and report.verdict in {"WARN", "FAIL"}):
        sys.exit(1)


if __name__ == "__main__":
    main()
