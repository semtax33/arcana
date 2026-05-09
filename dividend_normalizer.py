from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from glob import glob
from pathlib import Path
from typing import Any


DEFAULT_INPUT_GLOB = "./data-lake/bronze/dart/dividend/**/*.json"
DEFAULT_COMPANY_SUMMARY_CSV = "./data-lake/silver/dart/dividend/dividend_company_summary.csv"
DEFAULT_BY_STOCK_KIND_CSV = "./data-lake/silver/dart/dividend/dividend_by_stock_kind.csv"

REPORT_NAMES = {
    "11011": "annual",
    "11012": "half",
    "11013": "q1",
    "11014": "q3",
}

COMMON_META_COLUMNS = [
    "stock_code",
    "corp_code",
    "corp_name",
    "bsns_year",
    "reprt_code",
    "report_name",
    "rcept_no",
    "stlm_dt",
]

COMPANY_SUMMARY_COLUMNS = [
    *COMMON_META_COLUMNS,
    "dividend_payment_amount_million_krw",
    "dividend_payment_amount_krw",
    "dividend_payout_ratio_pct",
    "source_file",
]

BY_STOCK_KIND_COLUMNS = [
    *COMMON_META_COLUMNS,
    "stock_knd",
    "market_dividend_yield_pct",
    "per_share_cash_dividend_krw",
    "stock_knd_infer_method",
    "source_file",
]

STOCK_KIND_UNKNOWN = "__UNKNOWN__"


@dataclass
class MetricValue:
    value: Decimal | None
    raw_value: str
    item_name: str
    stock_knd: str = ""
    infer_method: str = "raw"


