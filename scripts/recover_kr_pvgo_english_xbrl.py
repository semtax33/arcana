from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import pandas as pd
import requests
import warnings

from engine.core.paths import DATA_LAKE, statement_symbol_name
from engine.core.source_storage import write_source_text
from engine.extractors._internal.dart_filings import (
    DartRequestThrottle,
    _wait_for_dart_request,
    request_with_retry,
)
from engine.transformers._internal.dart_filings import (
    EXPECTED_HEADER,
    current_statement_amount_column,
    parse_amount,
)
from engine.transformers._internal.filing_periods import (
    REPORT_METADATA_PATH as GLOBAL_REPORT_METADATA_PATH,
)


SOURCE_START_DATE = "2011-01-01"
SOURCE_END_DATE = "2016-12-31"
SOURCE_MIN_FISCAL_YEAR = 2010
SOURCE_MAX_FISCAL_YEAR = 2016
SEARCH_URL = "https://englishdart.fss.or.kr/dsbd002/search.ax"
VIEWER_URL = "https://englishdart.fss.or.kr/dsbh002/viewer.do"

DEFAULT_TARGET_PATH = DATA_LAKE.meta("kr_pvgo_2012_2016_factor_targets.csv")
DEFAULT_PART_DIR = DATA_LAKE.meta("kr_pvgo_2012_2016_report_metadata_parts")
DEFAULT_METADATA_PATH = DATA_LAKE.silver(
    "dart", "kr_pvgo_2012_2016_report_metadata.csv"
)
DEFAULT_SOURCE_ROOT = DATA_LAKE.bronze(
    "dart-xbrl-en", "finance-statement", "kr_pvgo_2012_2016"
)
DEFAULT_SNAPSHOT_ROOT = DATA_LAKE.silver(
    "dart", "pvgo-2012-2016-normalized-snapshots"
)
DEFAULT_OUTPUT_DIR = DATA_LAKE.silver(
    "dart", "pvgo-2012-2016-normalized"
)
DEFAULT_STATUS_PATH = DATA_LAKE.meta(
    "kr_pvgo_2012_2016_english_xbrl_recovery_status.json"
)
DEFAULT_EXISTING_NORMALIZED_DIR = DATA_LAKE.silver("dart", "normalized")
DEFAULT_EXISTING_SOURCE_ROOT = DATA_LAKE.bronze("dart", "finance-statement")

METADATA_COLUMNS = [
    "security_id",
    "stock_code",
    "fiscal_year",
    "fiscal_month",
    "period_end_date",
    "report_date",
    "rcept_no",
    "report_name",
    "source_type",
    "source_url",
    "updated_at",
]

SECTION_TYPES = {
    "D21": "BS",
    "D22": "BS",
    "D23": "BS",
    "D31": "IS",
    "D32": "IS",
    "D41": "CIS",
    "D42": "CIS",
    "D43": "CIS",
    "D44": "CIS",
    "D51": "CF",
    "D52": "CF",
}


def _rule(
    canonical_id: str,
    statement_type: str,
    patterns: Iterable[str],
    *,
    exclude: Iterable[str] = (),
    amount_policy: str = "as_reported",
) -> dict[str, Any]:
    return {
        "canonical_id": canonical_id,
        "statement_type": statement_type,
        "patterns": tuple(re.compile(value, re.IGNORECASE) for value in patterns),
        "exclude": tuple(re.compile(value, re.IGNORECASE) for value in exclude),
        "amount_policy": amount_policy,
    }


