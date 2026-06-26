from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import re
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

from engine.core.paths import DATA_LAKE, first_existing_path, market_csv_name
from engine.extractors._internal.erp_inputs import BRONZE_DAMODARAN_COUNTRY_ERP_PATH


SILVER_COUNTRY_ERP_PATH = DATA_LAKE.silver("wacc", "country_equity_risk_premiums.csv")
SILVER_RISK_FREE_RATE_PATH = DATA_LAKE.silver("wacc", "risk_free_rates.csv")
KR_BENCHMARK_PATH = DATA_LAKE.silver(
    "krx",
    "benchmark",
    market_csv_name("normalized_benchmark_price"),
)
LEGACY_KR_BENCHMARK_PATH = DATA_LAKE.silver("krx", "benchmark", "kr_normalized_benchmark_price.csv")
COUNTRY_ERP_COLUMNS = [
    "country",
    "country_code",
    "moody_rating",
    "default_spread",
    "country_risk_premium",
    "equity_risk_premium",
    "corporate_tax_rate",
    "source",
    "source_date",
    "updated_at",
]
RISK_FREE_COLUMNS = ["market", "country_code", "date", "risk_free_rate", "source", "series_id", "updated_at"]
PERCENT_COLUMNS = [
    "default_spread",
    "country_risk_premium",
    "equity_risk_premium",
    "corporate_tax_rate",
]
COUNTRY_CODES = {
    "korea": "KR",
    "south korea": "KR",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
}


def normalize_country_erp(
    path: str | Path | None = None,
    *,
    output_path: str | Path | None = SILVER_COUNTRY_ERP_PATH,
    allow_kr_fallback: bool = True,
    risk_free_path: str | Path | None = None,
    benchmark_path: str | Path | None = None,
) -> pd.DataFrame:
    try:
        result = normalize_damodaran_country_erp_frame(_read_damodaran_country_erp(path))
    except Exception:
        if not allow_kr_fallback:
            raise
        result = estimate_kr_equity_risk_premium(
            risk_free_path=risk_free_path,
            benchmark_path=benchmark_path,
        )

    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False, encoding="utf-8-sig")
    return result


def normalize_damodaran_country_erp_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=COUNTRY_ERP_COLUMNS)

    source_date = _source_date_from_frame(frame)
    table = _extract_damodaran_table(frame)
    column_map = {
        "country": _pick_column(table, ["country"]),
        "moody_rating": _pick_column(table, ["moody", "rating"], required=False),
        "default_spread": _pick_column(table, ["default", "spread"], required=False),
        "country_risk_premium": _pick_column(table, ["country", "risk", "premium"], required=False),
        "equity_risk_premium": _pick_column(table, ["equity", "risk", "premium"]),
        "corporate_tax_rate": _pick_column(table, ["corporate", "tax"], required=False),
    }

    result = pd.DataFrame(
        {
            "country": table[column_map["country"]].astype(str).str.strip(),
            "country_code": table[column_map["country"]].map(_country_code),
            "moody_rating": (
                table[column_map["moody_rating"]].astype(str).str.strip()
                if column_map["moody_rating"] is not None
                else ""
            ),
            "default_spread": _percent_column(table, column_map["default_spread"]),
            "country_risk_premium": _percent_column(table, column_map["country_risk_premium"]),
            "equity_risk_premium": _percent_column(table, column_map["equity_risk_premium"]),
            "corporate_tax_rate": _percent_column(table, column_map["corporate_tax_rate"]),
            "source": "damodaran_nyu",
            "source_date": source_date,
            "updated_at": datetime.now().replace(microsecond=0),
        }
    )
    result = result.loc[result["country"].ne("") & result["equity_risk_premium"].notna()].copy()
    return result[COUNTRY_ERP_COLUMNS].reset_index(drop=True)


def normalize_fred_risk_free_rates(
    paths: list[str | Path] | str | Path,
    *,
    output_path: str | Path | None = SILVER_RISK_FREE_RATE_PATH,
) -> pd.DataFrame:
    if not isinstance(paths, list):
        paths = [paths]

    frames = []
    for path in paths:
        frame = pd.read_csv(path)
        frames.append(normalize_fred_risk_free_rate_frame(frame, series_id=_fred_series_id_from_path(path)))

    result = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True) if frames else pd.DataFrame(columns=RISK_FREE_COLUMNS)
    result = result.sort_values(["market", "date"]).reset_index(drop=True)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output, index=False, encoding="utf-8-sig")
    return result


def _fred_series_id_from_path(path: str | Path) -> str:
    stem = Path(path).stem.strip().upper()
    aliases = {
        "US_DGS10": "DGS10",
        "KR_10Y_GOV_BOND": "IRLTLT01KRM156N",
        "KR_10Y_GOVERNMENT_BOND": "IRLTLT01KRM156N",
    }
    return aliases.get(stem, stem)


