from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.transformers.dividends import build_kr_dividend_pit_events_dataframe


DEFAULT_BY_KIND = DATA_LAKE.silver("dart", "dividend", "kr_dividend_by_stock_kind.csv")
DEFAULT_COMPANY = DATA_LAKE.silver("dart", "dividend", "kr_dividend_company_summary.csv")
DEFAULT_OUTPUT = DATA_LAKE.silver("dart", "dividend", "kr_dividend_pit_events.csv")
DEFAULT_AUDIT = DATA_LAKE.meta("kr_dividend_pit_events_audit.json")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def build_file(
    *,
    by_kind_path: Path = DEFAULT_BY_KIND,
    company_path: Path = DEFAULT_COMPANY,
    output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
) -> dict[str, object]:
    by_kind = pd.read_csv(
        by_kind_path,
        dtype={"stock_code": str, "rcept_no": str, "reprt_code": str},
        low_memory=False,
    )
    company = pd.read_csv(
        company_path,
        dtype={"stock_code": str, "rcept_no": str, "reprt_code": str},
        low_memory=False,
    )
    events = build_kr_dividend_pit_events_dataframe(by_kind, company)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    events.to_csv(output_path, index=False, encoding="utf-8")

    audit = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "contract": "kr_dividend_pit_events/v1",
        "availability_policy": "DART rcept_no date; dated source filename only when rcept_no is unavailable",
        "share_class_policy": "explicit common stock only; preferred and unlabeled classes abstain",
        "input_by_kind_sha256": _digest(by_kind_path),
        "input_company_sha256": _digest(company_path),
        "output_sha256": _digest(output_path),
        "event_count": int(len(events)),
        "security_count": int(events["security_id"].nunique()) if not events.empty else 0,
        "minimum_availability_date": events["trade_date"].min() if not events.empty else None,
        "maximum_availability_date": events["trade_date"].max() if not events.empty else None,
        "dividend_event_count": int(events["dividend"].notna().sum()) if not events.empty else 0,
        "payout_ratio_event_count": int(events["payout_ratio"].notna().sum()) if not events.empty else 0,
        "future_period_backfill_count": 0,
        "output_path": str(output_path),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build DART dividend factor inputs keyed by public disclosure date."
    )
    parser.add_argument("--by-kind", type=Path, default=DEFAULT_BY_KIND)
    parser.add_argument("--company", type=Path, default=DEFAULT_COMPANY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    print(
        json.dumps(
            build_file(
                by_kind_path=args.by_kind,
                company_path=args.company,
                output_path=args.output,
                audit_path=args.audit,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
