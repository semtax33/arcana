from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup, SoupStrainer
from bs4.element import Tag

from engine.core.paths import DATA_LAKE


DEFAULT_RULE_PATH = DATA_LAKE.rules("kr_business_info.yaml")
OUTPUT_DIR = DATA_LAKE.silver("dart", "business-info")
SECTION_OUTPUT_NAME = "kr_business_info_sections.csv"
TABLE_OUTPUT_NAME = "kr_business_info_tables.csv"
CELL_OUTPUT_NAME = "kr_business_info_cells.csv"
ROW_OUTPUT_NAME = "kr_business_info_rows.csv"
SECTION_OUTPUT_PATH = OUTPUT_DIR / SECTION_OUTPUT_NAME
TABLE_OUTPUT_PATH = OUTPUT_DIR / TABLE_OUTPUT_NAME
CELL_OUTPUT_PATH = OUTPUT_DIR / CELL_OUTPUT_NAME
ROW_OUTPUT_PATH = OUTPUT_DIR / ROW_OUTPUT_NAME
PARSER_VERSION = "1.1.0"
REQUIRED_RULE_KEYS = {
    "section_aliases",
    "template_rules",
    "heading_patterns",
    "not_applicable_patterns",
    "layout_table_rules",
    "table_kind_rules",
    "metadata_patterns",
    "header_detection_rules",
    "data_type_patterns",
    "source_uri_rules",
    "business_domain_patterns",
    "sector_keyword_rules",
    "extraction_keywords",
}
BUSINESS_INFO_FILE_RE = re.compile(
    r"business_info_\((?P<year>\d{4})[._](?P<month>\d{1,2})\)\.html$",
    re.IGNORECASE,
)


class BusinessInfoRuleError(ValueError):
    pass


@dataclass(frozen=True)
class BusinessInfoCell:
    row_idx: int
    col_idx: int
    raw_text: str
    normalized_text: str
    is_header: bool
    header_level: int
    header_path: list[str]
    rowspan: int
    colspan: int
    unit: str
    data_type: str
    source_cell_id: str


@dataclass(frozen=True)
class BusinessInfoTable:
    table_index: int
    table_id: str
    html_table_hash: str
    table_kind: str
    table_title: str
    caption_or_context: str
    caption: str
    context_before: str
    context_after: str
    unit_text: str
    headers: list[str]
    header_paths: list[list[str]]
    rows: list[list[str]]
    cells: list[BusinessInfoCell]
    raw_index: int


@dataclass(frozen=True)
class BusinessInfoSection:
    canonical_key: str
    raw_title: str
    level: int
    business_domain: str
    text: str
    tables: list[BusinessInfoTable] = field(default_factory=list)
    is_not_applicable: bool = False
    matched_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BusinessInfoDocument:
    market: str
    stock_code: str
    period: str
    source_path: Path
    source_uri: str
    source_html_hash: str
    corp_code: str
    corp_name: str
    rcept_no: str
    report_code: str
    report_type: str
    parser_version: str
    template_type: str
    sections: list[BusinessInfoSection]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SectionRange:
    raw_title: str
    level: int
    business_domain: str
    nodes: list[Tag]


@dataclass(frozen=True)
class _TableCellMeta:
    text: str
    is_header: bool
    rowspan: int
    colspan: int
    source_cell_id: str


def load_business_info_rules(rule_path: str | Path = DEFAULT_RULE_PATH) -> dict[str, Any]:
    path = Path(rule_path)
    if not path.exists():
        raise FileNotFoundError(f"business-info rule file not found: {path}")

    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise BusinessInfoRuleError("PyYAML is required to load business-info rules") from exc

    try:
        with path.open("r", encoding="utf-8") as file:
            rules = yaml.safe_load(file) or {}
    except Exception as exc:
        raise BusinessInfoRuleError(f"failed to load business-info rules: {path}: {exc}") from exc

    missing = sorted(REQUIRED_RULE_KEYS - set(rules))
    if missing:
        raise BusinessInfoRuleError(f"business-info rule file missing required keys: {', '.join(missing)}")
    return rules


def parse_business_info_html(
    path: str | Path,
    stock_code: str | None = None,
    period: str | None = None,
    *,
    sector_code: str | None = None,
    rule_path: str | Path = DEFAULT_RULE_PATH,
) -> BusinessInfoDocument:
    rules = load_business_info_rules(rule_path)
    return _parse_business_info_html(
        path,
        stock_code=stock_code,
        period=period,
        sector_code=sector_code,
        rules=rules,
    )


def parse_business_info_files(
    paths: Iterable[str | Path],
    *,
    max_workers: int | None = None,
    sector_code_map: dict[str, str] | None = None,
    rule_path: str | Path = DEFAULT_RULE_PATH,
) -> list[BusinessInfoDocument]:
    file_paths = [Path(path) for path in paths]
    if not file_paths:
        return []

    rules = load_business_info_rules(rule_path)
    worker_count = _normalize_worker_count(max_workers, len(file_paths))
    if worker_count == 1:
        return [
            _parse_business_info_html(
                file_path,
                sector_code=_sector_for_path(file_path, sector_code_map),
                rules=rules,
            )
            for file_path in file_paths
        ]

    documents: list[BusinessInfoDocument | None] = [None] * len(file_paths)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _parse_business_info_html,
                file_path,
                sector_code=_sector_for_path(file_path, sector_code_map),
                rules=rules,
            ): index
            for index, file_path in enumerate(file_paths)
        }
        for future in as_completed(futures):
            index = futures[future]
            documents[index] = future.result()

    return [document for document in documents if document is not None]


