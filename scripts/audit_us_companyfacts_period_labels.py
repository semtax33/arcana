from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.paths import DATA_LAKE, statement_symbol_name
from engine.transformers._internal.sec_filings import (
    ALLOWED_SEC_FORMS,
    US_COMPANYFACTS_DIR,
    US_MAPPING_RULE_PATH,
    US_NORMALIZED_DIR,
    US_TICKER_MAP_PATH,
    _candidate_from_companyfacts_unit,
    _companyfacts_accession_key,
    _companyfacts_accession_period_ends,
    _companyfacts_end_date,
    _fact_units_for_rule,
    _select_current_companyfacts_unit_rows,
    add_formula_derived_candidates,
    canonical_name_map,
    dedupe_candidates,
    extract_companyfacts_candidates,
    load_sec_ticker_map,
    load_us_mapping_rules,
    resolve_companyfacts_files,
    safe_str,
    split_tag_spec,
)

try:
    import orjson
except ImportError:  # pragma: no cover - the project runtime includes orjson.
    orjson = None


_WORKER_RULES: list[dict[str, Any]] = []
_WORKER_CANONICAL_NAMES: dict[str, str] = {}
_WORKER_START_YEAR = 0
_WORKER_END_YEAR = 0
_WORKER_NORMALIZED_DIR = Path()
_WORKER_EXACT_RULES: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}


def _init_worker(
    rules: list[dict[str, Any]],
    canonical_names: dict[str, str],
    start_year: int,
    end_year: int,
    normalized_dir: str,
) -> None:
    global _WORKER_RULES
    global _WORKER_CANONICAL_NAMES
    global _WORKER_START_YEAR
    global _WORKER_END_YEAR
    global _WORKER_NORMALIZED_DIR
    global _WORKER_EXACT_RULES

    _WORKER_RULES = rules
    _WORKER_CANONICAL_NAMES = canonical_names
    _WORKER_START_YEAR = start_year
    _WORKER_END_YEAR = end_year
    _WORKER_NORMALIZED_DIR = Path(normalized_dir)
    exact_rules: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for rule in rules:
        for source, field_name in (
            ("companyfacts_primary", "primary_tags"),
            ("companyfacts_alternate", "alternate_tags"),
        ):
            for tag_spec in rule.get(field_name, []) or []:
                namespace, tag = split_tag_spec(tag_spec)
                exact_rules.setdefault((namespace or "*", tag), []).append((rule, source))
    _WORKER_EXACT_RULES = exact_rules


def _candidate_map(path: Path, symbol: str) -> dict[tuple[int, int, str], Any]:
    candidates = extract_companyfacts_candidates(
        path,
        symbol=symbol,
        rules=_WORKER_RULES,
        canonical_names=_WORKER_CANONICAL_NAMES,
        start_year=_WORKER_START_YEAR,
        end_year=_WORKER_END_YEAR,
    )
    candidates = dedupe_candidates(candidates)
    candidates = dedupe_candidates(
        add_formula_derived_candidates(
            candidates,
            canonical_names=_WORKER_CANONICAL_NAMES,
        )
    )
    return {
        (candidate.fiscal_year, candidate.fiscal_month, candidate.canonical_id): candidate
        for candidate in candidates
    }


def _read_current_map(path: Path) -> tuple[pd.DataFrame, dict[tuple[int, int, str], float]]:
    if not path.exists():
        return pd.DataFrame(), {}
    try:
        frame = pd.read_csv(path, low_memory=False)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame(), {}
    if frame.empty:
        return frame, {}

    years = pd.to_numeric(frame.get("fiscal_year"), errors="coerce")
    months = pd.to_numeric(frame.get("fiscal_month"), errors="coerce")
    amounts = pd.to_numeric(frame.get("normalized_amount"), errors="coerce")
    accounts = frame.get(
        "canonical_account_id",
        pd.Series("", index=frame.index, dtype="object"),
    ).astype(str)
    current: dict[tuple[int, int, str], float] = {}
    for year, month, account, amount in zip(
        years,
        months,
        accounts,
        amounts,
        strict=True,
    ):
        if pd.isna(year) or pd.isna(month) or pd.isna(amount):
            continue
        year_int = int(year)
        if not _WORKER_START_YEAR <= year_int <= _WORKER_END_YEAR:
            continue
        current[(year_int, int(month), account)] = float(amount)
    return frame, current