@dataclass
class DividendRecordBuilder:
    stock_code: str
    bsns_year: str
    reprt_code: str
    report_name: str
    source_file: str

    corp_code: str = ""
    corp_name: str = ""
    rcept_no: str = ""
    stlm_dt: str = ""

    cash_total: MetricValue | None = None
    payout_ratio: MetricValue | None = None

    yield_by_stock_kind: dict[str, MetricValue] = field(default_factory=dict)
    per_share_cash_by_stock_kind: dict[str, MetricValue] = field(default_factory=dict)

    def update_common_meta(self, row: dict[str, Any]) -> None:
        self.corp_code = self.corp_code or clean_text(row.get("corp_code"))
        self.corp_name = self.corp_name or clean_text(row.get("corp_name"))
        self.rcept_no = self.rcept_no or clean_text(row.get("rcept_no"))
        self.stlm_dt = self.stlm_dt or clean_text(row.get("stlm_dt"))

    def common_meta(self) -> dict[str, str]:
        return {
            "stock_code": self.stock_code,
            "corp_code": self.corp_code,
            "corp_name": self.corp_name,
            "bsns_year": self.bsns_year,
            "reprt_code": self.reprt_code,
            "report_name": self.report_name,
            "rcept_no": self.rcept_no,
            "stlm_dt": self.stlm_dt,
        }

    def set_cash_total(self, metric: MetricValue) -> None:
        self.cash_total = choose_metric(self.cash_total, metric)

    def set_payout_ratio(self, metric: MetricValue) -> None:
        self.payout_ratio = choose_metric(self.payout_ratio, metric)

    def set_yield(self, metric: MetricValue) -> None:
        key = normalize_stock_knd(metric.stock_knd)
        metric.stock_knd = key
        self.yield_by_stock_kind[key] = choose_metric(
            self.yield_by_stock_kind.get(key),
            metric,
        )

    def set_per_share_cash(self, metric: MetricValue) -> None:
        key = normalize_stock_knd(metric.stock_knd)
        metric.stock_knd = key
        self.per_share_cash_by_stock_kind[key] = choose_metric(
            self.per_share_cash_by_stock_kind.get(key),
            metric,
        )

    def to_company_summary_row(self) -> dict[str, Any] | None:
        """
        회사 전체 지표만 담는다.

        대상:
        - 현금배당금총액
        - 현금배당성향

        여기에는 stock_knd를 넣지 않는다.
        """
        cash_total_million = metric_to_csv_value(self.cash_total)
        payout_ratio = metric_to_csv_value(self.payout_ratio)

        if not any([cash_total_million, payout_ratio]):
            return None

        cash_total_krw = ""
        if self.cash_total is not None:
            cash_total_krw = decimal_to_csv_value(
                multiply_decimal(self.cash_total.value, Decimal("1000000"))
            )

        return {
            **self.common_meta(),
            "dividend_payment_amount_million_krw": cash_total_million,
            "dividend_payment_amount_krw": cash_total_krw,
            "dividend_payout_ratio_pct": payout_ratio,
            "source_file": self.source_file,
        }

    def to_by_stock_kind_rows(self) -> list[dict[str, Any]]:
        """
        주식종류별 지표만 담는다.

        대상:
        - 현금배당수익률 / 시가배당률
        - 주당현금배당금
        """
        raw_stock_kinds = sorted(
            {
                *(
                    key
                    for key, metric in self.yield_by_stock_kind.items()
                    if metric.value is not None
                ),
                *(
                    key
                    for key, metric in self.per_share_cash_by_stock_kind.items()
                    if metric.value is not None
                ),
            }
        )

        if not raw_stock_kinds:
            return []

        resolved_stock_kinds = infer_blank_stock_kinds(raw_stock_kinds)

        merged_by_stock_kind: dict[str, dict[str, Any]] = {}
        infer_methods_by_stock_kind: dict[str, set[str]] = {}

        for raw_stock_knd in raw_stock_kinds:
            stock_knd, infer_method = resolved_stock_kinds[raw_stock_knd]

            dividend_yield = metric_to_csv_value(
                self.yield_by_stock_kind.get(raw_stock_knd)
            )
            per_share_cash = metric_to_csv_value(
                self.per_share_cash_by_stock_kind.get(raw_stock_knd)
            )

            if not any([dividend_yield, per_share_cash]):
                continue

            row = merged_by_stock_kind.setdefault(
                stock_knd,
                {
                    **self.common_meta(),
                    "stock_knd": stock_knd,
                    "market_dividend_yield_pct": "",
                    "per_share_cash_dividend_krw": "",
                    "stock_knd_infer_method": "",
                    "source_file": self.source_file,
                },
            )

            infer_methods_by_stock_kind.setdefault(stock_knd, set()).add(infer_method)

            # 같은 stock_knd로 병합될 때는 빈 값만 채운다.
            # 이미 명시적 stock_knd row가 채운 값이 있으면 그 값을 우선한다.
            if not row["market_dividend_yield_pct"] and dividend_yield:
                row["market_dividend_yield_pct"] = dividend_yield

            if not row["per_share_cash_dividend_krw"] and per_share_cash:
                row["per_share_cash_dividend_krw"] = per_share_cash

        rows: list[dict[str, Any]] = []
        for stock_knd, row in sorted(merged_by_stock_kind.items()):
            row["stock_knd_infer_method"] = "+".join(
                sorted(infer_methods_by_stock_kind[stock_knd])
            )
            rows.append(row)

        return rows


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_item_name(value: Any) -> str:
    text = clean_text(value)
    return re.sub(r"\s+", "", text)


def normalize_stock_knd(value: Any) -> str:
    """
    DART stock_knd 값은 회사마다 표현이 조금씩 다르다.
    여기서는 지나친 추론을 피하고, 명백한 보통주/우선주 계열만 표준화한다.
    """
    text = clean_text(value)

    if not text or text in {"-", "--", "N/A", "n/a", "해당사항없음", "해당 사항 없음"}:
        return ""

    compact = re.sub(r"\s+", "", text)
    compact = re.sub(r"\(주\d+\)|주\d+|\*+", "", compact)

    # DART 원문/OCR/HTML 파싱 과정에서 흔히 보이는 변형까지(주\d+\)|주\d+|\*+", "", compact)

    # DART 원 최소 보정
    if any(
        token in compact
        for token in [
            "보통주",
            "보통주식",
            "기명식보통주",
            "보통",
            "보동주",
            "보퉁주",
            "보통중",
            "보통부",
            "보통투",
        ]
    ):
        return "보통주"

    if any(token in compact for token in ["1우선", "제1우선", "1종"]):
        return "1우선주"

    if any(token in compact for token in ["2우선", "제2우선", "2종"]):
        return "2우선주"

    if any(token in compact for token in ["3우선", "제3우선", "3종"]):
        return "3우선주"

    if any(token in compact for token in ["4우선", "제4우선", "4종"]):
        return "4우선주"

    if any(token in compact for token in ["우선주", "우선", "종류주식", "상환전환"]):
        return "우선주"

    if "기타" in compact:
        return "기타주"

    return text


