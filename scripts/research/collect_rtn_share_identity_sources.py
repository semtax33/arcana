"""Preserve bounded RTN issuer/share-class evidence without changing market inputs."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_frame, export_json
from engine.core.source_storage import write_source_bytes
from engine.extractors._internal.sec_filings import DEFAULT_SEC_USER_AGENT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, help="NAME=SEC_URL")
    parser.add_argument("--run-scope", default="share_identity_20260911",
                        help="Distinct collection scope; earlier source indexes remain unchanged.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_]+", args.run_scope):
        raise ValueError("Unsafe collection scope")
    bronze = DATA_LAKE.bronze("sec", "listings", args.run_scope, "RTN")
    silver = DATA_LAKE.silver("survivorship", "financial_research", f"us_rtn_{args.run_scope}")
    summary_path = silver / "collection.json"
    summary = json.loads(summary_path.read_bytes()) if summary_path.exists() else {
        "status": "collecting", "sources": {}, "production_changed": False,
        "coverage_complete": False,
    }
    script = Path(__file__).resolve()
    script_hash = sha256(script.read_bytes()).hexdigest()
    archive = silver / "implementation" / script_hash / script.name
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        archive.write_bytes(script.read_bytes())
    summary["collector"] = {"path": str(archive), "sha256": script_hash}
    for specification in args.source:
        name, url = specification.split("=", 1)
        parsed = urlparse(url)
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError("Unsafe evidence name")
        if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
            raise ValueError("Only original SEC evidence is accepted")
        if not (parsed.path.startswith("/Archives/edgar/data/1047122/") or
                parsed.path.startswith("/submissions/CIK0001047122")):
            raise ValueError("Source must concern the reviewed RTN issuer")
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".json", ".txt", ".html", ".htm"}:
            raise ValueError("Unsupported original format")
        path = bronze / (name + suffix)
        metadata_path = bronze / (name + ".metadata.json")
        if path.exists():
            raw = path.read_bytes()
            metadata = json.loads(metadata_path.read_bytes())
            if metadata["source_sha256"] != sha256(raw).hexdigest() or metadata["source_url"] != url:
                raise ValueError("Previously retained original changed")
        else:
            response = requests.get(url, headers={"User-Agent": DEFAULT_SEC_USER_AGENT,
                "Accept-Encoding": "identity"}, timeout=30)
            raw = response.content
            write_source_bytes(path, raw, source="SEC-RTN-share-identity-original")
            metadata = {"provider": "SEC", "source_url": url, "source_path": str(path),
                "source_sha256": sha256(raw).hexdigest(), "http_status": response.status_code,
                "retrieved_at": datetime.now(timezone.utc).isoformat()}
            export_json(metadata_path, metadata)
        summary["sources"][name] = dict(metadata)
        summary["status"] = "collecting"
        export_json(summary_path, summary)
        if metadata["http_status"] != 200:
            summary["status"] = "source_response_unavailable"
            export_json(summary_path, summary)
            raise ValueError("SEC failure response retained in Bronze")
        if suffix == ".json":
            payload = json.loads(raw)
            filings = payload.get("filings", {}).get("recent", payload)
            if "accessionNumber" in filings:
                frame = pd.DataFrame(filings)
                summary["sources"][name]["filings"] = export_frame(silver / (name + ".parquet"), frame)
            if "filings" in payload:
                summary["sources"][name]["older_submission_files"] = payload["filings"].get("files", [])
                summary["sources"][name]["issuer"] = {key: payload.get(key) for key in
                    ("cik", "name", "tickers", "exchanges", "formerNames")}
        else:
            soup = BeautifulSoup(raw, "html.parser")
            for node in soup.find_all(["script", "style"]):
                node.decompose()
            text_path = silver / (name + ".txt")
            text_path.write_text(soup.get_text("\n", strip=True), "utf-8")
            summary["sources"][name]["review_text"] = {
                "path": str(text_path), "sha256": sha256(text_path.read_bytes()).hexdigest()}
        summary["status"] = ("originals_retained_with_unavailable_responses"
            if any(source["http_status"] != 200 for source in summary["sources"].values())
            else "originals_retained_pending_review")
        export_json(summary_path, summary)
        print(json.dumps({"document": name, "bytes": len(raw), "status": summary["status"]}), flush=True)


if __name__ == "__main__":
    main()
