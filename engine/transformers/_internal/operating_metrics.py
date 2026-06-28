from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from engine.core.paths import DATA_LAKE
from engine.transformers._internal.estimate_consensus import build_consensus
from engine.transformers._internal.pqc import assumptions_json, forecast_unit_economics, pct_change, period_end_date


SILVER_ROOT = DATA_LAKE.silver("dart", "business-info")
OPERATING_METRIC_GOLD_ROOT = DATA_LAKE.root / "gold" / "operating-metrics"
ESTIMATE_GOLD_ROOT = DATA_LAKE.root / "gold" / "estimates"

RAW_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "report_date",
    "rcept_no",
    "source_url",
    "section_key",
    "section_title",
    "table_id",
    "table_kind",
    "row_idx",
    "col_idx",
    "raw_label",
    "raw_value",
    "raw_unit",
    "row_text",
    "header_value_map_json",
    "metric_candidate",
    "product_candidate",
    "segment_candidate",
    "parsed_value",
    "parsed_unit",
    "parser_rule_id",
    "confidence",
    "created_at",
]

METRIC_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "business_domain",
    "segment_id",
    "segment_name",
    "product_id",
    "product_name",
    "metric_id",
    "metric_name",
    "metric_value",
    "metric_unit",
    "value_type",
    "source_type",
    "source_table_id",
    "source_row_idx",
    "source_url",
    "confidence",
    "quality_flags",
    "model_version",
    "created_at",
]

UNIT_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "business_domain",
    "segment_id",
    "segment_name",
    "product_id",
    "product_name",
    "revenue",
    "quantity",
    "quantity_unit",
    "p",
    "asp",
    "revenue_source",
    "quantity_source",
    "revenue_coverage_ratio",
    "confidence",
    "quality_flags",
    "model_version",
    "created_at",
    "c",
    "gross_profit",
    "gross_margin",
    "cogs_source",
    "cogs_allocation_method",
]

DRIVER_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "business_domain",
    "segment_id",
    "segment_name",
    "product_id",
    "product_name",
    "q_yoy_pct",
    "asp_yoy_pct",
    "unit_cost_yoy_pct",
    "revenue_yoy_pct",
    "gross_margin_change_pctp",
    "model_version",
    "created_at",
]

COMPONENT_COLUMNS = [
    "security_id",
    "stock_code",
    "target_period",
    "metric_id",
    "model_id",
    "scenario",
    "estimate_value",
    "currency",
    "source_actual_period",
    "assumptions_json",
    "confidence",
    "quality_flags",
    "as_of_date",
]

