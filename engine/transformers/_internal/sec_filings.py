from __future__ import annotations

import csv
import contextlib
import io
import json
import math
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml

from engine.core.identifiers import security_id_of
from engine.core.paths import DATA_LAKE, first_existing_path, statement_symbol_name
from engine.markets.us import US_MARKET_CONFIG
from engine.transformers._internal.statement_files import (
    add_statement_period_columns,
    consolidated_statement_path,
    legacy_statement_snapshot_files,
)
from engine.transformers._internal.dart_filings import (
    DEBUG_COLUMNS,
    EXPECTED_HEADER,
    apply_cash_direction,
    safe_str,
)
from engine.transformers._internal.edgar_identity import configure_edgar_identity


US_MAPPING_RULE_PATH = first_existing_path(
    DATA_LAKE.rules("us_mapping.yaml"),
    DATA_LAKE.rules("mapping_us.yaml"),
)
US_COMPANYFACTS_DIR = DATA_LAKE.bronze("sec", "companyfacts")
US_NOTES_DATASET_DIR = DATA_LAKE.bronze("sec", "financial-statement-and-notes-data-set")
US_NORMALIZED_DIR = DATA_LAKE.silver("sec", "normalized")
US_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
US_REPORT_METADATA_PATH = DATA_LAKE.silver("sec", "us_report_metadata.csv")

ALLOWED_SEC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
SOURCE_PRIORITY = {
    "companyfacts_primary": 0,
    "companyfacts_alternate": 1,
    "companyfacts_label": 2,
    "notes": 3,
    "edgartools": 4,
    "derived_formula": 5,
}
EPS_CANONICAL_IDS = {"BASIC_EPS", "DILUTED_EPS"}
SHARE_CANONICAL_IDS = {
    "BASIC_SHARES",
    "DILUTED_SHARES",
    "COMMON_SHARES_OUTSTANDING",
}
DEFAULT_LABEL_EXCLUDE_NAMESPACES = {"us-gaap", "srt", "dei", "country", "exch"}


@dataclass(frozen=True)
class SecFactCandidate:
    symbol: str
    cik: str
    entity_name: str
    canonical_id: str
    canonical_name: str
    statement_type: str
    fiscal_year: int
    fiscal_month: int
    value: float
    raw_value: float
    period_end: str
    filed: str
    accn: str
    form: str
    fp: str
    source: str
    rule_id: str
    reason: str
    original_account_name: str
    amount_policy: str
    cash_direction: str = ""

    @property
    def period(self) -> str:
        return f"{self.fiscal_year}.{self.fiscal_month:02d}"

    @property
    def source_rank(self) -> int:
        return SOURCE_PRIORITY.get(self.source, 99)


@dataclass(frozen=True)
class CompanyFactsExtractResult:
    path: str
    symbol: str
    cik: str
    entity_name: str
    candidates: list[SecFactCandidate]
    has_usable_facts: bool
    error: str = ""


EdgarToolsProvider = Callable[
    [str, str, str, list[dict[str, Any]], int, int],
    list[dict[str, Any]],
]

_COMPANYFACTS_WORKER_RULES: list[dict[str, Any]] = []
_COMPANYFACTS_WORKER_CANONICAL_NAMES: dict[str, str] = {}
_COMPANYFACTS_WORKER_START_YEAR = 0
_COMPANYFACTS_WORKER_END_YEAR = 0


def load_us_mapping_rules(path: str | Path = US_MAPPING_RULE_PATH) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    for key in ["companyfacts_rules", "notes_rules", "edgartools_fallback_rules"]:
        value = data.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list: {path}")

    return data


def load_sec_ticker_map(path: str | Path = US_TICKER_MAP_PATH) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["cik", "ticker", "title"])

    df = pd.read_csv(path, dtype=str).fillna("")
    columns = {column.lower(): column for column in df.columns}
    rename_map = {}
    for canonical, aliases in {
        "cik": ["cik", "cik_str"],
        "ticker": ["ticker", "symbol"],
        "title": ["title", "name", "company_name"],
    }.items():
        for alias in aliases:
            if alias in columns:
                rename_map[columns[alias]] = canonical
                break
    df = df.rename(columns=rename_map)
    for column in ["cik", "ticker", "title"]:
        if column not in df.columns:
            df[column] = ""
    df["cik"] = df["cik"].map(normalize_cik)
    df["ticker"] = df["ticker"].map(lambda value: US_MARKET_CONFIG.normalize_symbol(value))
    return df[["cik", "ticker", "title"]].drop_duplicates()


def normalize_cik(value: Any) -> str:
    text = safe_str(value).strip()
    if text.upper().startswith("CIK"):
        text = text[3:]
    text = re.sub(r"\D", "", text)
    return str(int(text)) if text else ""


def cik_file_key(value: Any) -> str:
    cik = normalize_cik(value)
    return f"CIK{int(cik):010d}" if cik else "CIK0000000000"


def fiscal_month_from_fp(fp: Any) -> int | None:
    fp_text = safe_str(fp).strip().upper()
    return {
        "Q1": 3,
        "Q2": 6,
        "Q3": 9,
        "Q4": 12,
        "FY": 12,
    }.get(fp_text)


def normalize_tag_name(value: Any) -> str:
    text = safe_str(value).strip()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text


def split_tag_spec(value: Any) -> tuple[str | None, str]:
    text = safe_str(value).strip()
    if ":" in text:
        namespace, tag = text.split(":", 1)
        return namespace, tag
    return None, text


def canonical_name_map(canonical_csv_path: str | Path | None = None) -> dict[str, str]:
    path = canonical_csv_path or DATA_LAKE.canonical_accounts()
    df = pd.read_csv(path, dtype=str).fillna("")
    missing = {"canonical_id", "canonical_nm"} - set(df.columns)
    if missing:
        raise ValueError(f"Canonical CSV missing columns: {missing}")
    return dict(zip(df["canonical_id"].astype(str), df["canonical_nm"].astype(str)))


def apply_amount_policy_numeric(value: float, policy: str) -> float:
    policy = safe_str(policy).strip() or "as_reported"
    if policy == "abs":
        return abs(value)
    if policy == "neg_abs":
        return -abs(value)
    return value