def normalize_fred_risk_free_rate_frame(frame: pd.DataFrame, *, series_id: str) -> pd.DataFrame:
    series_id = str(series_id or "").strip().upper()
    if frame is None or frame.empty:
        return pd.DataFrame(columns=RISK_FREE_COLUMNS)

    date_col = _pick_column(frame, ["observation_date"], required=False) or _pick_column(frame, ["date"])
    value_col = _pick_column(frame, [series_id], required=False) or frame.columns[-1]
    market, country_code = _risk_free_market(series_id)
    result = pd.DataFrame(
        {
            "market": market,
            "country_code": country_code,
            "date": pd.to_datetime(frame[date_col], errors="coerce"),
            "risk_free_rate": _coerce_percent(frame[value_col]),
            "source": "fred",
            "series_id": series_id,
            "updated_at": datetime.now().replace(microsecond=0),
        }
    )
    result = result.dropna(subset=["date", "risk_free_rate"])
    result["date"] = result["date"].dt.date
    return result[RISK_FREE_COLUMNS].reset_index(drop=True)


def estimate_kr_equity_risk_premium(
    *,
    risk_free_path: str | Path | None = None,
    benchmark_path: str | Path | None = None,
    benchmark_ids: list[str] | None = None,
    lookback_years: float = 2.0,
) -> pd.DataFrame:
    benchmark_path = Path(benchmark_path) if benchmark_path is not None else first_existing_path(
        KR_BENCHMARK_PATH,
        LEGACY_KR_BENCHMARK_PATH,
    )
    risk_free_path = Path(risk_free_path) if risk_free_path is not None else SILVER_RISK_FREE_RATE_PATH
    benchmark_ids = benchmark_ids or ["KOSPI", "KOSPI200", "KOSDAQ"]

    expected_return = _annualized_benchmark_return(benchmark_path, benchmark_ids, lookback_years)
    risk_free_rate = _latest_kr_risk_free_rate(risk_free_path)
    erp = expected_return - risk_free_rate
    now = datetime.now().replace(microsecond=0)
    return pd.DataFrame(
        [
            {
                "country": "Korea",
                "country_code": "KR",
                "moody_rating": "",
                "default_spread": pd.NA,
                "country_risk_premium": pd.NA,
                "equity_risk_premium": erp,
                "corporate_tax_rate": pd.NA,
                "source": "kr_benchmark_minus_government_bond",
                "source_date": pd.Timestamp.now().date(),
                "updated_at": now,
            }
        ],
        columns=COUNTRY_ERP_COLUMNS,
    )


def _read_damodaran_country_erp(path: str | Path | None) -> pd.DataFrame:
    source = Path(path) if path is not None else BRONZE_DAMODARAN_COUNTRY_ERP_PATH
    try:
        return pd.read_excel(source, sheet_name=0, header=None)
    except ImportError:
        return _read_xlsx_first_sheet_without_openpyxl(source)


def _read_xlsx_first_sheet_without_openpyxl(path: Path) -> pd.DataFrame:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared_strings = _xlsx_shared_strings(archive, ns)
        sheet_name = _xlsx_first_sheet_name(archive)
        sheet_xml = archive.read(sheet_name)

    root = ET.fromstring(sheet_xml)
    rows = []
    max_col = 0
    for row in root.findall(".//main:sheetData/main:row", ns):
        values = []
        for cell in row.findall("main:c", ns):
            col_index = _xlsx_cell_col_index(cell.attrib.get("r", ""))
            while len(values) <= col_index:
                values.append(None)
            values[col_index] = _xlsx_cell_value(cell, shared_strings, ns)
        max_col = max(max_col, len(values))
        rows.append(values)

    normalized_rows = [row + [None] * (max_col - len(row)) for row in rows]
    return pd.DataFrame(normalized_rows)


def _xlsx_shared_strings(archive: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    try:
        xml = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml)
    strings = []
    for item in root.findall("main:si", ns):
        text_parts = [node.text or "" for node in item.findall(".//main:t", ns)]
        strings.append("".join(text_parts))
    return strings


def _xlsx_first_sheet_name(archive: zipfile.ZipFile) -> str:
    preferred = _xlsx_sheet_target_by_name(archive, "ERPs by country")
    if preferred is not None:
        return preferred
    names = archive.namelist()
    sheet_names = sorted(
        name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
    )
    if not sheet_names:
        raise ValueError("xlsx workbook does not contain worksheets")
    return sheet_names[0]


