from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.semantic import build_kr_financial_availability_dataframe


DEFAULT_INPUT = DATA_LAKE.silver("dart", "kr_report_metadata.csv")
DEFAULT_OUTPUT = DATA_LAKE.silver("dart", "kr_annual_financial_availability.csv")
DEFAULT_AUDIT = DATA_LAKE.meta("kr_annual_financial_availability_audit.json")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def build_file(
    *,
    input_path: Path = DEFAULT_INPUT,
    output_path: Path = DEFAULT_OUTPUT,
    audit_path: Path = DEFAULT_AUDIT,
) -> dict[str, object]:
    metadata = pd.read_csv(
        input_path,
        dtype={
            "security_id": str,
            "stock_code": str,
            "rcept_no": str,
            "source_type": str,
        },
        low_memory=False,
    )
    availability, audit = build_kr_financial_availability_dataframe(metadata)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    availability.to_csv(output_path, index=False, encoding="utf-8")
    payload = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "contract": "kr_annual_financial_availability/v1",
        "availability_policy": "latest ingested DART statement filing date; no period-end fallback",
        "input_sha256": _digest(input_path),
        "output_sha256": _digest(output_path),
        "output_path": str(output_path),
        **audit,
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build strict DART annual financial fact availability dates."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()
    print(
        json.dumps(
            build_file(
                input_path=args.input,
                output_path=args.output,
                audit_path=args.audit,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