def infer_blank_stock_kinds(stock_kinds: list[str]) -> dict[str, tuple[str, str]]:
    """
    by_stock_kind 테이블에 들어오는 값은 주당배당금/배당수익률처럼 주식종류별 지표다.

    보정 규칙:
    1. stock_knd가 원문에 있으면 raw/normalized로 사용
    2. stock_knd가 비어 있고, 해당 보고서 안에 명시적 주식종류가 1개뿐이면 그 종류로 병합
    3. stock_knd가 비어 있고, 해당 보고서 안에 다른 주식종류가 없으면 보통주로 추정
    4. stock_knd가 비어 있는데 다른 주식종류가 2개 이상 함께 있으면 UNKNOWN으로 보존
    """
    nonblank = [kind for kind in stock_kinds if kind]
    result: dict[str, tuple[str, str]] = {}

    for kind in stock_kinds:
        if kind:
            result[kind] = (kind, "raw_or_normalized")
        elif len(nonblank) == 1:
            result[kind] = (nonblank[0], "inferred_blank_from_single_explicit_kind")
        elif len(nonblank) == 0:
            result[kind] = ("보통주", "inferred_blank_single_kind_as_common")
        else:
            result[kind] = (
                STOCK_KIND_UNKNOWN,
                "unknown_blank_with_multiple_explicit_kinds",
            )

    return result


def parse_decimal(value: Any) -> Decimal | None:
    text = clean_text(value)

    if not text or text in {"-", "--", "N/A", "n/a"}:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace(",", "").replace("%", "").replace(" ", "")
    text = text.replace("△", "-").replace("▲", "-")

    if not text or text == "-":
        return None

    try:
        number = Decimal(text)
    except InvalidOperation:
        return None

    return -number if negative else number


def decimal_to_csv_value(value: Decimal | None) -> str:
    if value is None:
        return ""

    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))

    return format(value.normalize(), "f")