def _xlsx_sheet_target_by_name(archive: zipfile.ZipFile, sheet_name: str) -> str | None:
    try:
        workbook_xml = archive.read("xl/workbook.xml")
        rels_xml = archive.read("xl/_rels/workbook.xml.rels")
    except KeyError:
        return None

    workbook_ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rels_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    workbook = ET.fromstring(workbook_xml)
    rel_id = None
    for sheet in workbook.findall(".//main:sheet", workbook_ns):
        if str(sheet.attrib.get("name", "")).strip().lower() == sheet_name.lower():
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            break
    if rel_id is None:
        return None

    rels = ET.fromstring(rels_xml)
    for relationship in rels.findall("rel:Relationship", rels_ns):
        if relationship.attrib.get("Id") == rel_id:
            target = relationship.attrib.get("Target", "")
            return target if target.startswith("xl/") else f"xl/{target}"
    return None


def _xlsx_cell_col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str], ns: dict[str, str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", ns))
    value_node = cell.find("main:v", ns)
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        index = int(raw)
        return shared_strings[index] if index < len(shared_strings) else ""
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number


def _extract_damodaran_table(frame: pd.DataFrame) -> pd.DataFrame:
    header_index = None
    for index, row in frame.iterrows():
        values = [str(value).strip().lower() for value in row.tolist()]
        if "country" in values and any("equity risk premium" in value for value in values):
            header_index = index
            break
    if header_index is None:
        raise ValueError("Damodaran ERP table header was not found")

    table = frame.iloc[header_index + 1 :].copy()
    table.columns = [str(value).strip() for value in frame.iloc[header_index].tolist()]
    table = table.dropna(how="all")
    return table


def _source_date_from_frame(frame: pd.DataFrame) -> Any:
    for value in frame.iloc[:10].to_numpy().ravel().tolist():
        text = str(value)
        if "last updated" in text.lower():
            return text.split(":", 1)[-1].strip()
    return pd.Timestamp.now().date()


def _pick_column(frame: pd.DataFrame, terms: list[str], *, required: bool = True) -> str | None:
    normalized_terms = [term.lower() for term in terms]
    for column in frame.columns:
        text = str(column).strip().lower()
        if all(term in text for term in normalized_terms):
            return column
    if required:
        raise ValueError(f"missing column containing: {', '.join(terms)}")
    return None


def _percent_column(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        return pd.Series(pd.NA, index=frame.index, dtype="Float64")
    return _coerce_percent(frame[column])


def _coerce_percent(series: Any) -> pd.Series:
    text = pd.Series(series).astype(str).str.strip()
    has_percent = text.str.endswith("%")
    values = pd.to_numeric(text.str.replace("%", "", regex=False).replace({"NA": pd.NA, "nan": pd.NA}), errors="coerce")
    decimal_mask = values.abs().le(1) & ~has_percent
    return values.mask(decimal_mask, values * 100)


def _country_code(value: Any) -> str:
    key = str(value or "").strip().lower()
    return COUNTRY_CODES.get(key, "")


def _risk_free_market(series_id: str) -> tuple[str, str]:
    if series_id == "DGS10":
        return "us", "US"
    if series_id == "IRLTLT01KRM156N":
        return "kr", "KR"
    return "", ""


def _annualized_benchmark_return(path: Path, benchmark_ids: list[str], lookback_years: float) -> float:
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError("benchmark data is empty")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "close"])
    if "benchmark_id" in frame.columns:
        wanted = [value.upper() for value in benchmark_ids]
        frame["_rank"] = frame["benchmark_id"].astype(str).str.upper().map(
            {benchmark_id: index for index, benchmark_id in enumerate(wanted)}
        )
        frame = frame.loc[frame["_rank"].notna()].sort_values(["_rank", "trade_date"])
        if frame.empty:
            raise ValueError("preferred KR benchmark was not found")
        frame = frame.loc[frame["_rank"] == frame["_rank"].min()].copy()
    frame = frame.sort_values("trade_date")
    end = frame["trade_date"].max()
    start_cutoff = end - pd.Timedelta(days=int(365.25 * lookback_years))
    window = frame.loc[frame["trade_date"] >= start_cutoff].copy()
    if len(window) < 2:
        raise ValueError("not enough benchmark history for KR ERP fallback")
    start_row = window.iloc[0]
    end_row = window.iloc[-1]
    days = max((end_row["trade_date"] - start_row["trade_date"]).days, 1)
    return ((float(end_row["close"]) / float(start_row["close"])) ** (365.25 / days) - 1) * 100


def _latest_kr_risk_free_rate(path: Path) -> float:
    frame = pd.read_csv(path)
    if frame.empty:
        raise ValueError("risk-free rate data is empty")
    if "market" in frame.columns:
        frame = frame.loc[frame["market"].astype(str).str.lower() == "kr"].copy()
    value_col = "risk_free_rate" if "risk_free_rate" in frame.columns else frame.columns[-1]
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna(subset=[value_col])
    if frame.empty:
        raise ValueError("KR risk-free rate was not found")
    return float(frame[value_col].iloc[-1])
