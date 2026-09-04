from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from engine.core.paths import DATA_LAKE
from engine.semantic import (
    DisclosureDocument,
    DisclosureHtmlParser,
    DisclosureSourceType,
    NarrativeAccountScanner,
    NarrativeRelationExtractor,
    load_semantic_mapping_rules,
    write_disclosure_csvs,
)
from engine.transformers._internal.dart_filings import normalize_account_name


DEFAULT_SEMANTIC_RULE_PATH = DATA_LAKE.rules("semantic_kr_v2.yaml")


def build_disclosure_parser(rule_path: str | Path = DEFAULT_SEMANTIC_RULE_PATH) -> DisclosureHtmlParser:
    ruleset = load_semantic_mapping_rules([rule_path], text_normalizer=normalize_account_name)
    scanner = NarrativeAccountScanner.from_ruleset(ruleset)
    return DisclosureHtmlParser(NarrativeRelationExtractor(scanner))


def normalize_disclosure_files(
    paths: Iterable[str | Path],
    *,
    source_type: DisclosureSourceType,
    output_root: str | Path,
    rule_path: str | Path = DEFAULT_SEMANTIC_RULE_PATH,
    sector_codes: Mapping[str, str] | None = None,
    industry_group_codes: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Normalize complete disclosure documents into stock-partitioned semantic CSVs."""

    parser = build_disclosure_parser(rule_path)
    documents_by_stock: dict[str, list[DisclosureDocument]] = defaultdict(list)
    failures: list[dict[str, str]] = []
    sector_codes = sector_codes or {}
    industry_group_codes = industry_group_codes or {}
    for raw_path in paths:
        path = Path(raw_path)
        stock_code = path.parent.name
        try:
            document = parser.parse(
                path,
                source_type=source_type,
                sector_code=str(sector_codes.get(stock_code, "")),
                industry_group_code=str(industry_group_codes.get(stock_code, "")),
            )
        except Exception as exc:
            failures.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        documents_by_stock[stock_code].append(document)

    written: dict[str, tuple[Path, Path, Path, Path]] = {}
    for stock_code, documents in sorted(documents_by_stock.items()):
        written[stock_code] = write_disclosure_csvs(documents, Path(output_root) / stock_code)
    return {
        "input_file_count": sum(len(items) for items in documents_by_stock.values()) + len(failures),
        "parsed_document_count": sum(len(items) for items in documents_by_stock.values()),
        "failed_document_count": len(failures),
        "candidate_count": sum(len(document.candidates) for items in documents_by_stock.values() for document in items),
        "review_required_count": sum(sum(candidate.review_required for candidate in document.candidates) for items in documents_by_stock.values() for document in items),
        "ambiguous_auto_emit_count": sum(sum(candidate.auto_emit_eligible for candidate in document.candidates if candidate.period_role == "AMBIGUOUS") for items in documents_by_stock.values() for document in items),
        "written": written,
        "failures": failures,
    }
