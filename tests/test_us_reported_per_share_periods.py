"""Reported per-share facts keep their own duration through public reloads."""
import json
import re
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


def normalize_per_share_source(lake, disclosures):
    accounts = {
        "BASIC_EPS": ("EarningsPerShareBasic", "USD/shares"),
        "DILUTED_EPS": ("EarningsPerShareDiluted", "USD/shares"),
        "BASIC_SHARES": ("WeightedAverageNumberOfSharesOutstandingBasic", "shares"),
        "DILUTED_SHARES": ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
        "NET_INCOME": ("NetIncomeLoss", "USD"), "RND": ("ResearchAndDevelopmentExpense", "USD"),
        "SGNA": ("SellingGeneralAndAdministrativeExpense", "USD"),
        "OPERATING_INCOME": ("OperatingIncomeLoss", "USD"),
        "COMMON_SHARES_OUTSTANDING": ("CommonStockSharesOutstanding", "shares"),
    }
    lake.rules().mkdir(parents=True, exist_ok=True)
    pd.DataFrame([dict(canonical_id=account, canonical_nm=account,
        fs_type="BS" if account == "COMMON_SHARES_OUTSTANDING" else "IS") for account in accounts]).to_csv(
        lake.canonical_accounts(), index=False)
    lake.rules("us_mapping.yaml").write_text(yaml.safe_dump({"companyfacts_rules": [
        dict(canonical_id=account, fs_type="BS" if account == "COMMON_SHARES_OUTSTANDING" else "IS",
             primary_tags=["us-gaap:"+tag])
        for account, (tag, _) in accounts.items()]}), "utf-8")
    pd.DataFrame([dict(cik="999990", ticker="999990", title="Synthetic duration disclosure")]).to_csv(
        lake.meta("sec_company_tickers.csv"), index=False)
    facts = {tag: {"label": tag, "units": {unit: []}} for tag, unit in accounts.values()}
    for quarter, filed, observations in disclosures:
        for start, end, amounts in observations:
            for account, amount in amounts.items():
                tag, unit = accounts[account]
                observation = dict(end=end, val=amount,
                    accn=f"0000999990-{end[2:4]}-00000{quarter}", fy=int(end[:4]), fp="FY" if quarter==4 else f"Q{quarter}",
                    form="10-K" if quarter==4 else "10-Q", filed=filed)
                if account != "COMMON_SHARES_OUTSTANDING":
                    observation["start"] = start
                facts[tag]["units"][unit].append(observation)
    source = lake.bronze("sec", "companyfacts", "CIK0000999990.json")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(dict(cik=999990, entityName="Synthetic duration disclosure",
                                    facts={"us-gaap": facts})), "utf-8")
    program = """
import sys
from pathlib import Path
from engine.core import paths
paths.DATA_LAKE=paths.DataLakePaths(Path(sys.argv[1]))
sys.argv=['normalize','--market','us','--symbols','999990','--start-year','2019','--end-year','2020','--no-notes','--workers','1']
from engine.workflows.normalize import main
main()
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(lake.root)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def write_quarter_filing(lake, filed):
    accession = "0000999990-19-000002"
    folder = lake.bronze("sec", "fillings", "10-Q", "999990", accession)
    folder.mkdir(parents=True)
    contexts = []
    for name, start in [("ytd", "2019-01-01"), ("qtd", "2019-04-01")]:
        contexts.append(f'''<xbrli:context id="{name}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">999990</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>2019-06-30</xbrli:endDate></xbrli:period></xbrli:context>''')
    facts = []
    for tag, unit, ytd, qtd in [
        ("EarningsPerShareBasic", "USDshares", .82, .5), ("EarningsPerShareDiluted", "USDshares", .82, .5),
        ("WeightedAverageNumberOfSharesOutstandingBasic", "shares", 110, 120),
        ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 110, 120),
        ("NetIncomeLoss", "USD", 90, 60), ("OperatingIncomeLoss", "USD", 90, 60),
        ("ResearchAndDevelopmentExpense", "USD", 0, 0), ("SellingGeneralAndAdministrativeExpense", "USD", 0, 0),
    ]:
        for context, amount in [("ytd", ytd), ("qtd", qtd)]:
            facts.append(f'<us-gaap:{tag} contextRef="{context}" unitRef="{unit}" decimals="2">{amount}</us-gaap:{tag}>')
    instance = folder / "synthetic.xml"
    instance.write_text('''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
        xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2019"
        xmlns:us-gaap="http://fasb.org/us-gaap/2019">''' + ''.join(contexts) + '''
        <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
        <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
        <xbrli:unit id="USDshares"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
        <dei:EntityRegistrantName contextRef="ytd">Synthetic duration disclosure</dei:EntityRegistrantName>
        <dei:DocumentFiscalYearFocus contextRef="ytd">2019</dei:DocumentFiscalYearFocus>
        <dei:DocumentFiscalPeriodFocus contextRef="ytd">Q2</dei:DocumentFiscalPeriodFocus>
        <dei:DocumentPeriodEndDate contextRef="ytd">2019-06-30</dei:DocumentPeriodEndDate>'''
        + ''.join(facts) + '</xbrli:xbrl>', "utf-8")
    (folder / "filing.json").write_text(json.dumps(dict(schema_version=2,
        source="sec-edgartools-xbrl-bundle", source_authority="SEC_10Q_REVIEWED", ticker="999990",
        cik="CIK0000999990", company_name="Synthetic duration disclosure", form="10-Q",
        filing_date=filed, period_of_report="2019-06-30", accession_number=accession,
        primary_document="", xbrl_documents=[dict(role="instance", document_name=instance.name)])), "utf-8")


@pytest.mark.parametrize("provider", ["companyfacts", "filing"])
def test_quarter_eps_and_weighted_shares_use_the_disclosed_quarter_not_ytd_differences(ordinary_us_refresh, provider):
    lake, days, _, run, value = ordinary_us_refresh
    if provider == "filing":
        write_quarter_filing(lake, str(days[-2].date()))
    normalize_per_share_source(lake, [
        (1, "2020-04-01", [("2019-01-01", "2019-03-31", {
            "BASIC_EPS": .3, "DILUTED_EPS": .3, "BASIC_SHARES": 100, "DILUTED_SHARES": 100,
            "NET_INCOME": 30, "OPERATING_INCOME": 30, "RND": 0, "SGNA": 0})]),
        (2, str(days[-2].date()), [
            ("2019-01-01", "2019-06-30", {"BASIC_EPS": .82, "DILUTED_EPS": .82,
                "BASIC_SHARES": 110, "DILUTED_SHARES": 110, "NET_INCOME": 90, "OPERATING_INCOME": 90, "RND": 0, "SGNA": 0}),
            ("2019-04-01", "2019-06-30", {"BASIC_EPS": .5, "DILUTED_EPS": .5,
                "BASIC_SHARES": 120, "DILUTED_SHARES": 120, "NET_INCOME": 60, "OPERATING_INCOME": 60, "RND": 0, "SGNA": 0}),
        ]),
    ])
    run("factors", "quarterly")
    assert value(days[-2], "eps", "quarterly") == pytest.approx(.3)
    assert value(days[-1], "eps", "quarterly") == pytest.approx(.5)
    assert value(days[-1], "intangible_adjusted_eps", "quarterly") == pytest.approx(.5)
    run("snapshots", "quarterly")
    assert value(days[-1], "eps", "quarterly", snapshot=True) == pytest.approx(.5)
    assert value(days[-1], "intangible_adjusted_eps", "quarterly", snapshot=True) == pytest.approx(.5)


def annual_disclosures(final_filed):
    records = []
    for quarter, end, filed, cumulative_income, shares, eps in [
        (1, "2019-03-31", "2019-05-01", 30, 100, .3),
        (2, "2019-06-30", "2019-08-01", 90, 110, .82),
        (3, "2019-09-30", "2019-11-01", 160, 120, 1.33),
        (4, "2019-12-31", final_filed, 240, 130, 1.85),
    ]:
        amounts = dict(BASIC_EPS=eps, DILUTED_EPS=eps, BASIC_SHARES=shares, DILUTED_SHARES=shares,
                       NET_INCOME=cumulative_income, OPERATING_INCOME=cumulative_income, RND=0, SGNA=0)
        observations = [("2019-01-01", end, amounts)]
        if quarter != 1:
            quarter_shares = {2: 120, 3: 140, 4: 160}[quarter]
            observations.append((f"2019-{(quarter-1)*3+1:02d}-01", end,
                dict(BASIC_EPS=.5, DILUTED_EPS=.5, BASIC_SHARES=quarter_shares,
                     DILUTED_SHARES=quarter_shares)))
        records.append((quarter, filed, observations))
    return records


@pytest.mark.parametrize("basis,eps,intangible_eps", [
    ("annual", 1.85, 240/130), ("quarterly", .5, 80/160), ("ttm", 1.85, 240/130),
])
def test_full_year_and_fourth_quarter_keep_their_own_reported_denominators(
        ordinary_us_refresh, basis, eps, intangible_eps):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_per_share_source(lake, annual_disclosures(str(days[-2].date())))
    run("factors", basis)
    run("snapshots", basis)
    for snapshot in [False, True]:
        assert value(days[-1], "eps", basis, snapshot=snapshot) == pytest.approx(eps)
        assert value(days[-1], "intangible_adjusted_eps", basis, snapshot=snapshot) == pytest.approx(intangible_eps)


def test_non_year_end_ttm_does_not_invent_reported_eps_or_weighted_shares(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    disclosures = annual_disclosures("2020-03-01")
    disclosures.append((1, str(days[-2].date()), [("2020-01-01", "2020-03-31", dict(
        BASIC_EPS=.5, DILUTED_EPS=.5, BASIC_SHARES=200, DILUTED_SHARES=200,
        COMMON_SHARES_OUTSTANDING=1000,
        NET_INCOME=100, OPERATING_INCOME=100, RND=0, SGNA=0))]))
    normalize_per_share_source(lake, disclosures)
    run("factors", "ttm")
    run("snapshots", "ttm")
    for snapshot in [False, True]:
        assert value(days[-2], "eps", "ttm", snapshot=snapshot) == pytest.approx(1.85)
        assert value(days[-1], "eps", "ttm", snapshot=snapshot) is None
        assert value(days[-1], "intangible_adjusted_eps", "ttm", snapshot=snapshot) is None


@pytest.mark.parametrize("source_gap", ["missing_quarter", "different_unit"])
def test_authoritative_filing_without_a_usable_quarter_does_not_borrow_companyfacts(
        ordinary_us_refresh, source_gap):
    lake, days, _, run, value = ordinary_us_refresh
    write_quarter_filing(lake, str(days[-2].date()))
    instance = lake.bronze("sec", "fillings", "10-Q", "999990", "0000999990-19-000002", "synthetic.xml")
    raw = instance.read_text("utf-8")
    pattern = r'<us-gaap:(?:EarningsPerShare\w+|WeightedAverageNumberOf\w+) contextRef="qtd"[^>]*>[^<]*</us-gaap:[^>]+>'
    raw = re.sub(pattern, "" if source_gap == "missing_quarter" else
                 lambda match: re.sub(r'unitRef="[^"]+"', 'unitRef="USD"', match[0]), raw)
    instance.write_text(raw, "utf-8")
    # A valid lower-authority QTD value exists; the filing boundary still wins.
    normalize_per_share_source(lake, annual_disclosures("2020-03-01")[:2])
    run("factors", "quarterly")
    run("snapshots", "quarterly")
    for snapshot in [False, True]:
        assert value(days[-1], "eps", "quarterly", snapshot=snapshot) is None
        assert value(days[-1], "intangible_adjusted_eps", "quarterly", snapshot=snapshot) is None
        assert value(days[-1], "ma_50", "quarterly", snapshot=snapshot) == 100


def test_a_changed_quarter_only_value_reopens_completed_factor_and_snapshot_reloads(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    disclosures = annual_disclosures("2020-03-01")[:2]
    normalize_per_share_source(lake, disclosures)
    latest_path = lake.silver("sec", "normalized", "us_normalized_999990.csv")
    latest_before = latest_path.read_bytes()
    run("factors", "quarterly")
    run("snapshots", "quarterly")
    assert value(days[-1], "eps", "quarterly", snapshot=True) == .5

    disclosures[1][2][1][2].update(BASIC_EPS=.6, DILUTED_EPS=.6, BASIC_SHARES=100, DILUTED_SHARES=100)
    normalize_per_share_source(lake, disclosures)
    assert latest_path.read_bytes() == latest_before
    assert "financial history changed" in run("snapshots", "quarterly", succeeds=False)
    run("factors", "quarterly")
    run("snapshots", "quarterly")
    for snapshot in [False, True]:
        assert value(days[-1], "eps", "quarterly", snapshot=snapshot) == .6
        assert value(days[-1], "intangible_adjusted_eps", "quarterly", snapshot=snapshot) == .6
    assert "skipping completed step: factors" in run("factors", "quarterly")
    assert "skipping completed step: snapshots" in run("snapshots", "quarterly")