def _parse_business_info_html(
    path: str | Path,
    stock_code: str | None = None,
    period: str | None = None,
    *,
    sector_code: str | None = None,
    rules: dict[str, Any],
) -> BusinessInfoDocument:
    source_path = Path(path)
    normalized_stock_code = _normalize_stock_code(stock_code or _stock_code_from_path(source_path))
    normalized_period = period or _period_from_path(source_path)
    html = _read_html(source_path)
    source_html_hash = _sha256_text(html)
    source_uri = _source_uri(source_path, rules)
    document_metadata = _extract_document_metadata(
        html,
        stock_code=normalized_stock_code,
        period=normalized_period,
        rules=rules,
    )
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer(["p", "table"]))
    nodes = _ordered_body_nodes(soup)
    warnings: list[str] = []

    ranges, used_fallback = _build_section_ranges(nodes, rules)
    if not ranges:
        ranges = [_SectionRange("II. 사업의 내용", 1, "", nodes)]
        used_fallback = True
        warnings.append("section_headings_not_found")

    sections = [
        _build_section(
            section_range,
            rules,
            source_path=source_path,
            sector_code=sector_code,
            document_stock_code=normalized_stock_code,
            document_period=normalized_period,
            rcept_no=document_metadata["rcept_no"],
        )
        for section_range in ranges
    ]
    template_type = _classify_template(sections, rules, used_fallback=used_fallback)
    return BusinessInfoDocument(
        market="KR",
        stock_code=normalized_stock_code,
        period=normalized_period,
        source_path=source_path,
        source_uri=source_uri,
        source_html_hash=source_html_hash,
        corp_code=document_metadata["corp_code"],
        corp_name=document_metadata["corp_name"],
        rcept_no=document_metadata["rcept_no"],
        report_code=document_metadata["report_code"],
        report_type=document_metadata["report_type"],
        parser_version=PARSER_VERSION,
        template_type=template_type,
        sections=sections,
        warnings=warnings,
    )


def parse_business_info_directory(
    stock_code_dir: str | Path,
    *,
    max_workers: int | None = None,
    sector_code: str | None = None,
    rule_path: str | Path = DEFAULT_RULE_PATH,
) -> list[BusinessInfoDocument]:
    root = Path(stock_code_dir)
    stock_code = _normalize_stock_code(root.name)
    files = sorted(root.glob("business_info_(*).html"))
    rules = load_business_info_rules(rule_path)
    worker_count = _normalize_worker_count(max_workers, len(files))
    if worker_count == 1:
        return [
            _parse_business_info_html(
                file_path,
                stock_code=stock_code,
                sector_code=sector_code,
                rules=rules,
            )
            for file_path in files
        ]

    documents: list[BusinessInfoDocument | None] = [None] * len(files)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _parse_business_info_html,
                file_path,
                stock_code=stock_code,
                sector_code=sector_code,
                rules=rules,
            ): index
            for index, file_path in enumerate(files)
        }
        for future in as_completed(futures):
            documents[futures[future]] = future.result()
    return [document for document in documents if document is not None]


def build_business_info_sections_frame(documents: Iterable[BusinessInfoDocument]) -> Any:
    import pandas as pd

    return pd.DataFrame([row for document in documents for row in document_to_section_records(document)])


def build_business_info_tables_frame(documents: Iterable[BusinessInfoDocument]) -> Any:
    import pandas as pd

    return pd.DataFrame([row for document in documents for row in document_to_table_records(document)])


def build_business_info_cells_frame(documents: Iterable[BusinessInfoDocument]) -> Any:
    import pandas as pd

    return pd.DataFrame([row for document in documents for row in document_to_cell_records(document)])


def build_business_info_rows_frame(documents: Iterable[BusinessInfoDocument]) -> Any:
    import pandas as pd

    return pd.DataFrame([row for document in documents for row in document_to_row_records(document)])


def write_business_info_csvs(
    documents: Iterable[BusinessInfoDocument],
    *,
    output_dir: str | Path = OUTPUT_DIR,
    section_output_path: str | Path | None = None,
    table_output_path: str | Path | None = None,
    cell_output_path: str | Path | None = None,
    row_output_path: str | Path | None = None,
) -> list[tuple[Path, Path, Path, Path]] | tuple[Path, Path, Path, Path]:
    document_list = list(documents)

    explicit_paths = [section_output_path, table_output_path, cell_output_path, row_output_path]
    if any(path is not None for path in explicit_paths):
        if not all(path is not None for path in explicit_paths):
            raise ValueError("section/table/cell/row output paths must be provided together")
        paths = (
            Path(section_output_path),  # type: ignore[arg-type]
            Path(table_output_path),  # type: ignore[arg-type]
            Path(cell_output_path),  # type: ignore[arg-type]
            Path(row_output_path),  # type: ignore[arg-type]
        )
        _write_business_info_csv_group(document_list, paths)
        return paths

    grouped_documents: dict[str, list[BusinessInfoDocument]] = {}
    for document in document_list:
        grouped_documents.setdefault(document.stock_code, []).append(document)

    written_paths: list[tuple[Path, Path, Path, Path]] = []
    base_dir = Path(output_dir)
    for stock_code in sorted(grouped_documents):
        stock_dir = base_dir / stock_code
        paths = (
            stock_dir / SECTION_OUTPUT_NAME,
            stock_dir / TABLE_OUTPUT_NAME,
            stock_dir / CELL_OUTPUT_NAME,
            stock_dir / ROW_OUTPUT_NAME,
        )
        _write_business_info_csv_group(grouped_documents[stock_code], paths)
        written_paths.append(paths)
    return written_paths


