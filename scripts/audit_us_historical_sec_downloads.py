from __future__ import annotations

"""Audit resumable SEC 10-K/10-Q bundle checkpoints and v3 manifests."""

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterable

from engine.core.paths import DATA_LAKE
from engine.extractors._internal.sec_filings import SEC_FILING_BUNDLE_SCHEMA_VERSION


DEFAULT_TARGET_PATH = DATA_LAKE.meta("us_historical_2006_2016_factor_targets.csv")
DEFAULT_TICKER_MAP_PATH = DATA_LAKE.meta("sec_company_tickers.csv")
DEFAULT_ALIAS_PATH = DATA_LAKE.meta("sec_ticker_aliases.csv")
DEFAULT_FILINGS_DIR = DATA_LAKE.bronze("sec", "fillings")
DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "deliverables"
    / "us_historical_2006_2016_sec_download_audit.json"
)
VALID_ROLES = {"instance", "schema", "label", "presentation", "definition", "calculation"}
RENDER_XML = re.compile(r"(?:r\d+|filingsummary|defnref)\.xml", re.IGNORECASE)


def query_fingerprint(start_date: str, end_date: str) -> str:
    query = {
        "forms": ["10-K", "10-Q"],
        "start_date": start_date,
        "end_date": end_date,
        "ir_only": False,
        "bundle_schema_version": SEC_FILING_BUNDLE_SCHEMA_VERSION,
    }
    return sha256(json.dumps(query, sort_keys=True).encode("utf-8")).hexdigest()


def _cik_key(value: str) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    return f"CIK{int(digits):010d}" if digits else ""


def load_target_ciks(
    target_path: Path,
    *,
    ticker_map_paths: Iterable[Path],
) -> tuple[list[str], list[str]]:
    ticker_to_cik: dict[str, str] = {}
    for path in ticker_map_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                ticker = str(row.get("ticker") or "").strip().upper()
                cik = _cik_key(str(row.get("cik") or ""))
                if ticker and cik:
                    ticker_to_cik.setdefault(ticker, cik)
    with target_path.open("r", encoding="utf-8-sig", newline="") as stream:
        symbols = sorted(
            {
                str(row.get("security_id") or "")
                .strip()
                .removeprefix("SEC_US_")
                .upper()
                for row in csv.DictReader(stream)
                if str(row.get("security_id") or "").strip()
            }
        )
    unresolved = [symbol for symbol in symbols if symbol not in ticker_to_cik]
    ciks = sorted({ticker_to_cik[symbol] for symbol in symbols if symbol in ticker_to_cik})
    return ciks, unresolved


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_bundle_child(bundle: Path, name: str) -> Path | None:
    filename = str(name).strip()
    if not filename or Path(filename).name != filename:
        return None
    return bundle / filename