# The English DART XBRL viewer uses FSS-standardized English labels.  Keep this
# intentionally narrow: these are the audited inputs required by the four PVGO
# factors, rather than a second general-purpose statement taxonomy.
PVGO_LABEL_RULES = (
    _rule("TOTAL_ASSETS", "BS", (r"^total\s+assets$",)),
    _rule("TOTAL_EQUITY", "BS", (r"^total\s+equity$", r"^equity\s+total$")),
    _rule(
        "EAOP",
        "BS",
        (
            r"^equity\s+attributable\s+to\s+(?:the\s+)?owners\s+of\s+(?:the\s+)?parent(?:\s+company)?$",
            r"^owners(?:'|\u2019)?\s+equity$",
        ),
    ),
    _rule("CURRENT_ASSETS", "BS", (r"^current\s+assets$",)),
    _rule("CURRENT_LIABILITIES", "BS", (r"^current\s+liabilit(?:y|ies)$",)),
    _rule(
        "CASH_AND_EQUIVALENTS",
        "BS",
        (r"^cash(?:\s+and|,)\s+cash\s+equivalents$",),
    ),
    _rule(
        "SHORT_TERM_FINANCIAL_ASSETS",
        "BS",
        (
            r"^short[- ]term\s+financial\s+(?:assets?|instruments?|products?|deposits?)$",
            r"^current\s+financial\s+assets$",
        ),
    ),
    _rule("INVENTORIES", "BS", (r"^inventor(?:y|ies)$",)),
    _rule(
        "TRADE_AND_OTHER_RECEIVABLES",
        "BS",
        (
            r"^(?:short[- ]term\s+)?trade\s+and\s+other\s+(?:current\s+)?(?:account\s+)?receivables?$",
        ),
    ),
    _rule(
        "TRADE_RECEIVABLES",
        "BS",
        (
            r"^(?:short[- ]term\s+)?(?:accounts?\s+receivable|account\s+receivables|trade\s+receivables?)(?:\s+trade)?$",
            r"^receivables\s+trade$",
        ),
    ),
    _rule(
        "TRADE_AND_OTHER_PAYABLES",
        "BS",
        (
            r"^(?:short[- ]term\s+)?trade\s+and\s+other\s+(?:current\s+)?(?:account\s+)?payables?$",
        ),
    ),
    _rule(
        "TRADE_PAYABLES",
        "BS",
        (
            r"^(?:short[- ]term\s+)?(?:accounts?\s+payable|account\s+payables|trade\s+payables?)(?:\s+trade)?$",
            r"^payables\s+trade$",
        ),
    ),
    _rule(
        "PPE",
        "BS",
        (r"^property,?\s+plant\s+and\s+equipment(?:,?\s+net)?$",),
    ),
    _rule(
        "INTANGIBLE_ASSETS",
        "BS",
        (
            r"^intangible\s+assets?(?:\s+other\s+than\s+goodwill)?(?:,?\s+net)?$",
        ),
        exclude=(r"goodwill",),
    ),
    _rule(
        "SHORT_TERM_DEBT",
        "BS",
        (
            r"^short[- ]term\s+(?:debt|borrowings?|loans?)",
            r"^current\s+portion\s+of\s+long[- ]term",
            r"^current\s+(?:debentures?|bonds?)",
        ),
    ),
    _rule(
        "LONG_TERM_DEBT",
        "BS",
        (
            r"^long[- ]term\s+(?:debt|borrowings?|loans?)",
            r"^non[- ]current\s+(?:debt|borrowings?|loans?)",
            r"^(?:debentures?|bonds?)(?:\s+payable)?$",
        ),
        exclude=(r"current\s+portion",),
    ),
    _rule(
        "REVENUE",
        "IS",
        (
            r"^(?:total\s+)?(?:net\s+)?revenue(?:\(sales\))?$",
            r"^(?:net\s+)?sales$",
            r"^operating\s+revenue$",
        ),
        amount_policy="abs",
    ),
    _rule("COGS", "IS", (r"^cost\s+of\s+(?:sales|revenue)$",), amount_policy="abs"),
    _rule("GROSS_PROFIT", "IS", (r"^gross\s+profit(?:\s*\(loss\))?$",)),
    _rule(
        "OPERATING_INCOME",
        "IS",
        (
            r"^operating\s+(?:income|profit)(?:\s*\(loss\))?$",
            r"^(?:profit|income)\s+from\s+operating\s+activities$",
        ),
    ),
    _rule(
        "PBT",
        "IS",
        (r"^(?:profit|income|loss).*before\s+(?:income\s+)?tax(?:es)?$",),
    ),
    _rule(
        "TAX_EXPENSE",
        "IS",
        (r"^(?:income\s+)?tax(?:es)?\s+(?:expense|expenses|benefit)$",),
        amount_policy="abs",
    ),
    _rule(
        "NET_INCOME_PARENT",
        "IS",
        (
            r"^(?:net\s+)?(?:income|profit|loss).*attributable\s+to\s+(?:the\s+)?owners\s+of\s+(?:the\s+)?parent",
        ),
    ),
    _rule(
        "NET_INCOME",
        "IS",
        (
            r"^(?:net\s+)?(?:income|profit)(?:\s*\(loss\))?(?:\s+for\s+the\s+period)?$",
            r"^(?:net\s+)?loss(?:\s+for\s+the\s+period)?$",
        ),
        exclude=(r"attributable",),
    ),
    _rule(
        "CFO",
        "CF",
        (r"^net\s+cash(?:\s+flows?)?\s+(?:provided\s+by|used\s+in|from)\s+operating\s+activities$",),
    ),
    _rule(
        "CFI",
        "CF",
        (r"^net\s+cash(?:\s+flows?)?\s+(?:provided\s+by|used\s+in|from)\s+investing\s+activities$",),
    ),
    _rule(
        "CFF",
        "CF",
        (r"^net\s+cash(?:\s+flows?)?\s+(?:provided\s+by|used\s+in|from)\s+financing\s+activities$",),
    ),
)