def _write_business_info_csv_group(
    documents: list[BusinessInfoDocument],
    paths: tuple[Path, Path, Path, Path],
) -> None:
    section_path, table_path, cell_path, row_path = paths
    section_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.parent.mkdir(parents=True, exist_ok=True)
    cell_path.parent.mkdir(parents=True, exist_ok=True)
    row_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv_records(section_path, [row for document in documents for row in document_to_section_records(document)])
    _write_csv_records(table_path, [row for document in documents for row in document_to_table_records(document)])
    _write_csv_records(cell_path, [row for document in documents for row in document_to_cell_records(document)])
    _write_csv_records(row_path, [row for document in documents for row in document_to_row_records(document)])


def _write_csv_records(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        if not records:
            return
        writer = csv.DictWriter(file, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)


def document_to_section_records(document: BusinessInfoDocument) -> list[dict[str, Any]]:
    parsed_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    for section in document.sections:
        metadata = _record_metadata(document)
        rows.append(
            {
                **metadata,
                "security_id": f"SEC_KR_{document.stock_code}",
                "template_type": document.template_type,
                "business_domain": section.business_domain,
                "section_key": section.canonical_key,
                "section_title": section.raw_title,
                "text": section.text,
                "is_not_applicable": section.is_not_applicable,
                "table_count": len(section.tables),
                "parsed_at": parsed_at,
            }
        )
    return rows


def document_to_table_records(document: BusinessInfoDocument) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in document.sections:
        for table in section.tables:
            metadata = _record_metadata(document)
            rows.append(
                {
                    **metadata,
                    "section_key": section.canonical_key,
                    "section_title": section.raw_title,
                    "table_index": table.table_index,
                    "table_id": table.table_id,
                    "html_table_hash": table.html_table_hash,
                    "table_kind": table.table_kind,
                    "table_title": table.table_title,
                    "subsection_title": table.table_title,
                    "caption": table.caption,
                    "context_before": table.context_before,
                    "context_after": table.context_after,
                    "caption_or_context": table.caption_or_context,
                    "unit_text": table.unit_text,
                    "headers_json": json.dumps(table.headers, ensure_ascii=False),
                    "header_paths_json": json.dumps(table.header_paths, ensure_ascii=False),
                    "rows_json": json.dumps(table.rows, ensure_ascii=False),
                }
            )
    return rows


def document_to_cell_records(document: BusinessInfoDocument) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in document.sections:
        for table in section.tables:
            for cell in table.cells:
                rows.append(
                    {
                        "table_id": table.table_id,
                        "row_idx": cell.row_idx,
                        "col_idx": cell.col_idx,
                        "raw_text": cell.raw_text,
                        "normalized_text": cell.normalized_text,
                        "is_header": cell.is_header,
                        "header_level": cell.header_level,
                        "header_path_json": json.dumps(cell.header_path, ensure_ascii=False),
                        "rowspan": cell.rowspan,
                        "colspan": cell.colspan,
                        "unit": cell.unit,
                        "data_type": cell.data_type,
                        "source_cell_id": cell.source_cell_id,
                    }
                )
    return rows


def document_to_row_records(document: BusinessInfoDocument) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in document.sections:
        for table in section.tables:
            for row_idx, row in enumerate(table.rows):
                rows.append(
                    {
                        "table_id": table.table_id,
                        "row_idx": row_idx,
                        "row_json": json.dumps(row, ensure_ascii=False),
                        "row_text": " | ".join(cell for cell in row if cell),
                        "header_value_map_json": json.dumps(
                            _header_value_map(table.header_paths, row),
                            ensure_ascii=False,
                        ),
                    }
                )
    return rows


def _build_section_ranges(nodes: list[Tag], rules: dict[str, Any]) -> tuple[list[_SectionRange], bool]:
    primary_indexes = [
        index
        for index, node in enumerate(nodes)
        if node.name and node.name.lower() == "p" and _section_level(node) == 2
    ]
    if primary_indexes:
        return _ranges_from_heading_indexes(nodes, primary_indexes, rules), False

    fallback_indexes = [
        index
        for index, node in enumerate(nodes)
        if node.name and node.name.lower() == "p" and _is_fallback_heading(node, rules)
    ]
    fallback_indexes = [index for index in fallback_indexes if _section_level(nodes[index]) != 1]
    if fallback_indexes:
        return _ranges_from_heading_indexes(nodes, fallback_indexes, rules), True

    section_one_indexes = [
        index
        for index, node in enumerate(nodes)
        if node.name and node.name.lower() == "p" and _section_level(node) == 1
    ]
    if section_one_indexes:
        start = section_one_indexes[0] + 1
        title = _tag_text(nodes[section_one_indexes[0]]) or "II. 사업의 내용"
        return [_SectionRange(title, 1, "", nodes[start:])], True
    return [], True


def _normalize_worker_count(max_workers: int | None, task_count: int) -> int:
    if task_count <= 1:
        return 1
    if max_workers is not None:
        requested_workers = int(max_workers)
        if requested_workers > 0:
            return max(1, min(requested_workers, task_count))
    cpu_count = os.cpu_count() or 2
    return max(1, min(task_count, cpu_count * 2, 32))


def _sector_for_path(path: Path, sector_code_map: dict[str, str] | None) -> str | None:
    if not sector_code_map:
        return None
    stock_code = _normalize_stock_code(_stock_code_from_path(path))
    return sector_code_map.get(stock_code) or sector_code_map.get(stock_code.lstrip("0"))


def _ranges_from_heading_indexes(
    nodes: list[Tag],
    heading_indexes: list[int],
    rules: dict[str, Any],
) -> list[_SectionRange]:
    ranges: list[_SectionRange] = []
    for pos, index in enumerate(heading_indexes):
        next_index = heading_indexes[pos + 1] if pos + 1 < len(heading_indexes) else len(nodes)
        heading = nodes[index]
        title = _tag_text(heading)
        ranges.append(
            _SectionRange(
                raw_title=title,
                level=_section_level(heading) or 2,
                business_domain=_business_domain(title, rules),
                nodes=nodes[index + 1 : next_index],
            )
        )
    return ranges


def _build_section(
    section_range: _SectionRange,
    rules: dict[str, Any],
    *,
    source_path: Path,
    sector_code: str | None,
    document_stock_code: str,
    document_period: str,
    rcept_no: str,
) -> BusinessInfoSection:
    text_nodes = [
        _tag_text(node)
        for node in section_range.nodes
        if node.name and node.name.lower() == "p"
    ]
    text = _normalize_text("\n".join(value for value in text_nodes if value))
    canonical_key = _canonical_section_key(section_range.raw_title, text, rules)
    tables = _extract_tables(
        section_range.nodes,
        rules,
        stock_code=document_stock_code,
        period=document_period,
        rcept_no=rcept_no,
        section_key=canonical_key,
    )
    return BusinessInfoSection(
        canonical_key=canonical_key,
        raw_title=section_range.raw_title,
        level=section_range.level,
        business_domain=section_range.business_domain,
        text=text,
        tables=tables,
        is_not_applicable=_is_not_applicable(text, rules),
        matched_keywords=_matched_keywords(canonical_key, text, rules, sector_code=sector_code),
    )


def _extract_tables(
    nodes: list[Tag],
    rules: dict[str, Any],
    *,
    stock_code: str,
    period: str,
    rcept_no: str,
    section_key: str,
) -> list[BusinessInfoTable]:
    tables: list[BusinessInfoTable] = []
    pending_unit = ""
    raw_index = 0
    for index, node in enumerate(nodes):
        if not node.name:
            continue
        candidate_tables = [node] if node.name.lower() == "table" else node.find_all("table")
        for table in candidate_tables:
            matrix, meta_matrix = _table_to_matrix_and_meta(table)
            if not matrix:
                table_hash = _table_hash(table)
                tables.append(
                    BusinessInfoTable(
                        table_index=len(tables),
                        table_id=_table_id(stock_code, rcept_no or period, section_key, len(tables), table_hash),
                        html_table_hash=table_hash,
                        table_kind="image_placeholder",
                        table_title="",
                        caption_or_context=_table_context(nodes, index),
                        caption="",
                        context_before=_table_context(nodes, index),
                        context_after=_table_context_after(nodes, index),
                        unit_text=pending_unit,
                        headers=[],
                        header_paths=[],
                        rows=[],
                        cells=[],
                        raw_index=raw_index,
                    )
                )
                raw_index += 1
                continue
            table_text = _normalize_text(" ".join(" ".join(row) for row in matrix))
            table_kind = _classify_table_kind(table, matrix, table_text, rules)
            is_unit_table = _is_unit_table(table, matrix, table_text, rules)
            if is_unit_table:
                pending_unit = table_text
            header_row_count = _header_row_count(table_kind, matrix, meta_matrix, rules)
            header_paths = _header_paths(matrix, header_row_count)
            headers = [_flat_header(path) for path in header_paths]
            rows = matrix[header_row_count:] if header_row_count else matrix
            if table_kind != "data_table":
                headers = []
                header_paths = []
                rows = matrix
            table_hash = _table_hash(table)
            context_before = _table_context(nodes, index)
            context_after = _table_context_after(nodes, index)
            table_title = _table_title(context_before, table_text, table_kind, rules)
            table_id = _table_id(stock_code, rcept_no or period, section_key, len(tables), table_hash)
            tables.append(
                BusinessInfoTable(
                    table_index=len(tables),
                    table_id=table_id,
                    html_table_hash=table_hash,
                    table_kind=table_kind,
                    table_title=table_title,
                    caption_or_context=_table_context(nodes, index),
                    caption=table_title,
                    context_before=context_before,
                    context_after=context_after,
                    unit_text=pending_unit,
                    headers=headers,
                    header_paths=header_paths,
                    rows=rows,
                    cells=_table_cells(
                        matrix,
                        meta_matrix,
                        header_row_count=header_row_count,
                        header_paths=header_paths,
                        unit_text=pending_unit,
                        rules=rules,
                    ),
                    raw_index=raw_index,
                )
            )
            if table_kind == "data_table":
                pending_unit = ""
            raw_index += 1
    return tables


def _table_to_matrix(table: Tag) -> list[list[str]]:
    matrix, _ = _table_to_matrix_and_meta(table)
    return matrix


def _table_to_matrix_and_meta(table: Tag) -> tuple[list[list[str]], list[list[_TableCellMeta]]]:
    grid: list[list[str]] = []
    meta_grid: list[list[_TableCellMeta | None]] = []
    occupied: dict[tuple[int, int], tuple[str, _TableCellMeta]] = {}
    rows = table.find_all("tr")
    for row_index, tr in enumerate(rows):
        while len(grid) <= row_index:
            grid.append([])
        while len(meta_grid) <= row_index:
            meta_grid.append([])
        col_index = 0
        for cell in tr.find_all(["th", "td"], recursive=False):
            while (row_index, col_index) in occupied:
                text, meta = occupied[(row_index, col_index)]
                _set_grid(grid, row_index, col_index, text)
                _set_meta_grid(meta_grid, row_index, col_index, meta)
                col_index += 1
            text = _tag_text(cell)
            rowspan = _positive_int(cell.get("rowspan"), default=1)
            colspan = _positive_int(cell.get("colspan"), default=1)
            meta = _TableCellMeta(
                text=text,
                is_header=cell.name.lower() == "th",
                rowspan=rowspan,
                colspan=colspan,
                source_cell_id=f"r{row_index}c{col_index}",
            )
            for row_delta in range(rowspan):
                for col_delta in range(colspan):
                    target = (row_index + row_delta, col_index + col_delta)
                    if row_delta == 0 and col_delta == 0:
                        _set_grid(grid, target[0], target[1], text)
                        _set_meta_grid(meta_grid, target[0], target[1], meta)
                    else:
                        occupied[target] = (text, meta)
            col_index += colspan

        while (row_index, col_index) in occupied:
            text, meta = occupied[(row_index, col_index)]
            _set_grid(grid, row_index, col_index, text)
            _set_meta_grid(meta_grid, row_index, col_index, meta)
            col_index += 1

    return _trim_matrix_and_meta(grid, meta_grid)


def _set_grid(grid: list[list[str]], row_index: int, col_index: int, value: str) -> None:
    while len(grid) <= row_index:
        grid.append([])
    while len(grid[row_index]) <= col_index:
        grid[row_index].append("")
    grid[row_index][col_index] = value


def _set_meta_grid(
    grid: list[list[_TableCellMeta | None]],
    row_index: int,
    col_index: int,
    value: _TableCellMeta,
) -> None:
    while len(grid) <= row_index:
        grid.append([])
    while len(grid[row_index]) <= col_index:
        grid[row_index].append(None)
    grid[row_index][col_index] = value


def _trim_matrix(matrix: list[list[str]]) -> list[list[str]]:
    trimmed, _ = _trim_matrix_and_meta(matrix, [])
    return trimmed


def _trim_matrix_and_meta(
    matrix: list[list[str]],
    meta_matrix: list[list[_TableCellMeta | None]],
) -> tuple[list[list[str]], list[list[_TableCellMeta]]]:
    rows = [[_normalize_text(cell) for cell in row] for row in matrix]
    normalized_meta: list[list[_TableCellMeta | None]] = []
    for row_index, row in enumerate(rows):
        source_meta = meta_matrix[row_index] if row_index < len(meta_matrix) else []
        normalized_meta.append(
            [
                source_meta[col_index] if col_index < len(source_meta) else None
                for col_index in range(len(row))
            ]
        )
    row_pairs = [
        (row, normalized_meta[row_index])
        for row_index, row in enumerate(rows)
        if any(cell for cell in row)
    ]
    if not row_pairs:
        return [], []
    rows = [row for row, _ in row_pairs]
    normalized_meta = [meta for _, meta in row_pairs]
    if not rows:
        return [], []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    normalized_meta = [row + [None] * (width - len(row)) for row in normalized_meta]
    non_empty_cols = [
        index
        for index in range(width)
        if any(row[index] for row in rows)
    ]
    trimmed_rows = [[row[index] for index in non_empty_cols] for row in rows]
    trimmed_meta: list[list[_TableCellMeta]] = []
    for row_index, row in enumerate(normalized_meta):
        trimmed_meta_row: list[_TableCellMeta] = []
        for col_index in non_empty_cols:
            meta = row[col_index]
            if meta is None:
                meta = _TableCellMeta(
                    text="",
                    is_header=False,
                    rowspan=1,
                    colspan=1,
                    source_cell_id=f"r{row_index}c{col_index}",
                )
            trimmed_meta_row.append(meta)
        trimmed_meta.append(trimmed_meta_row)
    return trimmed_rows, trimmed_meta


def _classify_table_kind(table: Tag, matrix: list[list[str]], text: str, rules: dict[str, Any]) -> str:
    kind_rules = rules.get("table_kind_rules") or {}
    if not matrix:
        return "image_placeholder"
    if _is_unit_table(table, matrix, text, rules):
        return str(kind_rules.get("unit_kind", "footnote"))
    if _is_note_only_table(table, matrix, text, rules):
        return "footnote"

    max_title_rows = int(kind_rules.get("max_title_rows", 1))
    max_title_cols = int(kind_rules.get("max_title_cols", 2))
    if len(matrix) <= max_title_rows and max(len(row) for row in matrix) <= max_title_cols:
        title_patterns = kind_rules.get("title_patterns", [])
        max_title_text_length = int(kind_rules.get("max_title_text_length", 80))
        if (
            not title_patterns
            or any(_regex_search(pattern, text) for pattern in title_patterns)
            or len(text) <= max_title_text_length
        ):
            return "title_block"

    paragraph_patterns = kind_rules.get("paragraph_patterns", [])
    if paragraph_patterns and any(_regex_search(pattern, text) for pattern in paragraph_patterns):
        return "paragraph_table"
    if len(matrix) <= int(kind_rules.get("max_paragraph_rows", 2)) and max(len(row) for row in matrix) <= 2:
        if len(text) >= int(kind_rules.get("paragraph_min_text_length", 120)):
            return "paragraph_table"

    data_min_rows = int(kind_rules.get("data_min_rows", 2))
    data_min_cols = int(kind_rules.get("data_min_cols", 2))
    if len(matrix) >= data_min_rows and max(len(row) for row in matrix) >= data_min_cols:
        return "data_table"
    return "unknown"


def _header_row_count(
    table_kind: str,
    matrix: list[list[str]],
    meta_matrix: list[list[_TableCellMeta]],
    rules: dict[str, Any],
) -> int:
    if table_kind != "data_table" or len(matrix) <= 1:
        return 0
    header_rules = rules.get("header_detection_rules") or {}
    max_header_rows = max(1, int(header_rules.get("max_header_rows", 3)))
    threshold = float(header_rules.get("th_ratio_threshold", 0.5))
    header_count = 0
    for row_index, row in enumerate(matrix[:max_header_rows]):
        meta_row = meta_matrix[row_index] if row_index < len(meta_matrix) else []
        th_count = sum(1 for meta in meta_row if meta.is_header)
        non_empty_count = max(1, sum(1 for cell in row if cell))
        row_text = " ".join(row)
        if row_index == 0 or th_count / non_empty_count >= threshold:
            header_count += 1
            continue
        if any(_regex_search(pattern, row_text) for pattern in header_rules.get("header_keyword_patterns", [])):
            header_count += 1
            continue
        break
    return max(1, min(header_count, len(matrix) - 1))


def _header_paths(matrix: list[list[str]], header_row_count: int) -> list[list[str]]:
    if not matrix or header_row_count <= 0:
        return []
    width = max(len(row) for row in matrix)
    paths: list[list[str]] = []
    for col_index in range(width):
        path: list[str] = []
        for row_index in range(header_row_count):
            value = matrix[row_index][col_index] if col_index < len(matrix[row_index]) else ""
            if value and value not in path:
                path.append(value)
        paths.append(path)
    return paths


def _flat_header(path: list[str]) -> str:
    return path[-1] if path else ""


def _table_cells(
    matrix: list[list[str]],
    meta_matrix: list[list[_TableCellMeta]],
    *,
    header_row_count: int,
    header_paths: list[list[str]],
    unit_text: str,
    rules: dict[str, Any],
) -> list[BusinessInfoCell]:
    cells: list[BusinessInfoCell] = []
    for row_idx, row in enumerate(matrix):
        for col_idx, raw_text in enumerate(row):
            meta = meta_matrix[row_idx][col_idx]
            is_header = row_idx < header_row_count or meta.is_header
            header_path = header_paths[col_idx] if col_idx < len(header_paths) else []
            cells.append(
                BusinessInfoCell(
                    row_idx=row_idx,
                    col_idx=col_idx,
                    raw_text=raw_text,
                    normalized_text=_normalize_text(raw_text),
                    is_header=is_header,
                    header_level=row_idx if is_header else -1,
                    header_path=header_path,
                    rowspan=meta.rowspan,
                    colspan=meta.colspan,
                    unit=unit_text,
                    data_type=_data_type(raw_text, rules),
                    source_cell_id=meta.source_cell_id,
                )
            )
    return cells


def _data_type(value: str, rules: dict[str, Any]) -> str:
    text = _normalize_text(value)
    if not text:
        return "empty"
    patterns = rules.get("data_type_patterns") or {}
    for data_type in ("percent", "date", "number"):
        for pattern in patterns.get(data_type, []) or []:
            if _regex_search(pattern, text):
                return data_type
    return "text"


def _table_hash(table: Tag) -> str:
    return hashlib.sha256(str(table).encode("utf-8", errors="replace")).hexdigest()[:16]


def _table_id(stock_code: str, rcept_no_or_period: str, section_key: str, table_index: int, html_table_hash: str) -> str:
    stable_period = re.sub(r"[^0-9A-Za-z]+", "", str(rcept_no_or_period))
    stable_section = re.sub(r"[^0-9A-Za-z_]+", "_", str(section_key)).strip("_") or "section"
    return f"KR_{stock_code}_{stable_period}_{stable_section}_{table_index:03d}_{html_table_hash[:8]}"


def _table_title(context_before: str, table_text: str, table_kind: str, rules: dict[str, Any]) -> str:
    if context_before:
        return context_before[:160]
    if table_kind in {"title_block", "footnote"}:
        return table_text[:160]
    return ""


def _ordered_body_nodes(soup: BeautifulSoup) -> list[Tag]:
    body = soup.body or soup
    nodes = [
        node
        for node in body.find_all(["p", "table"], recursive=False)
        if isinstance(node, Tag)
    ]
    if nodes:
        return nodes
    return [node for node in body.find_all(["p", "table"]) if isinstance(node, Tag)]


def _section_level(node: Tag) -> int:
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    for class_name in classes:
        match = re.fullmatch(r"section-(\d+)", str(class_name).strip(), flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _is_fallback_heading(node: Tag, rules: dict[str, Any]) -> bool:
    text = _tag_text(node)
    if not text or len(text) > 180:
        return False
    style = str(node.get("style") or "")
    if "font-weight:bold" in style.replace(" ", "").lower():
        return True
    for pattern in rules.get("heading_patterns", []):
        if _regex_search(pattern, text):
            return True
    return False


def _business_domain(title: str, rules: dict[str, Any]) -> str:
    for domain, patterns in (rules.get("business_domain_patterns") or {}).items():
        if any(_regex_search(pattern, title) for pattern in patterns or []):
            return str(domain)
    return ""


def _canonical_section_key(title: str, text: str, rules: dict[str, Any]) -> str:
    title_match = _best_section_alias_match(_compact_text(title), rules)
    if title_match != "other":
        return title_match
    if title_match == "other" and _compact_text("기타") in _compact_text(title):
        return title_match
    haystack = _compact_text(text[:500])
    return _best_section_alias_match(haystack, rules)


def _best_section_alias_match(haystack: str, rules: dict[str, Any]) -> str:
    best_key = "other"
    best_length = -1
    for key, aliases in (rules.get("section_aliases") or {}).items():
        for alias in aliases or []:
            needle = _compact_text(str(alias))
            if not needle or needle not in haystack:
                continue
            if key != "other" and best_key == "other":
                best_key = str(key)
                best_length = len(needle)
                continue
            if (key == "other") != (best_key == "other"):
                continue
            if len(needle) > best_length:
                best_key = str(key)
                best_length = len(needle)
    return best_key


def _classify_template(
    sections: list[BusinessInfoSection],
    rules: dict[str, Any],
    *,
    used_fallback: bool,
) -> str:
    titles = " ".join(section.raw_title for section in sections)
    text = " ".join(section.text[:1000] for section in sections)
    keys = {section.canonical_key for section in sections}
    domains = {section.business_domain for section in sections if section.business_domain}
    template_rules = rules.get("template_rules") or {}

    mixed_patterns = template_rules.get("mixed", {}).get("title_patterns", [])
    if len(domains) >= 2 or sum(1 for pattern in mixed_patterns if _regex_search(pattern, titles)) >= 2:
        return "mixed"

    real_estate_rules = template_rules.get("real_estate_light", {})
    not_applicable_count = sum(1 for section in sections if section.is_not_applicable)
    real_estate_min = int(real_estate_rules.get("not_applicable_min_count", 2))
    if (
        not_applicable_count >= real_estate_min
        and any(_regex_search(pattern, text) for pattern in real_estate_rules.get("text_patterns", []))
    ):
        return "real_estate_light"

    if used_fallback:
        return "legacy"

    financial_required = set(template_rules.get("financial", {}).get("required_section_keys", []))
    financial_patterns = template_rules.get("financial", {}).get("title_patterns", [])
    if financial_required.issubset(keys) or sum(1 for pattern in financial_patterns if _regex_search(pattern, titles)) >= 2:
        return "financial"

    standard_required = set(template_rules.get("standard", {}).get("required_section_keys", []))
    if standard_required.issubset(keys):
        return "standard"
    return "standard"


def _is_not_applicable(text: str, rules: dict[str, Any]) -> bool:
    if not text:
        return False
    has_phrase = any(_regex_search(pattern, text) for pattern in rules.get("not_applicable_patterns", []))
    return bool(has_phrase and len(text) <= 350)


def _matched_keywords(
    section_key: str,
    text: str,
    rules: dict[str, Any],
    *,
    sector_code: str | None,
) -> list[str]:
    keywords: list[str] = []
    for keyword in (rules.get("extraction_keywords") or {}).get(section_key, []):
        if str(keyword) in text:
            keywords.append(str(keyword))
    if sector_code:
        sector_rules = (rules.get("sector_keyword_rules") or {}).get(str(sector_code), {})
        for keyword in sector_rules.get("keywords", []) or []:
            if str(keyword) in text and str(keyword) not in keywords:
                keywords.append(str(keyword))
    return keywords


def _is_unit_table(table: Tag, matrix: list[list[str]], text: str, rules: dict[str, Any]) -> bool:
    layout_rules = rules.get("layout_table_rules") or {}
    if len(matrix) > int(layout_rules.get("max_layout_rows", 2)):
        return False
    for pattern in layout_rules.get("unit_patterns", []):
        if _regex_search(pattern, text):
            return True
    return False


def _is_note_only_table(table: Tag, matrix: list[list[str]], text: str, rules: dict[str, Any]) -> bool:
    layout_rules = rules.get("layout_table_rules") or {}
    class_values = table.get("class") or []
    if isinstance(class_values, str):
        class_values = class_values.split()
    has_layout_class = any(
        str(class_name) in set(layout_rules.get("layout_classes", []))
        for class_name in class_values
    )
    row_limit = int(layout_rules.get("max_layout_rows", 2))
    col_limit = int(layout_rules.get("max_layout_cols", 2))
    if not has_layout_class or len(matrix) > row_limit or max(len(row) for row in matrix) > col_limit:
        return False
    return any(_regex_search(pattern, text) for pattern in layout_rules.get("note_patterns", []))


def _table_context(nodes: list[Tag], table_node_index: int) -> str:
    for index in range(table_node_index - 1, -1, -1):
        node = nodes[index]
        if node.name and node.name.lower() == "p":
            text = _tag_text(node)
            if text:
                return text[:300]
    return ""


def _table_context_after(nodes: list[Tag], table_node_index: int) -> str:
    for index in range(table_node_index + 1, len(nodes)):
        node = nodes[index]
        if node.name and node.name.lower() == "table":
            return ""
        if node.name and node.name.lower() == "p":
            text = _tag_text(node)
            if text:
                return text[:300]
    return ""


def _record_metadata(document: BusinessInfoDocument) -> dict[str, Any]:
    return {
        "market": document.market,
        "corp_code": document.corp_code,
        "corp_name": document.corp_name,
        "stock_code": document.stock_code,
        "rcept_no": document.rcept_no,
        "report_code": document.report_code,
        "report_type": document.report_type,
        "period": document.period,
        "parser_version": document.parser_version,
        "source_uri": document.source_uri,
        "source_html_hash": document.source_html_hash,
        "source_path": str(document.source_path),
    }


def _header_value_map(header_paths: list[list[str]], row: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for col_index, value in enumerate(row):
        if col_index >= len(header_paths):
            key = f"column_{col_index}"
        else:
            key = " > ".join(header_paths[col_index]) or f"column_{col_index}"
        values[key] = value
    return values


def _extract_document_metadata(
    html: str,
    *,
    stock_code: str,
    period: str,
    rules: dict[str, Any],
) -> dict[str, str]:
    metadata_rules = rules.get("metadata_patterns") or {}
    rcept_no = _first_regex_group(html, metadata_rules.get("rcept_no_patterns", []))
    return {
        "corp_code": _first_regex_group(html, metadata_rules.get("corp_code_patterns", [])),
        "corp_name": _first_regex_group(html, metadata_rules.get("corp_name_patterns", [])),
        "rcept_no": rcept_no,
        "report_code": _report_code_from_period(period),
        "report_type": _report_type_from_period(period),
    }


def _first_regex_group(text: str, patterns: Iterable[str]) -> str:
    for pattern in patterns or []:
        try:
            match = re.search(str(pattern), text, flags=re.IGNORECASE)
        except re.error:
            continue
        if not match:
            continue
        if match.groups():
            return _normalize_text(match.group(1))
        return _normalize_text(match.group(0))
    return ""


def _report_code_from_period(period: str) -> str:
    month = _period_month(period)
    return {
        3: "11013",
        6: "11012",
        9: "11014",
        12: "11011",
    }.get(month, "")


def _report_type_from_period(period: str) -> str:
    month = _period_month(period)
    return {
        3: "quarter",
        6: "half",
        9: "quarter",
        12: "annual",
    }.get(month, "")


def _period_month(period: str) -> int:
    match = re.search(r"\.(\d{1,2})$", str(period))
    return int(match.group(1)) if match else 0


def _source_uri(path: Path, rules: dict[str, Any]) -> str:
    base_text = (rules.get("source_uri_rules") or {}).get("relative_to", "project_root")
    base = DATA_LAKE.root if base_text == "data_lake" else DATA_LAKE.root.parent
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _tag_text(tag: Tag) -> str:
    return _normalize_text(tag.get_text(" ", strip=True))


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _regex_search(pattern: str, text: str) -> bool:
    try:
        return re.search(str(pattern), str(text), flags=re.IGNORECASE) is not None
    except re.error:
        return str(pattern) in str(text)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, result)


def _normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _stock_code_from_path(path: Path) -> str:
    if path.parent.name:
        return path.parent.name
    raise ValueError(f"stock_code is required for path without parent stock-code directory: {path}")


def _period_from_path(path: Path) -> str:
    match = BUSINESS_INFO_FILE_RE.match(path.name)
    if not match:
        raise ValueError(f"cannot infer business-info period from filename: {path.name}")
    return f"{int(match.group('year'))}.{int(match.group('month')):02d}"


def _read_html(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")
