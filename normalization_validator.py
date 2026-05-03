# validation/generic_normalization_validator.py
from __future__ import annotations

import time
from datetime import datetime
import argparse
import csv
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
import hashlib

import pandas as pd
import yaml



# ============================================================
# Data models
# ============================================================

@dataclass
class CheckResult:
    name: str
    status: str
    lhs: float | None = None
    rhs: float | None = None
    diff: float | None = None
    tolerance: float | None = None
    message: str = ""
    mode: str = ""
    formula: str = ""


@dataclass
class ValidationReport:
    file: str
    debug_file: str
    verdict: str
    score: int
    row_counts: dict[str, int]
    mapped_rows: int
    unmapped_rows: int
    mapped_ratio: float
    checks: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    missing_core_ids: dict[str, list[str]]
    duplicate_risks: list[dict[str, Any]]
    unmapped_candidates: list[dict[str, Any]]
    factor_snapshot: dict[str, Any]


@dataclass
class ZaiConfig:
    api_key: str
    base_url: str = "https://api.z.ai/api/paas/v4/"
    model: str = "glm-5.1"
    temperature: float = 0.1
    max_tokens: int = 4096
    retry: int = 3
    retry_sleep_sec: float = 5.0
    max_context_chars: int = 120_000


# ============================================================
# Basic utilities
# ============================================================

def safe_str(v: Any) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)


