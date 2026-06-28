from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE


OPERATING_GOLD_ROOT = DATA_LAKE.root / "gold" / "operating-metrics"
ESTIMATE_GOLD_ROOT = DATA_LAKE.root / "gold" / "estimates"

OPERATING_TABLE_FILES = {
    "business_operating_metric_raw": "business_operating_metric_raw.csv",
    "business_operating_metric": "business_operating_metric.csv",
    "business_unit_economics": "business_unit_economics.csv",
    "business_unit_economics_driver": "business_unit_economics_driver.csv",
}

ESTIMATE_TABLE_FILES = {
    "arcana_estimate_component": "arcana_estimate_component.csv",
    "arcana_estimate_consensus": "arcana_estimate_consensus.csv",
}

STRING_COLUMNS = {
    "security_id",
    "stock_code",
    "rcept_no",
    "source_url",
    "section_key",
    "section_title",
    "table_id",
    "table_kind",
    "raw_label",
    "raw_value",
    "raw_unit",
    "row_text",
    "header_value_map_json",
    "metric_candidate",
    "product_candidate",
    "segment_candidate",
    "parsed_unit",
    "parser_rule_id",
    "business_domain",
    "segment_id",
    "segment_name",
    "product_id",
    "product_name",
    "metric_id",
    "metric_name",
    "metric_unit",
    "value_type",
    "source_type",
    "source_table_id",
    "quality_flags",
    "model_version",
    "quantity_unit",
    "revenue_source",
    "quantity_source",
    "cogs_source",
    "cogs_allocation_method",
    "target_period",
    "model_id",
    "scenario",
    "currency",
    "source_actual_period",
    "assumptions_json",
}

DATE_COLUMNS = {
    "period_end_date",
    "report_date",
    "as_of_date",
}

DATETIME_COLUMNS = {
    "created_at",
}

INTEGER_COLUMNS = {
    "fiscal_year",
    "fiscal_month",
    "row_idx",
    "col_idx",
    "source_row_idx",
    "model_count",
}

FLOAT_COLUMNS = {
    "parsed_value",
    "confidence",
    "metric_value",
    "revenue",
    "quantity",
    "p",
    "asp",
    "revenue_coverage_ratio",
    "c",
    "gross_profit",
    "gross_margin",
    "q_yoy_pct",
    "asp_yoy_pct",
    "unit_cost_yoy_pct",
    "revenue_yoy_pct",
    "gross_margin_change_pctp",
    "estimate_value",
    "consensus_mean",
    "consensus_median",
    "consensus_low",
    "consensus_high",
    "dispersion",
}


def load_operating_metrics(
    stock_codes: list[str] | None = None,
    *,
    dry_run: bool = False,
    client: Any = None,
    operating_gold_root: str | Path = OPERATING_GOLD_ROOT,
    estimate_gold_root: str | Path = ESTIMATE_GOLD_ROOT,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    codes = stock_codes or _discover_stock_codes(Path(operating_gold_root))
    owns_client = client is None
    if not dry_run:
        client = client or get_clickhouse_client()
    try:
        for stock_code in codes:
            stock_code = normalize_stock_code(stock_code)
            counts.update(
                _load_table_group(
                    stock_code,
                    root=Path(operating_gold_root),
                    table_files=OPERATING_TABLE_FILES,
                    dry_run=dry_run,
                    client=client,
                    counts=counts,
                )
            )
            counts.update(
                _load_table_group(
                    stock_code,
                    root=Path(estimate_gold_root),
                    table_files=ESTIMATE_TABLE_FILES,
                    dry_run=dry_run,
                    client=client,
                    counts=counts,
                )
            )
    finally:
        close = getattr(client, "close", None)
        if owns_client and callable(close):
            close()
    return counts


def _load_table_group(
    stock_code: str,
    *,
    root: Path,
    table_files: dict[str, str],
    dry_run: bool,
    client: Any,
    counts: dict[str, int],
) -> dict[str, int]:
    for table_name, file_name in table_files.items():
        path = root / stock_code / file_name
        frame = _read_csv(path, table_name=table_name)
        counts[table_name] = counts.get(table_name, 0) + len(frame)
        if dry_run or frame.empty:
            continue
        client.insert_df(table_name, frame, column_names=list(frame.columns))
    return counts


def _read_csv(path: Path, *, table_name: str = "") -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return _prepare_insert_frame(frame, table_name=table_name)


def _prepare_insert_frame(frame: pd.DataFrame, *, table_name: str = "") -> pd.DataFrame:
    result = frame.copy()

    for column in result.columns:
        if column in STRING_COLUMNS:
            result[column] = result[column].map(_string_value)
        elif column in DATE_COLUMNS:
            result[column] = result[column].map(_date_value)
        elif column in DATETIME_COLUMNS:
            result[column] = result[column].map(_datetime_value)
        elif column in INTEGER_COLUMNS:
            result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0).astype("int64")
        elif column in FLOAT_COLUMNS:
            result[column] = pd.to_numeric(result[column], errors="coerce").astype("float64")

    if "stock_code" in result.columns:
        result["stock_code"] = result["stock_code"].map(normalize_stock_code)

    return result


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return str(value)


def _date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        try:
            return datetime.fromisoformat(str(value)[:19])
        except ValueError:
            return None


def _discover_stock_codes(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def normalize_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _parse_stock_codes(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [normalize_stock_code(item) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Load operating metric and estimate gold CSVs into ClickHouse.")
    parser.add_argument("--stock-codes", help="Comma-separated stock codes. Defaults to all gold dirs.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    counts = load_operating_metrics(_parse_stock_codes(args.stock_codes), dry_run=args.dry_run)
    for table_name, count in sorted(counts.items()):
        print(f"{table_name}: {count:,}")


if __name__ == "__main__":
    main()