def format_amount(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return safe_str(value)

    if math.isnan(number) or math.isinf(number):
        return ""
    if number.is_integer():
        return str(int(number))
    return format(number, ".15g")


def _fact_units_for_rule(fact: dict[str, Any], canonical_id: str) -> list[tuple[str, list[dict[str, Any]]]]:
    units = fact.get("units", {}) or {}
    if canonical_id in EPS_CANONICAL_IDS:
        preferred = ["USD/shares", "USD / shares", "USD/share", "USD"]
    elif canonical_id in SHARE_CANONICAL_IDS:
        preferred = ["shares"]
    else:
        preferred = ["USD"]

    found = [(unit, units[unit]) for unit in preferred if unit in units and isinstance(units[unit], list)]
    if found:
        return found

    return [
        (unit, rows)
        for unit, rows in units.items()
        if isinstance(rows, list) and rows
    ][:1]


def _find_company_fact(facts: dict[str, Any], tag_spec: str) -> tuple[str, str, dict[str, Any]] | None:
    namespace, tag = split_tag_spec(tag_spec)
    if namespace:
        namespace_facts = facts.get(namespace)
        if isinstance(namespace_facts, dict) and tag in namespace_facts:
            return namespace, tag, namespace_facts[tag]
        return None

    for ns, ns_facts in facts.items():
        if isinstance(ns_facts, dict) and tag in ns_facts:
            return ns, tag, ns_facts[tag]
    return None


def _label_text_for_fact(namespace: str, tag: str, fact: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in [
            namespace,
            tag,
            safe_str(fact.get("label")),
            safe_str(fact.get("description")),
        ]
        if part
    )


def _fact_matches_label_rule(
    *,
    namespace: str,
    tag: str,
    fact: dict[str, Any],
    rule: dict[str, Any],
) -> bool:
    label_patterns = _compile_patterns(rule.get("label_patterns", []))
    if not label_patterns:
        return False

    excluded_namespaces = {
        safe_str(item).strip()
        for item in (rule.get("label_exclude_namespaces") or DEFAULT_LABEL_EXCLUDE_NAMESPACES)
        if safe_str(item).strip()
    }
    if namespace in excluded_namespaces:
        return False

    namespaces = rule.get("label_namespaces")
    if namespaces:
        allowed_namespaces = {safe_str(item).strip() for item in namespaces if safe_str(item).strip()}
        if namespace not in allowed_namespaces:
            return False

    label_text = _label_text_for_fact(namespace, tag, fact)
    if not any(pattern.search(label_text) for pattern in label_patterns):
        return False

    exclude_patterns = _compile_patterns(rule.get("label_exclude_patterns", []))
    return not any(pattern.search(label_text) for pattern in exclude_patterns)


def _find_company_facts_by_label(
    facts: dict[str, Any],
    rule: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for namespace, ns_facts in facts.items():
        if not isinstance(ns_facts, dict):
            continue
        for tag, fact in ns_facts.items():
            if isinstance(fact, dict) and _fact_matches_label_rule(
                namespace=safe_str(namespace),
                tag=safe_str(tag),
                fact=fact,
                rule=rule,
            ):
                matches.append((safe_str(namespace), safe_str(tag), fact))
    return matches


def _candidate_from_companyfacts_unit(
    *,
    symbol: str,
    cik: str,
    entity_name: str,
    canonical_names: dict[str, str],
    rule: dict[str, Any],
    source: str,
    namespace: str,
    tag: str,
    fact: dict[str, Any],
    unit_row: dict[str, Any],
) -> SecFactCandidate | None:
    form = safe_str(unit_row.get("form")).strip().upper()
    if form not in ALLOWED_SEC_FORMS:
        return None

    fiscal_year_raw = unit_row.get("fy")
    try:
        fiscal_year = int(fiscal_year_raw)
    except Exception:
        return None

    fiscal_month = fiscal_month_from_fp(unit_row.get("fp"))
    if fiscal_month is None:
        return None

    try:
        raw_value = float(unit_row.get("val"))
    except Exception:
        return None

    amount_policy = safe_str(rule.get("amount_policy")) or "as_reported"
    normalized_value = apply_amount_policy_numeric(raw_value, amount_policy)
    canonical_id = safe_str(rule.get("canonical_id")).strip()
    label = safe_str(fact.get("label")) or tag
    full_tag = f"{namespace}:{tag}"
    return SecFactCandidate(
        symbol=symbol,
        cik=cik,
        entity_name=entity_name,
        canonical_id=canonical_id,
        canonical_name=canonical_names.get(canonical_id, "미매핑"),
        statement_type=safe_str(rule.get("fs_type")) or "UNKNOWN",
        fiscal_year=fiscal_year,
        fiscal_month=fiscal_month,
        value=normalized_value,
        raw_value=raw_value,
        period_end=safe_str(unit_row.get("end") or unit_row.get("ddate")),
        filed=safe_str(unit_row.get("filed")),
        accn=safe_str(unit_row.get("accn")),
        form=form,
        fp=safe_str(unit_row.get("fp")),
        source=source,
        rule_id=f"{source}:{canonical_id}:{full_tag}",
        reason=f"{source} tag match: {full_tag}",
        original_account_name=label,
        amount_policy=amount_policy,
        cash_direction=safe_str(rule.get("cash_direction")),
    )


def extract_companyfacts_candidates(
    companyfacts_path: str | Path,
    *,
    symbol: str,
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
) -> list[SecFactCandidate]:
    path = Path(companyfacts_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return extract_companyfacts_candidates_from_data(
        data,
        companyfacts_path=path,
        symbol=symbol,
        rules=rules,
        canonical_names=canonical_names,
        start_year=start_year,
        end_year=end_year,
    )


def extract_companyfacts_candidates_from_data(
    data: dict[str, Any],
    *,
    companyfacts_path: str | Path,
    symbol: str,
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
) -> list[SecFactCandidate]:
    path = Path(companyfacts_path)
    cik = normalize_cik(data.get("cik")) or normalize_cik(path.stem)
    entity_name = safe_str(data.get("entityName"))
    facts = data.get("facts", {}) or {}
    candidates: list[SecFactCandidate] = []

    for rule in rules:
        for source, field_name in [
            ("companyfacts_primary", "primary_tags"),
            ("companyfacts_alternate", "alternate_tags"),
            ("companyfacts_label", "label_matches"),
        ]:
            if field_name == "label_matches":
                matched_facts = _find_company_facts_by_label(facts, rule)
            else:
                matched_facts = [
                    matched
                    for tag_spec in rule.get(field_name, []) or []
                    if (matched := _find_company_fact(facts, tag_spec)) is not None
                ]

            for namespace, tag, fact in matched_facts:
                for _, unit_rows in _fact_units_for_rule(fact, safe_str(rule.get("canonical_id"))):
                    for unit_row in unit_rows:
                        candidate = _candidate_from_companyfacts_unit(
                            symbol=symbol,
                            cik=cik,
                            entity_name=entity_name,
                            canonical_names=canonical_names,
                            rule=rule,
                            source=source,
                            namespace=namespace,
                            tag=tag,
                            fact=fact,
                            unit_row=unit_row,
                        )
                        if candidate is None:
                            continue
                        if start_year <= candidate.fiscal_year <= end_year:
                            candidates.append(candidate)

    return candidates


def companyfacts_data_has_usable_facts(data: dict[str, Any]) -> bool:
    facts = data.get("facts")
    if not isinstance(facts, dict):
        return False

    return any(
        isinstance(namespace_facts, dict) and bool(namespace_facts)
        for namespace_facts in facts.values()
    )


def companyfacts_has_usable_facts(companyfacts_path: str | Path) -> bool:
    try:
        data = json.loads(Path(companyfacts_path).read_text(encoding="utf-8"))
    except Exception:
        return False

    return companyfacts_data_has_usable_facts(data)


def extract_companyfacts_file(
    path: str | Path,
    symbol: str,
    cik: str,
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
) -> CompanyFactsExtractResult:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates = extract_companyfacts_candidates_from_data(
            data,
            companyfacts_path=path,
            symbol=symbol,
            rules=rules,
            canonical_names=canonical_names,
            start_year=start_year,
            end_year=end_year,
        )
    except Exception as exc:
        return CompanyFactsExtractResult(
            path=str(path),
            symbol=symbol,
            cik=cik,
            entity_name="",
            candidates=[],
            has_usable_facts=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    entity_name = ""
    if candidates:
        entity_name = candidates[0].entity_name
    else:
        entity_name = safe_str(data.get("entityName"))

    return CompanyFactsExtractResult(
        path=str(path),
        symbol=symbol,
        cik=cik,
        entity_name=entity_name,
        candidates=candidates,
        has_usable_facts=companyfacts_data_has_usable_facts(data),
    )


def _init_companyfacts_worker(
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
) -> None:
    global _COMPANYFACTS_WORKER_RULES
    global _COMPANYFACTS_WORKER_CANONICAL_NAMES
    global _COMPANYFACTS_WORKER_START_YEAR
    global _COMPANYFACTS_WORKER_END_YEAR

    _COMPANYFACTS_WORKER_RULES = rules
    _COMPANYFACTS_WORKER_CANONICAL_NAMES = canonical_names
    _COMPANYFACTS_WORKER_START_YEAR = start_year
    _COMPANYFACTS_WORKER_END_YEAR = end_year


def _extract_companyfacts_file_worker(args: tuple[Path, str, str]) -> CompanyFactsExtractResult:
    path, symbol, cik = args
    return extract_companyfacts_file(
        path,
        symbol,
        cik,
        _COMPANYFACTS_WORKER_RULES,
        _COMPANYFACTS_WORKER_CANONICAL_NAMES,
        _COMPANYFACTS_WORKER_START_YEAR,
        _COMPANYFACTS_WORKER_END_YEAR,
    )


def _resolve_worker_count(workers: int | None) -> int:
    if workers is None:
        return 1
    workers = int(workers)
    if workers == 0:
        return max(1, os.cpu_count() or 1)
    return max(1, workers)


def _should_log_progress(processed_count: int, total_count: int, progress_interval: int) -> bool:
    if not total_count:
        return False
    if processed_count == total_count:
        return True
    return progress_interval > 0 and processed_count % progress_interval == 0


def _log_companyfacts_progress(
    *,
    processed_count: int,
    total_count: int,
    candidate_count: int,
    empty_count: int,
    failed_count: int,
    started_at: float,
    progress_interval: int,
) -> None:
    if not _should_log_progress(processed_count, total_count, progress_interval):
        return

    elapsed = time.monotonic() - started_at
    print(
        "[PROGRESS] companyfacts "
        f"processed={processed_count}/{total_count}, "
        f"candidates={candidate_count}, empty={empty_count}, "
        f"failed={failed_count}, elapsed={elapsed:.1f}s"
    )


def extract_companyfacts_files(
    files: list[tuple[Path, str, str]],
    *,
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
    workers: int = 1,
    log_progress: bool = True,
    progress_interval: int = 100,
) -> list[CompanyFactsExtractResult]:
    total_count = len(files)
    if not total_count:
        return []

    started_at = time.monotonic()
    worker_count = min(_resolve_worker_count(workers), total_count)
    tasks = [(path, symbol, cik) for path, symbol, cik in files]
    if log_progress:
        mode = "multiprocess" if worker_count > 1 else "single-process"
        print(
            "[INFO] companyfacts extraction start "
            f"files={total_count}, workers={worker_count}, mode={mode}"
        )

    results: list[CompanyFactsExtractResult] = []
    processed_count = 0
    candidate_count = 0
    empty_count = 0
    failed_count = 0

    def collect(result: CompanyFactsExtractResult) -> None:
        nonlocal processed_count, candidate_count, empty_count, failed_count
        results.append(result)
        processed_count += 1
        candidate_count += len(result.candidates)
        if result.error:
            failed_count += 1
            print(f"[WARN] companyfacts skipped: {result.path} ({result.error})")
        elif not result.has_usable_facts:
            empty_count += 1
        if log_progress:
            _log_companyfacts_progress(
                processed_count=processed_count,
                total_count=total_count,
                candidate_count=candidate_count,
                empty_count=empty_count,
                failed_count=failed_count,
                started_at=started_at,
                progress_interval=progress_interval,
            )

    if worker_count <= 1:
        for path, symbol, cik in tasks:
            collect(
                extract_companyfacts_file(
                    path,
                    symbol,
                    cik,
                    rules,
                    canonical_names,
                    start_year,
                    end_year,
                )
            )
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_companyfacts_worker,
            initargs=(rules, canonical_names, start_year, end_year),
        ) as executor:
            futures = [executor.submit(_extract_companyfacts_file_worker, task) for task in tasks]
            for future in as_completed(futures):
                collect(future.result())

    if log_progress:
        elapsed = time.monotonic() - started_at
        print(
            "[INFO] companyfacts extraction done "
            f"files={total_count}, candidates={candidate_count}, "
            f"empty={empty_count}, failed={failed_count}, elapsed={elapsed:.1f}s"
        )

    return results


def _compile_patterns(values: Any) -> list[re.Pattern[str]]:
    raw_values = values if isinstance(values, list) else [values]
    return [re.compile(safe_str(value)) for value in raw_values if safe_str(value).strip()]


def _notes_rule_tag_patterns(rule: dict[str, Any]) -> list[re.Pattern[str]]:
    return _compile_patterns(rule.get("tag_patterns", []))


def _notes_rule_tag_exclude_patterns(rule: dict[str, Any]) -> list[re.Pattern[str]]:
    return _compile_patterns(rule.get("tag_exclude_patterns", []))


def _notes_rule_matches_tag(
    rule: dict[str, Any],
    tag: Any,
    tag_patterns: list[re.Pattern[str]] | None = None,
) -> bool:
    normalized_tag = normalize_tag_name(tag)
    exclude_patterns = _notes_rule_tag_exclude_patterns(rule)
    if exclude_patterns and any(pattern.search(normalized_tag) for pattern in exclude_patterns):
        return False

    exact_tags = {
        normalize_tag_name(value)
        for value in (rule.get("tags", []) or [])
        if safe_str(value).strip()
    }
    if normalized_tag in exact_tags:
        return True

    patterns = tag_patterns if tag_patterns is not None else _notes_rule_tag_patterns(rule)
    return any(pattern.search(normalized_tag) for pattern in patterns)


def _read_tsv_if_exists(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False, **kwargs)


def _load_notes_submissions(
    notes_dir: Path,
    *,
    ciks: set[str],
    start_year: int,
    end_year: int,
) -> pd.DataFrame:
    sub = _read_tsv_if_exists(
        notes_dir / "sub.tsv",
        usecols=lambda column: column in {"adsh", "cik", "name", "form", "period", "fy", "fp", "filed"},
    )
    if sub.empty:
        return sub

    sub["cik"] = sub["cik"].map(normalize_cik)
    sub["form"] = sub["form"].astype(str).str.upper()
    sub["fy_num"] = pd.to_numeric(sub.get("fy"), errors="coerce")
    sub = sub.loc[
        sub["cik"].isin(ciks)
        & sub["form"].isin(ALLOWED_SEC_FORMS)
        & sub["fy_num"].between(start_year, end_year)
    ].copy()
    sub["fiscal_month"] = sub["fp"].map(fiscal_month_from_fp)
    sub = sub.dropna(subset=["fiscal_month"])
    sub["fiscal_month"] = sub["fiscal_month"].astype(int)
    return sub


def _load_notes_labels(notes_dir: Path, needed_adsh: set[str], needed_tags: set[str]) -> pd.DataFrame:
    tag_df = _read_tsv_if_exists(
        notes_dir / "tag.tsv",
        usecols=lambda column: column in {"tag", "version", "tlabel", "doc"},
    )
    if tag_df.empty:
        tag_df = pd.DataFrame(columns=["tag", "version", "tlabel", "doc"])

    pre_parts: list[pd.DataFrame] = []
    pre_path = notes_dir / "pre.tsv"
    if pre_path.exists() and needed_adsh and needed_tags:
        for chunk in pd.read_csv(
            pre_path,
            sep="\t",
            dtype=str,
            keep_default_na=False,
            chunksize=250_000,
            usecols=lambda column: column in {"adsh", "report", "line", "stmt", "tag", "version", "plabel"},
        ):
            part = chunk.loc[chunk["adsh"].isin(needed_adsh) & chunk["tag"].isin(needed_tags)].copy()
            if not part.empty:
                pre_parts.append(part)
    pre_df = pd.concat(pre_parts, ignore_index=True) if pre_parts else pd.DataFrame()

    if pre_df.empty:
        return tag_df.assign(report="", line="", stmt="", plabel="")

    ren_df = _read_tsv_if_exists(
        notes_dir / "ren.tsv",
        usecols=lambda column: column in {"adsh", "report", "shortname", "longname"},
    )
    if not ren_df.empty:
        pre_df = pre_df.merge(ren_df, on=["adsh", "report"], how="left")

    return pre_df.merge(tag_df, on=["tag", "version"], how="left")


def extract_notes_candidates(
    notes_root: str | Path,
    *,
    cik_to_symbol: dict[str, str],
    cik_to_name: dict[str, str],
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
) -> list[SecFactCandidate]:
    root = Path(notes_root)
    if not root.exists() or not rules:
        return []

    ciks = set(cik_to_symbol)
    tag_to_rules: dict[str, list[dict[str, Any]]] = {}
    pattern_rules: list[tuple[dict[str, Any], list[re.Pattern[str]]]] = []
    for rule in rules:
        for tag in rule.get("tags", []) or []:
            tag_to_rules.setdefault(normalize_tag_name(tag), []).append(rule)
        tag_patterns = _notes_rule_tag_patterns(rule)
        if tag_patterns:
            pattern_rules.append((rule, tag_patterns))
    needed_tags = set(tag_to_rules)
    if not ciks or (not needed_tags and not pattern_rules):
        return []

    candidates: list[SecFactCandidate] = []
    for notes_dir in sorted([path for path in root.iterdir() if path.is_dir()]):
        sub = _load_notes_submissions(notes_dir, ciks=ciks, start_year=start_year, end_year=end_year)
        if sub.empty:
            continue

        sub_by_adsh = sub.set_index("adsh").to_dict("index")
        needed_adsh = set(sub_by_adsh)
        num_parts: list[pd.DataFrame] = []
        num_path = notes_dir / "num.tsv"
        if not num_path.exists():
            continue

        for chunk in pd.read_csv(
            num_path,
            sep="\t",
            dtype=str,
            keep_default_na=False,
            chunksize=250_000,
            usecols=lambda column: column in {"adsh", "tag", "version", "ddate", "uom", "dimh", "value"},
        ):
            tag_series = chunk["tag"].map(normalize_tag_name)
            tag_mask = tag_series.isin(needed_tags)
            if pattern_rules:
                pattern_mask = pd.Series(False, index=chunk.index)
                for _, tag_patterns in pattern_rules:
                    pattern_mask |= tag_series.map(
                        lambda value: any(pattern.search(value) for pattern in tag_patterns)
                    )
                tag_mask |= pattern_mask

            part = chunk.loc[
                chunk["adsh"].isin(needed_adsh)
                & tag_mask
                & chunk["uom"].eq("USD")
                & chunk["dimh"].isin({"0x00000000", "0"})
            ].copy()
            if not part.empty:
                num_parts.append(part)

        if not num_parts:
            continue

        num_df = pd.concat(num_parts, ignore_index=True)
        labels = _load_notes_labels(notes_dir, needed_adsh, set(num_df["tag"].unique()))
        if not labels.empty:
            label_cols = [column for column in ["adsh", "tag", "version", "report", "line", "stmt", "plabel", "shortname", "longname", "tlabel", "doc"] if column in labels.columns]
            join_keys = [key for key in ["adsh", "tag", "version"] if key in label_cols and key in num_df.columns]
            if join_keys:
                labels = labels[label_cols].drop_duplicates(join_keys)
                num_df = num_df.merge(labels, on=join_keys, how="left")

        for row in num_df.to_dict("records"):
            sub_row = sub_by_adsh.get(safe_str(row.get("adsh")), {})
            cik = normalize_cik(sub_row.get("cik"))
            symbol = cik_to_symbol.get(cik)
            if not symbol:
                continue

            normalized_tag = normalize_tag_name(row.get("tag"))
            matched_rules = list(tag_to_rules.get(normalized_tag, []))
            for rule, tag_patterns in pattern_rules:
                if _notes_rule_matches_tag(rule, normalized_tag, tag_patterns):
                    matched_rules.append(rule)

            seen_rule_ids: set[int] = set()
            for rule in matched_rules:
                rule_identity = id(rule)
                if rule_identity in seen_rule_ids:
                    continue
                seen_rule_ids.add(rule_identity)
                if not _notes_rule_matches(rule, row):
                    continue
                try:
                    raw_value = float(row.get("value"))
                except Exception:
                    continue
                amount_policy = safe_str(rule.get("amount_policy")) or "as_reported"
                value = apply_amount_policy_numeric(raw_value, amount_policy)
                fiscal_year = int(float(sub_row.get("fy")))
                fiscal_month = int(sub_row.get("fiscal_month"))
                canonical_id = safe_str(rule.get("canonical_id"))
                label = (
                    safe_str(row.get("plabel"))
                    or safe_str(row.get("tlabel"))
                    or safe_str(row.get("tag"))
                )
                candidates.append(
                    SecFactCandidate(
                        symbol=symbol,
                        cik=cik,
                        entity_name=cik_to_name.get(cik, safe_str(sub_row.get("name"))),
                        canonical_id=canonical_id,
                        canonical_name=canonical_names.get(canonical_id, "미매핑"),
                        statement_type=safe_str(rule.get("fs_type")) or "UNKNOWN",
                        fiscal_year=fiscal_year,
                        fiscal_month=fiscal_month,
                        value=value,
                        raw_value=raw_value,
                        period_end=safe_str(row.get("ddate")),
                        filed=safe_str(sub_row.get("filed")),
                        accn=safe_str(row.get("adsh")),
                        form=safe_str(sub_row.get("form")),
                        fp=safe_str(sub_row.get("fp")),
                        source="notes",
                        rule_id=f"notes:{safe_str(rule.get('id'))}:{canonical_id}:{safe_str(row.get('tag'))}",
                        reason=f"SEC Notes Data Sets match: {safe_str(rule.get('id'))}",
                        original_account_name=label,
                        amount_policy=amount_policy,
                        cash_direction=safe_str(rule.get("cash_direction")),
                    )
                )
    return candidates


def _notes_rule_matches(rule: dict[str, Any], row: dict[str, Any]) -> bool:
    label_text = " ".join(
        safe_str(row.get(column))
        for column in ["plabel", "tlabel", "doc"]
        if safe_str(row.get(column))
    )
    report_text = " ".join(
        safe_str(row.get(column))
        for column in ["shortname", "longname", "stmt"]
        if safe_str(row.get(column))
    )

    label_patterns = _compile_patterns(rule.get("label_patterns", []))
    if label_patterns and not any(pattern.search(label_text) for pattern in label_patterns):
        return False

    label_exclude_patterns = _compile_patterns(rule.get("label_exclude_patterns", []))
    if label_exclude_patterns and any(pattern.search(label_text) for pattern in label_exclude_patterns):
        return False

    report_patterns = _compile_patterns(rule.get("report_name_patterns", []))
    if report_patterns and not any(pattern.search(report_text) for pattern in report_patterns):
        return False

    report_exclude_patterns = _compile_patterns(rule.get("report_name_exclude_patterns", []))
    if report_exclude_patterns and any(pattern.search(report_text) for pattern in report_exclude_patterns):
        return False

    return True


def default_edgartools_provider(
    symbol: str,
    cik: str,
    entity_name: str,
    rules: list[dict[str, Any]],
    start_year: int,
    end_year: int,
) -> list[dict[str, Any]]:
    try:
        from edgar import Company, set_identity  # type: ignore
    except Exception as exc:
        print(f"[WARN] edgartools fallback skipped for {symbol}: import failed ({type(exc).__name__})")
        return []

    try:
        configure_edgar_identity(set_identity)
    except Exception:
        pass

    try:
        company_arg = symbol
        if not company_arg or str(company_arg).upper().startswith("CIK"):
            company_arg = int(cik)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            company = Company(company_arg)
            facts_obj = getattr(company, "facts", None)
            facts_obj = facts_obj() if callable(facts_obj) else facts_obj
            if facts_obj is None:
                return []
            to_pandas = getattr(facts_obj, "to_pandas", None)
            if callable(to_pandas):
                df = to_pandas()
            elif isinstance(facts_obj, pd.DataFrame):
                df = facts_obj
            else:
                return []
    except Exception as exc:
        message = str(exc)
        expected_empty_fact_error = (
            "No company facts found" in message
            or "No facts found" in message
            or type(exc).__name__ in {"NoCompanyFactsFound", "NoFactsFound"}
        )
        if not expected_empty_fact_error:
            print(f"[WARN] edgartools fallback skipped for {symbol}: {type(exc).__name__}: {exc}")
        return []

    if df is None or df.empty:
        return []

    rows: list[dict[str, Any]] = []
    lower_columns = {str(column).lower(): column for column in df.columns}
    tag_col = next((lower_columns[name] for name in ["tag", "concept", "name"] if name in lower_columns), None)
    value_col = next((lower_columns[name] for name in ["value", "val"] if name in lower_columns), None)
    fy_col = next((lower_columns[name] for name in ["fy", "fiscal_year", "year"] if name in lower_columns), None)
    fp_col = next((lower_columns[name] for name in ["fp", "fiscal_period", "period"] if name in lower_columns), None)
    filed_col = next((lower_columns[name] for name in ["filed", "filing_date"] if name in lower_columns), None)
    end_col = next((lower_columns[name] for name in ["end", "period_end", "ddate"] if name in lower_columns), None)
    form_col = next((lower_columns[name] for name in ["form"] if name in lower_columns), None)
    accn_col = next((lower_columns[name] for name in ["accn", "accession", "adsh"] if name in lower_columns), None)

    if tag_col is None or value_col is None:
        return []

    wanted_tags = {
        normalize_tag_name(tag): rule
        for rule in rules
        for tag in (rule.get("tags", []) or [])
    }

    for item in df.to_dict("records"):
        tag = normalize_tag_name(item.get(tag_col))
        rule = wanted_tags.get(tag)
        if rule is None:
            continue
        try:
            fiscal_year = int(float(item.get(fy_col))) if fy_col else None
        except Exception:
            fiscal_year = None
        if fiscal_year is None or not (start_year <= fiscal_year <= end_year):
            continue
        fp = safe_str(item.get(fp_col)) if fp_col else "FY"
        fiscal_month = fiscal_month_from_fp(fp) or 12
        rows.append(
            {
                "symbol": symbol,
                "cik": cik,
                "entity_name": entity_name,
                "canonical_id": safe_str(rule.get("canonical_id")),
                "statement_type": safe_str(rule.get("fs_type")) or "UNKNOWN",
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "value": item.get(value_col),
                "period_end": item.get(end_col) if end_col else "",
                "filed": item.get(filed_col) if filed_col else "",
                "accn": item.get(accn_col) if accn_col else "",
                "form": item.get(form_col) if form_col else "",
                "fp": fp,
                "tag": tag,
                "amount_policy": safe_str(rule.get("amount_policy")) or "as_reported",
                "cash_direction": safe_str(rule.get("cash_direction")),
            }
        )
    return rows


def extract_edgartools_candidates(
    *,
    symbols: list[str],
    cik_by_symbol: dict[str, str],
    name_by_symbol: dict[str, str],
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
    provider: EdgarToolsProvider | None = None,
    log_progress: bool = False,
    progress_interval: int = 100,
) -> list[SecFactCandidate]:
    provider = provider or default_edgartools_provider
    candidates: list[SecFactCandidate] = []
    total_count = len(symbols)
    started_at = time.monotonic()
    for index, symbol in enumerate(symbols, start=1):
        cik = cik_by_symbol.get(symbol, "")
        entity_name = name_by_symbol.get(symbol, "")
        for row in provider(symbol, cik, entity_name, rules, start_year, end_year):
            try:
                raw_value = float(row.get("value"))
            except Exception:
                continue
            amount_policy = safe_str(row.get("amount_policy")) or "as_reported"
            value = apply_amount_policy_numeric(raw_value, amount_policy)
            canonical_id = safe_str(row.get("canonical_id"))
            tag = safe_str(row.get("tag"))
            candidates.append(
                SecFactCandidate(
                    symbol=US_MARKET_CONFIG.normalize_symbol(row.get("symbol") or symbol),
                    cik=normalize_cik(row.get("cik") or cik),
                    entity_name=safe_str(row.get("entity_name") or entity_name),
                    canonical_id=canonical_id,
                    canonical_name=canonical_names.get(canonical_id, "미매핑"),
                    statement_type=safe_str(row.get("statement_type")) or "UNKNOWN",
                    fiscal_year=int(row.get("fiscal_year")),
                    fiscal_month=int(row.get("fiscal_month")),
                    value=value,
                    raw_value=raw_value,
                    period_end=safe_str(row.get("period_end")),
                    filed=safe_str(row.get("filed")),
                    accn=safe_str(row.get("accn")),
                    form=safe_str(row.get("form")),
                    fp=safe_str(row.get("fp")),
                    source="edgartools",
                    rule_id=f"edgartools:{canonical_id}:{tag}",
                    reason=f"edgartools fallback tag match: {tag}",
                    original_account_name=tag,
                    amount_policy=amount_policy,
                    cash_direction=safe_str(row.get("cash_direction")),
                )
            )
        if log_progress and _should_log_progress(index, total_count, progress_interval):
            elapsed = time.monotonic() - started_at
            print(
                "[PROGRESS] edgartools "
                f"processed={index}/{total_count}, "
                f"candidates={len(candidates)}, elapsed={elapsed:.1f}s"
            )
    return candidates


def dedupe_candidates(candidates: list[SecFactCandidate]) -> list[SecFactCandidate]:
    def sort_key(candidate: SecFactCandidate) -> tuple[int, str, str]:
        return (candidate.source_rank, candidate.filed, candidate.accn)

    grouped: dict[tuple[str, int, int, str], SecFactCandidate] = {}
    for candidate in sorted(candidates, key=sort_key, reverse=True):
        key = (
            candidate.symbol,
            candidate.fiscal_year,
            candidate.fiscal_month,
            candidate.canonical_id,
        )
        current = grouped.get(key)
        if current is None:
            grouped[key] = candidate
            continue
        if candidate.source_rank < current.source_rank:
            grouped[key] = candidate
        elif candidate.source_rank == current.source_rank and (candidate.filed, candidate.accn) > (current.filed, current.accn):
            grouped[key] = candidate

    return sorted(
        grouped.values(),
        key=lambda c: (c.symbol, c.fiscal_year, c.fiscal_month, c.statement_type, c.canonical_id),
    )


def _formula_candidate(
    *,
    prototype: SecFactCandidate,
    canonical_id: str,
    canonical_names: dict[str, str],
    value: float,
    formula: str,
    statement_type: str,
) -> SecFactCandidate:
    return SecFactCandidate(
        symbol=prototype.symbol,
        cik=prototype.cik,
        entity_name=prototype.entity_name,
        canonical_id=canonical_id,
        canonical_name=canonical_names.get(canonical_id, "Unknown"),
        statement_type=statement_type,
        fiscal_year=prototype.fiscal_year,
        fiscal_month=prototype.fiscal_month,
        value=value,
        raw_value=value,
        period_end=prototype.period_end,
        filed=prototype.filed,
        accn=prototype.accn,
        form=prototype.form,
        fp=prototype.fp,
        source="derived_formula",
        rule_id=f"derived_formula:{canonical_id}:{formula}",
        reason=f"derived formula: {formula}",
        original_account_name=formula,
        amount_policy="as_reported",
        cash_direction="",
    )


def add_formula_derived_candidates(
    candidates: list[SecFactCandidate],
    *,
    canonical_names: dict[str, str],
) -> list[SecFactCandidate]:
    by_period: dict[tuple[str, int, int], dict[str, SecFactCandidate]] = {}
    for candidate in candidates:
        key = (candidate.symbol, candidate.fiscal_year, candidate.fiscal_month)
        by_period.setdefault(key, {})[candidate.canonical_id] = candidate

    formulas: tuple[tuple[str, str, str, str, str], ...] = (
        ("GROSS_PROFIT", "REVENUE", "COGS", "REVENUE - COGS", "IS"),
        ("COGS", "REVENUE", "GROSS_PROFIT", "REVENUE - GROSS_PROFIT", "IS"),
        (
            "OPERATING_INCOME",
            "GROSS_PROFIT",
            "OPERATING_EXPENSES_TOTAL",
            "GROSS_PROFIT - OPERATING_EXPENSES_TOTAL",
            "IS",
        ),
        ("TOTAL_EQUITY", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_ASSETS - TOTAL_LIABILITIES", "BS"),
        ("TOTAL_LIABILITIES", "TOTAL_ASSETS", "TOTAL_EQUITY", "TOTAL_ASSETS - TOTAL_EQUITY", "BS"),
        ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY", "TOTAL_LIABILITIES + TOTAL_EQUITY", "BS"),
        ("PBT", "NET_INCOME", "TAX_EXPENSE", "NET_INCOME + TAX_EXPENSE", "IS"),
        ("TAX_EXPENSE", "PBT", "NET_INCOME", "PBT - NET_INCOME", "IS"),
        ("NET_INCOME", "PBT", "TAX_EXPENSE", "PBT - TAX_EXPENSE", "IS"),
    )
    derived: list[SecFactCandidate] = []
    for period_candidates in by_period.values():
        for target, left_id, right_id, formula, statement_type in formulas:
            if target in period_candidates:
                continue
            left = period_candidates.get(left_id)
            right = period_candidates.get(right_id)
            if left is None or right is None:
                continue
            if formula.endswith(f"{left_id} + {right_id}") or " + " in formula:
                value = left.value + right.value
            else:
                value = left.value - right.value
            prototype = left if left.source_rank <= right.source_rank else right
            candidate = _formula_candidate(
                prototype=prototype,
                canonical_id=target,
                canonical_names=canonical_names,
                value=value,
                formula=formula,
                statement_type=statement_type,
            )
            period_candidates[target] = candidate
            derived.append(candidate)

    return candidates + derived


def candidate_to_rows(candidate: SecFactCandidate) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_amount = candidate.value
    cash_effect_amount = apply_cash_direction(normalized_amount, candidate.cash_direction)
    base = {
        "canonical_account_id": candidate.canonical_id,
        "canonical_account_name": candidate.canonical_name,
        "original_account_name": candidate.original_account_name,
        "statement_type": candidate.statement_type,
        "period": candidate.period,
        "amount": format_amount(normalized_amount),
        "raw_amount": format_amount(candidate.raw_value),
        "normalized_amount": format_amount(normalized_amount),
        "cash_effect_amount": format_amount(cash_effect_amount),
        "amount_policy": candidate.amount_policy,
        "cash_direction": candidate.cash_direction,
        "fiscal_year": candidate.fiscal_year,
        "fiscal_month": candidate.fiscal_month,
        "fiscal_quarter": (candidate.fiscal_month - 1) // 3 + 1,
    }
    debug = dict(base)
    debug.update(
        {
            "rule_id": candidate.rule_id,
            "reason": candidate.reason,
            "raw_account_name": candidate.original_account_name,
            "normalized_name": normalize_tag_name(candidate.original_account_name),
            "indent_level": "0",
            "has_children": "False",
            "section_context": candidate.source,
            "parent_context": candidate.form,
            "context_path": candidate.accn,
            "context_rule_id": candidate.rule_id,
            "context_reason": candidate.reason,
            "amount_raw": format_amount(candidate.raw_value),
            "unit_factor": "1",
            "market": "us",
            "symbol": candidate.symbol,
            "security_id": security_id_of(candidate.symbol, US_MARKET_CONFIG),
            "cik": candidate.cik,
            "entity_name": candidate.entity_name,
            "fiscal_year": candidate.fiscal_year,
            "fiscal_month": candidate.fiscal_month,
            "period_end": candidate.period_end,
            "filed": candidate.filed,
            "accn": candidate.accn,
            "form": candidate.form,
            "fp": candidate.fp,
            "source": candidate.source,
        }
    )
    return base, debug


def write_symbol_outputs(
    candidates: list[SecFactCandidate],
    *,
    output_dir: str | Path = US_NORMALIZED_DIR,
    save_debug: bool = True,
) -> list[Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    by_symbol: dict[str, list[SecFactCandidate]] = {}
    for candidate in candidates:
        by_symbol.setdefault(candidate.symbol, []).append(candidate)

    for symbol, rows in sorted(by_symbol.items()):
        normalized_rows: list[dict[str, Any]] = []
        debug_rows: list[dict[str, Any]] = []
        for candidate in sorted(rows, key=lambda row: (row.fiscal_year, row.fiscal_month, row.statement_type, row.canonical_id)):
            normalized, debug = candidate_to_rows(candidate)
            normalized_rows.append(normalized)
            debug_rows.append(debug)

        output_path = output_dir / statement_symbol_name(symbol, market="us")
        output_frame = _merged_symbol_normalized_frame(
            output_dir=output_dir,
            symbol=symbol,
            normalized_rows=normalized_rows,
        )
        output_frame.to_csv(
            output_path,
            index=False,
            encoding="utf-8-sig",
            quoting=csv.QUOTE_ALL,
        )
        written.append(output_path)
        if save_debug:
            debug_columns = EXPECTED_HEADER + DEBUG_COLUMNS + [
                "market",
                "symbol",
                "security_id",
                "cik",
                "entity_name",
                "fiscal_year",
                "fiscal_month",
                "period_end",
                "filed",
                "accn",
                "form",
                "fp",
                "source",
            ]
            debug_columns = list(dict.fromkeys(debug_columns))
            output_path.with_suffix(".debug.csv").write_text("", encoding="utf-8-sig")
            pd.DataFrame(debug_rows).reindex(columns=debug_columns).to_csv(
                output_path.with_suffix(".debug.csv"),
                index=False,
                encoding="utf-8-sig",
                quoting=csv.QUOTE_ALL,
            )
        _remove_legacy_symbol_outputs(output_dir, symbol)

    return written


def _merged_symbol_normalized_frame(
    *,
    output_dir: Path,
    symbol: str,
    normalized_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    consolidated_path = consolidated_statement_path(output_dir, symbol, market="us")
    if consolidated_path.exists():
        frames.append(pd.read_csv(consolidated_path))

    for path in legacy_statement_snapshot_files(symbol, output_dir, market="us"):
        meta = path.name
        parsed = re.match(r"us_normalized_.+_(\d{4})[._](\d{2})\.csv$", meta, re.IGNORECASE)
        if parsed is None:
            continue
        frames.append(
            add_statement_period_columns(
                pd.read_csv(path),
                int(parsed.group(1)),
                int(parsed.group(2)),
            )
        )

    if normalized_rows:
        frames.append(pd.DataFrame(normalized_rows))

    if not frames:
        return pd.DataFrame(columns=EXPECTED_HEADER)

    output = pd.concat(frames, ignore_index=True)
    output = output.drop_duplicates(
        ["fiscal_year", "fiscal_month", "canonical_account_id"],
        keep="last",
    )
    output = output.sort_values(["fiscal_year", "fiscal_month", "statement_type", "canonical_account_id"], kind="stable")
    return output.reindex(columns=EXPECTED_HEADER)


def _remove_legacy_symbol_outputs(output_dir: Path, symbol: str) -> None:
    for path in legacy_statement_snapshot_files(symbol, output_dir, market="us"):
        debug_path = path.with_suffix(".debug.csv")
        path.unlink(missing_ok=True)
        debug_path.unlink(missing_ok=True)


def write_report_metadata(candidates: list[SecFactCandidate], path: str | Path = US_REPORT_METADATA_PATH) -> None:
    if not candidates:
        return

    rows = []
    seen = set()
    for candidate in candidates:
        key = (candidate.symbol, candidate.fiscal_year, candidate.fiscal_month, candidate.accn)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "security_id": security_id_of(candidate.symbol, US_MARKET_CONFIG),
                "stock_code": candidate.symbol,
                "country": "US",
                "market_mic": US_MARKET_CONFIG.default_market_mic,
                "filing_system": "SEC",
                "fiscal_year": candidate.fiscal_year,
                "fiscal_month": candidate.fiscal_month,
                "period_end_date": candidate.period_end,
                "report_date": candidate.filed or candidate.period_end,
                "rcept_no": candidate.accn,
                "report_name": candidate.form,
                "source_type": "statement",
                "source_url": f"https://www.sec.gov/Archives/edgar/data/{candidate.cik}/{candidate.accn.replace('-', '')}/",
            }
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)


def resolve_companyfacts_files(
    companyfacts_dir: str | Path,
    *,
    symbols: list[str] | None,
    ticker_map: pd.DataFrame,
) -> list[tuple[Path, str, str]]:
    cik_to_symbol = dict(zip(ticker_map["cik"], ticker_map["ticker"])) if not ticker_map.empty else {}
    wanted_symbols = {
        US_MARKET_CONFIG.normalize_symbol(symbol)
        for symbol in (symbols or [])
        if safe_str(symbol).strip()
    }

    files: list[tuple[Path, str, str]] = []
    for path in sorted(Path(companyfacts_dir).glob("CIK*.json")):
        cik = normalize_cik(path.stem)
        symbol = cik_to_symbol.get(cik) or cik_file_key(cik)
        if wanted_symbols and symbol not in wanted_symbols and cik_file_key(cik) not in wanted_symbols:
            continue
        files.append((path, symbol, cik))
    return files


def normalize_us_sec_filings(
    *,
    symbols: list[str] | None = None,
    start_year: int,
    end_year: int,
    companyfacts_dir: str | Path = US_COMPANYFACTS_DIR,
    notes_root: str | Path = US_NOTES_DATASET_DIR,
    output_dir: str | Path = US_NORMALIZED_DIR,
    mapping_rule_path: str | Path = US_MAPPING_RULE_PATH,
    ticker_map_path: str | Path = US_TICKER_MAP_PATH,
    canonical_csv_path: str | Path | None = None,
    report_metadata_path: str | Path = US_REPORT_METADATA_PATH,
    save_debug: bool = True,
    use_notes: bool = True,
    use_edgartools: bool = True,
    edgartools_provider: EdgarToolsProvider | None = None,
    workers: int = 1,
    log_progress: bool = True,
    progress_interval: int = 100,
) -> list[Path]:
    started_at = time.monotonic()
    rules = load_us_mapping_rules(mapping_rule_path)
    canonical_names = canonical_name_map(canonical_csv_path)
    ticker_map = load_sec_ticker_map(ticker_map_path)
    files = resolve_companyfacts_files(companyfacts_dir, symbols=symbols, ticker_map=ticker_map)
    worker_count = min(_resolve_worker_count(workers), len(files) or 1)
    if log_progress:
        print(
            "[INFO] US SEC normalize start "
            f"symbols={len(symbols) if symbols else 'ALL'}, "
            f"companyfacts_files={len(files)}, years={start_year}-{end_year}, "
            f"workers={worker_count}, notes={use_notes}, edgartools={use_edgartools}"
        )
    if ticker_map.empty:
        print(
            f"[WARN] SEC ticker map not found or empty: {ticker_map_path}. "
            "Run `python -m engine.workflows.download --market us sec-tickers` first, "
            "or pass CIK000... symbols."
        )
    if symbols and not files:
        print(f"[WARN] no SEC companyfacts files matched symbols={symbols}")

    candidates: list[SecFactCandidate] = []
    cik_to_symbol: dict[str, str] = {}
    cik_to_name: dict[str, str] = {}
    symbol_to_cik: dict[str, str] = {}
    symbol_to_name: dict[str, str] = {}
    symbols_with_local_facts: set[str] = set()

    companyfacts_results = extract_companyfacts_files(
        files,
        rules=rules.get("companyfacts_rules", []),
        canonical_names=canonical_names,
        start_year=start_year,
        end_year=end_year,
        workers=worker_count,
        log_progress=log_progress,
        progress_interval=progress_interval,
    )

    for result in companyfacts_results:
        if result.error:
            continue
        if result.has_usable_facts:
            symbols_with_local_facts.add(result.symbol)
        if result.candidates:
            candidates.extend(result.candidates)
        cik_to_symbol[result.cik] = result.symbol
        symbol_to_cik[result.symbol] = result.cik
        if result.entity_name:
            cik_to_name[result.cik] = result.entity_name
            symbol_to_name[result.symbol] = result.entity_name

    if use_notes:
        notes_started_at = time.monotonic()
        if log_progress:
            print(
                "[INFO] notes extraction start "
                f"ciks={len(cik_to_symbol)}, root={notes_root}"
            )
        notes_candidates = extract_notes_candidates(
            notes_root,
            cik_to_symbol=cik_to_symbol,
            cik_to_name=cik_to_name,
            rules=rules.get("notes_rules", []),
            canonical_names=canonical_names,
            start_year=start_year,
            end_year=end_year,
        )
        candidates.extend(notes_candidates)
        if log_progress:
            elapsed = time.monotonic() - notes_started_at
            print(
                "[INFO] notes extraction done "
                f"candidates={len(notes_candidates)}, elapsed={elapsed:.1f}s"
            )

    if use_edgartools:
        edgartools_symbols = sorted(symbol_to_cik)
        if edgartools_provider is None:
            original_count = len(edgartools_symbols)
            edgartools_symbols = [
                symbol for symbol in edgartools_symbols if symbol in symbols_with_local_facts
            ]
            skipped_count = original_count - len(edgartools_symbols)
            if skipped_count:
                print(
                    "[INFO] edgartools fallback skipped for "
                    f"{skipped_count} symbols with empty local SEC companyfacts files"
                )

        edgartools_started_at = time.monotonic()
        if log_progress:
            print(f"[INFO] edgartools fallback start symbols={len(edgartools_symbols)}")
        edgartools_candidates = extract_edgartools_candidates(
            symbols=edgartools_symbols,
            cik_by_symbol=symbol_to_cik,
            name_by_symbol=symbol_to_name,
            rules=rules.get("edgartools_fallback_rules", []),
            canonical_names=canonical_names,
            start_year=start_year,
            end_year=end_year,
            provider=edgartools_provider,
            log_progress=log_progress,
            progress_interval=progress_interval,
        )
        candidates.extend(edgartools_candidates)
        if log_progress:
            elapsed = time.monotonic() - edgartools_started_at
            print(
                "[INFO] edgartools fallback done "
                f"candidates={len(edgartools_candidates)}, elapsed={elapsed:.1f}s"
            )

    if log_progress:
        print(f"[INFO] dedupe start candidates={len(candidates)}")
    deduped = dedupe_candidates(candidates)
    derived_count = 0
    deduped_with_derived = add_formula_derived_candidates(
        deduped,
        canonical_names=canonical_names,
    )
    if len(deduped_with_derived) != len(deduped):
        derived_count = len(deduped_with_derived) - len(deduped)
        deduped = dedupe_candidates(deduped_with_derived)
    if log_progress:
        print(f"[INFO] dedupe done candidates={len(deduped)}, derived_formula={derived_count}")
        print(f"[INFO] write outputs start output_dir={output_dir}")
    written = write_symbol_outputs(deduped, output_dir=output_dir, save_debug=save_debug)
    write_report_metadata(deduped, report_metadata_path)
    if log_progress:
        elapsed = time.monotonic() - started_at
        print(
            "[DONE] US SEC normalize "
            f"written={len(written)}, elapsed={elapsed:.1f}s"
        )
    return written