def normalize_name(v: Any) -> str:
    s = safe_str(v)
    s = re.sub(r"\(주\s*\d+(?:\s*,\s*\d+)*\)", "", s)
    s = re.sub(r"\(단위\s*[:：]\s*[^)]*\)", "", s)
    s = s.replace("(", "").replace(")", "")
    s = s.replace("\u3000", "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("ㆍ", "").replace("·", "")
    s = s.replace("/", "").replace("-", "")
    return s.strip()


def read_csv_flexible(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    for enc in ["utf-8-sig", "utf-8", "cp949"]:
        try:
            return pd.read_csv(path, dtype=str, encoding=enc).fillna("")
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, dtype=str).fillna("")


def to_number_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace(" ", "", regex=False),
        errors="coerce",
    ).fillna(0.0)


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_csv_pair(
    normalized_csv: str | Path,
    debug_csv: str | Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = read_csv_flexible(normalized_csv)
    d = read_csv_flexible(debug_csv)

    required = [
        "canonical_account_id",
        "canonical_account_name",
        "original_account_name",
        "statement_type",
        "period",
        "amount",
    ]

    for col in required:
        if col not in n.columns:
            raise ValueError(f"normalized csv missing column: {col}")
        if col not in d.columns:
            raise ValueError(f"debug csv missing column: {col}")

    for df in [n, d]:
        df["amount_num"] = to_number_series(df["amount"])
        df["name_norm"] = df["original_account_name"].map(normalize_name)

    for col in [
        "section_context",
        "parent_context",
        "context_path",
        "rule_id",
        "reason",
        "context_rule_id",
        "context_reason",
    ]:
        if col not in d.columns:
            d[col] = ""

    d["context_all"] = (
        d["section_context"].astype(str)
        + " "
        + d["parent_context"].astype(str)
        + " "
        + d["context_path"].astype(str)
    ).map(normalize_name)

    return n, d


# ============================================================
# Rule expression evaluator
# ============================================================

def filter_rows(df: pd.DataFrame, spec: dict[str, Any]) -> pd.DataFrame:
    out = df

    statement_type = spec.get("statement_type")
    if statement_type:
        out = out[out["statement_type"].eq(statement_type)]

    if "cid" in spec:
        out = out[out["canonical_account_id"].eq(spec["cid"])]

    if "canonical_id" in spec:
        out = out[out["canonical_account_id"].eq(spec["canonical_id"])]

    if "canonical_id_not" in spec:
        out = out[~out["canonical_account_id"].eq(spec["canonical_id_not"])]

    if "canonical_id_in" in spec:
        out = out[out["canonical_account_id"].isin(spec["canonical_id_in"])]

    if "name_exact_any" in spec:
        names = {normalize_name(x) for x in spec["name_exact_any"]}
        out = out[out["name_norm"].isin(names)]

    if "name_exclude_any" in spec:
        tokens = [normalize_name(x) for x in spec["name_exclude_any"]]
        tokens = [t for t in tokens if t]
        if tokens:
            pattern = "|".join(re.escape(t) for t in tokens)
            out = out[~out["name_norm"].str.contains(pattern, regex=True, na=False)]

    if "name_contains_any" in spec:
        tokens = [normalize_name(x) for x in spec["name_contains_any"]]
        tokens = [t for t in tokens if t]
        if tokens:
            pattern = "|".join(re.escape(t) for t in tokens)
            out = out[out["name_norm"].str.contains(pattern, regex=True, na=False)]

    if "name_contains_all" in spec:
        for token in spec["name_contains_all"]:
            t = normalize_name(token)
            if t:
                out = out[out["name_norm"].str.contains(re.escape(t), regex=True, na=False)]

    if "context_contains_any" in spec:
        if "context_all" not in out.columns:
            return out.iloc[0:0]
        tokens = [normalize_name(x) for x in spec["context_contains_any"]]
        tokens = [t for t in tokens if t]
        if tokens:
            pattern = "|".join(re.escape(t) for t in tokens)
            out = out[out["context_all"].str.contains(pattern, regex=True, na=False)]

    if "context_contains_all" in spec:
        if "context_all" not in out.columns:
            return out.iloc[0:0]
        for token in spec["context_contains_all"]:
            t = normalize_name(token)
            if t:
                out = out[out["context_all"].str.contains(re.escape(t), regex=True, na=False)]

    return out

def run_formula_choice_check(
    rule: dict[str, Any],
    normalized_df: pd.DataFrame,
    debug_df: pd.DataFrame,
    default_tol: float,
) -> CheckResult:
    source = rule.get("source", "normalized")
    tolerance = float(rule.get("tolerance_abs", default_tol))
    skip_if_missing = bool(rule.get("skip_if_missing", True))

    candidates = rule.get("candidates", []) or []
    evaluated: list[CheckResult] = []

    if not candidates:
        return CheckResult(
            name=safe_str(rule.get("name", "formula_choice_check")),
            status="SKIP" if skip_if_missing else "FAIL",
            tolerance=tolerance,
            message="candidates가 비어 있습니다.",
        )

    for candidate in candidates:
        mode = safe_str(candidate.get("mode"))
        formula = safe_str(candidate.get("formula"))

        # expected/lhs와 actual/rhs 모두 지원
        expected_expr = (
            candidate.get("expected")
            or candidate.get("lhs")
            or rule.get("expected")
            or rule.get("lhs")
        )

        actual_expr = (
            candidate.get("actual")
            or candidate.get("rhs")
            or rule.get("actual")
            or rule.get("rhs")
        )

        if expected_expr is None or actual_expr is None:
            evaluated.append(
                CheckResult(
                    name=safe_str(rule.get("name", "formula_choice_check")),
                    status="SKIP" if skip_if_missing else "FAIL",
                    tolerance=tolerance,
                    message="후보식에 expected/lhs 또는 actual/rhs가 없습니다.",
                    mode=mode,
                    formula=formula,
                )
            )
            continue

        expected = eval_expr(expected_expr, normalized_df, debug_df, source)
        actual = eval_expr(actual_expr, normalized_df, debug_df, source)

        evaluated.append(
            check_equal(
                name=safe_str(rule.get("name", "formula_choice_check")),
                lhs=expected,
                rhs=actual,
                tolerance=tolerance,
                skip_if_missing=skip_if_missing,
                mode=mode,
                formula=formula,
            )
        )

    passed = [r for r in evaluated if r.status == "PASS"]
    if passed:
        return min(passed, key=lambda r: abs(r.diff or 0.0))

    failed = [r for r in evaluated if r.status == "FAIL"]
    if failed:
        best = min(failed, key=lambda r: abs(r.diff or float("inf")))
        return CheckResult(
            name=safe_str(rule.get("name", "formula_choice_check")),
            status="FAIL",
            lhs=best.lhs,
            rhs=best.rhs,
            diff=best.diff,
            tolerance=tolerance,
            message=f"모든 후보식 실패; best={best.mode}; diff={best.diff:,.0f}",
            mode=best.mode,
            formula=best.formula,
        )

    # 전부 SKIP이면, 어떤 후보가 왜 SKIP됐는지 일부라도 보여주는 게 좋음
    messages = [r.message for r in evaluated if r.message]
    return CheckResult(
        name=safe_str(rule.get("name", "formula_choice_check")),
        status="SKIP" if skip_if_missing else "FAIL",
        tolerance=tolerance,
        message="모든 후보식 필요 항목 부족"
        if not messages
        else " / ".join(sorted(set(messages))[:3]),
    )

def aggregate_amount(rows: pd.DataFrame, agg: str) -> float | None:
    if rows.empty:
        return None
    if agg == "first":
        return float(rows["amount_num"].iloc[0])
    if agg == "sum":
        return float(rows["amount_num"].sum())
    if agg == "abs_sum":
        return float(rows["amount_num"].abs().sum())
    raise ValueError(f"unknown agg: {agg}")


def eval_expr(
    expr: dict[str, Any],
    normalized_df: pd.DataFrame,
    debug_df: pd.DataFrame,
    default_source: str = "normalized",
) -> float | None:
    if expr is None:
        return None

    if not isinstance(expr, dict):
        raise TypeError(
            f"formula expression must be dict or None, got {type(expr).__name__}: {expr!r}"
        )

    if "sum" in expr:
        total = 0.0
        for child in expr["sum"]:
            value = eval_expr(child, normalized_df, debug_df, default_source)
            if value is None:
                if child.get("optional", False):
                    value = 0.0
                else:
                    return None
            total += value
        return total

    if "sub" in expr:
        # [A, B, C] -> A - B - C
        items = expr["sub"]
        if len(items) < 2:
            raise ValueError("sub expects at least 2 items")

        first = eval_expr(items[0], normalized_df, debug_df, default_source)
        if first is None:
            return None

        total = first
        for child in items[1:]:
            value = eval_expr(child, normalized_df, debug_df, default_source)
            if value is None:
                if child.get("optional", False):
                    value = 0.0
                else:
                    return None
            total -= value

        return total

    if "neg" in expr:
        value = eval_expr(expr["neg"], normalized_df, debug_df, default_source)
        return None if value is None else -value

    source = expr.get("source", default_source)
    df = debug_df if source == "debug" else normalized_df
    rows = filter_rows(df, expr)
    value = aggregate_amount(rows, expr.get("agg", "first"))

    if value is None and expr.get("optional", False):
        return 0.0

    return value


def check_equal(
    name: str,
    lhs: float | None,
    rhs: float | None,
    tolerance: float,
    skip_if_missing: bool,
    mode: str = "",
    formula: str = "",
) -> CheckResult:
    if lhs is None or rhs is None:
        return CheckResult(
            name=name,
            status="SKIP" if skip_if_missing else "FAIL",
            lhs=lhs,
            rhs=rhs,
            diff=None,
            tolerance=tolerance,
            message="필요 항목 부족",
            mode=mode,
            formula=formula,
        )

    diff = lhs - rhs
    status = "PASS" if abs(diff) <= tolerance else "FAIL"

    return CheckResult(
        name=name,
        status=status,
        lhs=lhs,
        rhs=rhs,
        diff=diff,
        tolerance=tolerance,
        message="" if status == "PASS" else f"차이 {diff:,.0f}",
        mode=mode,
        formula=formula,
    )

def run_single_formula_check(
    rule: dict[str, Any],
    normalized_df: pd.DataFrame,
    debug_df: pd.DataFrame,
    default_tol: float,
) -> CheckResult:
    source = rule.get("source", "normalized")
    tolerance = float(rule.get("tolerance_abs", default_tol))
    skip_if_missing = bool(rule.get("skip_if_missing", True))

    lhs = eval_expr(
        rule["lhs"],
        normalized_df,
        debug_df,
        source,
    )

    rhs = eval_expr(
        rule["rhs"],
        normalized_df,
        debug_df,
        source,
    )

    return check_equal(
        name=rule["name"],
        lhs=lhs,
        rhs=rhs,
        tolerance=tolerance,
        skip_if_missing=skip_if_missing,
    )

def run_formula_checks(
    normalized_df: pd.DataFrame,
    debug_df: pd.DataFrame,
    config: dict[str, Any],
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    default_tol = float(
        config.get("settings", {}).get("default_tolerance_abs", 10_000)
    )

    for rule in config.get("formula_checks", []):
        # 하위 호환:
        # 기존 단일 공식은 run_single_formula_check
        # formula_checks 안에 candidates가 있으면 choice check로 처리
        if "candidates" in rule:
            checks.append(
                run_formula_choice_check(
                    rule,
                    normalized_df,
                    debug_df,
                    default_tol,
                )
            )
        else:
            checks.append(
                run_single_formula_check(
                    rule,
                    normalized_df,
                    debug_df,
                    default_tol,
                )
            )

    for rule in config.get("formula_choice_checks", []):
        checks.append(
            run_formula_choice_check(
                rule,
                normalized_df,
                debug_df,
                default_tol,
            )
        )

    return checks


# ============================================================
# Validation detectors
# ============================================================

def run_warning_rules(
    debug_df: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for rule in config.get("warning_rules", []):
        cond = rule.get("conditions", {})
        matched = filter_rows(debug_df, cond)

        if matched.empty:
            continue

        sample_cols = [
            "statement_type",
            "original_account_name",
            "canonical_account_id",
            "canonical_account_name",
            "amount",
            "section_context",
            "context_path",
            "rule_id",
            "reason",
        ]
        sample_cols = [c for c in sample_cols if c in matched.columns]

        item = {
            "id": rule.get("id", ""),
            "severity": rule.get("severity", "WARN"),
            "description": rule.get("description", ""),
            "count": int(len(matched)),
            "sample_rows": matched[sample_cols].head(10).to_dict("records"),
        }

        if item["severity"] == "ERROR":
            errors.append(item)
        else:
            warnings.append(item)

    return warnings, errors


def check_statement_presence(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    counts = df["statement_type"].value_counts().to_dict()

    for st in config.get("required_statements", []):
        if counts.get(st, 0) == 0:
            warnings.append(
                {
                    "id": "missing_statement",
                    "severity": "WARN",
                    "description": f"{st} row가 없습니다. 파싱 누락 가능성 확인 필요",
                    "statement_type": st,
                }
            )

    return warnings


def check_missing_core_ids(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    for fs_type, ids in config.get("core_ids", {}).items():
        sub = df[df["statement_type"].eq(fs_type)]
        present = set(sub["canonical_account_id"])
        missing = [cid for cid in ids if cid not in present]
        if missing:
            result[fs_type] = missing

    return result


def detect_duplicate_risks(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ids = config.get("dangerous_duplicate_ids", [])

    for cid in sorted(ids):
        sub = df[df["canonical_account_id"].eq(cid)]
        if len(sub) <= 1:
            continue

        result.append(
            {
                "canonical_account_id": cid,
                "count": int(len(sub)),
                "rows": sub[
                    ["statement_type", "original_account_name", "amount"]
                ].to_dict("records"),
            }
        )

    return result


def collect_unmapped_candidates(
    debug_df: pd.DataFrame,
    config: dict[str, Any],
    limit: int = 30,
) -> list[dict[str, Any]]:
    threshold = float(
        config.get("settings", {}).get("large_unmapped_threshold", 10_000_000_000)
    )

    sub = debug_df[debug_df["canonical_account_id"].eq("UNMAPPED")].copy()
    if sub.empty:
        return []

    grouped = (
        sub.groupby(["statement_type", "original_account_name"], as_index=False)
        .agg(
            amount_sum=("amount_num", "sum"),
            count=("original_account_name", "size"),
            sample_context=("context_path", "first"),
            sample_rule=("rule_id", "first"),
            sample_reason=("reason", "first"),
        )
    )

    grouped["abs_amount_sum"] = grouped["amount_sum"].abs()
    grouped = grouped[grouped["abs_amount_sum"] >= threshold]
    grouped = grouped.sort_values("abs_amount_sum", ascending=False).head(limit)

    return grouped[
        [
            "statement_type",
            "original_account_name",
            "amount_sum",
            "count",
            "sample_context",
            "sample_rule",
            "sample_reason",
        ]
    ].to_dict("records")


# ============================================================
# Factor snapshot
# ============================================================

def first_amount(df: pd.DataFrame, cid: str) -> float | None:
    sub = df[df["canonical_account_id"].eq(cid)]
    if sub.empty:
        return None
    return float(sub["amount_num"].iloc[0])


def sum_amount(df: pd.DataFrame, cid: str) -> float:
    return float(df[df["canonical_account_id"].eq(cid)]["amount_num"].sum())


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def pct(v: float | None) -> float | None:
    if v is None:
        return None
    return round(v * 100, 4)


def build_factor_snapshot(
    df: pd.DataFrame,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not config.get("factor_snapshot", {}).get("enabled", True):
        return {}

    revenue = first_amount(df, "REVENUE")
    cogs = first_amount(df, "COGS")
    gross_profit = first_amount(df, "GROSS_PROFIT")
    op_income = first_amount(df, "OPERATING_INCOME")
    net_income = first_amount(df, "NET_INCOME")
    net_income_parent = first_amount(df, "NET_INCOME_PARENT")

    total_assets = first_amount(df, "TOTAL_ASSETS")
    total_equity = first_amount(df, "TOTAL_EQUITY")
    eaop = first_amount(df, "EAOP")

    cfo = first_amount(df, "CFO")
    capex_ppe = sum_amount(df, "CAPEX_PPE")
    capex_intang = sum_amount(df, "CAPEX_INTANG")
    ppe_disposal = sum_amount(df, "PPE_DISPOSAL_PROCEEDS")
    intang_disposal = sum_amount(df, "INTANGIBLE_DISPOSAL_PROCEEDS")

    gross_capex = abs(capex_ppe) + abs(capex_intang)
    disposal = abs(ppe_disposal) + abs(intang_disposal)
    net_capex = gross_capex - disposal

    fcf = None if cfo is None else cfo - gross_capex
    fcf_after_disposal = None if cfo is None else cfo - net_capex

    inventory = first_amount(df, "INVENTORIES")
    receivables = first_amount(df, "TRADE_RECEIVABLES")
    other_receivables = first_amount(df, "OTHER_RECEIVABLES")
    receivable_proxy = receivables if receivables is not None else other_receivables

    cash = first_amount(df, "CASH_AND_EQUIVALENTS")
    short_fin_assets = first_amount(df, "SHORT_TERM_FINANCIAL_ASSETS")
    short_debt = first_amount(df, "SHORT_TERM_DEBT")
    long_debt = first_amount(df, "LONG_TERM_DEBT")
    lease_liab = first_amount(df, "LEASE_LIABILITY")

    interest_bearing_debt = sum(
        v for v in [short_debt, long_debt, lease_liab] if v is not None
    )

    net_debt_cash_only = None if cash is None else interest_bearing_debt - cash

    net_debt_with_short_fin_assets = None
    if cash is not None and short_fin_assets is not None:
        net_debt_with_short_fin_assets = interest_bearing_debt - cash - short_fin_assets

    return {
        "revenue": revenue,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "operating_income": op_income,
        "net_income": net_income,
        "net_income_parent": net_income_parent,
        "total_assets": total_assets,
        "total_equity": total_equity,
        "parent_equity": eaop,
        "cfo": cfo,
        "gross_capex": gross_capex,
        "net_capex": net_capex,
        "fcf": fcf,
        "fcf_after_disposal": fcf_after_disposal,
        "gross_margin_pct": pct(safe_div(gross_profit, revenue)),
        "operating_margin_pct": pct(safe_div(op_income, revenue)),
        "net_margin_pct": pct(safe_div(net_income, revenue)),
        "fcf_margin_pct": pct(safe_div(fcf, revenue)),
        "roa_pct": pct(safe_div(net_income, total_assets)),
        "roe_pct": pct(safe_div(net_income, total_equity)),
        "roe_parent_pct": pct(safe_div(net_income_parent, eaop)),
        "inventory_turnover": safe_div(cogs, inventory),
        "receivables_turnover_proxy": safe_div(revenue, receivable_proxy),
        "interest_bearing_debt": interest_bearing_debt,
        "net_debt_cash_only": net_debt_cash_only,
        "net_debt_with_short_financial_assets": net_debt_with_short_fin_assets,
    }


# ============================================================
# Verdict
# ============================================================

def decide_verdict(
    checks: list[CheckResult],
    warnings: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    duplicate_risks: list[dict[str, Any]],
    missing_core_ids: dict[str, list[str]],
) -> tuple[str, int, list[dict[str, Any]]]:
    final_errors = list(errors)

    fail_checks = [c for c in checks if c.status == "FAIL"]
    for c in fail_checks:
        final_errors.append(
            {
                "id": "formula_check_failed",
                "severity": "ERROR",
                "description": c.name,
                "diff": c.diff,
            }
        )

    high_risk_duplicates = [
        d
        for d in duplicate_risks
        if d["canonical_account_id"]
        in {
            "NET_INCOME",
            "NET_INCOME_PARENT",
            "TOTAL_ASSETS",
            "CFO",
            "CFI",
            "CFF",
        }
    ]

    for d in high_risk_duplicates:
        final_errors.append(
            {
                "id": "dangerous_duplicate",
                "severity": "ERROR",
                "description": f"{d['canonical_account_id']} {d['count']}회 매핑",
                "rows": d["rows"],
            }
        )

    score = 100
    score -= len(final_errors) * 20
    score -= len(warnings) * 5
    score -= len(missing_core_ids) * 5
    score = max(0, min(100, score))

    if final_errors:
        verdict = "FAIL"
    elif warnings or missing_core_ids:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return verdict, score, final_errors


# ============================================================
# Single-year validation
# ============================================================

def evaluate_normalized_pair(
    normalized_csv: str | Path,
    debug_csv: str | Path,
    validation_rule_path: str | Path,
) -> ValidationReport:
    config = load_config(validation_rule_path)
    n, d = load_csv_pair(normalized_csv, debug_csv)

    row_counts = {
        k: int(v)
        for k, v in n["statement_type"].value_counts().to_dict().items()
    }
    mapped_rows = int(n["canonical_account_id"].ne("UNMAPPED").sum())
    unmapped_rows = int(n["canonical_account_id"].eq("UNMAPPED").sum())
    mapped_ratio = round(mapped_rows / max(1, len(n)), 4)

    checks = run_formula_checks(n, d, config)

    warnings: list[dict[str, Any]] = []
    warnings.extend(check_statement_presence(n, config))

    rule_warnings, rule_errors = run_warning_rules(d, config)
    warnings.extend(rule_warnings)

    duplicate_risks = detect_duplicate_risks(n, config)
    missing_core_ids = check_missing_core_ids(n, config)
    unmapped_candidates = collect_unmapped_candidates(d, config)
    factor_snapshot = build_factor_snapshot(n, config)

    verdict, score, final_errors = decide_verdict(
        checks=checks,
        warnings=warnings,
        errors=rule_errors,
        duplicate_risks=duplicate_risks,
        missing_core_ids=missing_core_ids,
    )

    return ValidationReport(
        file=str(normalized_csv),
        debug_file=str(debug_csv),
        verdict=verdict,
        score=score,
        row_counts=row_counts,
        mapped_rows=mapped_rows,
        unmapped_rows=unmapped_rows,
        mapped_ratio=mapped_ratio,
        checks=[asdict(c) for c in checks],
        warnings=warnings,
        errors=final_errors,
        missing_core_ids=missing_core_ids,
        duplicate_risks=duplicate_risks,
        unmapped_candidates=unmapped_candidates,
        factor_snapshot=factor_snapshot,
    )


# ============================================================
# Markdown report writers
# ============================================================

def write_markdown_report(report: ValidationReport, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# 정규화 검증 리포트")
    lines.append("")
    lines.append(f"- file: `{report.file}`")
    lines.append(f"- debug_file: `{report.debug_file}`")
    lines.append(f"- verdict: **{report.verdict}**")
    lines.append(f"- score: **{report.score}**")
    lines.append(f"- row_counts: `{report.row_counts}`")
    lines.append(f"- mapped_rows: `{report.mapped_rows}`")
    lines.append(f"- unmapped_rows: `{report.unmapped_rows}`")
    lines.append(f"- mapped_ratio: `{report.mapped_ratio}`")
    lines.append("")

    lines.append("## 1. 검산")
    lines.append("")
    lines.append("| check | status | diff | message |")
    lines.append("|---|---:|---:|---|")
    for c in report.checks:
        diff = c.get("diff")
        diff_text = "" if diff is None else f"{diff:,.0f}"
        lines.append(
            f"| {c['name']} | {c['status']} | {diff_text} | {c.get('message', '')} |"
        )

    lines.append("")
    lines.append("## 2. 오류")
    lines.append("")
    if report.errors:
        for e in report.errors:
            lines.append(f"- `{e.get('id', '')}`: {e.get('description', '')}")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 3. 경고")
    lines.append("")
    if report.warnings:
        for w in report.warnings:
            lines.append(f"- `{w.get('id', '')}`: {w.get('description', '')}")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 4. 핵심 계정 누락")
    lines.append("")
    if report.missing_core_ids:
        for fs, ids in report.missing_core_ids.items():
            lines.append(f"- {fs}: {', '.join(ids)}")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 5. 중복 위험")
    lines.append("")
    if report.duplicate_risks:
        for d in report.duplicate_risks:
            lines.append(f"- {d['canonical_account_id']}: {d['count']}회")
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 6. 큰 금액 UNMAPPED 후보")
    lines.append("")
    if report.unmapped_candidates:
        lines.append("| statement_type | original_account_name | amount_sum | count |")
        lines.append("|---|---|---:|---:|")
        for r in report.unmapped_candidates:
            lines.append(
                f"| {r['statement_type']} | {r['original_account_name']} | "
                f"{r['amount_sum']:,.0f} | {r['count']} |"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 7. Factor snapshot")
    lines.append("")
    lines.append("| factor | value |")
    lines.append("|---|---:|")
    for k, v in report.factor_snapshot.items():
        if isinstance(v, float):
            lines.append(f"| {k} | {v:,.4f} |")
        else:
            lines.append(f"| {k} | {v} |")

    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# Pair discovery
# ============================================================

def expected_debug_candidates(normalized_path: Path) -> list[Path]:
    stem = normalized_path.stem
    candidates = [normalized_path.with_name(stem + ".debug.csv")]

    m = re.match(r"^(.*?)(\(\d+\))$", stem)
    if m:
        base, suffix = m.groups()
        candidates.append(normalized_path.with_name(base + ".debug" + suffix + ".csv"))

    return candidates


def find_pairs(input_dir: str | Path) -> list[tuple[Path, Path]]:
    input_dir = Path(input_dir)

    normalized_files = [
        p
        for p in input_dir.rglob("*.csv")
        if ".debug" not in p.name and ".validation" not in p.name
    ]

    pairs: list[tuple[Path, Path]] = []

    for n in sorted(normalized_files):
        debug = None
        for c in expected_debug_candidates(n):
            if c.exists():
                debug = c
                break

        if debug is not None:
            pairs.append((n, debug))
        else:
            print(f"[WARN] debug file not found for {n}")

    return pairs


# ============================================================
# Z.AI integration
# ============================================================

def build_zai_config_from_args(args: argparse.Namespace) -> ZaiConfig | None:
    zai_mode = getattr(args, "zai_mode", "none")

    if zai_mode == "none":
        return None

    api_key = getattr(args, "zai_api_key", "") or os.getenv("ZAI_API_KEY", "")
    if not api_key:
        raise ValueError("ZAI_API_KEY 환경변수 또는 --zai-api-key가 필요합니다.")

    return ZaiConfig(
        api_key=api_key,
        base_url=getattr(args, "zai_base_url", "") or os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4/"),
        model=getattr(args, "zai_model", "") or os.getenv("ZAI_MODEL", "glm-5.1"),
        temperature=getattr(args, "zai_temperature", 0.1),
        max_tokens=getattr(args, "zai_max_tokens", 4096),
        retry=getattr(args, "zai_retry", 3),
        retry_sleep_sec=getattr(args, "zai_retry_sleep_sec", 5.0),
        max_context_chars=getattr(args, "zai_max_context_chars", 120_000),
    )


def should_call_zai(verdict: str, zai_mode: Literal["none", "warn_fail", "all"]) -> bool:
    if zai_mode == "none":
        return False
    if zai_mode == "all":
        return True
    return verdict in {"WARN", "FAIL", "ERROR"}


def load_canonical_compact(canonical_csv_path: str | Path | None) -> list[dict[str, Any]]:
    if not canonical_csv_path:
        return []

    path = Path(canonical_csv_path)
    if not path.exists():
        return []

    df = read_csv_flexible(path)
    cols = [
        "canonical_id",
        "canonical_nm",
        "fs_type",
        "is_derived",
        "formula",
        "description",
        "비고",
    ]
    cols = [c for c in cols if c in df.columns]
    return df[cols].to_dict("records")


def build_suspicious_rows_from_debug(
    debug_csv: str | Path,
    max_rows: int = 100,
    min_abs_amount: int = 10_000_000_000,
) -> list[dict[str, Any]]:
    df = read_csv_flexible(debug_csv)

    for col in [
        "statement_type",
        "original_account_name",
        "canonical_account_id",
        "canonical_account_name",
        "amount",
        "section_context",
        "context_path",
        "rule_id",
        "reason",
    ]:
        if col not in df.columns:
            df[col] = ""

    df["amount_num"] = to_number_series(df["amount"])

    masks = []

    masks.append(
        df["canonical_account_id"].eq("UNMAPPED")
        & df["amount_num"].abs().ge(min_abs_amount)
    )

    risk_ids = {
        "NET_INCOME",
        "NET_INCOME_PARENT",
        "TOTAL_ASSETS",
        "TOTAL_EQUITY",
        "CFO",
        "CFI",
        "CFF",
    }

    masks.append(
        df.duplicated(
            ["statement_type", "canonical_account_id", "amount"],
            keep=False,
        )
        & df["canonical_account_id"].isin(risk_ids)
    )

    masks.append(
        df["original_account_name"].astype(str).str.contains(
            "순차입|순상환|지배기업|비지배|총포괄|당기순이익",
            regex=True,
            na=False,
        )
    )

    if "rule_id" in df.columns:
        masks.append(df["rule_id"].astype(str).str.startswith("post_", na=False))

    mask = masks[0]
    for m in masks[1:]:
        mask = mask | m

    cols = [
        "statement_type",
        "original_account_name",
        "canonical_account_id",
        "canonical_account_name",
        "amount",
        "section_context",
        "context_path",
        "rule_id",
        "reason",
    ]

    out = (
        df.loc[mask, cols + ["amount_num"]]
        .assign(abs_amount=lambda x: x["amount_num"].abs())
        .sort_values("abs_amount", ascending=False)
        .head(max_rows)
        .drop(columns=["amount_num", "abs_amount"], errors="ignore")
    )

    return out.to_dict("records")


def compact_year_report_for_zai(report: ValidationReport) -> dict[str, Any]:
    return {
        "file": report.file,
        "debug_file": report.debug_file,
        "verdict": report.verdict,
        "score": report.score,
        "row_counts": report.row_counts,
        "mapped_rows": report.mapped_rows,
        "unmapped_rows": report.unmapped_rows,
        "mapped_ratio": report.mapped_ratio,
        "failed_or_skipped_checks": [
            c for c in report.checks if c.get("status") in {"FAIL", "SKIP"}
        ],
        "warnings": report.warnings,
        "errors": report.errors,
        "missing_core_ids": report.missing_core_ids,
        "duplicate_risks": report.duplicate_risks[:20],
        "unmapped_candidates": report.unmapped_candidates[:30],
        "factor_snapshot": report.factor_snapshot,
    }


def compact_stock_report_for_zai(stock_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_code": stock_report.get("stock_code"),
        "start_year": stock_report.get("start_year"),
        "end_year": stock_report.get("end_year"),
        "expected_years": stock_report.get("expected_years"),
        "found_years": stock_report.get("found_years"),
        "missing_years": stock_report.get("missing_years"),
        "verdict": stock_report.get("verdict"),
        "score": stock_report.get("score"),
        "messages": stock_report.get("messages"),
        "yearly_summary": stock_report.get("yearly_summary", []),
        "recurring_unmapped": stock_report.get("recurring_unmapped", [])[:50],
        "mapping_inconsistency": stock_report.get("mapping_inconsistency", [])[:50],
        "time_series_anomalies": stock_report.get("time_series_anomalies", [])[:50],
        "factor_trend": stock_report.get("factor_trend", []),
    }


def truncate_context_json(data: dict[str, Any], max_chars: int) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)

    if len(text) <= max_chars:
        return text

    # 너무 크면 canonical_accounts, suspicious_rows를 줄인다.
    data = dict(data)

    if "canonical_accounts" in data:
        data["canonical_accounts"] = data["canonical_accounts"][:80]

    if "suspicious_rows" in data:
        data["suspicious_rows"] = data["suspicious_rows"][:40]

    if "validation_report" in data and isinstance(data["validation_report"], dict):
        vr = dict(data["validation_report"])
        if "factor_snapshot" in vr:
            vr["factor_snapshot"] = vr["factor_snapshot"]
        if "unmapped_candidates" in vr:
            vr["unmapped_candidates"] = vr["unmapped_candidates"][:20]
        data["validation_report"] = vr

    text = json.dumps(data, ensure_ascii=False, indent=2)

    if len(text) <= max_chars:
        return text

    return text[:max_chars] + "\n... TRUNCATED ..."


def build_zai_year_prompt(context_bundle: dict[str, Any], max_context_chars: int) -> str:
    context_text = truncate_context_json(context_bundle, max_context_chars)

    return f"""
너는 한국 DART 재무제표 정규화 결과를 검증하는 회계 데이터 품질 평가자이자 룰 개선 제안자다.

역할:
- 이미 Python validator가 만든 검증 결과를 2차 리뷰한다.
- 재무제표 전체를 다시 정규화하지 않는다.
- validation_report, suspicious_rows, canonical_accounts만 근거로 판단한다.
- 제공된 canonical_accounts에 없는 계정은 확정 매핑하지 말고 canonical_add_candidates로만 제안한다.
- rules.yaml에 추가할 후보는 rule_patch_candidates로 제안한다.
- 근거가 부족하면 확인 불가라고 쓴다.
- 최종 출력은 JSON 객체 하나만 출력한다.
- 마크다운, 설명문, 코드블록을 출력하지 않는다.

평가 기준:
1. 검산 FAIL은 치명 오류 후보로 본다.
2. CFO/CFI/CFF/NET_INCOME/TOTAL_ASSETS 중복은 치명 오류 후보로 본다.
3. 큰 금액 UNMAPPED는 canonical 추가 또는 mapping rule 추가 후보로 검토한다.
4. 총포괄손익 귀속이 NET_INCOME_PARENT로 매핑되면 오류다.
5. 영업활동에서 창출된 현금흐름이 CFO로 매핑되면 오류다.
6. 순차입/순상환이 DEBT_ISSUE 또는 DEBT_REPAY로 직접 매핑되면 경고다.
7. canonical_add_candidates에는 재무제표 원문에 존재할 수 있는 계정과목만 제안하라.
8. 비율, 회전율, 마진, 순부채, ROIC, NOPAT, FCF 같은 파생지표는 절대 canonical_add_candidates에 넣지 마라.
9. 이미 canonical_accounts에 존재하는 의미의 alias는 canonical_add_candidates가 아니라 rule_patch_candidates로 제안하라.
10. placeholder 값(CANONICAL_ID, 계정과목1 등)은 절대 출력하지 마라.
11. rule_patch_candidates.yaml은 반드시 지정 schema만 사용하라.

출력 JSON 스키마:
{{
  "verdict": "PASS | WARN | FAIL",
  "score": 0,
  "summary": "요약",
  "critical_errors": [
    {{
      "type": "오류 유형",
      "description": "설명",
      "affected_rows": [],
      "impact": "퀀트 계산 영향"
    }}
  ],
  "warnings": [
    {{
      "type": "경고 유형",
      "description": "설명",
      "suggested_action": "조치"
    }}
  ],
  "passed_checks": [],
  "rule_patch_candidates": [
    {{
      "rule_id": "추천 rule id",
      "target_file": "mapping_common.yaml 또는 context_common.yaml 또는 validation_common.yaml",
      "yaml": "추가할 YAML rule",
      "reason": "추가 이유"
    }}
  ],
  "canonical_add_candidates": [
    {{
      "canonical_id": "추가 후보 ID",
      "canonical_nm": "계정명",
      "fs_type": "BS | IS | CF",
      "description": "설명",
      "reason": "추가 이유"
    }}
  ],
  "factor_readiness": {{
    "fcf": "READY | NEEDS_FIX | UNAVAILABLE",
    "capex": "READY | NEEDS_FIX | UNAVAILABLE",
    "roe": "READY | NEEDS_FIX | UNAVAILABLE",
    "roa": "READY | NEEDS_FIX | UNAVAILABLE",
    "pbr": "READY | NEEDS_FIX | UNAVAILABLE",
    "per": "READY | NEEDS_FIX | UNAVAILABLE",
    "roic": "READY | NEEDS_FIX | UNAVAILABLE",
    "ev_ebitda": "READY | NEEDS_FIX | UNAVAILABLE"
  }}
}}

입력 데이터:
{context_text}

JSON 객체 하나만 출력하라.
""".strip()


def build_zai_stock_prompt(context_bundle: dict[str, Any], max_context_chars: int) -> str:
    context_text = truncate_context_json(context_bundle, max_context_chars)

    return f"""
너는 한국 DART 재무제표 정규화 결과를 종목 단위로 검증하는 회계 데이터 품질 평가자이자 룰 개선 제안자다.

역할:
- 한 종목의 여러 연도 검증 결과를 보고 반복 문제를 찾는다.
- 재무제표 전체를 다시 정규화하지 않는다.
- stock_validation_report, canonical_accounts만 근거로 판단한다.
- 제공된 canonical_accounts에 없는 계정은 확정 매핑하지 말고 canonical_add_candidates로만 제안한다.
- rules.yaml에 추가할 후보는 rule_patch_candidates로 제안한다.
- 근거가 부족하면 확인 불가라고 쓴다.
- 최종 출력은 JSON 객체 하나만 출력한다.
- 마크다운, 설명문, 코드블록을 출력하지 않는다.

주요 판단 기준:
1. FAIL 연도가 있으면 원인을 요약한다.
2. 반복 UNMAPPED는 canonical 추가 후보 또는 mapping rule 후보로 제안한다.
3. 같은 계정명이 같은 문맥에서 연도별로 다르게 매핑되면 룰 충돌 후보로 본다.
4. factor_trend 급변은 실제 실적 변화 가능성과 매핑 오류 가능성을 구분해서 쓴다.
5. 검산은 통과했지만 반복 UNMAPPED가 크면 WARN으로 둔다.

출력 JSON 스키마:
{{
  "verdict": "PASS | WARN | FAIL",
  "score": 0,
  "summary": "요약",
  "critical_errors": [],
  "warnings": [],
  "year_level_findings": [
    {{
      "year": 2025,
      "finding": "설명",
      "severity": "INFO | WARN | ERROR"
    }}
  ],
  "recurring_unmapped_findings": [
    {{
      "original_account_name": "계정명",
      "statement_type": "BS | IS | CF",
      "years": [],
      "suggested_action": "mapping_rule | canonical_add | keep_unmapped",
      "reason": "이유"
    }}
  ],
  "mapping_inconsistency_findings": [],
  "rule_patch_candidates": [
    {{
      "rule_id": "추천 rule id",
      "target_file": "mapping_common.yaml 또는 context_common.yaml 또는 validation_common.yaml",
      "yaml": "추가할 YAML rule",
      "reason": "추가 이유"
    }}
  ],
  "canonical_add_candidates": [
    {{
      "canonical_id": "추가 후보 ID",
      "canonical_nm": "계정명",
      "fs_type": "BS | IS | CF",
      "description": "설명",
      "reason": "추가 이유"
    }}
  ],
  "factor_readiness": {{
    "fcf": "READY | NEEDS_FIX | UNAVAILABLE",
    "capex": "READY | NEEDS_FIX | UNAVAILABLE",
    "roe": "READY | NEEDS_FIX | UNAVAILABLE",
    "roa": "READY | NEEDS_FIX | UNAVAILABLE",
    "pbr": "READY | NEEDS_FIX | UNAVAILABLE",
    "per": "READY | NEEDS_FIX | UNAVAILABLE",
    "roic": "READY | NEEDS_FIX | UNAVAILABLE",
    "ev_ebitda": "READY | NEEDS_FIX | UNAVAILABLE"
  }}
}}

입력 데이터:
{context_text}

JSON 객체 하나만 출력하라.
""".strip()


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise ValueError("JSON object start not found")

    depth = 0
    in_str = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])

    raise ValueError("JSON object end not found")


def call_zai(prompt: str, config: ZaiConfig) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError("pip install openai 가 필요합니다.") from e

    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    last_error: Exception | None = None

    for attempt in range(config.retry):
        try:
            resp = client.chat.completions.create(
                model=config.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a strict JSON-only financial data quality evaluator.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )

            text = resp.choices[0].message.content or ""
            return extract_json_object(text)

        except Exception as e:
            last_error = e
            print(f"[WARN] z.ai call failed attempt={attempt + 1}: {e}")
            time.sleep(config.retry_sleep_sec * (attempt + 1))

    raise RuntimeError(f"z.ai call failed after retries: {last_error}")


def save_zai_suggestions(
    zai_eval: dict[str, Any],
    out_dir: str | Path,
    base_name: str,
) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}

    rule_candidates = zai_eval.get("rule_patch_candidates", []) or []
    canonical_candidates = zai_eval.get("canonical_add_candidates", []) or []

    if rule_candidates:
        grouped: dict[str, list[str]] = {}

        for item in rule_candidates:
            target = safe_str(item.get("target_file", "mapping_common.yaml")) or "mapping_common.yaml"
            yaml_text = safe_str(item.get("yaml", ""))
            reason = safe_str(item.get("reason", ""))
            rule_id = safe_str(item.get("rule_id", ""))

            block = []
            block.append(f"# rule_id: {rule_id}")
            block.append(f"# reason: {reason}")
            block.append(yaml_text.rstrip())
            block.append("")
            grouped.setdefault(target, []).append("\n".join(block))

        for target, blocks in grouped.items():
            safe_target = target.replace("/", "_").replace("\\", "_")
            if safe_target.endswith((".yaml", ".yml")):
                path = out_dir / f"{base_name}.suggested.{safe_target}"
            else:
                path = out_dir / f"{base_name}.suggested.{safe_target}.yaml"
            path.write_text("\n".join(blocks), encoding="utf-8")
            paths[f"suggested_rules_{safe_target}"] = str(path)

    if canonical_candidates:
        path = out_dir / f"{base_name}.suggested_canonical_accounts.csv"

        cols = [
            "canonical_id",
            "canonical_nm",
            "fs_type",
            "description",
            "reason",
        ]

        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for item in canonical_candidates:
                writer.writerow({c: safe_str(item.get(c, "")) for c in cols})

        paths["suggested_canonical_accounts"] = str(path)

    return paths


def run_zai_for_year_report(
    report: ValidationReport,
    debug_csv: str | Path,
    canonical_csv: str | Path | None,
    out_dir: str | Path,
    base_name: str,
    config: ZaiConfig,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    canonical_accounts = load_canonical_compact(canonical_csv)
    suspicious_rows = build_suspicious_rows_from_debug(debug_csv)

    context_bundle = {
        "canonical_accounts": canonical_accounts,
        "validation_report": compact_year_report_for_zai(report),
        "suspicious_rows": suspicious_rows,
    }

    context_path = out_dir / f"{base_name}.zai_context.json"
    write_json(context_path, context_bundle)

    prompt = build_zai_year_prompt(context_bundle, config.max_context_chars)
    prompt_path = out_dir / f"{base_name}.zai_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if dry_run:
        return None

    zai_eval = call_zai(prompt, config)
    eval_path = out_dir / f"{base_name}.zai_eval.json"
    write_json(eval_path, zai_eval)

    save_zai_suggestions(zai_eval, out_dir, base_name)

    return zai_eval


def run_zai_for_stock_report(
    stock_report: dict[str, Any],
    canonical_csv: str | Path | None,
    out_dir: str | Path,
    base_name: str,
    config: ZaiConfig,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    canonical_accounts = load_canonical_compact(canonical_csv)

    context_bundle = {
        "canonical_accounts": canonical_accounts,
        "stock_validation_report": compact_stock_report_for_zai(stock_report),
    }

    context_path = out_dir / f"{base_name}.zai_context.json"
    write_json(context_path, context_bundle)

    prompt = build_zai_stock_prompt(context_bundle, config.max_context_chars)
    prompt_path = out_dir / f"{base_name}.zai_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if dry_run:
        return None

    zai_eval = call_zai(prompt, config)
    eval_path = out_dir / f"{base_name}.zai_eval.json"
    write_json(eval_path, zai_eval)

    save_zai_suggestions(zai_eval, out_dir, base_name)

    return zai_eval


# ============================================================
# Aggregate Z.AI suggestions across stocks
# ============================================================


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_yaml_candidate_text(text: str) -> str:
    """
    같은 의미의 YAML 후보를 최대한 같은 key로 묶기 위한 정규화.
    너무 공격적으로 바꾸면 위험하므로 공백/주석 정도만 정리한다.
    """
    s = safe_str(text).strip()
    lines = []

    for line in s.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Z.AI가 붙인 설명 주석은 후보 동일성 판단에서 제외
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)

    return "\n".join(lines).strip()


def extract_stock_code_from_eval_path(path: str | Path) -> str:
    p = Path(path)

    # 보통 validation_by_stock/011780/011780_2016_2025.stock.zai_eval.json
    if re.match(r"^\d{6}$", p.parent.name):
        return p.parent.name

    m = re.search(r"(\d{6})_", p.name)
    if m:
        return m.group(1)

    return ""


def read_json_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_stock_zai_eval_files(input_dir: str | Path) -> list[Path]:
    input_dir = Path(input_dir)

    # 종목 단위 Z.AI 결과만 집계
    files = sorted(input_dir.rglob("*.stock.zai_eval.json"))

    # 혹시 파일명이 다르게 저장된 경우 fallback
    if not files:
        files = sorted(input_dir.rglob("*stock*.zai_eval.json"))

    return files


def aggregate_zai_suggestions(
    input_dir: str | Path,
    out_dir: str | Path,
    min_rule_count: int = 2,
    min_canonical_count: int = 2,
) -> dict[str, Any]:
    """
    종목별 *.stock.zai_eval.json을 모아서 추천 룰/캐노니컬 후보를 집계한다.
    """
    input_dir = Path(input_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    eval_files = find_stock_zai_eval_files(input_dir)

    rule_groups: dict[str, dict[str, Any]] = {}
    canonical_groups: dict[str, dict[str, Any]] = {}

    raw_rows = []

    for eval_path in eval_files:
        stock_code = extract_stock_code_from_eval_path(eval_path)

        try:
            data = read_json_file(eval_path)
        except Exception as e:
            print(f"[WARN] failed to read {eval_path}: {e}")
            continue

        verdict = safe_str(data.get("verdict"))
        score = data.get("score", "")

        # ----------------------------
        # Rule patch candidates
        # ----------------------------
        for item in data.get("rule_patch_candidates", []) or []:
            target_file = safe_str(item.get("target_file", "mapping_common.yaml")) or "mapping_common.yaml"
            yaml_text = safe_str(item.get("yaml", ""))
            rule_id = safe_str(item.get("rule_id", ""))
            reason = safe_str(item.get("reason", ""))

            normalized_yaml = normalize_yaml_candidate_text(yaml_text)

            if not normalized_yaml:
                continue

            key = f"{target_file}:{stable_hash(normalized_yaml)}"

            group = rule_groups.setdefault(
                key,
                {
                    "key": key,
                    "target_file": target_file,
                    "yaml": yaml_text.strip(),
                    "normalized_yaml": normalized_yaml,
                    "rule_ids": set(),
                    "reasons": [],
                    "stock_codes": set(),
                    "source_files": [],
                    "zai_verdicts": [],
                    "zai_scores": [],
                },
            )

            if rule_id:
                group["rule_ids"].add(rule_id)

            if reason:
                group["reasons"].append(reason)

            if stock_code:
                group["stock_codes"].add(stock_code)

            group["source_files"].append(str(eval_path))
            group["zai_verdicts"].append(verdict)
            group["zai_scores"].append(score)

            raw_rows.append({
                "type": "rule",
                "stock_code": stock_code,
                "target_file": target_file,
                "rule_id": rule_id,
                "reason": reason,
                "yaml": yaml_text,
                "source_file": str(eval_path),
            })

        # ----------------------------
        # Canonical add candidates
        # ----------------------------
        for item in data.get("canonical_add_candidates", []) or []:
            canonical_id = safe_str(item.get("canonical_id", "")).strip()
            canonical_nm = safe_str(item.get("canonical_nm", "")).strip()
            fs_type = safe_str(item.get("fs_type", "")).strip()
            description = safe_str(item.get("description", "")).strip()
            reason = safe_str(item.get("reason", "")).strip()

            if not canonical_id:
                continue

            key = canonical_id

            group = canonical_groups.setdefault(
                key,
                {
                    "canonical_id": canonical_id,
                    "canonical_nm": canonical_nm,
                    "fs_type": fs_type,
                    "description": description,
                    "reasons": [],
                    "stock_codes": set(),
                    "source_files": [],
                    "zai_verdicts": [],
                    "zai_scores": [],
                },
            )

            if canonical_nm and not group["canonical_nm"]:
                group["canonical_nm"] = canonical_nm
            if fs_type and not group["fs_type"]:
                group["fs_type"] = fs_type
            if description and not group["description"]:
                group["description"] = description

            if reason:
                group["reasons"].append(reason)

            if stock_code:
                group["stock_codes"].add(stock_code)

            group["source_files"].append(str(eval_path))
            group["zai_verdicts"].append(verdict)
            group["zai_scores"].append(score)

            raw_rows.append({
                "type": "canonical",
                "stock_code": stock_code,
                "canonical_id": canonical_id,
                "canonical_nm": canonical_nm,
                "fs_type": fs_type,
                "description": description,
                "reason": reason,
                "source_file": str(eval_path),
            })

    # ----------------------------
    # Convert groups to serializable rows
    # ----------------------------
    rule_rows = []

    for group in rule_groups.values():
        stock_codes = sorted(group["stock_codes"])
        count = len(stock_codes)

        priority = "C"
        if count >= 3:
            priority = "A"
        elif count >= 2:
            priority = "B"

        rule_rows.append({
            "priority": priority,
            "stock_count": count,
            "stock_codes": stock_codes,
            "target_file": group["target_file"],
            "rule_ids": sorted(group["rule_ids"]),
            "yaml": group["yaml"],
            "reasons": group["reasons"][:10],
            "source_files": group["source_files"][:20],
        })

    rule_rows = sorted(
        rule_rows,
        key=lambda x: (x["priority"], -x["stock_count"], x["target_file"]),
    )

    canonical_rows = []

    for group in canonical_groups.values():
        stock_codes = sorted(group["stock_codes"])
        count = len(stock_codes)

        priority = "C"
        if count >= 3:
            priority = "A"
        elif count >= 2:
            priority = "B"

        canonical_rows.append({
            "priority": priority,
            "stock_count": count,
            "stock_codes": stock_codes,
            "canonical_id": group["canonical_id"],
            "canonical_nm": group["canonical_nm"],
            "fs_type": group["fs_type"],
            "description": group["description"],
            "reasons": group["reasons"][:10],
            "source_files": group["source_files"][:20],
        })

    canonical_rows = sorted(
        canonical_rows,
        key=lambda x: (x["priority"], -x["stock_count"], x["canonical_id"]),
    )

    result = {
        "input_dir": str(input_dir),
        "eval_file_count": len(eval_files),
        "rule_candidate_count": len(rule_rows),
        "canonical_candidate_count": len(canonical_rows),
        "min_rule_count": min_rule_count,
        "min_canonical_count": min_canonical_count,
        "rule_candidates": rule_rows,
        "canonical_candidates": canonical_rows,
    }

    # ----------------------------
    # Save JSON
    # ----------------------------
    write_json(out_dir / "aggregated_suggestions.json", result)

    # ----------------------------
    # Save raw rows
    # ----------------------------
    if raw_rows:
        pd.DataFrame(raw_rows).to_csv(
            out_dir / "aggregated_suggestions_raw.csv",
            index=False,
            encoding="utf-8-sig",
        )

    # ----------------------------
    # Save review CSV
    # ----------------------------
    review_rows = []

    for r in rule_rows:
        review_rows.append({
            "type": "rule",
            "priority": r["priority"],
            "stock_count": r["stock_count"],
            "stock_codes": ",".join(r["stock_codes"][:20]),
            "target_file": r["target_file"],
            "id": ",".join(r["rule_ids"]),
            "name": "",
            "fs_type": "",
            "suggestion": r["yaml"],
            "reasons": " / ".join(r["reasons"][:3]),
            "decision": "",
            "reviewer_note": "",
        })

    for c in canonical_rows:
        review_rows.append({
            "type": "canonical",
            "priority": c["priority"],
            "stock_count": c["stock_count"],
            "stock_codes": ",".join(c["stock_codes"][:20]),
            "target_file": "CanonicalAccount.csv",
            "id": c["canonical_id"],
            "name": c["canonical_nm"],
            "fs_type": c["fs_type"],
            "suggestion": c["description"],
            "reasons": " / ".join(c["reasons"][:3]),
            "decision": "",
            "reviewer_note": "",
        })

    pd.DataFrame(review_rows).to_csv(
        out_dir / "aggregated_suggestions_review.csv",
        index=False,
        encoding="utf-8-sig",
    )

    # ----------------------------
    # Save canonical candidates CSV
    # ----------------------------
    canonical_csv_rows = [
        {
            "canonical_id": c["canonical_id"],
            "canonical_nm": c["canonical_nm"],
            "fs_type": c["fs_type"],
            "is_derived": "FALSE",
            "formula": "",
            "description": c["description"],
            "비고": f"ZAI suggested; stock_count={c['stock_count']}; stocks={','.join(c['stock_codes'][:20])}",
        }
        for c in canonical_rows
        if c["stock_count"] >= min_canonical_count
    ]

    if canonical_csv_rows:
        pd.DataFrame(canonical_csv_rows).to_csv(
            out_dir / "aggregated_canonical_candidates.csv",
            index=False,
            encoding="utf-8-sig",
        )

    # ----------------------------
    # Save YAML candidates by target_file
    # ----------------------------
    selected_rule_rows = [
        r for r in rule_rows
        if r["stock_count"] >= min_rule_count
    ]

    by_target: dict[str, list[dict[str, Any]]] = {}

    for r in selected_rule_rows:
        by_target.setdefault(r["target_file"], []).append(r)

    for target_file, rows in by_target.items():
        safe_target = target_file.replace("/", "_").replace("\\", "_")
        out_path = out_dir / f"aggregated_suggested_{safe_target}"

        blocks = []
        for r in rows:
            blocks.append("")
            blocks.append(f"# priority: {r['priority']}")
            blocks.append(f"# stock_count: {r['stock_count']}")
            blocks.append(f"# stock_codes: {','.join(r['stock_codes'][:30])}")
            blocks.append(f"# rule_ids: {','.join(r['rule_ids'])}")
            if r["reasons"]:
                blocks.append(f"# reason: {r['reasons'][0]}")
            blocks.append(r["yaml"].rstrip())
            blocks.append("")

        out_path.write_text("\n".join(blocks), encoding="utf-8")

    # ----------------------------
    # Save Markdown summary
    # ----------------------------
    write_aggregated_suggestions_markdown(
        result=result,
        out_path=out_dir / "aggregated_suggestions.md",
    )

    return result


def write_aggregated_suggestions_markdown(
    result: dict[str, Any],
    out_path: str | Path,
) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# Z.AI 추천 룰/캐노니컬 집계 리포트")
    lines.append("")
    lines.append(f"- eval_file_count: `{result['eval_file_count']}`")
    lines.append(f"- rule_candidate_count: `{result['rule_candidate_count']}`")
    lines.append(f"- canonical_candidate_count: `{result['canonical_candidate_count']}`")
    lines.append("")

    lines.append("## 1. Rule candidates")
    lines.append("")
    if result["rule_candidates"]:
        lines.append("| priority | stock_count | target_file | rule_ids | stock_codes |")
        lines.append("|---|---:|---|---|---|")
        for r in result["rule_candidates"][:100]:
            lines.append(
                f"| {r['priority']} | {r['stock_count']} | {r['target_file']} | "
                f"{','.join(r['rule_ids'])} | {','.join(r['stock_codes'][:20])} |"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 2. Canonical candidates")
    lines.append("")
    if result["canonical_candidates"]:
        lines.append("| priority | stock_count | canonical_id | canonical_nm | fs_type | stock_codes |")
        lines.append("|---|---:|---|---|---|---|")
        for c in result["canonical_candidates"][:100]:
            lines.append(
                f"| {c['priority']} | {c['stock_count']} | {c['canonical_id']} | "
                f"{c['canonical_nm']} | {c['fs_type']} | {','.join(c['stock_codes'][:20])} |"
            )
    else:
        lines.append("- 없음")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# Single / batch commands
# ============================================================

def evaluate_one_to_files(
    normalized_csv: str | Path,
    debug_csv: str | Path,
    validation_rule_path: str | Path,
    out_dir: str | Path,
    canonical_csv: str | Path | None = None,
    zai_mode: Literal["none", "warn_fail", "all"] = "none",
    zai_config: ZaiConfig | None = None,
    zai_dry_run: bool = False,
) -> dict[str, Any]:
    normalized_csv = Path(normalized_csv)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = evaluate_normalized_pair(
        normalized_csv=normalized_csv,
        debug_csv=debug_csv,
        validation_rule_path=validation_rule_path,
    )

    base = normalized_csv.stem
    json_path = out_dir / f"{base}.validation.json"
    md_path = out_dir / f"{base}.validation.md"

    write_json(json_path, asdict(report))
    write_markdown_report(report, md_path)

    zai_eval = None
    if should_call_zai(report.verdict, zai_mode):
        if zai_config is None:
            raise ValueError("zai_config is required")
        zai_eval = run_zai_for_year_report(
            report=report,
            debug_csv=debug_csv,
            canonical_csv=canonical_csv,
            out_dir=out_dir,
            base_name=base,
            config=zai_config,
            dry_run=zai_dry_run,
        )

    return {
        "file": str(normalized_csv),
        "debug_file": str(debug_csv),
        "verdict": report.verdict,
        "score": report.score,
        "mapped_rows": report.mapped_rows,
        "unmapped_rows": report.unmapped_rows,
        "mapped_ratio": report.mapped_ratio,
        "warning_count": len(report.warnings),
        "error_count": len(report.errors),
        "zai_called": should_call_zai(report.verdict, zai_mode),
        "zai_verdict": "" if zai_eval is None else safe_str(zai_eval.get("verdict")),
        "zai_score": "" if zai_eval is None else safe_str(zai_eval.get("score")),
        "validation_json": str(json_path),
        "validation_md": str(md_path),
    }


def evaluate_batch(
    input_dir: str | Path,
    validation_rule_path: str | Path,
    out_dir: str | Path,
    canonical_csv: str | Path | None = None,
    zai_mode: Literal["none", "warn_fail", "all"] = "none",
    zai_config: ZaiConfig | None = None,
    zai_dry_run: bool = False,
) -> pd.DataFrame:
    pairs = find_pairs(input_dir)
    rows = []

    for normalized_csv, debug_csv in pairs:
        try:
            rows.append(
                evaluate_one_to_files(
                    normalized_csv=normalized_csv,
                    debug_csv=debug_csv,
                    validation_rule_path=validation_rule_path,
                    out_dir=out_dir,
                    canonical_csv=canonical_csv,
                    zai_mode=zai_mode,
                    zai_config=zai_config,
                    zai_dry_run=zai_dry_run,
                )
            )
        except Exception as e:
            rows.append(
                {
                    "file": str(normalized_csv),
                    "debug_file": str(debug_csv),
                    "verdict": "ERROR",
                    "score": 0,
                    "error": str(e),
                }
            )

    summary = pd.DataFrame(rows)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out_dir / "validation_summary.csv", index=False, encoding="utf-8-sig")
    return summary


# ============================================================
# Stock-level validation
# ============================================================

def parse_stock_period_from_filename(path: str | Path) -> dict[str, Any]:
    name = Path(path).name
    m = re.search(r"normalized_(\d{6})_(\d{4}\.\d{2})", name)

    if not m:
        return {"stock_code": "", "period": "", "year": None}

    period = m.group(2)
    return {"stock_code": m.group(1), "period": period, "year": int(period[:4])}


def file_version_num(path: str | Path) -> int:
    name = Path(path).name
    m = re.search(r"\((\d+)\)\.csv$", name)
    return int(m.group(1)) if m else 0

def period_month(path: str | Path) -> int:
    meta = parse_stock_period_from_filename(path)
    period = meta.get("period", "")
    if "." not in period:
        return 0
    try:
        return int(period.split(".")[1])
    except Exception:
        return 0

def file_select_key(path: str | Path) -> tuple[int, int]:
    # 1순위: 월. 연간 검증에서는 12월 우선
    # 2순위: 버전. 같은 월이면 (2), (3) 같은 높은 버전 우선
    return period_month(path), file_version_num(path)

def find_stock_year_pairs(
    input_dir: str | Path,
    stock_code: str,
    start_year: int,
    end_year: int,
) -> list[dict[str, Any]]:
    input_dir = Path(input_dir)

    candidates = [
        p
        for p in input_dir.rglob(f"normalized_{stock_code}_*.csv")
        if ".debug" not in p.name and ".validation" not in p.name
    ]

    by_year: dict[int, list[Path]] = {}

    for p in candidates:
        meta = parse_stock_period_from_filename(p)
        year = meta["year"]
        if year is None:
            continue
        if start_year <= year <= end_year:
            by_year.setdefault(year, []).append(p)

    results: list[dict[str, Any]] = []

    for year in range(start_year, end_year + 1):
        files = by_year.get(year, [])

        if not files:
            results.append(
                {
                    "year": year,
                    "period": f"{year}.12",
                    "normalized": "",
                    "debug": "",
                    "status": "MISSING_NORMALIZED",
                }
            )
            continue

        files = sorted(files, key=file_select_key, reverse=True)
        selected_normalized = None
        selected_debug = None

        for normalized_path in files:
            for debug_candidate in expected_debug_candidates(normalized_path):
                if debug_candidate.exists():
                    selected_normalized = normalized_path
                    selected_debug = debug_candidate
                    break
            if selected_normalized is not None:
                break

        if selected_normalized is None:
            selected_normalized = files[0]
            results.append(
                {
                    "year": year,
                    "period": parse_stock_period_from_filename(selected_normalized)["period"],
                    "normalized": str(selected_normalized),
                    "debug": "",
                    "status": "MISSING_DEBUG",
                }
            )
        else:
            results.append(
                {
                    "year": year,
                    "period": parse_stock_period_from_filename(selected_normalized)["period"],
                    "normalized": str(selected_normalized),
                    "debug": str(selected_debug),
                    "status": "FOUND",
                }
            )

    return results


def discover_stock_codes(input_dir: str | Path) -> list[str]:
    input_dir = Path(input_dir)
    codes = set()

    for p in input_dir.rglob("normalized_*.csv"):
        if ".debug" in p.name or ".validation" in p.name:
            continue
        meta = parse_stock_period_from_filename(p)
        if meta["stock_code"]:
            codes.add(meta["stock_code"])

    return sorted(codes)


def build_stock_year_factor_row(
    year: int,
    period: str,
    normalized_csv: str,
    debug_csv: str,
    report: ValidationReport,
) -> dict[str, Any]:
    row = {
        "year": year,
        "period": period,
        "normalized_csv": normalized_csv,
        "debug_csv": debug_csv,
        "verdict": report.verdict,
        "score": report.score,
        "mapped_rows": report.mapped_rows,
        "unmapped_rows": report.unmapped_rows,
        "mapped_ratio": report.mapped_ratio,
        "warning_count": len(report.warnings),
        "error_count": len(report.errors),
        "row_counts": json.dumps(report.row_counts, ensure_ascii=False),
    }

    for k, v in report.factor_snapshot.items():
        row[k] = v

    return row


def collect_stock_recurring_unmapped(
    yearly_reports: list[dict[str, Any]],
    min_year_count: int = 2,
) -> list[dict[str, Any]]:
    rows = []

    for yr in yearly_reports:
        report = yr.get("report")
        if report is None:
            continue

        year = yr["year"]

        for item in report.unmapped_candidates:
            rows.append(
                {
                    "year": year,
                    "statement_type": item.get("statement_type", ""),
                    "original_account_name": item.get("original_account_name", ""),
                    "amount_sum": float(item.get("amount_sum", 0) or 0),
                    "count": int(item.get("count", 0) or 0),
                    "sample_context": item.get("sample_context", ""),
                    "sample_rule": item.get("sample_rule", ""),
                    "sample_reason": item.get("sample_reason", ""),
                }
            )

    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["abs_amount_sum"] = df["amount_sum"].abs()

    grouped = (
        df.groupby(["statement_type", "original_account_name"], as_index=False)
        .agg(
            year_count=("year", "nunique"),
            years=("year", lambda s: sorted(set(map(int, s)))),
            total_abs_amount=("abs_amount_sum", "sum"),
            max_abs_amount=("abs_amount_sum", "max"),
            sample_context=("sample_context", "first"),
            sample_rule=("sample_rule", "first"),
            sample_reason=("sample_reason", "first"),
        )
    )

    grouped = grouped[grouped["year_count"] >= min_year_count]
    grouped = grouped.sort_values(["year_count", "total_abs_amount"], ascending=[False, False])
    return grouped.to_dict("records")


def context_bucket(row: pd.Series) -> str:
    st = safe_str(row.get("statement_type"))
    ctx = normalize_name(
        safe_str(row.get("section_context"))
        + " "
        + safe_str(row.get("parent_context"))
        + " "
        + safe_str(row.get("context_path"))
    )

    if st == "BS":
        if "비유동부채" in ctx:
            return "BS_NON_CURRENT_LIABILITIES"
        if "유동부채" in ctx:
            return "BS_CURRENT_LIABILITIES"
        if "비유동자산" in ctx:
            return "BS_NON_CURRENT_ASSETS"
        if "유동자산" in ctx:
            return "BS_CURRENT_ASSETS"
        if "자본" in ctx:
            return "BS_EQUITY"

    if st == "IS":
        if "당기순이익" in ctx or "당기순손익" in ctx:
            return "IS_NET_INCOME_ATTRIBUTION"
        if "총포괄" in ctx:
            return "IS_COMPREHENSIVE_INCOME_ATTRIBUTION"

    if st == "CF":
        if "영업활동" in ctx:
            return "CF_OPERATING"
        if "투자활동" in ctx:
            return "CF_INVESTING"
        if "재무활동" in ctx:
            return "CF_FINANCING"

    return ""


def collect_stock_mapping_inconsistency(
    debug_paths_by_year: dict[int, str],
    min_year_count: int = 2,
) -> list[dict[str, Any]]:
    rows = []

    for year, debug_path in debug_paths_by_year.items():
        d = read_csv_flexible(debug_path)

        for col in [
            "statement_type",
            "original_account_name",
            "canonical_account_id",
            "canonical_account_name",
            "amount",
            "section_context",
            "parent_context",
            "context_path",
        ]:
            if col not in d.columns:
                d[col] = ""

        d["name_norm"] = d["original_account_name"].map(normalize_name)
        d["context_bucket"] = d.apply(context_bucket, axis=1)

        for _, r in d.iterrows():
            cid = safe_str(r["canonical_account_id"])
            if cid == "" or cid == "UNMAPPED":
                continue

            rows.append(
                {
                    "year": year,
                    "statement_type": r["statement_type"],
                    "name_norm": r["name_norm"],
                    "context_bucket": r["context_bucket"],
                    "original_account_name": r["original_account_name"],
                    "canonical_account_id": cid,
                }
            )

    if not rows:
        return []

    df = pd.DataFrame(rows)

    grouped = (
        df.groupby(["statement_type", "name_norm", "context_bucket"], as_index=False)
        .agg(
            original_account_name=("original_account_name", "first"),
            year_count=("year", "nunique"),
            years=("year", lambda s: sorted(set(map(int, s)))),
            canonical_ids=("canonical_account_id", lambda s: sorted(set(s))),
        )
    )

    grouped["canonical_id_count"] = grouped["canonical_ids"].map(len)

    suspicious = grouped[
        (grouped["year_count"] >= min_year_count)
        & (grouped["canonical_id_count"] > 1)
    ].copy()

    suspicious = suspicious.sort_values(["canonical_id_count", "year_count"], ascending=[False, False])
    return suspicious.to_dict("records")


def detect_stock_time_series_anomalies(
    factor_df: pd.DataFrame,
    jump_threshold: float = 5.0,
) -> list[dict[str, Any]]:
    if factor_df.empty:
        return []

    metrics = [
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "net_income_parent",
        "total_assets",
        "total_equity",
        "parent_equity",
        "cfo",
        "gross_capex",
        "net_capex",
        "fcf",
        "interest_bearing_debt",
        "net_debt_cash_only",
    ]

    anomalies = []
    df = factor_df.sort_values("year").copy()

    for metric in metrics:
        if metric not in df.columns:
            continue

        values = pd.to_numeric(df[metric], errors="coerce")

        for i in range(1, len(df)):
            prev = values.iloc[i - 1]
            curr = values.iloc[i]

            if pd.isna(prev) or pd.isna(curr):
                continue

            if abs(prev) < 1:
                continue

            ratio = abs(curr / prev)

            if ratio >= jump_threshold:
                anomalies.append(
                    {
                        "metric": metric,
                        "prev_year": int(df["year"].iloc[i - 1]),
                        "year": int(df["year"].iloc[i]),
                        "prev_value": float(prev),
                        "current_value": float(curr),
                        "abs_ratio": float(ratio),
                        "message": f"{metric}가 전년 대비 {ratio:.2f}배 변화",
                    }
                )

    return anomalies


def build_stock_verdict(
    yearly_summary: pd.DataFrame,
    recurring_unmapped: list[dict[str, Any]],
    mapping_inconsistency: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    expected_years: list[int],
) -> tuple[str, int, list[str]]:
    messages = []
    score = 100

    if yearly_summary.empty:
        return "FAIL", 0, ["검증 가능한 연도별 파일이 없습니다"]

    present_years = set(map(int, yearly_summary["year"]))
    missing_years = [y for y in expected_years if y not in present_years]

    fail_years = yearly_summary[yearly_summary["verdict"].eq("FAIL")]
    warn_years = yearly_summary[yearly_summary["verdict"].eq("WARN")]

    if missing_years:
        score -= min(30, len(missing_years) * 5)
        messages.append(f"누락 연도: {missing_years}")

    if len(fail_years) > 0:
        years = fail_years["year"].astype(int).tolist()
        score -= min(50, len(years) * 15)
        messages.append(f"FAIL 연도 {len(years)}개: {years}")

    if len(warn_years) > 0:
        years = warn_years["year"].astype(int).tolist()
        score -= min(30, len(years) * 5)
        messages.append(f"WARN 연도 {len(years)}개: {years}")

    if recurring_unmapped:
        score -= min(20, len(recurring_unmapped) * 2)
        messages.append(f"반복 UNMAPPED 후보 {len(recurring_unmapped)}개")

    if mapping_inconsistency:
        score -= min(20, len(mapping_inconsistency) * 5)
        messages.append(f"연도별 매핑 불일치 후보 {len(mapping_inconsistency)}개")

    if anomalies:
        score -= min(10, len(anomalies))
        messages.append(f"시계열 급변 후보 {len(anomalies)}개")

    score = max(0, min(100, score))

    if len(fail_years) > 0:
        verdict = "FAIL"
    elif missing_years or len(warn_years) > 0 or recurring_unmapped or mapping_inconsistency or anomalies:
        verdict = "WARN"
    else:
        verdict = "PASS"

    if not messages:
        messages.append("종목 단위 검증 결과 특이사항 없음")

    return verdict, score, messages


def write_stock_markdown_report(
    report: dict[str, Any],
    out_path: str | Path,
) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    stock_code = report["stock_code"]
    start_year = report["start_year"]
    end_year = report["end_year"]

    lines.append(f"# {stock_code} {start_year}~{end_year} 종목 단위 정규화 검증 리포트")
    lines.append("")
    lines.append(f"- verdict: **{report['verdict']}**")
    lines.append(f"- score: **{report['score']}**")
    lines.append(f"- found_years: `{report['found_years']}`")
    lines.append(f"- missing_years: `{report['missing_years']}`")
    lines.append("")

    lines.append("## 1. 요약")
    lines.append("")
    for m in report["messages"]:
        lines.append(f"- {m}")
    lines.append("")

    lines.append("## 2. 연도별 평가")
    lines.append("")
    lines.append("| year | verdict | score | mapped_ratio | warnings | errors |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for r in report["yearly_summary"]:
        lines.append(
            f"| {r['year']} | {r['verdict']} | {r['score']} | "
            f"{r['mapped_ratio']} | {r['warning_count']} | {r['error_count']} |"
        )

    lines.append("")
    lines.append("## 3. 반복 UNMAPPED 후보")
    lines.append("")
    if report["recurring_unmapped"]:
        lines.append("| statement_type | original_account_name | year_count | years | total_abs_amount |")
        lines.append("|---|---|---:|---|---:|")
        for r in report["recurring_unmapped"][:50]:
            lines.append(
                f"| {r['statement_type']} | {r['original_account_name']} | "
                f"{r['year_count']} | {r['years']} | {r['total_abs_amount']:,.0f} |"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 4. 연도별 매핑 불일치 후보")
    lines.append("")
    if report["mapping_inconsistency"]:
        lines.append("| statement_type | original_account_name | context_bucket | canonical_ids | years |")
        lines.append("|---|---|---|---|---|")
        for r in report["mapping_inconsistency"][:50]:
            lines.append(
                f"| {r['statement_type']} | {r['original_account_name']} | "
                f"{r['context_bucket']} | {r['canonical_ids']} | {r['years']} |"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 5. 시계열 급변 후보")
    lines.append("")
    if report["time_series_anomalies"]:
        lines.append("| metric | prev_year | year | ratio |")
        lines.append("|---|---:|---:|---:|")
        for r in report["time_series_anomalies"][:50]:
            lines.append(
                f"| {r['metric']} | {r['prev_year']} | {r['year']} | {r['abs_ratio']:.2f} |"
            )
    else:
        lines.append("- 없음")

    lines.append("")
    lines.append("## 6. Factor trend")
    lines.append("")
    factor_df = pd.DataFrame(report["factor_trend"])

    if not factor_df.empty:
        cols = [
            "year",
            "revenue",
            "operating_income",
            "net_income",
            "net_income_parent",
            "cfo",
            "gross_capex",
            "fcf",
            "total_assets",
            "total_equity",
            "roe_parent_pct",
        ]
        cols = [c for c in cols if c in factor_df.columns]

        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "|".join(["---:" for _ in cols]) + "|")

        for _, row in factor_df.iterrows():
            vals = []
            for c in cols:
                v = row.get(c)
                if isinstance(v, float):
                    vals.append(f"{v:,.4f}")
                elif pd.isna(v):
                    vals.append("")
                else:
                    vals.append(str(v))
            lines.append("| " + " | ".join(vals) + " |")
    else:
        lines.append("- 없음")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def evaluate_stock_to_files(
    stock_code: str,
    input_dir: str | Path,
    validation_rule_path: str | Path,
    out_dir: str | Path,
    start_year: int,
    end_year: int,
    canonical_csv: str | Path | None = None,
    zai_mode: Literal["none", "warn_fail", "all"] = "none",
    zai_config: ZaiConfig | None = None,
    zai_dry_run: bool = False,
) -> dict[str, Any]:
    stock_out_dir = Path(out_dir) / stock_code
    stock_out_dir.mkdir(parents=True, exist_ok=True)

    pairs = find_stock_year_pairs(
        input_dir=input_dir,
        stock_code=stock_code,
        start_year=start_year,
        end_year=end_year,
    )

    expected_years = list(range(start_year, end_year + 1))

    yearly_reports: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    debug_paths_by_year: dict[int, str] = {}

    for pair in pairs:
        year = int(pair["year"])

        if pair["status"] != "FOUND":
            yearly_reports.append(
                {
                    "year": year,
                    "period": pair["period"],
                    "status": pair["status"],
                    "normalized": pair["normalized"],
                    "debug": pair["debug"],
                    "report": None,
                }
            )
            continue

        report = evaluate_normalized_pair(
            normalized_csv=pair["normalized"],
            debug_csv=pair["debug"],
            validation_rule_path=validation_rule_path,
        )

        yearly_reports.append(
            {
                "year": year,
                "period": pair["period"],
                "status": "FOUND",
                "normalized": pair["normalized"],
                "debug": pair["debug"],
                "report": report,
            }
        )

        factor_rows.append(
            build_stock_year_factor_row(
                year=year,
                period=pair["period"],
                normalized_csv=pair["normalized"],
                debug_csv=pair["debug"],
                report=report,
            )
        )

        debug_paths_by_year[year] = pair["debug"]

        year_json = stock_out_dir / f"{stock_code}_{year}.validation.json"
        year_md = stock_out_dir / f"{stock_code}_{year}.validation.md"

        write_json(year_json, asdict(report))
        write_markdown_report(report, year_md)

    factor_df = pd.DataFrame(factor_rows).sort_values("year") if factor_rows else pd.DataFrame()

    if factor_df.empty:
        yearly_summary = pd.DataFrame()
    else:
        summary_cols = [
            "year",
            "period",
            "verdict",
            "score",
            "mapped_rows",
            "unmapped_rows",
            "mapped_ratio",
            "warning_count",
            "error_count",
        ]
        yearly_summary = factor_df[summary_cols].copy()

    recurring_unmapped = collect_stock_recurring_unmapped(yearly_reports, min_year_count=2)
    mapping_inconsistency = collect_stock_mapping_inconsistency(debug_paths_by_year, min_year_count=2)
    time_series_anomalies = detect_stock_time_series_anomalies(factor_df, jump_threshold=5.0)

    found_years = sorted(debug_paths_by_year.keys())
    missing_years = [y for y in expected_years if y not in found_years]

    verdict, score, messages = build_stock_verdict(
        yearly_summary=yearly_summary,
        recurring_unmapped=recurring_unmapped,
        mapping_inconsistency=mapping_inconsistency,
        anomalies=time_series_anomalies,
        expected_years=expected_years,
    )

    stock_report = {
        "stock_code": stock_code,
        "start_year": start_year,
        "end_year": end_year,
        "expected_years": expected_years,
        "found_years": found_years,
        "missing_years": missing_years,
        "verdict": verdict,
        "score": score,
        "messages": messages,
        "yearly_summary": yearly_summary.to_dict("records") if not yearly_summary.empty else [],
        "factor_trend": factor_df.to_dict("records") if not factor_df.empty else [],
        "recurring_unmapped": recurring_unmapped,
        "mapping_inconsistency": mapping_inconsistency,
        "time_series_anomalies": time_series_anomalies,
    }

    base = f"{stock_code}_{start_year}_{end_year}.stock"
    json_path = stock_out_dir / f"{base}.validation.json"
    md_path = stock_out_dir / f"{base}.validation.md"
    factor_csv_path = stock_out_dir / f"{base}.factor_trend.csv"

    write_json(json_path, stock_report)
    write_stock_markdown_report(stock_report, md_path)

    if not factor_df.empty:
        factor_df.to_csv(factor_csv_path, index=False, encoding="utf-8-sig")

    zai_eval = None
    if should_call_zai(verdict, zai_mode):
        if zai_config is None:
            raise ValueError("zai_config is required")
        zai_eval = run_zai_for_stock_report(
            stock_report=stock_report,
            canonical_csv=canonical_csv,
            out_dir=stock_out_dir,
            base_name=base,
            config=zai_config,
            dry_run=zai_dry_run,
        )

    return {
        "stock_code": stock_code,
        "start_year": start_year,
        "end_year": end_year,
        "verdict": verdict,
        "score": score,
        "found_years": json.dumps(found_years, ensure_ascii=False),
        "missing_years": json.dumps(missing_years, ensure_ascii=False),
        "year_count": len(found_years),
        "message_count": len(messages),
        "recurring_unmapped_count": len(recurring_unmapped),
        "mapping_inconsistency_count": len(mapping_inconsistency),
        "time_series_anomaly_count": len(time_series_anomalies),
        "zai_called": should_call_zai(verdict, zai_mode),
        "zai_verdict": "" if zai_eval is None else safe_str(zai_eval.get("verdict")),
        "zai_score": "" if zai_eval is None else safe_str(zai_eval.get("score")),
        "json_path": str(json_path),
        "md_path": str(md_path),
        "factor_csv_path": str(factor_csv_path),
    }

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_progress_csv(path: str | Path, row: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    file_exists = path.exists()

    cols = [
        "timestamp",
        "index",
        "total",
        "stock_code",
        "status",
        "verdict",
        "score",
        "elapsed_sec",
        "error",
        "json_path",
    ]

    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)

        if not file_exists:
            writer.writeheader()

        writer.writerow({c: row.get(c, "") for c in cols})


def load_completed_stock_result(
    stock_code: str,
    out_dir: str | Path,
    start_year: int,
    end_year: int,
) -> dict[str, Any] | None:
    stock_out_dir = Path(out_dir) / stock_code
    json_path = stock_out_dir / f"{stock_code}_{start_year}_{end_year}.stock.validation.json"

    if not json_path.exists():
        return None

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    return {
        "stock_code": stock_code,
        "start_year": start_year,
        "end_year": end_year,
        "verdict": data.get("verdict", ""),
        "score": data.get("score", ""),
        "found_years": json.dumps(data.get("found_years", []), ensure_ascii=False),
        "missing_years": json.dumps(data.get("missing_years", []), ensure_ascii=False),
        "year_count": len(data.get("found_years", [])),
        "message_count": len(data.get("messages", [])),
        "recurring_unmapped_count": len(data.get("recurring_unmapped", [])),
        "mapping_inconsistency_count": len(data.get("mapping_inconsistency", [])),
        "time_series_anomaly_count": len(data.get("time_series_anomalies", [])),
        "json_path": str(json_path),
        "md_path": str(stock_out_dir / f"{stock_code}_{start_year}_{end_year}.stock.validation.md"),
        "factor_csv_path": str(stock_out_dir / f"{stock_code}_{start_year}_{end_year}.stock.factor_trend.csv"),
        "resumed": True,
    }

def evaluate_stock_batch_to_files(
    input_dir: str | Path,
    validation_rule_path: str | Path,
    out_dir: str | Path,
    start_year: int,
    end_year: int,
    stock_codes: list[str] | None = None,
    canonical_csv: str | Path | None = None,
    zai_mode: Literal["none", "warn_fail", "all"] = "none",
    zai_config: ZaiConfig | None = None,
    zai_dry_run: bool = False,
    resume: bool = False,
) -> pd.DataFrame:
    if stock_codes is None or len(stock_codes) == 0:
        stock_codes = discover_stock_codes(input_dir)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(stock_codes)

    progress_path = out_dir / f"stock_validation_progress_{start_year}_{end_year}.csv"
    summary_path = out_dir / f"stock_validation_summary_{start_year}_{end_year}.csv"

    rows = []

    batch_start = time.time()

    print(f"[BATCH START] stocks={total}, years={start_year}-{end_year}, resume={resume}")
    print(f"[PROGRESS CSV] {progress_path}")
    print(f"[SUMMARY CSV]  {summary_path}")

    for idx, stock_code in enumerate(stock_codes, start=1):
        item_start = time.time()

        print(f"[{idx}/{total}] START stock_code={stock_code}")

        try:
            if resume:
                completed = load_completed_stock_result(
                    stock_code=stock_code,
                    out_dir=out_dir,
                    start_year=start_year,
                    end_year=end_year,
                )

                if completed is not None:
                    elapsed = time.time() - item_start

                    completed["status"] = "SKIPPED_RESUME"
                    rows.append(completed)

                    append_progress_csv(
                        progress_path,
                        {
                            "timestamp": now_str(),
                            "index": idx,
                            "total": total,
                            "stock_code": stock_code,
                            "status": "SKIPPED_RESUME",
                            "verdict": completed.get("verdict", ""),
                            "score": completed.get("score", ""),
                            "elapsed_sec": round(elapsed, 2),
                            "error": "",
                            "json_path": completed.get("json_path", ""),
                        },
                    )

                    print(
                        f"[{idx}/{total}] SKIP stock_code={stock_code} "
                        f"verdict={completed.get('verdict')} score={completed.get('score')}"
                    )

                    pd.DataFrame(rows).to_csv(
                        summary_path,
                        index=False,
                        encoding="utf-8-sig",
                    )

                    continue

            result = evaluate_stock_to_files(
                stock_code=stock_code,
                input_dir=input_dir,
                validation_rule_path=validation_rule_path,
                out_dir=out_dir,
                start_year=start_year,
                end_year=end_year,
                canonical_csv=canonical_csv,
                zai_mode=zai_mode,
                zai_config=zai_config,
                zai_dry_run=zai_dry_run,
            )

            elapsed = time.time() - item_start

            result["status"] = "DONE"
            result["elapsed_sec"] = round(elapsed, 2)

            rows.append(result)

            append_progress_csv(
                progress_path,
                {
                    "timestamp": now_str(),
                    "index": idx,
                    "total": total,
                    "stock_code": stock_code,
                    "status": "DONE",
                    "verdict": result.get("verdict", ""),
                    "score": result.get("score", ""),
                    "elapsed_sec": round(elapsed, 2),
                    "error": "",
                    "json_path": result.get("json_path", ""),
                },
            )

            print(
                f"[{idx}/{total}] DONE stock_code={stock_code} "
                f"verdict={result.get('verdict')} score={result.get('score')} "
                f"elapsed={elapsed:.1f}s"
            )

        except Exception as e:
            elapsed = time.time() - item_start

            error_row = {
                "stock_code": stock_code,
                "start_year": start_year,
                "end_year": end_year,
                "verdict": "ERROR",
                "score": 0,
                "status": "ERROR",
                "elapsed_sec": round(elapsed, 2),
                "error": str(e),
            }

            rows.append(error_row)

            append_progress_csv(
                progress_path,
                {
                    "timestamp": now_str(),
                    "index": idx,
                    "total": total,
                    "stock_code": stock_code,
                    "status": "ERROR",
                    "verdict": "ERROR",
                    "score": 0,
                    "elapsed_sec": round(elapsed, 2),
                    "error": str(e),
                    "json_path": "",
                },
            )

            print(
                f"[{idx}/{total}] ERROR stock_code={stock_code} "
                f"elapsed={elapsed:.1f}s error={e}"
            )

        # 종목 하나 끝날 때마다 summary도 중간 저장
        pd.DataFrame(rows).to_csv(
            summary_path,
            index=False,
            encoding="utf-8-sig",
        )

        total_elapsed = time.time() - batch_start
        avg_sec = total_elapsed / idx
        remain = total - idx
        eta_sec = remain * avg_sec

        print(
            f"[PROGRESS] {idx}/{total} "
            f"avg={avg_sec:.1f}s/stock "
            f"eta={eta_sec/60:.1f}min"
        )

    summary = pd.DataFrame(rows)
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    print(f"[BATCH DONE] elapsed={(time.time() - batch_start) / 60:.1f}min")
    print(f"[SAVED] {summary_path}")
    print(f"[SAVED] {progress_path}")

    return summary


# ============================================================
# CLI
# ============================================================

def add_zai_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--zai-mode",
        choices=["none", "warn_fail", "all"],
        default="none",
        help="none=Z.AI 미사용, warn_fail=WARN/FAIL만 호출, all=전체 호출",
    )
    parser.add_argument("--canonical", default="", help="CanonicalAccount.csv 경로")
    parser.add_argument("--zai-api-key", default="")
    parser.add_argument("--zai-base-url", default="")
    parser.add_argument("--zai-model", default="")
    parser.add_argument("--zai-temperature", type=float, default=0.1)
    parser.add_argument("--zai-max-tokens", type=int, default=4096)
    parser.add_argument("--zai-retry", type=int, default=3)
    parser.add_argument("--zai-retry-sleep-sec", type=float, default=5.0)
    parser.add_argument("--zai-max-context-chars", type=int, default=120_000)
    parser.add_argument(
        "--zai-dry-run",
        action="store_true",
        help="Z.AI 호출은 하지 않고 zai_context.json / zai_prompt.txt만 저장",
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    sub = parser.add_subparsers(dest="cmd", required=True)

    aggregate = sub.add_parser("aggregate-suggestions")
    aggregate.add_argument("--input-dir", required=True)
    aggregate.add_argument("--out-dir", required=True)
    aggregate.add_argument("--min-rule-count", type=int, default=2)
    aggregate.add_argument("--min-canonical-count", type=int, default=2)

    one = sub.add_parser("one")
    one.add_argument("--normalized", required=True)
    one.add_argument("--debug", required=True)
    one.add_argument("--rules", required=True)
    one.add_argument("--out-dir", required=True)
    add_zai_args(one)

    batch = sub.add_parser("batch")
    batch.add_argument("--input-dir", required=True)
    batch.add_argument("--rules", required=True)
    batch.add_argument("--out-dir", required=True)
    add_zai_args(batch)

    stock = sub.add_parser("stock")
    stock.add_argument("--stock-code", required=True)
    stock.add_argument("--input-dir", required=True)
    stock.add_argument("--rules", required=True)
    stock.add_argument("--out-dir", required=True)
    stock.add_argument("--start-year", type=int, required=True)
    stock.add_argument("--end-year", type=int, required=True)
    add_zai_args(stock)

    stock_batch = sub.add_parser("stock-batch")
    stock_batch.add_argument("--input-dir", required=True)
    stock_batch.add_argument("--rules", required=True)
    stock_batch.add_argument("--out-dir", required=True)
    stock_batch.add_argument("--start-year", type=int, required=True)
    stock_batch.add_argument("--end-year", type=int, required=True)
    stock_batch.add_argument("--stock-codes", nargs="*", default=[])
    stock_batch.add_argument(
        "--resume",
        action="store_true",
        help="이미 생성된 종목 단위 validation.json이 있으면 해당 종목 skip",
    )
    add_zai_args(stock_batch)

    args = parser.parse_args()
    zai_config = build_zai_config_from_args(args)

    canonical_csv = getattr(args, "canonical", "") or None

    if args.cmd == "one":
        result = evaluate_one_to_files(
            normalized_csv=args.normalized,
            debug_csv=args.debug,
            validation_rule_path=args.rules,
            out_dir=args.out_dir,
            canonical_csv=canonical_csv,
            zai_mode=args.zai_mode,
            zai_config=zai_config,
            zai_dry_run=args.zai_dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif args.cmd == "aggregate-suggestions":
        result = aggregate_zai_suggestions(
            input_dir=args.input_dir,
            out_dir=args.out_dir,
            min_rule_count=args.min_rule_count,
            min_canonical_count=args.min_canonical_count,
        )
        print(json.dumps({
            "eval_file_count": result["eval_file_count"],
            "rule_candidate_count": result["rule_candidate_count"],
            "canonical_candidate_count": result["canonical_candidate_count"],
            "out_dir": args.out_dir,
        }, ensure_ascii=False, indent=2))
    
    elif args.cmd == "batch":
        summary = evaluate_batch(
            input_dir=args.input_dir,
            validation_rule_path=args.rules,
            out_dir=args.out_dir,
            canonical_csv=canonical_csv,
            zai_mode=args.zai_mode,
            zai_config=zai_config,
            zai_dry_run=args.zai_dry_run,
        )
        print(summary.to_string(index=False))

    elif args.cmd == "stock":
        result = evaluate_stock_to_files(
            stock_code=args.stock_code,
            input_dir=args.input_dir,
            validation_rule_path=args.rules,
            out_dir=args.out_dir,
            start_year=args.start_year,
            end_year=args.end_year,
            canonical_csv=canonical_csv,
            zai_mode=args.zai_mode,
            zai_config=zai_config,
            zai_dry_run=args.zai_dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "stock-batch":
        summary = evaluate_stock_batch_to_files(
            input_dir=args.input_dir,
            validation_rule_path=args.rules,
            out_dir=args.out_dir,
            start_year=args.start_year,
            end_year=args.end_year,
            stock_codes=args.stock_codes,
            canonical_csv=canonical_csv,
            zai_mode=args.zai_mode,
            zai_config=zai_config,
            zai_dry_run=args.zai_dry_run,
            resume=args.resume,
        )
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()