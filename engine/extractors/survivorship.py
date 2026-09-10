"""Retain official listing candidates independently of accepted lifecycle facts."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import write_source_bytes, write_source_text
from engine.extractors.alpha_vantage_prices import download_alpha_vantage_listings
from engine.extractors.opendart_stock_splits import OpenDartClient


def download_survivorship_sources(*, market, end_date, start_date=None, output_dir=None, force=False):
    if market == "us":
        result = download_alpha_vantage_listings(dates=[end_date], states=("active", "delisted"),
                                                output_dir=output_dir, force=force)
        if result["errors"]:
            raise RuntimeError("Alpha Vantage listing collection incomplete; see retained download report")
        return result
    if market != "kr":
        raise ValueError("market must be us or kr")
    root = Path(output_dir or DATA_LAKE.bronze("dart", "listings"))
    root.mkdir(parents=True, exist_ok=True)
    end = date.fromisoformat(end_date)
    state_path = root / "collection_state.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if start_date:
        value = str(start_date)
        start = date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}" if len(value) == 8 else value)
    else:
        start = date.fromisoformat(previous["covered_through"]) - timedelta(days=7) if previous else end - timedelta(days=31)
    if start > end:
        raise ValueError("DART listing collection start is after end")
    client = OpenDartClient()
    report = {"provider": "DART", "start_date": start.isoformat(), "end_date": end.isoformat(),
              "searches": [], "candidates": [], "coverage_complete": False, "status": "running"}
    candidates = {}

    def save_report():
        report["candidates"] = list(candidates.values())
        write_source_text(root / "collection_report.json", json.dumps(report, ensure_ascii=False, indent=2), source="dart-listing-inventory")

    def request(endpoint, params, relative_path, *, refresh):
        path = root / relative_path
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        if path.exists() and metadata_path.exists() and not refresh:
            raw = path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if hashlib.sha256(raw).hexdigest() == metadata["source_sha256"]:
                return raw, metadata
        response = client.get(endpoint, **params)
        raw = response.content
        metadata = {"provider": "DART", "request": params,
                    "source_url": f"https://opendart.fss.or.kr/api/{endpoint}?{urlencode(params)}",
                    "source_sha256": hashlib.sha256(raw).hexdigest(), "path": str(relative_path),
                    "retrieved_at": datetime.now(timezone.utc).isoformat()}
        write_source_bytes(path, raw, source="dart-listing-source")
        write_source_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2), source="dart-listing-provenance")
        return raw, metadata

    try:
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=30))
            for kind in ("I003", "I001", "B001", "E003"):
                page, found, expected = 1, 0, None
                while True:
                    params = {"bgn_de": cursor.strftime("%Y%m%d"), "end_de": stop.strftime("%Y%m%d"),
                              "pblntf_detail_ty": kind, "last_reprt_at": "N", "page_count": 100,
                              "page_no": page, "sort": "date", "sort_mth": "asc"}
                    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
                    raw, source = request("list.json", params, Path("lifecycle_search") / f"{digest}.json",
                                          refresh=force or stop >= date.today() - timedelta(days=7))
                    payload = json.loads(raw)
                    report["searches"].append(source)
                    status = payload.get("status")
                    if status == "013":
                        if page != 1:
                            raise ValueError("DART pagination ended before its declared count")
                        break
                    if status != "000":
                        raise ValueError(f"OpenDART list status={status}")
                    total = int(payload["total_count"])
                    if expected is not None and expected != total:
                        raise ValueError("DART listing pagination changed during collection")
                    expected = total
                    rows = payload.get("list", [])
                    found += len(rows)
                    for row in rows:
                        title = re.sub(r"\s+", "", row["report_nm"])
                        if not any(token in title for token in ("상장폐지", "정리매매", "합병", "주식의포괄적교환", "주식교환", "주식소각")):
                            continue
                        receipt = row["rcept_no"]
                        if not re.fullmatch(r"\d{14}", receipt):
                            raise ValueError("Invalid DART receipt")
                        published = datetime.strptime(row["rcept_dt"], "%Y%m%d").date().isoformat()
                        if receipt in candidates and candidates[receipt]["corp_code"] != row["corp_code"]:
                            raise ValueError("Conflicting DART issuer identity")
                        candidates[receipt] = {**row, "source_id": receipt, "published_date": published,
                                               "review_status": "pending", "index_source": source}
                    if page >= int(payload["total_page"]):
                        if found != expected:
                            raise ValueError("DART listing pagination count mismatch")
                        break
                    page += 1
            cursor = stop + timedelta(days=1)
            save_report()
            print(f"[DART LISTINGS] indexed through={stop} candidates={len(candidates)}", flush=True)
        for receipt, candidate in candidates.items():
            relative = Path("lifecycle_documents") / f"{receipt}.response"
            raw, source = request("document.xml", {"rcept_no": receipt}, relative, refresh=force)
            if raw.startswith(b"PK"):
                with ZipFile(io.BytesIO(raw)) as archive:
                    main = [name for name in archive.namelist() if Path(name).name == receipt + ".xml"]
                status = "available" if len(main) == 1 else "missing_or_ambiguous_main"
            else:
                status = ET.fromstring(raw).findtext("status", "unknown")
            candidate.update(document_status=status, document_path=str(relative), document_sha256=source["source_sha256"])
            if status not in {"available", "missing_or_ambiguous_main", "014"}:
                raise ValueError(f"OpenDART document status={status}")
        report["status"] = "collected_pending_review"
        save_report()
        write_source_text(state_path, json.dumps({"covered_from": start.isoformat(), "covered_through": end.isoformat()}),
                          source="dart-listing-collection-state")
        return {"provider": "DART", "status": report["status"], "candidate_count": len(candidates),
                "report_path": str((root / "collection_report.json").resolve()), "coverage_complete": False}
    except Exception:
        report["status"] = "incomplete"
        save_report()
        raise
