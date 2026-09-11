"""Disclosed SEC versions survive normalization and reach public FactorLab reads."""
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest
import yaml

from test_us_period_vintage_refresh import ordinary_us_refresh
from test_historical_refresh_resume import us_refresh_environment
from test_share_input_refresh import share_refresh_environment

pytestmark = pytest.mark.integration


def normalize_disclosed_balances(lake, versions, *, start_year=2019, end_year=2019, share_only_date=None):
    """Run the real normalization CLI with isolated, synthetic SEC source data."""
    tags = {"TOTAL_ASSETS": "Assets", "TOTAL_EQUITY": "StockholdersEquity",
            "LONG_TERM_DEBT": "LongTermDebtNoncurrent", "SHORT_TERM_DEBT": "LongTermDebtCurrent"}
    if share_only_date:
        tags["COMMON_SHARES_OUTSTANDING"] = "CommonStockSharesOutstanding"
    lake.rules().mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(canonical_id=account, canonical_nm=account, fs_type="BS")
                  for account in tags]).to_csv(lake.canonical_accounts(), index=False)
    lake.rules("us_mapping.yaml").write_text(yaml.safe_dump({"companyfacts_rules": [
        dict(canonical_id=account, fs_type="BS", primary_tags=["us-gaap:" + tag])
        for account, tag in tags.items()]}), "utf-8")
    pd.DataFrame([dict(cik="999990", ticker="999990", title="Synthetic disclosed versions")]).to_csv(
        lake.meta("sec_company_tickers.csv"), index=False)
    facts = {tag: {"label": tag, "units": {"USD": []}} for tag in tags.values()}
    for accession, received, accounts in versions:
        for account, amount in accounts.items():
            facts[tags[account]]["units"]["USD"].append(dict(end="2019-12-31", val=amount,
                accn=accession, fy=2019, fp="FY", form="10-K" if accession.endswith("000001") else "10-K/A",
                filed=received))
    if share_only_date:
        facts["CommonStockSharesOutstanding"]["units"] = {"shares": [dict(
            end=share_only_date, val=300, accn="0000999990-20-000003", fy=2019, fp="FY",
            form="10-K/A", filed=share_only_date)]}
    source = lake.bronze("sec", "companyfacts", "CIK0000999990.json")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(dict(cik=999990, entityName="Synthetic disclosed versions",
                                    facts={"us-gaap": facts})), "utf-8")
    program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))
sys.argv=['normalize','--market','us','--symbols','999990','--start-year',sys.argv[2],
          '--end-year',sys.argv[3],'--no-notes','--no-debug','--workers','1']
from engine.workflows.normalize import main
main()
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root),
        str(start_year), str(end_year)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("basis", ["annual", "quarterly", "ttm"])
def test_original_balance_remains_available_until_amendment_after_full_source_reload(ordinary_us_refresh, basis):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_disclosed_balances(lake, [
        ("0000999990-20-000001", "2020-04-01", {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}),
        ("0000999990-20-000002", str(days[-2].date()), {"TOTAL_ASSETS": 400, "TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 150, "SHORT_TERM_DEBT": 0}),
    ])
    run("factors", basis)
    assert value(days[-2], "debt_to_equity", basis) == .5
    assert value(days[-1], "debt_to_equity", basis) == .75
    run("snapshots", basis)
    assert value(days[-2], "debt_to_equity", basis, snapshot=True) == .5
    assert value(days[-1], "debt_to_equity", basis, snapshot=True) == .75


def test_reloading_an_earlier_accession_changes_only_its_available_interval(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    versions = [
        ("0000999990-20-000001", "2020-04-01", {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}),
        ("0000999990-20-000002", str(days[-2].date()), {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 150, "SHORT_TERM_DEBT": 0}),
    ]
    normalize_disclosed_balances(lake, versions)
    run("factors", "annual")
    run("snapshots", "annual")
    latest_path = lake.silver("sec", "normalized", "us_normalized_999990.csv")
    latest_before = latest_path.read_bytes()
    versions[0][2]["LONG_TERM_DEBT"] = 80
    normalize_disclosed_balances(lake, versions)
    assert latest_path.read_bytes() == latest_before
    assert "financial history changed" in run("snapshots", "annual", succeeds=False)
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-2], "debt_to_equity", "annual", snapshot=snapshot) == .4
        assert value(days[-1], "debt_to_equity", "annual", snapshot=snapshot) == .75
    normalize_disclosed_balances(lake, versions)
    assert "skipping completed step: factors" in run("factors", "annual")
    assert "skipping completed step: snapshots" in run("snapshots", "annual")


def test_amendment_cannot_borrow_a_missing_debt_component_from_original(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_disclosed_balances(lake, [
        ("0000999990-20-000001", "2020-04-01", {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}),
        ("0000999990-20-000002", str(days[-2].date()), {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 150}),
    ])
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-2], "debt_to_equity", "annual", snapshot=snapshot) == .5
        assert value(days[-1], "debt_to_equity", "annual", snapshot=snapshot) is None


