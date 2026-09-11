"""Use exact DART table-of-contents viewer parameters for capital source follow-up."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import write_source_bytes
from engine.transformers._internal.dart_document import _decode


def main():
    bronze = DATA_LAKE.bronze("dart", "corporate_actions", "proposed_listings_20260911", "public_viewers", "204210")
    silver = DATA_LAKE.silver("survivorship", "financial_research", "kr_capital_followup_sections_20260911")
    if silver.exists():
        raise ValueError("Preserve previous section collection")
    silver.mkdir(parents=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Arcana Research", "Referer": "https://dart.fss.or.kr/"})
    report = {"status": "collecting", "sources": [], "production_changed": False,
        "prior_viewer_diagnosis": "The default dtd=HTML request returned raw XML with unreadable Korean; it is retained but not approved as readable content. These requests use the exact main-page section parameters."}
    try:
        for receipt, titles in {"20160819000146": {"2. 집합투자기구의 연혁", "3. 투자회사의 출자금에 관한 사항"},
                                "20170518000045": {"3. 자본금 변동사항", "4. 주식의 총수 등"}}.items():
            main_path = bronze / (receipt + ".main.html")
            metadata = json.loads(main_path.with_suffix(main_path.suffix + ".metadata.json").read_bytes())
            if sha256(main_path.read_bytes()).hexdigest() != metadata["source_sha256"]:
                raise ValueError("Original DART main page changed")
            text = main_path.read_text("utf-8")
            pattern = r'''node(\d+)\['text'\]\s*=\s*"([^"]+)";([\s\S]*?)node\1\['dtd'\]\s*=\s*"([^"]+)";'''
            found = set()
            for match in re.finditer(pattern, text):
                title = match.group(2)
                if title not in titles:
                    continue
                if title in found:
                    raise ValueError("Ambiguous requested document section")
                found.add(title)
                params = dict(re.findall(r'''\['(rcpNo|dcmNo|eleId|offset|length)'\]\s*=\s*"([^"]+)"''', match.group(3)))
                params["dtd"] = match.group(4)
                if set(params) != {"rcpNo", "dcmNo", "eleId", "offset", "length", "dtd"} or params["rcpNo"] != receipt:
                    raise ValueError("Incomplete or mismatched section parameters")
                url = "https://dart.fss.or.kr/report/viewer.do?" + urlencode(params)
                path = bronze / (receipt + ".section_" + params["eleId"] + ".html")
                if path.exists():
                    raw = path.read_bytes()
                    item = json.loads(path.with_suffix(path.suffix + ".metadata.json").read_bytes())
                    if item["source_sha256"] != sha256(raw).hexdigest() or item["source_url"] != url:
                        raise ValueError("Retained original section changed")
                else:
                    response = session.get(url, timeout=25)
                    raw = response.content
                    write_source_bytes(path, raw, source="DART-original-capital-section-viewer")
                    item = {"receipt": receipt, "title": title, "request": params, "source_path": str(path),
                        "source_url": url, "source_sha256": sha256(raw).hexdigest(), "http_status": response.status_code,
                        "main_path": str(main_path), "main_sha256": metadata["source_sha256"],
                        "retrieved_at": datetime.now(timezone.utc).isoformat()}
                    export_json(path.with_suffix(path.suffix + ".metadata.json"), item)
                report["sources"].append(item)
                if item["http_status"] != 200:
                    raise ValueError("Section response unavailable")
                decoded, encoding = _decode(raw)
                soup = BeautifulSoup(decoded, "lxml")
                for node in soup.find_all(["style", "script"]):
                    node.decompose()
                clean = soup.get_text("\n", strip=True)
                if re.sub(r"\s+", "", title) not in re.sub(r"\s+", "", clean):
                    raise ValueError("Returned section lacks its selected Korean title")
                text_path = silver / (receipt + ".section_" + params["eleId"] + ".txt")
                text_path.write_text(clean, "utf-8")
                item.update(review_text_path=str(text_path), review_text_sha256=sha256(text_path.read_bytes()).hexdigest(), encoding=encoding)
                export_json(silver / "summary.json", report)
                print(json.dumps({"receipt": receipt, "title": title, "text_characters": len(clean)}, ensure_ascii=False), flush=True)
            if found != titles:
                raise ValueError("Requested section absent from original main page")
        report["status"] = "original_sections_retained_and_titles_verified"
    except BaseException as error:
        report.update(status="collection_failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        Path(silver / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
        export_json(silver / "summary.json", report)


if __name__ == "__main__":
    main()
