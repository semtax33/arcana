from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable

from bs4 import BeautifulSoup, Tag

from .detection import AccountingRegimeDetector, detect_document_dialect, detect_scope
from .models import (
    DocumentNode,
    DocumentRelation,
    FinancialDocumentIR,
    NodeKind,
    RelationType,
    SemanticAddress,
    SourceLocation,
)


def _positive_span(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def reconstruct_html_table_grid(table: Tag) -> list[list[Tag | None]]:
    """Expand rowspan/colspan into a logical 2-D grid."""

    rows = table.find_all("tr")
    occupied: dict[tuple[int, int], Tag] = {}
    max_column = 0
    for row_index, row in enumerate(rows):
        column_index = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (row_index, column_index) in occupied:
                column_index += 1
            row_span = _positive_span(cell.get("rowspan"))
            column_span = _positive_span(cell.get("colspan"))
            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    occupied[(row_index + row_offset, column_index + column_offset)] = cell
            column_index += column_span
            max_column = max(max_column, column_index)
    if not occupied:
        return []
    max_row = max(row for row, _ in occupied) + 1
    return [
        [occupied.get((row, column)) for column in range(max_column)]
        for row in range(max_row)
    ]


def build_document_ir_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    document_id: str,
    dialect=None,
    accounting_regime=None,
    metadata: dict[str, Any] | None = None,
    text_normalizer: Callable[[Any], str] = str,
) -> FinancialDocumentIR:
    from .models import AccountingRegime, AccountingRegimeFamily, DocumentDialect

    document = FinancialDocumentIR(
        document_id=document_id,
        dialect=dialect or DocumentDialect.UNKNOWN,
        accounting_regime=accounting_regime
        or AccountingRegime(AccountingRegimeFamily.UNKNOWN),
        metadata=dict(metadata or {}),
    )
    root = DocumentNode(document_id, NodeKind.DOCUMENT)
    document.add_node(root)
    table_ids: dict[tuple[str, int], str] = {}
    previous_row_by_table: dict[str, tuple[str, int]] = {}
    context_stack_by_table: dict[str, list[tuple[int, str, str]]] = {}

    for ordinal, row in enumerate(rows):
        statement_type = str(row.get("statement_type", "UNKNOWN"))
        table_index = int(row.get("table_index", 0) or 0)
        table_key = (statement_type, table_index)
        table_id = table_ids.get(table_key)
        if table_id is None:
            table_id = f"{document_id}:table:{statement_type}:{table_index}"
            table_ids[table_key] = table_id
            document.add_node(
                DocumentNode(
                    table_id,
                    NodeKind.TABLE,
                    raw_text=str(row.get("table_title", "")),
                    normalized_text=text_normalizer(row.get("table_title", "")),
                    attributes={"statement_type": statement_type, "table_index": table_index},
                )
            )
            document.add_relation(
                DocumentRelation(document_id, RelationType.PARENT_OF, table_id)
            )
            context_stack_by_table[table_id] = []

        row_index = int(row.get("row_index", ordinal) or ordinal)
        row_id = f"{table_id}:row:{row_index}"
        raw_label = str(row.get("original_account_name", ""))
        indent = int(row.get("indent_level", 0) or 0)
        stack = context_stack_by_table[table_id]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        row_header_path = tuple(label for _, _, label in stack) + (raw_label,)
        address = SemanticAddress(
            section_path=tuple(
                value
                for value in (
                    str(row.get("section_context", "")),
                    str(row.get("table_title", "")),
                )
                if value
            ),
            row_header_path=row_header_path,
            unit=str(row.get("unit", "")),
            currency=str(row.get("currency", "")),
            scope=detect_scope(str(row.get("table_title", ""))),
        )
        source = SourceLocation(
            source_uri=str(row.get("source_uri", "")),
            document_id=document_id,
            table_index=table_index,
            row_index=row_index,
        )
        document.add_node(
            DocumentNode(
                row_id,
                NodeKind.ROW,
                raw_text=raw_label,
                normalized_text=text_normalizer(raw_label),
                semantic_address=address,
                attributes={**row, "indent_level": indent},
                source=source,
            )
        )
        document.add_relation(DocumentRelation(table_id, RelationType.PARENT_OF, row_id))
        if stack:
            document.add_relation(
                DocumentRelation(stack[-1][1], RelationType.PARENT_OF, row_id)
            )
        previous = previous_row_by_table.get(table_id)
        if previous is not None:
            document.add_relation(
                DocumentRelation(previous[0], RelationType.ABOVE, row_id)
            )
        previous_row_by_table[table_id] = (row_id, indent)
        if bool(row.get("has_children")):
            stack.append((indent, row_id, raw_label))
    return document


