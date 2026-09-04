from __future__ import annotations

import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.core.paths import DATA_LAKE
from engine.semantic import FactorDependencyGraph, capex_direction_correction
from engine.semantic.corrections import correction_record


DEFAULT_INPUT = DATA_LAKE.silver("dart", "normalized")
DEFAULT_LEDGER = PROJECT_ROOT / "deliverables" / "semantic_correction_ledger_v3.csv"
DEFAULT_SUMMARY = PROJECT_ROOT / "deliverables" / "semantic_correction_ledger_v3_summary.json"
_STOCK_RE = re.compile(r"kr_normalized_(\d{6})\.debug\.csv$")


def hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_path(stock_code: str, period: str) -> Path | None:
    match = re.match(r"^(\d{4})[._](\d{1,2})$", period.strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    path = DATA_LAKE.bronze("dart", "finance-statement", stock_code, f"finance_statement_({year}.{month:02d}).html")
    return path if path.exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a PIT-safe semantic correction ledger without mutating facts")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--max-files", type=int)
    parser.add_argument(
        "--reuse-existing-hashes",
        action="store_true",
        help="Reuse path/hash pairs from an existing ledger only when source files are known unchanged.",
    )
    args = parser.parse_args()

    graph = FactorDependencyGraph.from_javascript(PROJECT_ROOT / "scripts" / "calculate_factor_coverage.js")
    factor_dependencies = {
        canonical_id: graph.affected_factors(canonical_id)
        for canonical_id in ("CAPEX_PPE", "CAPEX_INTANG")
    }
    paths = sorted(args.input_dir.glob("kr_normalized_*.debug.csv"))
    if args.max_files is not None:
        paths = paths[: args.max_files]
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    # A retry after an interrupted/failed validation can safely reuse hashes
    # from the previous complete ledger; source identity remains path+SHA-256.
    hash_cache: dict[Path, str] = {}
    if args.reuse_existing_hashes and args.ledger.exists():
        with args.ledger.open("r", encoding="utf-8-sig", newline="") as previous:
            for record in csv.DictReader(previous):
                previous_path = Path(str(record.get("source_path") or ""))
                previous_hash = str(record.get("source_hash") or "")
                if previous_path and previous_hash:
                    hash_cache.setdefault(previous_path, previous_hash)
    counts = Counter()
    affected_entities: set[str] = set()
    affected_periods: set[str] = set()
    fieldnames = [
        "old_fact_id", "old_semantics", "new_fact_id", "new_semantics", "reason",
        "source_hash", "migration_version", "affected_factors", "stock_code", "period",
        "statement_type", "original_account_name", "raw_amount", "rule_id", "source_path",
    ]
    with args.ledger.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for debug_path in paths:
            match = _STOCK_RE.search(debug_path.name)
            stock_code = match.group(1) if match else ""
            debug_hash = ""
            with debug_path.open("r", encoding="utf-8-sig", newline="") as stream:
                for source_row_ordinal, row in enumerate(csv.DictReader(stream), start=2):
                    counts["scanned_row_count"] += 1
                    canonical_id = str(row.get("canonical_account_id") or "")
                    if canonical_id not in factor_dependencies or str(row.get("cash_direction") or "").lower() != "inflow":
                        continue
                    period = str(row.get("period") or "")
                    original_path = source_path(stock_code, period)
                    if original_path is not None:
                        if original_path not in hash_cache:
                            hash_cache[original_path] = hash_file(original_path)
                        source_hash = hash_cache[original_path]
                        source_uri = str(original_path)
                    else:
                        if not debug_hash:
                            debug_hash = hash_file(debug_path)
                        source_hash = debug_hash
                        source_uri = str(debug_path)
                    correction = capex_direction_correction(
                        {
                            **row,
                            "stock_code": stock_code,
                            "source_uri": source_uri,
                            "debug_file": debug_path.name,
                            "source_row_ordinal": source_row_ordinal,
                        },
                        source_hash=source_hash,
                        affected_factors=factor_dependencies[canonical_id],
                    )
                    if correction is None:
                        continue
                    record = correction_record(correction)
                    record.update(
                        {
                            "stock_code": stock_code,
                            "period": period,
                            "statement_type": row.get("statement_type", ""),
                            "original_account_name": row.get("original_account_name", ""),
                            "raw_amount": row.get("raw_amount", ""),
                            "rule_id": row.get("rule_id", ""),
                            "source_path": source_uri,
                        }
                    )
                    writer.writerow(record)
                    counts[canonical_id] += 1
                    counts["correction_count"] += 1
                    affected_entities.add(stock_code)
                    affected_periods.add(period)

    summary = {
        "mode": "append-only-correction-ledger-no-source-mutation",
        "migration_version": "semantic-v3",
        "input_file_count": len(paths),
        **dict(counts),
        "affected_entity_count": len(affected_entities),
        "affected_period_count": len(affected_periods),
        "factor_drift": {
            canonical_id: {
                "affected_factor_count": len(factors),
                "affected_factors": list(factors),
                "interpretation": "old inflow was excluded from CAPEX input after semantic correction",
            }
            for canonical_id, factors in factor_dependencies.items()
        },
        "ledger": str(args.ledger),
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ledger": str(args.ledger), "summary": str(args.summary), **dict(counts)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