def multiply_decimal(value: Decimal | None, multiplier: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value * multiplier


def metric_to_csv_value(metric: MetricValue | None) -> str:
    if metric is None:
        return ""
    return decimal_to_csv_value(metric.value)


def metric_priority(metric: MetricValue) -> tuple[int, int]:
    """
    같은 metric이 여러 개 있으면 연결 기준, 값 존재 기준을 우선한다.
    """
    item_name = metric.item_name
    is_consolidated = "(연결)" in item_name or "연결" in item_name
    has_value = metric.value is not None

    return (
        1 if is_consolidated else 0,
        1 if has_value else 0,
    )


def choose_metric(current: MetricValue | None, candidate: MetricValue) -> MetricValue:
    if current is None:
        return candidate

    if metric_priority(candidate) >= metric_priority(current):
        return candidate

    return current


def classify_metric(item_name: str) -> str | None:
    normalized = normalize_item_name(item_name)

    if "현금배당금총액" in normalized:
        return "cash_total"

    if "현금배당성향" in normalized:
        return "payout_ratio"

    if "현금배당수익률" in normalized or "시가배당률" in normalized:
        return "market_yield"

    if "주당현금배당금" in normalized:
        return "per_share_cash"

    return None


def infer_file_meta(path: Path) -> dict[str, str]:
    """
    기본 경로 가정:
    ./data-lake/bronze/dart/dividend/{stock_code}/{bsns_year}/{reprt_code}_{report_name}.json
    """
    stock_code = path.parents[1].name if len(path.parents) >= 2 else ""
    bsns_year = path.parent.name if path.parent.name.isdigit() else ""

    reprt_code = ""
    report_name = ""

    stem_parts = path.stem.split("_", 1)
    if stem_parts:
        reprt_code = stem_parts[0]
        report_name = (
            stem_parts[1]
            if len(stem_parts) > 1
            else REPORT_NAMES.get(reprt_code, "")
        )

    return {
        "stock_code": stock_code.zfill(6) if stock_code.isdigit() else stock_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "report_name": report_name,
    }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def normalize_dividend_file(
    path: str | Path,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    path = Path(path)
    meta = infer_file_meta(path)
    data = load_json(path)

    rows = data.get("list")

    if not isinstance(rows, list) or str(data.get("status", "")).strip() != "000":
        return None, []

    builder = DividendRecordBuilder(
        stock_code=meta["stock_code"],
        bsns_year=meta["bsns_year"],
        reprt_code=meta["reprt_code"],
        report_name=meta["report_name"],
        source_file=str(path),
    )

    for row in rows:
        if not isinstance(row, dict):
            continue

        builder.update_common_meta(row)

        item_name = clean_text(row.get("se"))
        metric_name = classify_metric(item_name)

        if metric_name is None:
            continue

        metric = MetricValue(
            value=parse_decimal(row.get("thstrm")),
            raw_value=clean_text(row.get("thstrm")),
            item_name=item_name,
            stock_knd=clean_text(row.get("stock_knd")),
        )

        if metric_name == "cash_total":
            builder.set_cash_total(metric)

        elif metric_name == "payout_ratio":
            builder.set_payout_ratio(metric)

        elif metric_name == "market_yield":
            builder.set_yield(metric)

        elif metric_name == "per_share_cash":
            builder.set_per_share_cash(metric)

    return builder.to_company_summary_row(), builder.to_by_stock_kind_rows()


def write_csv(path: str | Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def normalize_dividends(
    input_glob: str = DEFAULT_INPUT_GLOB,
    company_summary_csv_path: str | Path = DEFAULT_COMPANY_SUMMARY_CSV,
    by_stock_kind_csv_path: str | Path = DEFAULT_BY_STOCK_KIND_CSV,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = sorted(glob(input_glob, recursive=True))

    if not files:
        raise FileNotFoundError(f"No dividend JSON files matched: {input_glob}")

    company_summary_rows: list[dict[str, Any]] = []
    by_stock_kind_rows: list[dict[str, Any]] = []
    failed_files: list[tuple[str, str]] = []

    for file_path in files:
        try:
            company_row, stock_kind_rows = normalize_dividend_file(file_path)

            if company_row is not None:
                company_summary_rows.append(company_row)

            by_stock_kind_rows.extend(stock_kind_rows)

        except (OSError, json.JSONDecodeError) as e:
            failed_files.append((file_path, repr(e)))

    write_csv(
        company_summary_csv_path,
        COMPANY_SUMMARY_COLUMNS,
        company_summary_rows,
    )

    write_csv(
        by_stock_kind_csv_path,
        BY_STOCK_KIND_COLUMNS,
        by_stock_kind_rows,
    )

    print(
        f"[DONE] files={len(files)}, "
        f"company_summary_rows={len(company_summary_rows)}, "
        f"by_stock_kind_rows={len(by_stock_kind_rows)}, "
        f"failed={len(failed_files)}, "
        f"company_summary={company_summary_csv_path}, "
        f"by_stock_kind={by_stock_kind_csv_path}"
    )

    if failed_files:
        failed_path = Path(company_summary_csv_path).with_suffix(".failed.csv")

        with failed_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["source_file", "error"])
            writer.writeheader()
            writer.writerows(
                {
                    "source_file": source_file,
                    "error": error,
                }
                for source_file, error in failed_files
            )

        print(f"[WARN] failed file list saved: {failed_path}")

    return company_summary_rows, by_stock_kind_rows


'''
    normalize_dividends(
        input_glob=args.input_glob,
        company_summary_csv_path=args.company_summary_output,
        by_stock_kind_csv_path=args.by_stock_kind_output,
    )
'''