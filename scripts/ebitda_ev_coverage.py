from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.core.paths import DATA_LAKE
from engine.transformers.factors import (
    calculate_factor_coverage,
    create_all_stock_factor_dataframe,
)


FOCUS_FACTORS = [
    "oibdp",
    "enterprise_value",
    "ev_to_ebitda",
    "ebitda_to_ev",
    "ebitda_margin",
]
OUT_DIR = DATA_LAKE.root / "gold" / "factor_coverage"


def build_ebitda_ev_coverage(
    frame: pd.DataFrame,
    *,
    market: str,
    universe: str,
    baseline: dict[str, float] | None = None,
) -> pd.DataFrame:
    coverage = calculate_factor_coverage(frame, columns=FOCUS_FACTORS)["factor_coverage"].copy()
    if coverage.empty:
        coverage = pd.DataFrame(
            [
                {
                    "factor": factor,
                    "row_count": len(frame),
                    "covered_count": 0,
                    "missing_count": len(frame),
                    "coverage_ratio": 0.0,
                    "coverage_pct": 0.0,
                }
                for factor in FOCUS_FACTORS
            ]
        )

    coverage.insert(0, "market", market)
    coverage.insert(1, "universe", universe)
    if baseline:
        coverage["baseline_coverage_pct"] = coverage["factor"].map(baseline)
        coverage["delta_pct"] = coverage.apply(
            lambda row: _delta(row["coverage_pct"], row["baseline_coverage_pct"]),
            axis=1,
        )
    return coverage


def latest_annual_by_stock(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "stock_code" not in frame.columns:
        return frame
    sort_columns = [column for column in ["stock_code", "financial_period", "trade_date"] if column in frame.columns]
    return frame.sort_values(sort_columns).drop_duplicates(["stock_code"], keep="last")


def write_reports(
    *,
    market: str,
    frame: pd.DataFrame,
    output_dir: str | Path = OUT_DIR,
    baseline: dict[str, float] | None = None,
) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    all_rows = build_ebitda_ev_coverage(
        frame,
        market=market,
        universe="all_rows",
        baseline=baseline,
    )
    latest_rows = build_ebitda_ev_coverage(
        latest_annual_by_stock(frame),
        market=market,
        universe="latest_by_stock",
        baseline=baseline,
    )
    combined = pd.concat([all_rows, latest_rows], ignore_index=True)

    csv_path = output / f"{market}_ebitda_ev_coverage.csv"
    json_path = output / f"{market}_ebitda_ev_coverage_summary.json"
    combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
    summary = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "market": market,
        "focus_factors": FOCUS_FACTORS,
        "row_count": int(len(frame)),
        "latest_by_stock_count": int(len(latest_annual_by_stock(frame))),
        "coverage": combined.to_dict(orient="records"),
        "quality_policy": {
            "covered_value": "finite numeric value only",
            "enterprise_value": "EV/EBITDA requires enterprise_value > 0",
            "ebitda": "EV/EBITDA requires EBITDA != 0; negative EBITDA is flagged but retained",
        },
    }
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"csv": csv_path, "summary": json_path}


def _delta(current: object, baseline: object) -> float | None:
    try:
        if baseline is None or pd.isna(baseline):
            return None
        return float(current) - float(baseline)
    except Exception:
        return None


def _baseline_from_csv(path: str | Path | None) -> dict[str, float]:
    if path is None:
        return {}
    frame = pd.read_csv(path)
    if "factor" not in frame.columns or "coverage_pct" not in frame.columns:
        return {}
    return {
        str(row["factor"]): float(row["coverage_pct"])
        for _, row in frame.iterrows()
        if str(row["factor"]) in FOCUS_FACTORS and math.isfinite(float(row["coverage_pct"]))
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report focused EBITDA and EV/EBITDA coverage.")
    parser.add_argument("--market", default="kr", choices=["kr", "us"])
    parser.add_argument("--symbols", help="Comma-separated symbols. Omit for full supported universe.")
    parser.add_argument("--financial-basis", default="annual", choices=["annual", "quarterly", "ttm"])
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--baseline-csv")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]

    frame = create_all_stock_factor_dataframe(
        stock_codes=symbols,
        market=args.market,
        financial_basis=args.financial_basis,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    written = write_reports(
        market=args.market,
        frame=frame,
        output_dir=args.out_dir,
        baseline=_baseline_from_csv(args.baseline_csv),
    )
    for label, path in written.items():
        print(f"wrote {label}: {path}")


if __name__ == "__main__":
    main()