DEBT_CANONICAL_IDS = {"SHORT_TERM_DEBT", "LONG_TERM_DEBT"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    write_source_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        source="arcana-kr-pvgo-english-xbrl",
    )


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)


def _target_symbols(path: Path) -> list[str]:
    frame = pd.read_csv(path, dtype={"symbol": str})
    return sorted(set(frame["symbol"].dropna().astype(str).str.zfill(6)))


def _search_payload(symbol: str, page: int) -> list[tuple[str, str]]:
    return [
        ("currentPage", str(page)),
        ("maxResults", "15"),
        ("maxLinks", "10"),
        ("sort", "date"),
        ("series", "asc"),
        ("textCrpCik", ""),
        ("textCrpNm", symbol),
        ("startDate", SOURCE_START_DATE.replace("-", "")),
        ("endDate", SOURCE_END_DATE.replace("-", "")),
        ("closingAccounts", "0401"),
        ("closingAccounts", "0403"),
        ("closingAccounts", "0402"),
        ("closingAccounts", "0404"),
        ("taxonomy", "0311,0315"),
        ("taxonomy", "0312"),
        ("taxonomy", "0313"),
        ("taxonomy", "0314"),
        ("taxonomy", "0316,0317,0318"),
    ]


def parse_english_xbrl_search_page(
    html: str,
    symbol: str,
) -> tuple[pd.DataFrame, int]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        report_anchor = tr.find(
            "a", href=lambda value: bool(value and "/dsbh002/main.do?rcpNo=" in value)
        )
        if report_anchor is None:
            continue
        title = " ".join(report_anchor.get_text(" ", strip=True).split())
        period_match = re.search(r"\((\d{4})\.(\d{2})\)", title)
        receipt_match = re.search(r"rcpNo=(\d{14})", str(report_anchor.get("href")))
        cells = tr.find_all("td", recursive=False)
        if period_match is None or receipt_match is None or len(cells) < 4:
            continue
        report_date = pd.to_datetime(cells[1].get_text(" ", strip=True), errors="coerce")
        if pd.isna(report_date):
            continue
        fiscal_year = int(period_match.group(1))
        fiscal_month = int(period_match.group(2))
        if not SOURCE_MIN_FISCAL_YEAR <= fiscal_year <= SOURCE_MAX_FISCAL_YEAR:
            continue
        rcept_no = receipt_match.group(1)
        corp_anchor = tr.find(
            "a", href=lambda value: bool(value and "/dsbc001/selectPopup.ax" in value)
        )
        corp_match = re.search(
            r"selectKey=(\d{8})", str(corp_anchor.get("href")) if corp_anchor else ""
        )
        period_end = pd.Timestamp(fiscal_year, fiscal_month, 1) + pd.offsets.MonthEnd(0)
        rows.append(
            {
                "security_id": f"SEC_KR_{symbol}",
                "stock_code": symbol,
                "fiscal_year": fiscal_year,
                "fiscal_month": fiscal_month,
                "period_end_date": period_end.date().isoformat(),
                "report_date": report_date.date().isoformat(),
                "rcept_no": rcept_no,
                "report_name": title,
                "source_type": "statement",
                "source_url": f"https://englishdart.fss.or.kr/dsbh002/main.do?rcpNo={rcept_no}",
                "updated_at": _utc_now(),
                "corp_code": corp_match.group(1) if corp_match else "",
                "corp_name_en": (
                    " ".join(corp_anchor.get_text(" ", strip=True).split())
                    if corp_anchor
                    else ""
                ),
            }
        )
    text = soup.get_text(" ", strip=True)
    page_matches = re.findall(r"\[(\d+)\s*/\s*(\d+)\]", text)
    total_pages = max((int(total) for _, total in page_matches), default=1)
    columns = METADATA_COLUMNS + ["corp_code", "corp_name_en"]
    return pd.DataFrame(rows, columns=columns), total_pages


