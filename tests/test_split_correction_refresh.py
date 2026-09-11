"""The public refresh removes withdrawn actions across DART source formats."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd


PROJECT = Path(__file__).resolve().parents[1]
ORIGINAL = "20200401000001"
WITHDRAWAL = "20200510000002"
DECISION = """<html><body><h1>주식분할 결정</h1><table>
<tr><td>분할 전</td><td>분할 후</td></tr>
<tr><td>1주당 가액(원)</td><td>5,000</td><td>2,500</td></tr>
<tr><td>보통주식</td><td>100</td><td>200</td></tr>
<tr><td>신주상장예정일</td><td>2020.05.20</td></tr>
<tr><td>이사회결의일</td><td>2020.04.01</td></tr>
</table></body></html>"""


def disclosure(lake, receipt, body, *, symbol="999990", family_id=None, family=None):
    path = lake / "bronze/dart/stock_splits/disclosures" / symbol / f"{receipt}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = body.encode("utf-8")
    path.write_bytes(raw)
    metadata = dict(provider="DART", source_id=receipt, security_id=f"SEC_KR_{symbol}",
        stock_code=symbol, published_date=f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}",
        source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
        source_sha256=sha256(raw).hexdigest(), family_id=family_id or f"api:{receipt}",
        fixture_kind="synthetic disclosure for the public refresh contract")
    if family is not None:
        metadata["family"] = family
    path.with_suffix(".html.metadata.json").write_text(json.dumps(metadata), "utf-8")
    return path


def prices(lake, symbol="999990"):
    path = lake / "bronze/krx/price" / f"kr_{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    # The decline is an ordinary market loss once the proposed split is withdrawn.
    path.write_text("날짜,시가,고가,저가,종가,거래량,등락률\n"
                    "2020-05-19,100,100,100,100,10,0\n"
                    "2020-05-20,50,50,50,50,20,-50\n", "utf-8")
    return path


def refresh(lake, *, as_of="2020-05-21", symbols="999990", with_prices=True):
    # Configure only the public filesystem boundary before importing the actual CLI.
    program = """
import runpy,sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv.pop(1)))
runpy.run_module('engine.workflows.stock_splits',run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake),
        "--market", "kr", "--symbols", symbols, "--end-date", as_of, "--skip-download",
        *([] if with_prices else ["--skip-prices"])],
        cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    gold = lake / "gold/corporate_actions/kr"
    return json.loads((gold / "stock_splits.json").read_bytes()), (
        pd.read_parquet(gold / "prices/kr_999990.parquet") if with_prices else None)


def test_withdrawal_removes_stale_candidate_when_api_and_public_family_ids_differ(tmp_path):
    lake = tmp_path / "data-lake"
    raw_price = prices(lake)
    original = disclosure(lake, ORIGINAL, DECISION)
    first, before = refresh(lake)
    assert first["events"] == []
    assert any(candidate["source_id"] == ORIGINAL for review in first["review"]
               for candidate in review.get("candidate_events", []))
    assert before.adj_close.tolist() == [100.0, 50.0]

    withdrawn = disclosure(lake, WITHDRAWAL, "<html><body>주식분할 결정을 철회합니다.</body></html>",
        family_id=ORIGINAL, family=[ORIGINAL, WITHDRAWAL])
    source_bytes = {path: path.read_bytes() for path in [original, withdrawn, raw_price]}
    latest, after = refresh(lake)

    assert not any(candidate["source_id"] == ORIGINAL for review in latest["review"]
                   for candidate in review.get("candidate_events", []))
    assert latest["events"] == []
    assert after.close.tolist() == after.adj_close.tolist() == [100.0, 50.0]
    assert after.adj_close.pct_change().iloc[-1] == -0.5
    assert all(path.read_bytes() == raw for path, raw in source_bytes.items())


def test_linked_correction_chain_uses_only_disclosures_available_by_cutoff(tmp_path):
    lake = tmp_path / "data-lake"
    prices(lake)
    intermediate = "20200420000003"
    disclosure(lake, ORIGINAL, DECISION)
    disclosure(lake, intermediate, DECISION, family_id=ORIGINAL, family=[ORIGINAL, intermediate])
    disclosure(lake, WITHDRAWAL, "<html><body>주식분할 결정을 철회합니다.</body></html>",
        family_id=intermediate, family=[intermediate, WITHDRAWAL])

    before, _ = refresh(lake, as_of="2020-05-05", with_prices=False)
    candidates = [candidate["source_id"] for review in before["review"]
                  for candidate in review.get("candidate_events", [])]
    assert candidates == [intermediate]

    after, panel = refresh(lake)
    assert not any(review.get("candidate_events") for review in after["review"])
    assert after["events"] == []
    assert panel.adj_close.tolist() == [100.0, 50.0]


def test_withdrawal_for_another_security_does_not_erase_the_subject_candidate(tmp_path):
    lake = tmp_path / "data-lake"
    prices(lake)
    disclosure(lake, ORIGINAL, DECISION)
    disclosure(lake, WITHDRAWAL, "<html><body>주식분할 결정을 철회합니다.</body></html>",
        symbol="999980", family_id=ORIGINAL, family=[ORIGINAL, WITHDRAWAL])

    report, panel = refresh(lake)

    candidates = [candidate["source_id"] for review in report["review"]
                  for candidate in review.get("candidate_events", [])]
    assert candidates == [ORIGINAL]
    assert panel.adj_close.tolist() == [100.0, 50.0]
