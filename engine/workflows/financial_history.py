"""Publish explicitly reviewed KR receipts to the factor reader's history index."""
from datetime import date
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from bs4 import BeautifulSoup, SoupStrainer
import pandas as pd

from engine.transformers._internal.dart_document import _decode
from engine.semantic.models import AccountingRegimeFamily


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _checked_file(root, name, expected):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Financial evidence path escapes its root")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError("Financial evidence digest mismatch")
    return raw


def _install(path, raw):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Immutable financial history artifact differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def publish_reviewed_financial_history(review_path, *, financial_dir):
    """Merge accepted receipts, committing the reader-visible manifest last.

    Normalization success and accounting identities are not review approval.
    Each input needs explicit account, period, unit and scope review evidence.
    This publishes files only; callers decide when to recompute/load factors.
    An additive review extension pins the previous manifest and preserves every
    previously accepted amount and its source/availability identity.
    """
    review_path = Path(review_path).resolve()
    review_raw = review_path.read_bytes()
    review = json.loads(review_raw)
    source_root = (review_path.parent / review.get("source_root", ".")).resolve()
    symbol, corp = review.get("symbol", ""), review.get("corp_code", "")
    if (review.get("schema_version") != 1 or review.get("market") != "kr"
            or not re.fullmatch(r"\d{6}", symbol) or not re.fullmatch(r"\d{8}", corp)):
        raise ValueError("A KR financial review must identify its symbol and DART issuer")
    prepared = {}
    artifacts = {}
    for item in review.get("receipts", []):
        receipt = item["rcept_no"]
        regime = AccountingRegimeFamily(item.get("accounting_regime", "UNKNOWN"))
        if (not re.fullmatch(r"\d{14}", receipt) or receipt in prepared
                or item.get("review_status") != "verified" or not item.get("review_evidence", "").strip()
                or item.get("financial_scope") not in {"CFS", "OFS"}
                or regime == AccountingRegimeFamily.UNKNOWN):
            raise ValueError("Each unique financial receipt requires explicit verified evidence")
        published = date.fromisoformat(item["report_date"])
        end = date.fromisoformat(item["period_end_date"])
        start = date.fromisoformat(item["period_start_date"])
        fiscal_month = int(item["fiscal_month"])
        fiscal_year = int(item["fiscal_year"])
        if (published.strftime("%Y%m%d") < receipt[:8] or published < end or start > end
                or fiscal_month not in {3, 6, 9, 12}
                or fiscal_year not in {end.year - 1, end.year}
                or (end.year - start.year) * 12 + end.month - start.month + 1 != fiscal_month
                or item["financial_basis"] not in {"annual", "quarterly"}
                or (item["financial_basis"] == "annual" and fiscal_month != 12)):
            raise ValueError("Receipt date or evidenced fiscal duration is inconsistent")
        publication_raw = None
        if item.get("publication_source_path"):
            publication_raw = _checked_file(source_root, item["publication_source_path"], item["publication_source_sha256"])
            publication_url = urlparse(item["publication_source_url"])
            publication_query = parse_qs(publication_url.query)
            if (publication_url.scheme != "https" or publication_url.hostname != "opendart.fss.or.kr"
                    or publication_url.path != "/api/list.json"
                    or {key.lower() for key in publication_query} & {"apikey", "api_key", "crtfc_key"}):
                raise ValueError("Actual publication date requires a retained OpenDART list response")
            index = json.loads(publication_raw)
            matching = [row for row in index.get("list", []) if row.get("rcept_no") == receipt and row.get("corp_code") == corp]
            if (index.get("status") != "000" or len(matching) != 1
                    or matching[0].get("rcept_dt") != published.strftime("%Y%m%d")):
                raise ValueError("Actual publication date differs from the retained DART index")
        elif published.strftime("%Y%m%d") != receipt[:8]:
            raise ValueError("A later publication date requires its DART index evidence")
        url = urlparse(item["source_url"])
        query = parse_qs(url.query)
        if (url.scheme != "https" or url.hostname not in {"dart.fss.or.kr", "opendart.fss.or.kr"}
                or query.get("rcept_no", query.get("rcpNo")) != [receipt]
                or {key.lower() for key in query} & {"apikey", "api_key", "crtfc_key"}):
            raise ValueError("Financial source URL must identify its DART receipt without credentials")
        source = _checked_file(source_root, item["source_path"], item["source_sha256"])
        soup = BeautifulSoup(re.sub(r"^\s*<\?xml[^>]*\?>", "", _decode(source)[0]), "html.parser",
                             parse_only=SoupStrainer("company-name"))
        issuer_ids = {node.get("aregcik") for node in soup.find_all("company-name")}
        if issuer_ids != {corp}:
            raise ValueError("Retained OpenDART document does not identify the reviewed issuer")
        normalized = _checked_file(source_root, item["normalized_path"], item["normalized_sha256"])
        frame = pd.read_csv(BytesIO(normalized), dtype=str).fillna("")
        ids = item.get("accepted_canonical_account_ids", [])
        if not ids or len(set(ids)) != len(ids) or "UNMAPPED" in ids:
            raise ValueError("Explicit reviewed canonical accounts are required")
        selected = frame.loc[frame.canonical_account_id.isin(ids)].copy()
        if (set(selected.canonical_account_id) != set(ids) or selected.canonical_account_id.duplicated().any()
                or not selected.period.eq(end.strftime("%Y.%m")).all()
                or not selected.statement_type.isin(["BS", "IS", "CIS", "CF"]).all()):
            raise ValueError("Accepted facts do not identify unique accounts in the reviewed period")
        amounts = pd.to_numeric(selected.normalized_amount, errors="coerce")
        if amounts.isna().any() or amounts.isin([float("inf"), -float("inf")]).any():
            raise ValueError("Accepted financial amounts must be finite reported values")
        output = selected.to_csv(index=False, lineterminator="\n").encode("utf-8")
        prefix = f"receipts/{receipt}"
        paths = {"normalized_path": f"{prefix}/{sha256(output).hexdigest()}.csv",
                 "source_path": item["source_path"],
                 "review_path": f"reviews/{sha256(review_raw).hexdigest()}.json"}
        record = {key: item[key] for key in (
            "rcept_no", "fiscal_year", "fiscal_month", "period_start_date", "period_end_date", "report_date",
            "financial_basis", "financial_scope", "source_url", "source_sha256", "review_evidence",
            "accepted_canonical_account_ids", "accounting_regime")}
        record.update(paths, evidence_root=str(source_root), normalized_sha256=sha256(output).hexdigest(),
                      review_sha256=sha256(review_raw).hexdigest(),
                      reviewed_normalization_sha256=item["normalized_sha256"],
                      fiscal_year=fiscal_year, fiscal_month=fiscal_month)
        if publication_raw is not None:
            publication_path = item["publication_source_path"]
            record.update(publication_source_path=publication_path,
                          publication_source_sha256=item["publication_source_sha256"],
                          publication_source_url=item["publication_source_url"])
        prepared[receipt] = record
        # Original provider responses already live in bronze. Keep their checked
        # locations instead of making raw HTML/API copies in the silver index.
        artifacts.update({paths["normalized_path"]: output, paths["review_path"]: review_raw})
    if not prepared:
        raise ValueError("No reviewed financial receipts were supplied")

    root = Path(financial_dir).resolve() / "history" / symbol
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    # Serialize competing publishers so an incremental merge cannot lose events.
    lock = root / ".publication.lock"
    lock_handle = lock.open("x")
    try:
        previous = manifest_path.read_bytes() if manifest_path.exists() else None
        existing = json.loads(previous) if previous else {
            "schema_version": 1, "market": "kr", "symbol": symbol, "corp_code": corp, "receipts": []}
        if any(existing.get(key) != expected for key, expected in (
                ("schema_version", 1), ("market", "kr"), ("symbol", symbol), ("corp_code", corp))):
            raise ValueError("Existing financial history identity differs")
        merged = {}
        for record in existing["receipts"]:
            for kind in ("normalized", "source", "review") + (("publication_source",) if record.get("publication_source_path") else ()):
                evidence_root = (Path(record.get("evidence_root", root))
                                 if kind in {"source", "publication_source"} else root)
                _checked_file(evidence_root, record[f"{kind}_path"], record[f"{kind}_sha256"])
            if record["rcept_no"] in merged:
                raise ValueError("Existing financial history duplicates a receipt")
            merged[record["rcept_no"]] = record
        extended = []
        for receipt, record in prepared.items():
            if receipt in merged:
                # Adding receipts changes the review bundle's digest, while an
                # already accepted event retains its original review provenance.
                unchanged = all(merged[receipt].get(key) == value for key, value in record.items()
                                if key not in {"review_path", "review_sha256", "evidence_root",
                                               "source_path", "publication_source_path"})
                if unchanged:
                    continue
                expected = review.get("expected_manifest_sha256", "")
                reason = review.get("revision_reason", "")
                if (not re.fullmatch(r"[0-9a-f]{64}", expected) or not isinstance(reason, str) or not reason.strip()
                        or previous is None or sha256(previous).hexdigest() != expected):
                    raise ValueError("Extending an accepted receipt requires its current manifest digest and revision reason")
                old = merged[receipt]
                identity = ("rcept_no", "fiscal_year", "fiscal_month", "period_start_date", "period_end_date",
                    "report_date", "financial_basis", "financial_scope", "accounting_regime", "source_url",
                    "source_sha256", "publication_source_sha256", "publication_source_url")
                if any(old.get(key) != record.get(key) for key in identity):
                    raise ValueError("Review extensions cannot change the original source or financial availability")
                old_ids = set(old["accepted_canonical_account_ids"])
                new_ids = set(record["accepted_canonical_account_ids"])
                if not old_ids < new_ids:
                    raise ValueError("Review extensions must add accounts and preserve all previously accepted accounts")
                old_rows = pd.read_csv(root / old["normalized_path"], dtype=str).fillna("").set_index("canonical_account_id").sort_index()
                new_rows = pd.read_csv(BytesIO(artifacts[record["normalized_path"]]), dtype=str).fillna("").set_index("canonical_account_id")
                retained = new_rows.loc[sorted(old_ids)]
                if list(old_rows.columns) != list(retained.columns) or not old_rows.equals(retained):
                    raise ValueError("Review extensions cannot revise previously accepted values or their metadata")
                extended.append(receipt)
            merged[receipt] = record
        periods = {}
        for record in merged.values():
            ordinal = (record["fiscal_year"], record["fiscal_month"])
            if ordinal in periods and periods[ordinal] != record["period_end_date"]:
                raise ValueError("Financial periods conflict within one fiscal quarter")
            periods[ordinal] = record["period_end_date"]
        manifest = dict(existing, receipts=sorted(merged.values(), key=lambda r: (r["report_date"], r["rcept_no"])))
        archive = None
        if extended:
            checksum = sha256(previous).hexdigest()
            archive = root / "manifests" / f"{checksum}.json"
            extension = dict(previous_manifest_path=f"manifests/{checksum}.json", previous_manifest_sha256=checksum,
                review_sha256=sha256(review_raw).hexdigest(), receipts=sorted(extended),
                reason=review["revision_reason"].strip())
            manifest["review_extensions"] = list(existing.get("review_extensions", [])) + [extension]
        encoded = _encoded(manifest)
        if previous == encoded:
            return {"status": "unchanged", "receipts": len(merged), "manifest_path": str(manifest_path)}
        if archive is not None:
            _install(archive, previous)
        for name, raw in artifacts.items():
            _install(root / name, raw)
        temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        temporary.replace(manifest_path)
        return {"status": "published", "receipts": len(merged), "manifest_path": str(manifest_path),
                **({"previous_manifest_path": str(archive), "extended_receipts": sorted(extended)} if archive is not None else {})}
    finally:
        lock_handle.close()
        lock.unlink()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--financial-dir", required=True)
    args = parser.parse_args()
    print(json.dumps(publish_reviewed_financial_history(args.review, financial_dir=args.financial_dir)))