def fetch_english_xbrl_metadata(
    symbol: str,
    *,
    throttle: DartRequestThrottle,
) -> pd.DataFrame:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://englishdart.fss.or.kr/dsbd002/main.do",
        "X-Requested-With": "XMLHttpRequest",
    }
    frames: list[pd.DataFrame] = []
    with requests.Session() as session:
        page = 1
        total_pages = 1
        while page <= total_pages:
            _wait_for_dart_request(throttle, 0)
            response = request_with_retry(
                session,
                "POST",
                SEARCH_URL,
                headers=headers,
                data=_search_payload(symbol, page),
                timeout=30,
                throttle=throttle,
            )
            response.encoding = "utf-8"
            frame, discovered_pages = parse_english_xbrl_search_page(
                response.text, symbol
            )
            total_pages = max(total_pages, discovered_pages)
            if not frame.empty:
                frames.append(frame)
            page += 1
    if not frames:
        return pd.DataFrame(columns=METADATA_COLUMNS + ["corp_code", "corp_name_en"])
    result = pd.concat(frames, ignore_index=True)
    return (
        result.sort_values(["report_date", "rcept_no"])
        .drop_duplicates(["stock_code", "rcept_no"], keep="last")
        .reset_index(drop=True)
    )


def consolidate_metadata_parts(
    *,
    target_path: Path = DEFAULT_TARGET_PATH,
    part_dir: Path = DEFAULT_PART_DIR,
    metadata_path: Path = DEFAULT_METADATA_PATH,
    legacy_metadata_path: Path = GLOBAL_REPORT_METADATA_PATH,
) -> pd.DataFrame:
    symbols = _target_symbols(target_path)
    frames = []
    if legacy_metadata_path.exists():
        try:
            legacy_metadata = pd.read_csv(
                legacy_metadata_path,
                dtype={"stock_code": str, "rcept_no": str},
            )
        except (OSError, ValueError, pd.errors.EmptyDataError):
            legacy_metadata = pd.DataFrame()
        if not legacy_metadata.empty and "stock_code" in legacy_metadata.columns:
            legacy_metadata["stock_code"] = (
                legacy_metadata["stock_code"].astype(str).str.zfill(6)
            )
            legacy_metadata = legacy_metadata.loc[
                legacy_metadata["stock_code"].isin(symbols)
            ].copy()
            if not legacy_metadata.empty:
                frames.append(legacy_metadata)
    for symbol in symbols:
        path = part_dir / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path, dtype=str)
        except pd.errors.EmptyDataError:
            continue
        if not frame.empty:
            frames.append(frame)
    merged = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=METADATA_COLUMNS)
    )
    for column in METADATA_COLUMNS:
        if column not in merged.columns:
            merged[column] = ""
    if not merged.empty:
        merged["report_date"] = pd.to_datetime(merged["report_date"], errors="coerce")
        merged["fiscal_year"] = pd.to_numeric(merged["fiscal_year"], errors="coerce")
        merged["fiscal_month"] = pd.to_numeric(merged["fiscal_month"], errors="coerce")
        merged = merged.loc[
            merged["report_date"].le(pd.Timestamp(SOURCE_END_DATE))
            & merged["fiscal_year"].le(SOURCE_MAX_FISCAL_YEAR)
            & merged["fiscal_month"].isin([3, 6, 9, 12])
        ].copy()
        merged = (
            merged.sort_values(
                [
                    "stock_code",
                    "fiscal_year",
                    "fiscal_month",
                    "report_date",
                    "rcept_no",
                ]
            )
            .drop_duplicates(
                ["stock_code", "fiscal_year", "fiscal_month"], keep="last"
            )
            .sort_values(["stock_code", "report_date"])
        )
        merged["report_date"] = merged["report_date"].dt.date.astype(str)
        merged["fiscal_year"] = merged["fiscal_year"].astype(int)
        merged["fiscal_month"] = merged["fiscal_month"].astype(int)
    _atomic_write_csv(merged[METADATA_COLUMNS], metadata_path)
    return merged