def _companyfacts_debug_keys(path: Path) -> set[tuple[int, int, str]]:
    if not path.exists():
        return set()
    try:
        frame = pd.read_csv(path, low_memory=False)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return set()
    if frame.empty:
        return set()

    sources = frame.get(
        "source",
        pd.Series("", index=frame.index, dtype="object"),
    ).astype(str)
    relevant = frame.loc[
        sources.str.startswith("companyfacts_") | sources.eq("derived_formula")
    ].copy()
    years = pd.to_numeric(relevant.get("fiscal_year"), errors="coerce")
    months = pd.to_numeric(relevant.get("fiscal_month"), errors="coerce")
    accounts = relevant.get(
        "canonical_account_id",
        pd.Series("", index=relevant.index, dtype="object"),
    ).astype(str)
    keys: set[tuple[int, int, str]] = set()
    for year, month, account in zip(years, months, accounts, strict=True):
        if pd.isna(year) or pd.isna(month):
            continue
        year_int = int(year)
        if _WORKER_START_YEAR <= year_int <= _WORKER_END_YEAR:
            keys.add((year_int, int(month), account))
    return keys


def _sample_item(
    kind: str,
    key: tuple[int, int, str],
    *,
    expected: float | None = None,
    current: float | None = None,
    candidate: Any | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "fiscal_year": key[0],
        "fiscal_month": key[1],
        "canonical_account_id": key[2],
        "expected": expected,
        "current": current,
    }
    if candidate is not None:
        item.update(
            {
                "period_end": candidate.period_end,
                "filed": candidate.filed,
                "accn": candidate.accn,
                "form": candidate.form,
                "source": candidate.source,
            }
        )
    return item