def audit_downloads(
    expected_ciks: Iterable[str],
    *,
    unresolved_symbols: Iterable[str],
    filings_dir: Path,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    ciks = sorted({_cik_key(value) for value in expected_ciks if _cik_key(value)})
    expected_fingerprint = query_fingerprint(start_date, end_date)
    checkpoint_dir = filings_dir / "_checkpoints"
    violations: list[str] = []
    complete_ciks = 0
    referenced_file_count = 0
    manifest_counts = {"10-K": 0, "10-Q": 0}
    manifests_without_xbrl = {"10-K": 0, "10-Q": 0}

    for cik in ciks:
        checkpoint_path = checkpoint_dir / f"{cik}.json"
        checkpoint = _load_json(checkpoint_path)
        if checkpoint is None:
            violations.append(f"{cik}:missing_or_invalid_checkpoint")
            continue
        if checkpoint.get("status") != "complete":
            violations.append(f"{cik}:checkpoint_not_complete")
            continue
        if checkpoint.get("query_fingerprint") != expected_fingerprint:
            violations.append(f"{cik}:checkpoint_query_mismatch")
            continue
        if checkpoint.get("errors"):
            violations.append(f"{cik}:checkpoint_has_errors")
            continue
        files = checkpoint.get("files")
        if not isinstance(files, list):
            violations.append(f"{cik}:checkpoint_files_invalid")
            continue
        missing_files = [
            str(relative)
            for relative in files
            if not (filings_dir / str(relative)).is_file()
            or (filings_dir / str(relative)).stat().st_size <= 0
        ]
        if missing_files:
            violations.append(f"{cik}:missing_referenced_files:{len(missing_files)}")
            continue
        complete_ciks += 1
        referenced_file_count += len(files)

        for relative in files:
            if Path(str(relative)).name.lower() != "filing.json":
                continue
            manifest_path = filings_dir / str(relative)
            manifest = _load_json(manifest_path)
            if manifest is None:
                violations.append(f"{cik}:{relative}:invalid_manifest")
                continue
            form = str(manifest.get("form") or "").upper()
            if form not in manifest_counts:
                violations.append(f"{cik}:{relative}:unsupported_form:{form}")
                continue
            manifest_counts[form] += 1
            if manifest.get("schema_version") != SEC_FILING_BUNDLE_SCHEMA_VERSION:
                violations.append(f"{cik}:{relative}:manifest_schema_mismatch")
            filing_date = str(manifest.get("filing_date") or "")[:10]
            if not filing_date or not start_date <= filing_date <= end_date:
                violations.append(f"{cik}:{relative}:filing_date_out_of_scope:{filing_date}")
            expected_authority = "SEC_10K_AUDITED" if form == "10-K" else "SEC_10Q_UNAUDITED"
            if manifest.get("source_authority") != expected_authority:
                violations.append(f"{cik}:{relative}:source_authority_mismatch")

            bundle = manifest_path.parent
            primary_name = str(manifest.get("primary_document") or "")
            primary_path = _safe_bundle_child(bundle, primary_name) if primary_name else None
            if primary_path is None or not primary_path.is_file() or primary_path.stat().st_size <= 0:
                violations.append(f"{cik}:{relative}:primary_document_missing")

            documents = manifest.get("xbrl_documents")
            if not isinstance(documents, list):
                violations.append(f"{cik}:{relative}:xbrl_documents_invalid")
                continue
            if not documents:
                manifests_without_xbrl[form] += 1
            for document in documents:
                if not isinstance(document, dict):
                    violations.append(f"{cik}:{relative}:xbrl_document_invalid")
                    continue
                role = str(document.get("role") or "")
                name = str(document.get("document_name") or "")
                if role not in VALID_ROLES:
                    violations.append(f"{cik}:{relative}:invalid_xbrl_role:{role}")
                if RENDER_XML.fullmatch(Path(name).name):
                    violations.append(f"{cik}:{relative}:render_xml_in_manifest:{name}")
                document_path = _safe_bundle_child(bundle, name) if name else None
                if (
                    document_path is None
                    or not document_path.is_file()
                    or document_path.stat().st_size <= 0
                ):
                    violations.append(f"{cik}:{relative}:xbrl_document_missing:{name}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": "us",
        "start_date": start_date,
        "end_date": end_date,
        "bundle_schema_version": SEC_FILING_BUNDLE_SCHEMA_VERSION,
        "expected_cik_count": len(ciks),
        "complete_cik_count": complete_ciks,
        "referenced_file_count": referenced_file_count,
        "manifest_counts": manifest_counts,
        "manifests_without_xbrl": manifests_without_xbrl,
        "explicit_identity_gaps": sorted(set(unresolved_symbols)),
        "violation_count": len(violations),
        "violations": violations,
        "passed": not violations and complete_ciks == len(ciks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2006-01-01")
    parser.add_argument("--end-date", default="2016-12-31")
    parser.add_argument("--target-path", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--ticker-map-path", type=Path, default=DEFAULT_TICKER_MAP_PATH)
    parser.add_argument("--alias-path", type=Path, default=DEFAULT_ALIAS_PATH)
    parser.add_argument("--filings-dir", type=Path, default=DEFAULT_FILINGS_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()
    if args.start_date > args.end_date:
        raise ValueError("start-date must not be after end-date")
    ciks, unresolved = load_target_ciks(
        args.target_path,
        ticker_map_paths=(args.alias_path, args.ticker_map_path),
    )
    report = audit_downloads(
        ciks,
        unresolved_symbols=unresolved,
        filings_dir=args.filings_dir,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "[AUDIT] "
        f"passed={report['passed']}, complete={report['complete_cik_count']:,}/"
        f"{report['expected_cik_count']:,}, manifests="
        f"{sum(report['manifest_counts'].values()):,}, "
        f"violations={report['violation_count']:,}, report={args.report_path}",
        flush=True,
    )
    if not report["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