def test_normalization_preserves_reported_fact_provenance_without_debug_output(ordinary_us_refresh):
    lake, _, _, _, _ = ordinary_us_refresh
    normalize_disclosed_balances(lake, [
        ("0000999990-20-000001", "2020-04-01", {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}),
    ])
    root = lake.silver("sec", "normalized", "accessions", "999990")
    manifest = json.loads((root / "manifest.json").read_bytes())
    frame = pd.read_csv(root / manifest["normalized_path"], dtype=str).fillna("")
    debt = frame.loc[frame.canonical_account_id.eq("LONG_TERM_DEBT")].iloc[0]
    source = lake.bronze("sec", "companyfacts", "CIK0000999990.json")
    assert debt.source_path == str(source.resolve())
    assert debt.source_sha256 == sha256(source.read_bytes()).hexdigest()
    assert debt.period_semantic == "INSTANT"
    assert debt.unit == "USD"
    assert debt.period_start == ""


def test_share_only_disclosure_does_not_replace_or_block_a_known_financial_balance(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_disclosed_balances(lake, [
        ("0000999990-20-000001", "2020-04-01", {"TOTAL_EQUITY": 200,
             "LONG_TERM_DEBT": 100, "SHORT_TERM_DEBT": 0}),
    ], share_only_date=str(days[-2].date()))
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-2], "debt_to_equity", "annual", snapshot=snapshot) == .5
        assert value(days[-1], "debt_to_equity", "annual", snapshot=snapshot) == .5


@pytest.mark.parametrize("other_end", ["2018-12-31", "2020-01-01"])
def test_filing_fact_for_another_date_cannot_become_current_debt_or_use_lower_authority(ordinary_us_refresh, other_end):
    lake, days, _, run, value = ordinary_us_refresh
    accession = "0000999990-20-000001"
    folder = lake.bronze("sec", "fillings", "10-K", "999990", accession)
    folder.mkdir(parents=True)
    instance = folder / "synthetic.xml"
    instance.write_text(f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2019"
      xmlns:us-gaap="http://fasb.org/us-gaap/2019">
      <xbrli:context id="current"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">999990</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2019-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="other"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">999990</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>{other_end}</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <dei:EntityRegistrantName contextRef="current">Synthetic disclosed versions</dei:EntityRegistrantName>
      <dei:DocumentFiscalYearFocus contextRef="current">2019</dei:DocumentFiscalYearFocus>
      <dei:DocumentFiscalPeriodFocus contextRef="current">FY</dei:DocumentFiscalPeriodFocus>
      <dei:DocumentPeriodEndDate contextRef="current">2019-12-31</dei:DocumentPeriodEndDate>
      <us-gaap:StockholdersEquity contextRef="current" unitRef="USD" decimals="0">200</us-gaap:StockholdersEquity>
      <us-gaap:LongTermDebtCurrent contextRef="current" unitRef="USD" decimals="0">0</us-gaap:LongTermDebtCurrent>
      <us-gaap:LongTermDebtNoncurrent contextRef="other" unitRef="USD" decimals="0">100</us-gaap:LongTermDebtNoncurrent>
    </xbrli:xbrl>''', "utf-8")
    (folder / "filing.json").write_text(json.dumps(dict(schema_version=2,
        source="sec-edgartools-xbrl-bundle", source_authority="SEC_10K_AUDITED", ticker="999990",
        cik="CIK0000999990", company_name="Synthetic disclosed versions", form="10-K",
        filing_date="2020-04-01", period_of_report="2019-12-31", accession_number=accession,
        primary_document="", xbrl_documents=[dict(role="instance", document_name=instance.name)])), "utf-8")
    normalize_disclosed_balances(lake, [(accession, "2020-04-01", {
        "TOTAL_EQUITY": 200, "LONG_TERM_DEBT": 150, "SHORT_TERM_DEBT": 0})])
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-1], "debt_to_equity", "annual", snapshot=snapshot) is None
        assert value(days[-1], "ma_50", "annual", snapshot=snapshot) == 100