def _audit_one(task: tuple[Path, str, str]) -> dict[str, Any]:
    path, symbol, cik = task
    try:
        expected = _candidate_map(path, symbol)
        normalized_path = _WORKER_NORMALIZED_DIR / statement_symbol_name(symbol, market="us")
        _, current = _read_current_map(normalized_path)
        debug_keys = _companyfacts_debug_keys(normalized_path.with_suffix(".debug.csv"))

        missing_keys = sorted(set(expected) - set(current))
        stale_keys = sorted((debug_keys & set(current)) - set(expected))
        mismatch_keys = sorted(
            key
            for key in set(expected) & set(current)
            if not math.isclose(
                expected[key].value,
                current[key],
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        )

        changed_keys = missing_keys + mismatch_keys + stale_keys
        samples: list[dict[str, Any]] = []
        for key in missing_keys[:3]:
            samples.append(
                _sample_item(
                    "missing",
                    key,
                    expected=expected[key].value,
                    candidate=expected[key],
                )
            )
        for key in mismatch_keys[:5]:
            samples.append(
                _sample_item(
                    "value_mismatch",
                    key,
                    expected=expected[key].value,
                    current=current[key],
                    candidate=expected[key],
                )
            )
        for key in stale_keys[:3]:
            samples.append(_sample_item("stale", key, current=current[key]))

        years = [key[0] for key in changed_keys]
        return {
            "symbol": symbol,
            "cik": cik,
            "affected": bool(changed_keys),
            "corrected_rows": len(expected),
            "current_rows": len(current),
            "missing_rows": len(missing_keys),
            "value_mismatch_rows": len(mismatch_keys),
            "stale_rows": len(stale_keys),
            "first_affected_year": min(years) if years else "",
            "last_affected_year": max(years) if years else "",
            "samples": json.dumps(samples, ensure_ascii=False, separators=(",", ":")),
            "error": "",
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "cik": cik,
            "affected": False,
            "corrected_rows": 0,
            "current_rows": 0,
            "missing_rows": 0,
            "value_mismatch_rows": 0,
            "stale_rows": 0,
            "first_affected_year": "",
            "last_affected_year": "",
            "samples": "[]",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _load_companyfacts(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    if orjson is not None:
        return orjson.loads(payload)
    return json.loads(payload)


def _fast_detect_one(task: tuple[Path, str, str]) -> dict[str, Any]:
    """Detect a Silver value copied from a non-current comparative fact.

    This pass intentionally stops after the first proven match.  It builds the
    accession period end from all financial facts, replays the former
    tag-local selection, and verifies that the selected comparative amount is
    the amount currently stored in Silver.  Full reconstruction is reserved
    for the affected symbols that will actually be rewritten.
    """

    path, symbol, cik = task
    try:
        normalized_path = _WORKER_NORMALIZED_DIR / statement_symbol_name(symbol, market="us")
        _, current = _read_current_map(normalized_path)
        if not current:
            return {
                "symbol": symbol,
                "cik": cik,
                "affected": False,
                "proof_rows": 0,
                "first_affected_year": "",
                "last_affected_year": "",
                "samples": "[]",
                "error": "",
            }

        data = _load_companyfacts(path)
        facts = data.get("facts", {}) or {}
        entity_name = safe_str(data.get("entityName"))
        matched_units: list[
            tuple[dict[str, Any], str, str, str, dict[str, Any], list[dict[str, Any]]]
        ] = []

        for namespace, namespace_facts in facts.items():
            if not isinstance(namespace_facts, dict):
                continue
            namespace_text = safe_str(namespace)
            for tag, fact in namespace_facts.items():
                if not isinstance(fact, dict):
                    continue
                tag_text = safe_str(tag)
                rule_matches = (
                    _WORKER_EXACT_RULES.get((namespace_text, tag_text), [])
                    + _WORKER_EXACT_RULES.get(("*", tag_text), [])
                )
                for rule, source in rule_matches:
                    for _, unit_rows in _fact_units_for_rule(
                        fact,
                        safe_str(rule.get("canonical_id")),
                    ):
                        ranged_rows = []
                        for unit_row in unit_rows:
                            if safe_str(unit_row.get("form")).strip().upper() not in ALLOWED_SEC_FORMS:
                                continue
                            try:
                                fiscal_year = int(unit_row.get("fy"))
                            except (TypeError, ValueError):
                                continue
                            if _WORKER_START_YEAR <= fiscal_year <= _WORKER_END_YEAR:
                                ranged_rows.append(unit_row)
                        if ranged_rows:
                            matched_units.append(
                                (rule, source, namespace_text, tag_text, fact, ranged_rows)
                            )

        accession_ends = _companyfacts_accession_period_ends(
            [
                (safe_str(rule.get("canonical_id")), unit_rows)
                for rule, _, _, _, _, unit_rows in matched_units
            ]
        )

        proof: list[dict[str, Any]] = []
        for rule, source, namespace, tag, fact, unit_rows in matched_units:
            if safe_str(rule.get("canonical_id")) == "COMMON_SHARES_OUTSTANDING":
                continue
            for unit_row in _select_current_companyfacts_unit_rows(unit_rows):
                accession_key = _companyfacts_accession_key(unit_row)
                accession_end = accession_ends.get(accession_key)
                selected_end = _companyfacts_end_date(unit_row)
                if not accession_end or not selected_end or selected_end >= accession_end:
                    continue
                candidate = _candidate_from_companyfacts_unit(
                    symbol=symbol,
                    cik=cik,
                    entity_name=entity_name,
                    canonical_names=_WORKER_CANONICAL_NAMES,
                    rule=rule,
                    source=source,
                    namespace=namespace,
                    tag=tag,
                    fact=fact,
                    unit_row=unit_row,
                )
                if candidate is None:
                    continue
                key = (candidate.fiscal_year, candidate.fiscal_month, candidate.canonical_id)
                silver_value = current.get(key)
                if silver_value is None or not math.isclose(
                    candidate.value,
                    silver_value,
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    continue
                proof.append(
                    {
                        "kind": "comparative_value_in_silver",
                        "fiscal_year": key[0],
                        "fiscal_month": key[1],
                        "canonical_account_id": key[2],
                        "silver_value": silver_value,
                        "selected_end": selected_end,
                        "accession_period_end": accession_end,
                        "filed": candidate.filed,
                        "accn": candidate.accn,
                        "form": candidate.form,
                        "source": candidate.source,
                    }
                )
                break
            if proof:
                break

        years = [int(item["fiscal_year"]) for item in proof]
        return {
            "symbol": symbol,
            "cik": cik,
            "affected": bool(proof),
            "proof_rows": len(proof),
            "first_affected_year": min(years) if years else "",
            "last_affected_year": max(years) if years else "",
            "samples": json.dumps(proof, ensure_ascii=False, separators=(",", ":")),
            "error": "",
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "cik": cik,
            "affected": False,
            "proof_rows": 0,
            "first_affected_year": "",
            "last_affected_year": "",
            "samples": "[]",
            "error": f"{type(exc).__name__}: {exc}",
        }


def audit_companyfacts_period_labels(
    *,
    symbols: list[str] | None,
    start_year: int,
    end_year: int,
    workers: int,
    output_path: str | Path,
    companyfacts_dir: str | Path = US_COMPANYFACTS_DIR,
    normalized_dir: str | Path = US_NORMALIZED_DIR,
    mapping_rule_path: str | Path = US_MAPPING_RULE_PATH,
    ticker_map_path: str | Path = US_TICKER_MAP_PATH,
    detect_only: bool = False,
) -> pd.DataFrame:
    rules = load_us_mapping_rules(mapping_rule_path).get("companyfacts_rules", [])
    canonical_names = canonical_name_map()
    ticker_map = load_sec_ticker_map(ticker_map_path)
    files = resolve_companyfacts_files(
        companyfacts_dir,
        symbols=symbols,
        ticker_map=ticker_map,
    )
    if detect_only:
        normalized_root = Path(normalized_dir)
        files = [
            task
            for task in files
            if (normalized_root / statement_symbol_name(task[1], market="us")).exists()
        ]
    worker_count = min(max(int(workers), 1), len(files) or 1)
    print(
        "[INFO] US Companyfacts period-label audit "
        f"mode={'detect' if detect_only else 'reconcile'}, "
        f"files={len(files)}, years={start_year}-{end_year}, workers={worker_count}"
    )

    started_at = time.monotonic()
    rows: list[dict[str, Any]] = []
    if worker_count == 1:
        _init_worker(
            rules,
            canonical_names,
            start_year,
            end_year,
            str(normalized_dir),
        )
        for index, task in enumerate(files, start=1):
            rows.append(_fast_detect_one(task) if detect_only else _audit_one(task))
            if index % 100 == 0 or index == len(files):
                affected = sum(bool(row["affected"]) for row in rows)
                errors = sum(bool(row["error"]) for row in rows)
                print(
                    f"[PROGRESS] processed={index}/{len(files)}, "
                    f"affected={affected}, errors={errors}, "
                    f"elapsed={time.monotonic() - started_at:.1f}s"
                )
    else:
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_worker,
            initargs=(
                rules,
                canonical_names,
                start_year,
                end_year,
                str(normalized_dir),
            ),
        ) as executor:
            worker = _fast_detect_one if detect_only else _audit_one
            futures = [executor.submit(worker, task) for task in files]
            for index, future in enumerate(as_completed(futures), start=1):
                rows.append(future.result())
                if index % 100 == 0 or index == len(files):
                    affected = sum(bool(row["affected"]) for row in rows)
                    errors = sum(bool(row["error"]) for row in rows)
                    print(
                        f"[PROGRESS] processed={index}/{len(files)}, "
                        f"affected={affected}, errors={errors}, "
                        f"elapsed={time.monotonic() - started_at:.1f}s"
                    )

    frame = pd.DataFrame(rows).sort_values(["affected", "symbol"], ascending=[False, True])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(
        "[DONE] US Companyfacts period-label audit "
        f"affected={int(frame['affected'].sum()) if not frame.empty else 0}, "
        f"errors={int(frame['error'].astype(bool).sum()) if not frame.empty else 0}, "
        f"output={output_path}"
    )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct SEC labels from accession/end/duration metadata and compare "
            "them with current US normalized statements."
        )
    )
    parser.add_argument("--symbols", help="Comma-separated symbols; default is all local files")
    parser.add_argument("--start-year", type=int, default=1900)
    parser.add_argument("--end-year", type=int, default=2100)
    parser.add_argument("--workers", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help=(
            "Fast proof pass: audit only symbols already present in Silver and stop "
            "after finding a comparative value that Silver actually stores."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DATA_LAKE.meta("us_companyfacts_period_label_audit.csv")),
    )
    args = parser.parse_args()
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    audit_companyfacts_period_labels(
        symbols=symbols,
        start_year=args.start_year,
        end_year=args.end_year,
        workers=args.workers,
        output_path=args.output,
        detect_only=args.detect_only,
    )


if __name__ == "__main__":
    main()