MODEL_VERSION = "pqc_mvp_v1"
_NUMBER_RE = re.compile(r"[-+△▲]?\s*\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class TransformResult:
    raw_path: Path
    metric_path: Path
    unit_economics_path: Path
    driver_path: Path
    estimate_component_path: Path
    estimate_consensus_path: Path
    raw_rows: int
    metric_rows: int
    unit_rows: int
    driver_rows: int
    estimate_component_rows: int
    estimate_consensus_rows: int


def create_operating_metric_gold(
    stock_code: str,
    *,
    silver_root: str | Path = SILVER_ROOT,
    operating_gold_root: str | Path = OPERATING_METRIC_GOLD_ROOT,
    estimate_gold_root: str | Path = ESTIMATE_GOLD_ROOT,
    estimate_start_period: str | None = None,
    estimate_end_period: str | None = None,
    estimate_all_periods: bool = False,
) -> TransformResult:
    stock_code = normalize_stock_code(stock_code)
    stock_silver_dir = Path(silver_root) / stock_code
    table_path = stock_silver_dir / "kr_business_info_tables.csv"
    row_path = stock_silver_dir / "kr_business_info_rows.csv"
    if not table_path.exists() or not row_path.exists():
        raise FileNotFoundError(f"business-info silver CSV not found for stock_code={stock_code}")

    table_df = pd.read_csv(table_path, dtype=str).fillna("")
    row_df = pd.read_csv(row_path, dtype=str).fillna("")
    raw_df = extract_raw_metrics(table_df, row_df, stock_code=stock_code)
    metric_df = normalize_operating_metrics(raw_df)
    unit_df = build_unit_economics(metric_df)
    driver_df = build_unit_economics_drivers(unit_df)
    component_df = build_estimate_components(
        unit_df,
        driver_df,
        start_period=estimate_start_period,
        end_period=estimate_end_period,
        all_periods=estimate_all_periods,
    )
    consensus_df = build_consensus(component_df)

    operating_dir = Path(operating_gold_root) / stock_code
    estimate_dir = Path(estimate_gold_root) / stock_code
    raw_path = operating_dir / "business_operating_metric_raw.csv"
    metric_path = operating_dir / "business_operating_metric.csv"
    unit_path = operating_dir / "business_unit_economics.csv"
    driver_path = operating_dir / "business_unit_economics_driver.csv"
    component_path = estimate_dir / "arcana_estimate_component.csv"
    consensus_path = estimate_dir / "arcana_estimate_consensus.csv"

    _write_csv(raw_path, raw_df, RAW_COLUMNS)
    _write_csv(metric_path, metric_df, METRIC_COLUMNS)
    _write_csv(unit_path, unit_df, UNIT_COLUMNS)
    _write_csv(driver_path, driver_df, DRIVER_COLUMNS)
    _write_csv(component_path, component_df, COMPONENT_COLUMNS)
    _write_csv(consensus_path, consensus_df, list(consensus_df.columns))

    return TransformResult(
        raw_path=raw_path,
        metric_path=metric_path,
        unit_economics_path=unit_path,
        driver_path=driver_path,
        estimate_component_path=component_path,
        estimate_consensus_path=consensus_path,
        raw_rows=len(raw_df),
        metric_rows=len(metric_df),
        unit_rows=len(unit_df),
        driver_rows=len(driver_df),
        estimate_component_rows=len(component_df),
        estimate_consensus_rows=len(consensus_df),
    )


def create_operating_metric_gold_for_stocks(
    stock_codes: list[str] | None = None,
    *,
    progress: bool = True,
    progress_interval: int = 25,
    continue_on_error: bool = True,
    **kwargs: Any,
) -> list[TransformResult]:
    codes = stock_codes or _discover_stock_codes(Path(kwargs.get("silver_root", SILVER_ROOT)))
    total = len(codes)
    results: list[TransformResult] = []
    failed_count = 0
    if progress:
        print(f"[START] operating metrics stocks={total:,}", flush=True)

    for index, stock_code in enumerate(codes, start=1):
        try:
            result = create_operating_metric_gold(stock_code, **kwargs)
        except Exception as exc:
            failed_count += 1
            if progress:
                print(
                    f"[ERROR] operating metrics {index:,}/{total:,} stock={stock_code}: {exc!r}",
                    flush=True,
                )
            if not continue_on_error:
                raise
            continue

        results.append(result)
        if progress and _should_log_progress(index, total, progress_interval):
            print(
                f"[PROGRESS] operating metrics {index:,}/{total:,} "
                f"stock={normalize_stock_code(stock_code)} "
                f"raw={result.raw_rows:,} metrics={result.metric_rows:,} "
                f"unit={result.unit_rows:,} drivers={result.driver_rows:,} "
                f"components={result.estimate_component_rows:,} consensus={result.estimate_consensus_rows:,} "
                f"failed={failed_count:,}",
                flush=True,
            )

    if progress:
        print(
            f"[DONE] operating metrics processed={len(results):,}/{total:,}, failed={failed_count:,}",
            flush=True,
        )
    return results


def extract_raw_metrics(table_df: pd.DataFrame, row_df: pd.DataFrame, *, stock_code: str) -> pd.DataFrame:
    tables = {str(row["table_id"]): row for row in table_df.to_dict("records")}
    created_at = _now_text()
    rows: list[dict[str, Any]] = []
    source_rows = row_df.to_dict("records")
    for table in table_df.to_dict("records"):
        source_rows.extend(_header_pseudo_rows(table))

    for row in source_rows:
        table = tables.get(str(row.get("table_id") or ""))
        if table is None:
            continue
        table_kind = str(table.get("table_kind") or "")
        text_context = " ".join(
            str(table.get(key) or "")
            for key in ("section_key", "section_title", "table_title", "caption_or_context", "unit_text")
        )
        if table_kind not in {"data_table", "paragraph_table"} and not _looks_like_metric_context(text_context):
            continue
        try:
            header_map = json.loads(str(row.get("header_value_map_json") or "{}"))
        except json.JSONDecodeError:
            header_map = {}
        if not isinstance(header_map, dict):
            continue
        period = str(table.get("period") or "")
        fiscal_year, fiscal_month = _parse_period(period)
        report_date = period_end_date(fiscal_year, fiscal_month).isoformat()
        for col_idx, (label, raw_value) in enumerate(header_map.items()):
            if _is_prior_period_label(str(label)):
                continue
            metric_id = infer_metric_id(str(label), str(raw_value), text_context, str(row.get("row_text") or ""))
            if metric_id is None:
                continue
            parsed_value = parse_number(raw_value)
            if parsed_value is None:
                continue
            parsed_unit, scaled_value = normalize_metric_value(metric_id, parsed_value, str(table.get("unit_text") or ""), str(label))
            segment_name, product_name = infer_entity(row, label)
            confidence = infer_confidence(metric_id, table_kind, parsed_unit)
            rows.append(
                {
                    "security_id": f"SEC_KR_{stock_code}",
                    "stock_code": stock_code,
                    "fiscal_year": fiscal_year,
                    "fiscal_month": fiscal_month,
                    "period_end_date": report_date,
                    "report_date": report_date,
                    "rcept_no": table.get("rcept_no") or "",
                    "source_url": table.get("source_uri") or "",
                    "section_key": table.get("section_key") or "",
                    "section_title": table.get("section_title") or "",
                    "table_id": table.get("table_id") or "",
                    "table_kind": table_kind,
                    "row_idx": int(float(row.get("row_idx") or 0)),
                    "col_idx": col_idx,
                    "raw_label": label,
                    "raw_value": raw_value,
                    "raw_unit": table.get("unit_text") or "",
                    "row_text": row.get("row_text") or "",
                    "header_value_map_json": row.get("header_value_map_json") or "",
                    "metric_candidate": metric_id,
                    "product_candidate": product_name,
                    "segment_candidate": segment_name,
                    "parsed_value": scaled_value,
                    "parsed_unit": parsed_unit,
                    "parser_rule_id": "operating_metric_rules:v1",
                    "confidence": confidence,
                    "created_at": created_at,
                }
            )
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


def normalize_operating_metrics(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df.empty:
        return pd.DataFrame(columns=METRIC_COLUMNS)
    rows = []
    for raw in raw_df.to_dict("records"):
        metric_id = str(raw["metric_candidate"])
        value_type = "derived" if metric_id.startswith("derived_") else "reported"
        flags = []
        if str(raw.get("parsed_unit") or "").startswith("inferred"):
            flags.append("UNIT_INFERRED")
        if metric_id in {"shipment_volume", "capacity", "utilization_rate"}:
            flags.append("QUANTITY_PROXY_USED")
        segment_name = str(raw.get("segment_candidate") or "")
        product_name = str(raw.get("product_candidate") or segment_name or "company")
        rows.append(
            {
                "security_id": raw["security_id"],
                "stock_code": raw["stock_code"],
                "fiscal_year": raw["fiscal_year"],
                "fiscal_month": raw["fiscal_month"],
                "period_end_date": raw["period_end_date"],
                "business_domain": "",
                "segment_id": stable_id(segment_name),
                "segment_name": segment_name,
                "product_id": stable_id(product_name),
                "product_name": product_name,
                "metric_id": metric_id,
                "metric_name": metric_id.replace("_", " ").title(),
                "metric_value": raw["parsed_value"],
                "metric_unit": raw["parsed_unit"],
                "value_type": value_type,
                "source_type": "dart_business_info",
                "source_table_id": raw["table_id"],
                "source_row_idx": raw["row_idx"],
                "source_url": raw["source_url"],
                "confidence": raw["confidence"],
                "quality_flags": ",".join(flags),
                "model_version": MODEL_VERSION,
                "created_at": raw["created_at"],
            }
        )
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)


def build_unit_economics(metric_df: pd.DataFrame) -> pd.DataFrame:
    if metric_df.empty:
        return pd.DataFrame(columns=UNIT_COLUMNS)
    rows = []
    group_columns = [
        "security_id",
        "stock_code",
        "fiscal_year",
        "fiscal_month",
        "period_end_date",
        "business_domain",
        "segment_id",
        "segment_name",
        "product_id",
        "product_name",
    ]
    total_revenue_by_period = (
        metric_df[metric_df["metric_id"] == "revenue"]
        .groupby(["stock_code", "fiscal_year", "fiscal_month"])["metric_value"]
        .sum()
        .to_dict()
    )
    for key, group in metric_df.groupby(group_columns, dropna=False):
        metrics = {metric_id: item for metric_id, item in group.groupby("metric_id")}
        revenue_row = _first_metric(metrics, "revenue")
        quantity_row = _first_available_metric(metrics, ["quantity_sold", "shipment_volume", "quantity_produced"])
        reported_asp_row = _first_metric(metrics, "reported_asp")
        unit_cost_row = _first_available_metric(metrics, ["reported_unit_cost", "derived_unit_cost"])
        revenue = _row_float(revenue_row, "metric_value")
        quantity = _row_float(quantity_row, "metric_value")
        reported_asp = _row_float(reported_asp_row, "metric_value")
        asp = reported_asp
        asp_source = "reported_asp" if reported_asp is not None else ""
        if asp is None and revenue is not None and quantity not in (None, 0):
            asp = revenue / quantity
            asp_source = "derived_asp"
        c = _row_float(unit_cost_row, "metric_value")
        gross_profit = revenue - quantity * c if revenue is not None and quantity is not None and c is not None else None
        gross_margin = gross_profit / revenue * 100 if gross_profit is not None and revenue not in (None, 0) else None
        total_revenue = total_revenue_by_period.get((key[1], key[2], key[3]))
        coverage = revenue / total_revenue if revenue is not None and total_revenue else None
        flags = []
        if quantity_row is None and revenue is not None:
            flags.append("QUANTITY_MISSING")
        if asp_source == "derived_asp":
            flags.append("ASP_DERIVED_FROM_REVENUE_Q")
        confidence_values = pd.to_numeric(group["confidence"], errors="coerce").dropna()
        confidence = float(confidence_values.mean()) if not confidence_values.empty else 0.0
        rows.append(
            {
                "security_id": key[0],
                "stock_code": key[1],
                "fiscal_year": key[2],
                "fiscal_month": key[3],
                "period_end_date": key[4],
                "business_domain": key[5],
                "segment_id": key[6],
                "segment_name": key[7],
                "product_id": key[8],
                "product_name": key[9],
                "revenue": revenue,
                "quantity": quantity,
                "quantity_unit": _row_text(quantity_row, "metric_unit"),
                "p": reported_asp or asp,
                "asp": asp,
                "revenue_source": _row_text(revenue_row, "source_type"),
                "quantity_source": _row_text(quantity_row, "source_type"),
                "revenue_coverage_ratio": coverage,
                "confidence": confidence,
                "quality_flags": ",".join(flags),
                "model_version": MODEL_VERSION,
                "created_at": _now_text(),
                "c": c,
                "gross_profit": gross_profit,
                "gross_margin": gross_margin,
                "cogs_source": _row_text(unit_cost_row, "source_type"),
                "cogs_allocation_method": "",
            }
        )
    return pd.DataFrame(rows, columns=UNIT_COLUMNS)


def build_unit_economics_drivers(unit_df: pd.DataFrame) -> pd.DataFrame:
    if unit_df.empty:
        return pd.DataFrame(columns=DRIVER_COLUMNS)
    rows = []
    lookup = {
        (
            row["stock_code"],
            row["product_id"],
            int(row["fiscal_year"]),
            int(row["fiscal_month"]),
        ): row
        for row in unit_df.to_dict("records")
    }
    for row in unit_df.to_dict("records"):
        previous = lookup.get((row["stock_code"], row["product_id"], int(row["fiscal_year"]) - 1, int(row["fiscal_month"])))
        rows.append(
            {
                "security_id": row["security_id"],
                "stock_code": row["stock_code"],
                "fiscal_year": row["fiscal_year"],
                "fiscal_month": row["fiscal_month"],
                "period_end_date": row["period_end_date"],
                "business_domain": row["business_domain"],
                "segment_id": row["segment_id"],
                "segment_name": row["segment_name"],
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "q_yoy_pct": pct_change(row.get("quantity"), previous.get("quantity") if previous else None),
                "asp_yoy_pct": pct_change(row.get("asp"), previous.get("asp") if previous else None),
                "unit_cost_yoy_pct": pct_change(row.get("c"), previous.get("c") if previous else None),
                "revenue_yoy_pct": pct_change(row.get("revenue"), previous.get("revenue") if previous else None),
                "gross_margin_change_pctp": _subtract_or_none(row.get("gross_margin"), previous.get("gross_margin") if previous else None),
                "model_version": MODEL_VERSION,
                "created_at": _now_text(),
            }
        )
    return pd.DataFrame(rows, columns=DRIVER_COLUMNS)


def build_estimate_components(
    unit_df: pd.DataFrame,
    driver_df: pd.DataFrame,
    *,
    start_period: str | None = None,
    end_period: str | None = None,
    all_periods: bool = False,
) -> pd.DataFrame:
    if unit_df.empty:
        return pd.DataFrame(columns=COMPONENT_COLUMNS)
    driver_lookup = {
        (row["stock_code"], row["product_id"], int(row["fiscal_year"]), int(row["fiscal_month"])): pd.Series(row)
        for row in driver_df.to_dict("records")
    }
    as_of_date = _now_date_text()
    selected_periods = _select_estimate_source_periods(
        unit_df,
        start_period=start_period,
        end_period=end_period,
        all_periods=all_periods,
    )

    rows: list[dict[str, Any]] = []
    for fiscal_year, fiscal_month in selected_periods:
        period_units = unit_df[
            (unit_df["fiscal_year"].astype(int) == fiscal_year)
            & (unit_df["fiscal_month"].astype(int) == fiscal_month)
        ]
        rows.extend(
            _build_estimate_component_rows_for_period(
                period_units,
                driver_lookup=driver_lookup,
                as_of_date=as_of_date,
            )
        )
    return pd.DataFrame(rows, columns=COMPONENT_COLUMNS)


def _build_estimate_component_rows_for_period(
    period_units: pd.DataFrame,
    *,
    driver_lookup: dict[tuple[Any, Any, int, int], pd.Series],
    as_of_date: str,
) -> list[dict[str, Any]]:
    forecast_rows = []
    for _, unit_row in period_units.iterrows():
        driver = driver_lookup.get(
            (
                unit_row["stock_code"],
                unit_row["product_id"],
                int(unit_row["fiscal_year"]),
                int(unit_row["fiscal_month"]),
            )
        )
        forecast = forecast_unit_economics(unit_row, driver)
        source_actual_period = f"{int(unit_row['fiscal_year'])}.{int(unit_row['fiscal_month']):02d}"
        forecast_rows.append(
            {
                "security_id": unit_row["security_id"],
                "stock_code": unit_row["stock_code"],
                "target_period": forecast.target_period,
                "source_actual_period": source_actual_period,
                "revenue": forecast.revenue_next,
                "quantity": forecast.q_next,
                "asp": forecast.asp_next,
                "unit_cost": forecast.c_next,
                "gross_profit": forecast.gross_profit_next,
                "confidence": forecast.confidence,
                "assumptions": forecast.assumptions,
                "quality_flags": unit_row.get("quality_flags") or "",
            }
        )

    if not forecast_rows:
        return []

    forecast_df = pd.DataFrame(forecast_rows)
    first_row = forecast_rows[0]
    quantity_sum = pd.to_numeric(forecast_df["quantity"], errors="coerce").sum(min_count=1)
    revenue_sum = pd.to_numeric(forecast_df["revenue"], errors="coerce").sum(min_count=1)
    gross_profit_sum = pd.to_numeric(forecast_df["gross_profit"], errors="coerce").sum(min_count=1)
    asp_value = revenue_sum / quantity_sum if pd.notna(revenue_sum) and pd.notna(quantity_sum) and quantity_sum else None
    cost_numerator = revenue_sum - gross_profit_sum if pd.notna(revenue_sum) and pd.notna(gross_profit_sum) else None
    unit_cost_value = cost_numerator / quantity_sum if cost_numerator is not None and pd.notna(quantity_sum) and quantity_sum else None
    confidence_values = pd.to_numeric(forecast_df["confidence"], errors="coerce").dropna()
    confidence = float(confidence_values.mean()) if not confidence_values.empty else 0.0
    merged_assumptions = {
        "formula": "company-level aggregate from product P/Q/C forecasts",
        "product_count": int(len(forecast_rows)),
        "product_assumptions": [row["assumptions"] for row in forecast_rows],
    }
    flags = ",".join(sorted({flag for row in forecast_rows for flag in str(row.get("quality_flags") or "").split(",") if flag}))
    estimates = {
        "revenue": _nan_to_none(revenue_sum),
        "quantity": _nan_to_none(quantity_sum),
        "asp": _nan_to_none(asp_value),
        "unit_cost": _nan_to_none(unit_cost_value),
        "gross_profit": _nan_to_none(gross_profit_sum),
    }
    rows = []
    for metric_id, value in estimates.items():
        if value is None:
            continue
        for variant in _pseudo_consensus_variants(metric_id, value, confidence):
            rows.append(
                {
                    "security_id": first_row["security_id"],
                    "stock_code": first_row["stock_code"],
                    "target_period": first_row["target_period"],
                    "metric_id": metric_id,
                    "model_id": variant["model_id"],
                    "scenario": "base",
                    "estimate_value": variant["estimate_value"],
                    "currency": "KRW",
                    "source_actual_period": first_row["source_actual_period"],
                    "assumptions_json": assumptions_json(
                        {
                            **merged_assumptions,
                            "pseudo_model": variant["model_id"],
                            "pseudo_model_method": variant["method"],
                        }
                    ),
                    "confidence": variant["confidence"],
                    "quality_flags": flags,
                    "as_of_date": as_of_date,
                }
            )
    return rows


def infer_metric_id(label: str, raw_value: str, context: str, row_text: str) -> str | None:
    label_head = str(label or "").split(">")[0].strip()
    label_compact = _compact(label)
    label_head_compact = _compact(label_head)
    context_compact = _compact(f"{context} {row_text}")
    if _is_excluded_metric_label(label_head_compact):
        return None
    if any(token in label_head_compact for token in ("단위당원가", "원가단가", "unitcost")):
        return "reported_unit_cost"
    if any(token in label_head_compact for token in ("판매가격", "평균판매가격", "가격변동", "요금", "asp")):
        return "reported_asp"
    if any(token in label_head_compact for token in ("판매량", "판매수량", "출하량", "공급량", "판매대수")):
        return "quantity_sold"
    if any(token in label_head_compact for token in ("생산량", "생산실적")):
        return "quantity_produced"
    if any(token in label_head_compact for token in ("생산능력", "capa", "설비능력")):
        return "capacity"
    if any(token in label_head_compact for token in ("가동률", "평균가동률")):
        return "utilization_rate"
    if any(token in label_head_compact for token in ("매출", "영업수익", "판매금액", "금액")):
        return "revenue"

    compact = f"{label_compact} {context_compact}"
    if _is_excluded_metric_label(label_compact):
        return None
    if any(token in compact for token in ("판매량", "판매수량", "출하량", "공급량", "판매대수")):
        return "quantity_sold"
    if any(token in compact for token in ("생산량", "생산실적")):
        return "quantity_produced"
    if any(token in compact for token in ("생산능력", "capa", "설비능력")):
        return "capacity"
    if any(token in compact for token in ("가동률", "평균가동률")):
        return "utilization_rate"
    if any(token in compact for token in ("판매가격", "평균판매가격", "가격변동", "요금", "asp")):
        return "reported_asp"
    if any(token in compact for token in ("단위당원가", "원가단가", "unitcost")):
        return "reported_unit_cost"
    if any(token in label_compact for token in ("금액", "amount")) and any(token in context_compact for token in ("매출", "영업수익")):
        return "revenue"
    return None


def infer_entity(row: dict[str, Any], label: str) -> tuple[str, str]:
    row_values = _json_list(row.get("row_json"))
    segment = ""
    product = ""
    if row_values:
        segment = str(row_values[0]).strip()
    if len(row_values) > 1:
        product = str(row_values[1]).strip()
    if not segment:
        parts = [part.strip() for part in str(row.get("row_text") or "").split("|") if part.strip()]
        segment = parts[0] if parts else ""
        product = parts[1] if len(parts) > 1 else ""
    if _is_total_label(segment):
        product = segment
    return segment, product or segment or "company"


def _header_pseudo_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    if str(table.get("table_kind") or "") != "data_table":
        return []
    header_paths = _json_list(table.get("header_paths_json"))
    if not header_paths or not all(isinstance(path, list) for path in header_paths):
        return []

    max_depth = max((len(path) for path in header_paths), default=0)
    if max_depth <= 1:
        return []

    pseudo_rows: list[dict[str, Any]] = []
    for depth_index in range(1, max_depth):
        header_map: dict[str, str] = {}
        row_values: list[str] = []
        for path in header_paths:
            if not path:
                continue
            key = str(path[0]).strip()
            if not key or depth_index >= len(path):
                continue
            value = str(path[depth_index]).strip()
            if not value:
                continue
            header_map[key] = value
            row_values.append(value)
        if not header_map or not any(parse_number(value) is not None for value in header_map.values()):
            continue
        pseudo_rows.append(
            {
                "table_id": table.get("table_id") or "",
                "row_idx": -1000 - depth_index,
                "row_json": json.dumps(row_values, ensure_ascii=False),
                "row_text": " | ".join(row_values),
                "header_value_map_json": json.dumps(header_map, ensure_ascii=False),
            }
        )
    return pseudo_rows


def parse_number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    number_text = match.group(0).replace(" ", "").replace(",", "")
    sign = -1 if number_text.startswith(("△", "▲")) else 1
    number_text = number_text.lstrip("△▲+")
    try:
        return sign * float(number_text)
    except ValueError:
        return None


def normalize_metric_value(metric_id: str, value: float, unit_text: str, label: str) -> tuple[str, float]:
    unit_source = f"{unit_text} {label}"
    compact = _compact(unit_source)
    if metric_id == "utilization_rate":
        return "percent", value
    if metric_id in {"revenue"}:
        if "억원" in compact:
            return "KRW", value * 100_000_000
        if "백만원" in compact:
            return "KRW", value * 1_000_000
        if "천원" in compact:
            return "KRW", value * 1_000
        return "inferred_KRW", value
    if metric_id in {"reported_asp", "reported_unit_cost", "derived_unit_cost"}:
        if "천원" in compact:
            return "KRW_PER_UNIT", value * 1_000
        if "만원" in compact and "백만원" not in compact:
            return "KRW_PER_UNIT", value * 10_000
        return "KRW_PER_UNIT", value
    if "gwh" in compact:
        return "MWh", value * 1000
    if "mwh" in compact:
        return "MWh", value
    if any(token in compact for token in ("천개", "천대", "천톤", "천명", "천매", "천본")):
        return "unit", value * 1000
    if any(token in compact for token in ("백만개", "백만대", "백만톤", "백만명", "백만매", "백만본")):
        return "unit", value * 1_000_000
    if "%" in unit_source:
        return "percent", value
    return "unit", value


def infer_confidence(metric_id: str, table_kind: str, parsed_unit: str) -> float:
    score = 0.95 if table_kind == "data_table" else 0.65
    if parsed_unit.startswith("inferred"):
        score *= 0.70
    if metric_id in {"capacity", "utilization_rate"}:
        score *= 0.85
    return round(score, 4)


def _is_excluded_metric_label(compact_label: str) -> bool:
    return any(
        token in compact_label
        for token in (
            "비중",
            "구성비",
            "ratio",
            "영업이익",
            "총자산",
            "매입",
            "내부거래",
            "소계",
            "주요매입처",
        )
    )


def _is_prior_period_label(label: str) -> bool:
    compact = _compact(label)
    if not re.search(r"제\d+기", compact):
        return False
    return not any(token in compact for token in ("분기", "반기", "당기", "현재", "누적"))


def stable_id(value: Any) -> str:
    text = str(value or "company").strip().upper()
    text = re.sub(r"[^0-9A-Z가-힣]+", "_", text)
    text = text.strip("_")
    return text or "COMPANY"


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _write_csv(path: Path, frame: pd.DataFrame, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[columns]
    frame.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def _discover_stock_codes(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def _select_estimate_source_periods(
    unit_df: pd.DataFrame,
    *,
    start_period: str | None,
    end_period: str | None,
    all_periods: bool,
) -> list[tuple[int, int]]:
    periods = [
        (int(row["fiscal_year"]), int(row["fiscal_month"]))
        for row in unit_df[["fiscal_year", "fiscal_month"]]
        .drop_duplicates()
        .sort_values(["fiscal_year", "fiscal_month"])
        .to_dict("records")
    ]
    if not periods:
        return []

    start_key = _period_filter_key(start_period)
    end_key = _period_filter_key(end_period)
    if not all_periods and start_key is None and end_key is None:
        return [periods[-1]]

    selected: list[tuple[int, int]] = []
    for fiscal_year, fiscal_month in periods:
        key = fiscal_year * 100 + fiscal_month
        if start_key is not None and key < start_key:
            continue
        if end_key is not None and key > end_key:
            continue
        selected.append((fiscal_year, fiscal_month))
    return selected


def _period_filter_key(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    fiscal_year, fiscal_month = _parse_period(str(value))
    if fiscal_year <= 0 or fiscal_month <= 0:
        raise ValueError(f"period must be YYYY.MM, got {value!r}")
    return fiscal_year * 100 + fiscal_month


def _parse_period(value: str) -> tuple[int, int]:
    match = re.search(r"(?P<year>\d{4})[._-](?P<month>\d{1,2})", str(value or ""))
    if not match:
        return 0, 12
    return int(match.group("year")), int(match.group("month"))


def _json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _first_metric(metrics: dict[str, pd.DataFrame], metric_id: str) -> pd.Series | None:
    frame = metrics.get(metric_id)
    if frame is None or frame.empty:
        return None
    return frame.sort_values("confidence", ascending=False).iloc[0]


def _first_available_metric(metrics: dict[str, pd.DataFrame], metric_ids: list[str]) -> pd.Series | None:
    for metric_id in metric_ids:
        row = _first_metric(metrics, metric_id)
        if row is not None:
            return row
    return None


def _row_float(row: pd.Series | None, key: str) -> float | None:
    if row is None:
        return None
    try:
        value = float(row.get(key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _row_text(row: pd.Series | None, key: str) -> str:
    if row is None:
        return ""
    value = row.get(key)
    return "" if value is None else str(value)


def _subtract_or_none(current: Any, previous: Any) -> float | None:
    current_value = _to_float(current)
    previous_value = _to_float(previous)
    if current_value is None or previous_value is None:
        return None
    return current_value - previous_value


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nan_to_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _pseudo_consensus_variants(metric_id: str, value: float, confidence: float) -> list[dict[str, Any]]:
    if metric_id in {"revenue", "gross_profit", "quantity", "asp", "unit_cost"}:
        return [
            {
                "model_id": "P/Q/C Driver",
                "estimate_value": value,
                "confidence": confidence,
                "method": "direct aggregate of product P/Q/C forecast",
            },
            {
                "model_id": "Historical Trend",
                "estimate_value": value * 0.98,
                "confidence": max(0.05, confidence * 0.85),
                "method": "conservative historical continuity proxy",
            },
            {
                "model_id": "Industry Peer",
                "estimate_value": value * 1.02,
                "confidence": max(0.05, confidence * 0.75),
                "method": "industry peer median proxy until peer model is available",
            },
        ]
    return [
        {
            "model_id": "P/Q/C Driver",
            "estimate_value": value,
            "confidence": confidence,
            "method": "direct aggregate of product P/Q/C forecast",
        }
    ]


def _looks_like_metric_context(text: str) -> bool:
    compact = _compact(text)
    return any(token in compact for token in ("매출", "판매", "가격", "생산", "출하", "capa", "가동"))


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _is_total_label(value: str) -> bool:
    return _compact(value) in {"합계", "총계", "총 계", "계", "total"}


def _now_text() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None).isoformat(timespec="seconds")


def _now_date_text() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _parse_stock_codes(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [normalize_stock_code(item) for item in value.split(",") if item.strip()]


def _should_log_progress(index: int, total: int, progress_interval: int) -> bool:
    if index in {1, total}:
        return True
    return progress_interval > 0 and index % progress_interval == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Create operating metric and P/Q/C estimate gold CSVs.")
    parser.add_argument("--stock-codes", help="Comma-separated stock codes. Defaults to all silver business-info dirs.")
    parser.add_argument("--start-period", help="First source actual period for estimates, e.g. 2023.12.")
    parser.add_argument("--end-period", help="Last source actual period for estimates, e.g. 2026.03.")
    parser.add_argument(
        "--all-periods",
        action="store_true",
        help="Build historical estimates for every available source actual period.",
    )
    parser.add_argument("--progress-interval", type=int, default=25)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    results = create_operating_metric_gold_for_stocks(
        _parse_stock_codes(args.stock_codes),
        estimate_start_period=args.start_period,
        estimate_end_period=args.end_period,
        estimate_all_periods=args.all_periods,
        progress=not args.no_progress,
        progress_interval=args.progress_interval,
        continue_on_error=not args.fail_fast,
    )
    for result in results:
        print(
            f"[DONE] {result.metric_path.parent.name}: "
            f"raw={result.raw_rows}, metrics={result.metric_rows}, "
            f"unit={result.unit_rows}, drivers={result.driver_rows}, "
            f"components={result.estimate_component_rows}, consensus={result.estimate_consensus_rows}"
        )


if __name__ == "__main__":
    main()