def recover_metadata(
    *,
    target_path: Path = DEFAULT_TARGET_PATH,
    part_dir: Path = DEFAULT_PART_DIR,
    metadata_path: Path = DEFAULT_METADATA_PATH,
    status_path: Path = DEFAULT_STATUS_PATH,
    workers: int = 2,
    request_interval: float = 1.0,
) -> dict[str, Any]:
    symbols = _target_symbols(target_path)
    part_dir.mkdir(parents=True, exist_ok=True)
    pending = [symbol for symbol in symbols if not (part_dir / f"{symbol}.csv").exists()]
    throttle = DartRequestThrottle(max(float(request_interval), 0.0))
    failures: list[dict[str, str]] = []
    completed = 0

    def fetch_one(symbol: str) -> tuple[str, int]:
        frame = fetch_english_xbrl_metadata(symbol, throttle=throttle)
        _atomic_write_csv(frame, part_dir / f"{symbol}.csv")
        return symbol, len(frame)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {executor.submit(fetch_one, symbol): symbol for symbol in pending}
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                _, row_count = future.result()
            except Exception as error:
                failures.append({"symbol": symbol, "error": repr(error)})
            else:
                completed += 1
                if completed % 50 == 0:
                    print(
                        f"[EN-XBRL-METADATA] completed={completed:,}/{len(pending):,} "
                        f"last={symbol} rows={row_count:,} failures={len(failures):,}",
                        flush=True,
                    )

    merged = consolidate_metadata_parts(
        target_path=target_path,
        part_dir=part_dir,
        metadata_path=metadata_path,
    )

    report = {
        "generated_at": _utc_now(),
        "stage": "metadata",
        "target_count": len(symbols),
        "preexisting_part_count": len(symbols) - len(pending),
        "attempted_count": len(pending),
        "completed_count": completed,
        "part_count": sum((part_dir / f"{symbol}.csv").exists() for symbol in symbols),
        "selected_metadata_rows": len(merged),
        "selected_metadata_securities": int(merged["stock_code"].nunique()) if not merged.empty else 0,
        "failure_count": len(failures),
        "failures": failures,
        "metadata_path": str(metadata_path),
    }
    _write_json(status_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def _statement_type(section_id: str) -> str | None:
    match = re.search(r"_(D\d{2})\d*", section_id)
    if match:
        return SECTION_TYPES.get(match.group(1))
    return None


def _english_unit_factor(text: str) -> int:
    normalized = " ".join(str(text).split()).lower()
    if re.search(r"\bunit\s*:\s*billion\b", normalized):
        return 1_000_000_000
    if re.search(r"\bunit\s*:\s*million\b", normalized):
        return 1_000_000
    if re.search(r"\bunit\s*:\s*thousand\b", normalized):
        return 1_000
    return 1


def _normalize_label(value: str) -> str:
    label = " ".join(str(value).replace("\xa0", " ").split()).strip()
    prefix = re.compile(
        r"^(?:(?:[IVXLCDM]+[\u2160-\u217f]*|[\u2160-\u217f]+)[.)]|\(\d+\)|\d+[.)])\s*",
        re.IGNORECASE,
    )
    previous = None
    while label and label != previous:
        previous = label
        label = prefix.sub("", label, count=1).strip()
    return label


def _match_label(label: str, statement_type: str) -> tuple[dict[str, Any], int] | None:
    effective_type = "IS" if statement_type == "CIS" else statement_type
    for rule_index, rule in enumerate(PVGO_LABEL_RULES):
        if rule["statement_type"] != effective_type:
            continue
        if any(pattern.search(label) for pattern in rule["exclude"]):
            continue
        if any(pattern.search(label) for pattern in rule["patterns"]):
            return rule, rule_index
    return None


def _canonical_names() -> dict[str, str]:
    frame = pd.read_csv(DATA_LAKE.canonical_accounts(), dtype=str).fillna("")
    return dict(zip(frame["canonical_id"], frame["canonical_nm"]))


def parse_english_xbrl_statement(
    html: str,
    *,
    symbol: str,
    fiscal_year: int,
    fiscal_month: int,
    rcept_no: str,
) -> pd.DataFrame:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html, "lxml")
    period = f"{int(fiscal_year):04d}.{int(fiscal_month):02d}"
    candidates: dict[str, list[dict[str, Any]]] = {}
    scope_prefix = "P_EDITOR_CONS_"
    sections = soup.find_all(id=lambda value: bool(value and str(value).startswith(scope_prefix)))
    if not sections:
        scope_prefix = "P_EDITOR_SEPT_"
        sections = soup.find_all(id=lambda value: bool(value and str(value).startswith(scope_prefix)))

    for section in sections:
        section_id = str(section.get("id") or "")
        statement_type = _statement_type(section_id)
        if statement_type not in {"BS", "IS", "CIS", "CF"}:
            continue
        tables = section.find_all("table")
        if not tables:
            continue
        body_table = tables[-1]
        amount_column = current_statement_amount_column(
            body_table,
            statement_type=statement_type,
            period=period,
        )
        unit_factor = _english_unit_factor(section.get_text(" ", strip=True)[:2_000])
        for row_index, tr in enumerate(body_table.find_all("tr")):
            cells = tr.find_all("td", recursive=False)
            if len(cells) <= amount_column:
                continue
            label = _normalize_label(cells[0].get_text(" ", strip=True))
            match = _match_label(label, statement_type)
            if match is None:
                continue
            rule, rule_index = match
            raw_text = cells[amount_column].get_text(" ", strip=True)
            raw_value = parse_amount(raw_text, unit_factor)
            value = float(raw_value)
            if rule["amount_policy"] == "abs":
                value = abs(value)
            canonical_id = str(rule["canonical_id"])
            candidates.setdefault(canonical_id, []).append(
                {
                    "canonical_id": canonical_id,
                    "statement_type": statement_type,
                    "label": label,
                    "raw_value": float(raw_value),
                    "value": value,
                    "amount_policy": rule["amount_policy"],
                    "rule_index": rule_index,
                    "row_index": row_index,
                }
            )

    canonical_names = _canonical_names()
    output_rows: list[dict[str, Any]] = []
    for canonical_id, matches in candidates.items():
        if canonical_id in DEBT_CANONICAL_IDS and len(matches) > 1:
            aggregate = [row for row in matches if re.search(r"\btotal\b", row["label"], re.I)]
            if aggregate:
                selected = min(aggregate, key=lambda row: (row["rule_index"], row["row_index"]))
            else:
                selected = dict(min(matches, key=lambda row: (row["rule_index"], row["row_index"])))
                selected["raw_value"] = sum(row["raw_value"] for row in matches)
                selected["value"] = sum(row["value"] for row in matches)
                selected["label"] = " + ".join(dict.fromkeys(row["label"] for row in matches))
        else:
            selected = min(
                matches,
                key=lambda row: (
                    row["rule_index"],
                    0 if re.search(r"\btotal\b", row["label"], re.I) else 1,
                    len(row["label"]),
                    row["row_index"],
                ),
            )
        value = selected["value"]
        raw_value = selected["raw_value"]
        output_rows.append(
            {
                "canonical_account_id": canonical_id,
                "canonical_account_name": canonical_names.get(canonical_id, canonical_id),
                "original_account_name": selected["label"],
                "statement_type": selected["statement_type"],
                "period": period,
                "amount": str(int(value)) if float(value).is_integer() else str(value),
                "raw_amount": str(int(raw_value)) if float(raw_value).is_integer() else str(raw_value),
                "normalized_amount": str(int(value)) if float(value).is_integer() else str(value),
                "cash_effect_amount": str(int(value)) if float(value).is_integer() else str(value),
                "amount_policy": selected["amount_policy"],
                "cash_direction": "",
                "fiscal_year": int(fiscal_year),
                "fiscal_month": int(fiscal_month),
                "fiscal_quarter": (int(fiscal_month) - 1) // 3 + 1,
                "semantic_provenance": json.dumps(
                    {
                        "source": "english_dart_xbrl_viewer",
                        "rcept_no": str(rcept_no),
                        "label": selected["label"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
    return pd.DataFrame(output_rows).reindex(columns=EXPECTED_HEADER)


def _load_selected_metadata(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"stock_code": str, "rcept_no": str})
    if frame.empty:
        return frame
    frame["stock_code"] = frame["stock_code"].astype(str).str.zfill(6)
    frame["fiscal_year"] = pd.to_numeric(frame["fiscal_year"], errors="coerce")
    frame["fiscal_month"] = pd.to_numeric(frame["fiscal_month"], errors="coerce")
    frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce")
    return frame.dropna(subset=["fiscal_year", "fiscal_month", "report_date"])


def _snapshot_path(root: Path, symbol: str, year: int, month: int) -> Path:
    return root / symbol / f"kr_normalized_{symbol}_{year:04d}.{month:02d}.csv"


def recover_statements(
    *,
    target_path: Path = DEFAULT_TARGET_PATH,
    metadata_path: Path = DEFAULT_METADATA_PATH,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    existing_normalized_dir: Path = DEFAULT_EXISTING_NORMALIZED_DIR,
    existing_source_root: Path = DEFAULT_EXISTING_SOURCE_ROOT,
    status_path: Path = DEFAULT_STATUS_PATH,
    workers: int = 4,
    request_interval: float = 0.5,
    consolidate: bool = True,
) -> dict[str, Any]:
    symbols = _target_symbols(target_path)
    metadata = _load_selected_metadata(metadata_path)
    jobs = []
    skipped_existing = 0
    for row in metadata.itertuples(index=False):
        year = int(row.fiscal_year)
        month = int(row.fiscal_month)
        if year < SOURCE_MIN_FISCAL_YEAR or year > SOURCE_MAX_FISCAL_YEAR:
            continue
        snapshot_path = _snapshot_path(snapshot_root, row.stock_code, year, month)
        if snapshot_path.exists() and snapshot_path.stat().st_size > 100:
            continue
        source_path = (
            source_root
            / row.stock_code
            / f"finance_statement_({year:04d}.{month:02d}).html"
        )
        unavailable_path = source_path.with_suffix(".unavailable.json")
        if unavailable_path.exists() and unavailable_path.stat().st_size > 0:
            continue
        existing_source_path = (
            existing_source_root
            / row.stock_code
            / f"finance_statement_({year:04d}.{month:02d}).html"
        )
        existing_normalized_path = existing_normalized_dir / statement_symbol_name(
            row.stock_code,
            market="kr",
        )
        if (
            existing_source_path.exists()
            and existing_source_path.stat().st_size > 1_000
            and existing_normalized_path.exists()
            and existing_normalized_path.stat().st_size > 100
        ):
            skipped_existing += 1
            continue
        jobs.append(
            {
                "symbol": row.stock_code,
                "fiscal_year": year,
                "fiscal_month": month,
                "rcept_no": str(row.rcept_no),
                "snapshot_path": snapshot_path,
                "source_path": source_path,
                "unavailable_path": unavailable_path,
            }
        )

    throttle = DartRequestThrottle(max(float(request_interval), 0.0))
    counts: dict[str, int] = {}
    failures: list[dict[str, str]] = []

    def process(job: dict[str, Any]) -> str:
        source_path = Path(job["source_path"])
        if source_path.exists() and source_path.stat().st_size > 1_000:
            html = source_path.read_text(encoding="utf-8", errors="ignore")
        else:
            _wait_for_dart_request(throttle, 0)
            response = request_with_retry(
                requests.Session(),
                "GET",
                VIEWER_URL,
                params={"rcpNo": job["rcept_no"]},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": f"https://englishdart.fss.or.kr/dsbh002/main.do?rcpNo={job['rcept_no']}",
                },
                timeout=30,
                throttle=throttle,
            )
            html = response.text
            if len(html) <= 1_000:
                _write_json(
                    Path(job["unavailable_path"]),
                    {
                        "reason": "empty_english_dart_xbrl_viewer_response",
                        "rcept_no": job["rcept_no"],
                        "symbol": job["symbol"],
                    },
                )
                raise RuntimeError("empty English DART XBRL viewer response")
            write_source_text(
                source_path,
                html,
                source="english-dart-xbrl-viewer",
                encoding="utf-8",
                metadata={"rcept_no": job["rcept_no"], "symbol": job["symbol"]},
            )
        frame = parse_english_xbrl_statement(
            html,
            symbol=job["symbol"],
            fiscal_year=job["fiscal_year"],
            fiscal_month=job["fiscal_month"],
            rcept_no=job["rcept_no"],
        )
        if frame.empty:
            _write_json(
                Path(job["unavailable_path"]),
                {
                    "reason": "no_pvgo_canonical_inputs_mapped",
                    "rcept_no": job["rcept_no"],
                    "symbol": job["symbol"],
                },
            )
            raise RuntimeError("no PVGO canonical inputs mapped from English XBRL")
        _atomic_write_csv(frame, Path(job["snapshot_path"]))
        return "normalized"

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {executor.submit(process, job): job for job in jobs}
        completed = 0
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                status = future.result()
            except Exception as error:
                status = "failed"
                failures.append(
                    {
                        "symbol": job["symbol"],
                        "period": f"{job['fiscal_year']:04d}.{job['fiscal_month']:02d}",
                        "rcept_no": job["rcept_no"],
                        "error": repr(error),
                    }
                )
            counts[status] = counts.get(status, 0) + 1
            completed += 1
            if completed % 100 == 0:
                print(
                    f"[EN-XBRL-STATEMENTS] completed={completed:,}/{len(jobs):,} "
                    f"statuses={dict(sorted(counts.items()))}",
                    flush=True,
                )

    written = 0
    canonical_counts: dict[str, int] = {}
    for symbol in symbols if consolidate else ():
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        existing_path = existing_normalized_dir / statement_symbol_name(symbol, market="kr")
        if existing_path.exists():
            try:
                existing = pd.read_csv(existing_path, dtype=str)
            except (OSError, ValueError, pd.errors.EmptyDataError):
                existing = pd.DataFrame()
            if not existing.empty:
                years = pd.to_numeric(existing.get("fiscal_year"), errors="coerce")
                existing = existing.loc[
                    years.le(SOURCE_MAX_FISCAL_YEAR)
                ].copy()
                if not existing.empty:
                    frames.append(existing)
        # Direct English XBRL observations are appended after legacy normalized
        # rows so the selected point-in-time filing wins on an exact period/account
        # collision while still retaining legacy-only accounts.
        for path in sorted((snapshot_root / symbol).glob("kr_normalized_*.csv")):
            try:
                frames.append(pd.read_csv(path, dtype=str))
            except (OSError, ValueError, pd.errors.EmptyDataError):
                continue
        if not frames:
            continue
        combined = pd.concat(frames, ignore_index=True)
        for column in EXPECTED_HEADER:
            if column not in combined.columns:
                combined[column] = pd.NA
        combined["fiscal_year"] = pd.to_numeric(combined["fiscal_year"], errors="coerce")
        combined["fiscal_month"] = pd.to_numeric(combined["fiscal_month"], errors="coerce")
        combined = combined.dropna(subset=["fiscal_year", "fiscal_month", "canonical_account_id"])
        combined = (
            combined.drop_duplicates(
                ["fiscal_year", "fiscal_month", "canonical_account_id"], keep="last"
            )
            .sort_values(["fiscal_year", "fiscal_month", "statement_type", "canonical_account_id"])
            .reindex(columns=EXPECTED_HEADER)
        )
        output_path = output_dir / statement_symbol_name(symbol, market="kr")
        _atomic_write_csv(combined, output_path)
        written += 1
        for canonical_id, count in combined["canonical_account_id"].value_counts().items():
            canonical_counts[str(canonical_id)] = canonical_counts.get(str(canonical_id), 0) + int(count)

    report = {
        "generated_at": _utc_now(),
        "stage": "statements",
        "metadata_rows": len(metadata),
        "attempted_jobs": len(jobs),
        "skipped_existing_statement_jobs": skipped_existing,
        "status_counts": dict(sorted(counts.items())),
        "failure_count": len(failures),
        "failures": failures,
        "consolidated_security_count": written,
        "canonical_row_counts": dict(sorted(canonical_counts.items())),
        "output_dir": str(output_dir),
        "consolidated": consolidate,
    }
    _write_json(status_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover PIT 2011-2016 KR PVGO inputs from official English DART XBRL."
    )
    parser.add_argument(
        "stage", choices=("metadata", "consolidate", "statements", "all")
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--part-dir", type=Path, default=DEFAULT_PART_DIR)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA_PATH)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument("--skip-consolidate", action="store_true")
    args = parser.parse_args()
    if args.stage in {"metadata", "all"}:
        recover_metadata(
            target_path=args.targets,
            part_dir=args.part_dir,
            metadata_path=args.metadata,
            status_path=args.status,
            workers=args.workers,
            request_interval=args.request_interval,
        )
    if args.stage == "consolidate":
        frame = consolidate_metadata_parts(
            target_path=args.targets,
            part_dir=args.part_dir,
            metadata_path=args.metadata,
        )
        print(
            json.dumps(
                {
                    "metadata_path": str(args.metadata),
                    "rows": len(frame),
                    "securities": int(frame["stock_code"].nunique())
                    if not frame.empty
                    else 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    if args.stage in {"statements", "all"}:
        recover_statements(
            target_path=args.targets,
            metadata_path=args.metadata,
            source_root=args.source_root,
            snapshot_root=args.snapshot_root,
            output_dir=args.output_dir,
            status_path=args.status,
            workers=args.workers,
            request_interval=args.request_interval,
            consolidate=not args.skip_consolidate,
        )


if __name__ == "__main__":
    main()
