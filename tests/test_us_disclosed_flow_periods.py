"""Disclosed flow durations survive normalization and public factor reloads."""
import json
import pytest

from test_us_reported_per_share_periods import normalize_per_share_source
from test_us_period_vintage_refresh import ordinary_us_refresh
from test_historical_refresh_resume import us_refresh_environment
from test_share_input_refresh import share_refresh_environment

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("basis,expected", [("annual", None), ("quarterly", 40), ("ttm", 100)])
def test_a_fourth_quarter_fact_in_a_10k_is_not_a_full_year_flow(ordinary_us_refresh, basis, expected):
    lake, days, _, run, value = ordinary_us_refresh
    disclosures = [
        (1, "2019-05-01", [("2019-01-01", "2019-03-31", {"NET_INCOME": 10})]),
        (2, "2019-08-01", [("2019-01-01", "2019-06-30", {"NET_INCOME": 30})]),
        (3, "2019-11-01", [("2019-01-01", "2019-09-30", {"NET_INCOME": 60})]),
        (4, str(days[-2].date()), [("2019-10-01", "2019-12-31", {"NET_INCOME": 40})]),
    ]
    normalize_per_share_source(lake, disclosures)
    run("factors", basis)
    run("snapshots", basis)
    for snapshot in [False, True]:
        observed = value(days[-1], "ni", basis, snapshot=snapshot)
        if expected is None:
            assert observed is None
        else:
            assert observed == expected


@pytest.mark.parametrize("first_quarter_present", [True, False])
def test_a_prior_standalone_quarter_is_not_subtracted_as_ytd(ordinary_us_refresh, first_quarter_present):
    lake, days, _, run, value = ordinary_us_refresh
    disclosures = []
    if first_quarter_present:
        disclosures.append((1, "2020-04-01", [("2019-01-01", "2019-03-31", {"NET_INCOME": 10})]))
    disclosures += [
        (2, str(days[-3].date()), [("2019-04-01", "2019-06-30", {"NET_INCOME": 40})]),
        (3, str(days[-2].date()), [("2019-01-01", "2019-09-30", {"NET_INCOME": 100})]),
    ]
    normalize_per_share_source(lake, disclosures)
    run("factors", "quarterly")
    run("snapshots", "quarterly")
    for snapshot in [False, True]:
        assert value(days[-2], "ni", "quarterly", snapshot=snapshot) == 40
        latest = value(days[-1], "ni", "quarterly", snapshot=snapshot)
        if first_quarter_present:
            assert latest == 50
        else:
            assert latest is None


def test_a_short_duration_in_a_filing_bundle_does_not_become_annual_by_its_form(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    accession = "0000999990-19-000004"
    folder = lake.bronze("sec", "fillings", "10-K", "999990", accession)
    folder.mkdir(parents=True)
    instance = folder / "synthetic.xml"
    instance.write_text('''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:dei="http://xbrl.sec.gov/dei/2019"
      xmlns:us-gaap="http://fasb.org/us-gaap/2019">
      <xbrli:context id="q4"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">999990</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2019-10-01</xbrli:startDate><xbrli:endDate>2019-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <dei:EntityRegistrantName contextRef="q4">Synthetic duration disclosure</dei:EntityRegistrantName>
      <dei:DocumentFiscalYearFocus contextRef="q4">2019</dei:DocumentFiscalYearFocus>
      <dei:DocumentFiscalPeriodFocus contextRef="q4">FY</dei:DocumentFiscalPeriodFocus>
      <dei:DocumentPeriodEndDate contextRef="q4">2019-12-31</dei:DocumentPeriodEndDate>
      <us-gaap:NetIncomeLoss contextRef="q4" unitRef="USD" decimals="0">40</us-gaap:NetIncomeLoss>
      </xbrli:xbrl>''', "utf-8")
    (folder / "filing.json").write_text(json.dumps(dict(schema_version=2,
        source="sec-edgartools-xbrl-bundle", source_authority="SEC_10K_AUDITED", ticker="999990",
        cik="CIK0000999990", company_name="Synthetic duration disclosure", form="10-K",
        filing_date=str(days[-2].date()), period_of_report="2019-12-31", accession_number=accession,
        primary_document="", xbrl_documents=[dict(role="instance", document_name=instance.name)])), "utf-8")
    normalize_per_share_source(lake, [(4, str(days[-2].date()), [
        ("2019-01-01", "2019-12-31", {"NET_INCOME": 100})])])
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-1], "ni", "annual", snapshot=snapshot) is None


@pytest.mark.parametrize("basis,expected", [("annual", 100), ("quarterly", 40), ("ttm", 100)])
def test_four_disjoint_quarters_in_one_10k_recover_the_full_year(ordinary_us_refresh, basis, expected):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_per_share_source(lake, [(4, str(days[-2].date()), [
        ("2019-01-01", "2019-03-31", {"NET_INCOME": 10}),
        ("2019-04-01", "2019-06-30", {"NET_INCOME": 20}),
        ("2019-07-01", "2019-09-30", {"NET_INCOME": 30}),
        ("2019-10-01", "2019-12-31", {"NET_INCOME": 40}),
    ])])
    run("factors", basis)
    run("snapshots", basis)
    for snapshot in [False, True]:
        assert value(days[-1], "ni", basis, snapshot=snapshot) == expected


@pytest.mark.parametrize("second_start", ["2019-04-02", "2019-03-31"])
def test_gaps_or_overlaps_cannot_become_a_full_year_sum(ordinary_us_refresh, second_start):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_per_share_source(lake, [(4, str(days[-2].date()), [
        ("2019-01-01", "2019-03-31", {"NET_INCOME": 10}),
        (second_start, "2019-06-30", {"NET_INCOME": 20}),
        ("2019-07-01", "2019-09-30", {"NET_INCOME": 30}),
        ("2019-10-01", "2019-12-31", {"NET_INCOME": 40}),
    ])])
    run("factors", "annual")
    run("snapshots", "annual")
    for snapshot in [False, True]:
        assert value(days[-1], "ni", "annual", snapshot=snapshot) is None


def test_the_current_disclosed_quarter_wins_over_differencing_an_older_ytd(ordinary_us_refresh):
    lake, days, _, run, value = ordinary_us_refresh
    normalize_per_share_source(lake, [
        (1, "2020-04-01", [("2019-01-01", "2019-03-31", {"NET_INCOME": 30})]),
        (2, str(days[-2].date()), [
            ("2019-01-01", "2019-06-30", {"NET_INCOME": 90}),
            ("2019-04-01", "2019-06-30", {"NET_INCOME": 70}),
        ]),
    ])
    run("factors", "quarterly")
    run("snapshots", "quarterly")
    for snapshot in [False, True]:
        assert value(days[-1], "ni", "quarterly", snapshot=snapshot) == 70
