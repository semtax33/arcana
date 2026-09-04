from __future__ import annotations

import argparse
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup, Tag

from engine.core.paths import DATA_LAKE, statement_symbol_name
from engine.transformers._internal.dart_filings import (
    current_statement_amount_column,
    find_next_data_table,
    get_statement_type_from_text,
    is_eps_account_name,
    is_header_node,
    normalize_account_name,
    normalize_statement_type,
    parse_amount,
    parse_unit_factor,
    safe_str,
)


BRONZE_ROOT = DATA_LAKE.bronze("dart", "finance-statement")
NORMALIZED_ROOT = DATA_LAKE.silver("dart", "normalized")
DEFAULT_OUTPUT = DATA_LAKE.meta("kr_dart_period_column_audit.csv")
_PERIOD_RE = re.compile(r"finance_statement_\((?P<year>\d{4})[.](?P<month>\d{2})\)[.]html$")


def _period_from_path(path: Path) -> str | None:
    match = _PERIOD_RE.match(path.name)
    if match is None:
        return None
    return f"{int(match.group('year'))}.{int(match.group('month'))}"


def _different_amounts(old_value: Any, new_value: Any) -> bool:
    if old_value is None or new_value is None:
        return False
    try:
        return float(old_value) != float(new_value)
    except (TypeError, ValueError):
        return safe_str(old_value) != safe_str(new_value)


def _audit_file(path: Path, symbol: str) -> dict[str, Any] | None:
    period = _period_from_path(path)
    if period is None or period.rsplit(".", 1)[-1] not in {"6", "9"}:
        return None

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "lxml")
    seen_body_tables: set[int] = set()
    for header_node in soup.find_all(["p", "table"]):
        if not isinstance(header_node, Tag) or not is_header_node(header_node):
            continue
        header_text = header_node.get_text(" ", strip=True)
        statement_type = get_statement_type_from_text(header_text)
        if normalize_statement_type(statement_type) not in {"IS", "CIS", "CF"}:
            continue
        if "자본변동표" in normalize_account_name(header_text):
            continue
        body_table, supporting_text = find_next_data_table(header_node)
        if body_table is None or id(body_table) in seen_body_tables:
            continue
        seen_body_tables.add(id(body_table))
        amount_column = current_statement_amount_column(
            body_table,
            statement_type=statement_type,
            period=period,
        )
        if amount_column <= 1:
            continue

        unit_factor = parse_unit_factor(f"{header_text} {supporting_text}".strip())
        for tr in body_table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) <= amount_column:
                continue
            account_name = cells[0].get_text("", strip=False).strip()
            if not account_name:
                continue
            row_unit_factor = 1 if is_eps_account_name(account_name) else unit_factor
            old_raw = cells[1].get_text(" ", strip=True)
            corrected_raw = cells[amount_column].get_text(" ", strip=True)
            old_amount = parse_amount(old_raw, row_unit_factor)
            corrected_amount = parse_amount(corrected_raw, row_unit_factor)
            if not _different_amounts(old_amount, corrected_amount):
                continue
            return {
                "symbol": symbol,
                "affected": True,
                "period": period,
                "statement_type": normalize_statement_type(statement_type),
                "account_name": account_name,
                "old_quarter_amount": safe_str(old_amount),
                "corrected_ytd_amount": safe_str(corrected_amount),
                "amount_column": amount_column,
                "proof_path": str(path),
                "error": "",
            }
    return None


def _audit_symbol(task: tuple[str, list[Path]]) -> dict[str, Any]:
    symbol, paths = task
    try:
        for path in paths:
            proof = _audit_file(path, symbol)
            if proof is not None:
                return proof
        return {
            "symbol": symbol,
            "affected": False,
            "period": "",
            "statement_type": "",
            "account_name": "",
            "old_quarter_amount": "",
            "corrected_ytd_amount": "",
            "amount_column": "",
            "proof_path": "",
            "error": "",
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "affected": False,
            "period": "",
            "statement_type": "",
            "account_name": "",
            "old_quarter_amount": "",
            "corrected_ytd_amount": "",
            "amount_column": "",
            "proof_path": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def audit_kr_period_columns(
    *,
    symbols: list[str] | None,
    workers: int,
    output_path: str | Path,
    bronze_root: str | Path = BRONZE_ROOT,
    normalized_root: str | Path = NORMALIZED_ROOT,
) -> pd.DataFrame:
    bronze_root = Path(bronze_root)
    normalized_root = Path(normalized_root)
    wanted = {str(symbol).strip().zfill(6) for symbol in symbols or []}
    tasks: list[tuple[str, list[Path]]] = []
    for stock_dir in sorted(path for path in bronze_root.iterdir() if path.is_dir()):
        symbol = stock_dir.name.zfill(6)
        if wanted and symbol not in wanted:
            continue
        if not (normalized_root / statement_symbol_name(symbol, market="kr")).exists():
            continue
        paths = sorted(
            (
                path
                for path in stock_dir.glob("finance_statement_(*).html")
                if (_period_from_path(path) or "").rsplit(".", 1)[-1] in {"6", "9"}
            ),
            reverse=True,
        )
        if paths:
            tasks.append((symbol, paths))

    worker_count = min(max(int(workers), 1), len(tasks) or 1)
    print(
        f"[INFO] KR DART period-column audit symbols={len(tasks)}, workers={worker_count}",
        flush=True,
    )
    started_at = time.monotonic()
    rows: list[dict[str, Any]] = []
    if worker_count == 1:
        for index, task in enumerate(tasks, start=1):
            rows.append(_audit_symbol(task))
            if index % 100 == 0 or index == len(tasks):
                print(
                    f"[PROGRESS] processed={index}/{len(tasks)}, "
                    f"affected={sum(bool(row['affected']) for row in rows)}, "
                    f"errors={sum(bool(row['error']) for row in rows)}, "
                    f"elapsed={time.monotonic() - started_at:.1f}s",
                    flush=True,
                )
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_audit_symbol, task) for task in tasks]
            for index, future in enumerate(as_completed(futures), start=1):
                rows.append(future.result())
                if index % 100 == 0 or index == len(tasks):
                    print(
                        f"[PROGRESS] processed={index}/{len(tasks)}, "
                        f"affected={sum(bool(row['affected']) for row in rows)}, "
                        f"errors={sum(bool(row['error']) for row in rows)}, "
                        f"elapsed={time.monotonic() - started_at:.1f}s",
                        flush=True,
                    )

    frame = pd.DataFrame(rows).sort_values(["affected", "symbol"], ascending=[False, True])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(
        f"[DONE] KR DART period-column audit "
        f"affected={int(frame['affected'].sum()) if not frame.empty else 0}, "
        f"errors={int(frame['error'].astype(bool).sum()) if not frame.empty else 0}, "
        f"output={output_path}",
        flush=True,
    )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit DART interim statements for quarter-vs-YTD column mapping."
    )
    parser.add_argument("--symbols", help="Comma-separated KR stock codes")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
    )
    audit_kr_period_columns(
        symbols=symbols,
        workers=args.workers,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