class DartHtmlDocumentAdapter:
    """Build a source-faithful graph IR from either legacy HTML or XBRL HTML."""

    def __init__(self, *, text_normalizer: Callable[[Any], str] = str) -> None:
        self.text_normalizer = text_normalizer
        self.regime_detector = AccountingRegimeDetector()

    def parse(self, path: str | Path, *, filing_date=None) -> FinancialDocumentIR:
        path = Path(path)
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="ignore")
        soup = BeautifulSoup(text, "lxml")
        document_id = sha256(raw).hexdigest()
        namespaces = [
            str(value)
            for tag in soup.find_all(True)
            for key, value in tag.attrs.items()
            if str(key).lower().startswith("xmlns")
        ]
        detection = self.regime_detector.detect(
            soup.get_text(" ", strip=True),
            taxonomy_namespaces=namespaces,
            filing_date=filing_date,
        )
        document = FinancialDocumentIR(
            document_id=document_id,
            dialect=detect_document_dialect(text, filing_date=filing_date),
            accounting_regime=detection.regime,
            metadata={
                "path": str(path),
                "sha256": document_id,
                "regime_confidence": detection.confidence,
                "regime_evidence": [item.__dict__ for item in detection.evidence],
            },
        )
        root = DocumentNode(
            document_id,
            NodeKind.DOCUMENT,
            source=SourceLocation(source_uri=str(path), document_id=document_id, sha256=document_id),
        )
        document.add_node(root)
        for table_index, table in enumerate(soup.find_all("table")):
            table_id = f"{document_id}:table:{table_index}"
            document.add_node(
                DocumentNode(
                    table_id,
                    NodeKind.TABLE,
                    raw_text=table.get_text(" ", strip=True),
                    normalized_text=self.text_normalizer(table.get_text(" ", strip=True)),
                    attributes={"table_index": table_index},
                )
            )
            document.add_relation(DocumentRelation(document_id, RelationType.PARENT_OF, table_id))
            grid = reconstruct_html_table_grid(table)
            seen_cells: set[int] = set()
            for row_index, grid_row in enumerate(grid):
                row_id = f"{table_id}:row:{row_index}"
                document.add_node(
                    DocumentNode(row_id, NodeKind.ROW, attributes={"row_index": row_index})
                )
                document.add_relation(DocumentRelation(table_id, RelationType.PARENT_OF, row_id))
                for column_index, cell in enumerate(grid_row):
                    if cell is None:
                        continue
                    origin = id(cell)
                    if origin in seen_cells:
                        continue
                    seen_cells.add(origin)
                    cell_id = f"{row_id}:cell:{column_index}"
                    raw_text = cell.get_text(" ", strip=True)
                    document.add_node(
                        DocumentNode(
                            cell_id,
                            NodeKind.CELL,
                            raw_text=raw_text,
                            normalized_text=self.text_normalizer(raw_text),
                            attributes={
                                "row_index": row_index,
                                "column_index": column_index,
                                "rowspan": _positive_span(cell.get("rowspan")),
                                "colspan": _positive_span(cell.get("colspan")),
                                "is_header": cell.name == "th",
                            },
                            source=SourceLocation(
                                source_uri=str(path),
                                document_id=document_id,
                                table_index=table_index,
                                row_index=row_index,
                                column_index=column_index,
                                sha256=document_id,
                            ),
                        )
                    )
                    document.add_relation(DocumentRelation(row_id, RelationType.PARENT_OF, cell_id))
        return document
