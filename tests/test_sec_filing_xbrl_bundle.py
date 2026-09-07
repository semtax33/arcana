from __future__ import annotations

import codecs
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from engine.transformers._internal import sec_filings


def test_sec_ticker_map_merges_verified_historical_aliases(tmp_path: Path):
    current = tmp_path / "current.csv"
    aliases = tmp_path / "aliases.csv"
    current.write_text("cik,ticker,title\n320193,AAPL,Apple Inc.\n", encoding="utf-8")
    aliases.write_text(
        "cik,ticker,title\n35527,FITBI,Fifth Third Bancorp\n",
        encoding="utf-8",
    )

    frame = sec_filings.load_sec_ticker_map(current, aliases_path=aliases)

    assert frame[["cik", "ticker"]].to_dict("records") == [
        {"cik": "320193", "ticker": "AAPL"},
        {"cik": "35527", "ticker": "FITBI"},
    ]


def test_manifest_xbrl_loader_ignores_generic_xml_and_handles_utf8_bom():
    with TemporaryDirectory() as tmpdir:
        bundle = Path(tmpdir)
        (bundle / "R1.xml").write_text(
            '<?xml version="1.0"?><Report><Row>not an instance</Row></Report>',
            encoding="utf-8",
        )
        instance = b'''<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
  xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
  xmlns:dei="http://xbrl.sec.gov/dei/2025"
  xmlns:us-gaap="http://fasb.org/us-gaap/2025">
  <xbrli:context id="instant">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">320193</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2016-09-24</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <dei:EntityRegistrantName contextRef="instant">Apple Inc.</dei:EntityRegistrantName>
  <dei:DocumentFiscalYearFocus contextRef="instant">2016</dei:DocumentFiscalYearFocus>
  <dei:DocumentFiscalPeriodFocus contextRef="instant">FY</dei:DocumentFiscalPeriodFocus>
  <dei:DocumentPeriodEndDate contextRef="instant">2016-09-24</dei:DocumentPeriodEndDate>
  <us-gaap:Assets contextRef="instant" unitRef="USD" decimals="-6">321686000000</us-gaap:Assets>
</xbrli:xbrl>
'''
        (bundle / "aapl-2016.xml").write_bytes(codecs.BOM_UTF8 + instance)
        manifest = {
            "ticker": "AAPL",
            "cik": "CIK0000320193",
            "form": "10-K",
            "period_of_report": "2016-09-24",
            "accession_number": "0000320193-16-000001",
            "xbrl_documents": [
                {
                    "role": "instance",
                    "document_type": "XML",
                    "document_name": "R1.xml",
                },
                {
                    "role": "instance",
                    "document_type": "EX-101.INS",
                    "document_name": "aapl-2016.xml",
                },
            ],
        }
        (bundle / "filing.json").write_text(json.dumps(manifest), encoding="utf-8")
        descriptor = sec_filings.SecFilingBundleDescriptor(bundle, manifest)

        xbrl = sec_filings.load_manifest_xbrl(descriptor)

        assert int(xbrl.entity_info["fiscal_year"]) == 2016
        assert not xbrl.facts.to_dataframe().empty


def test_legacy_dei_derives_fiscal_year_and_quarter_from_year_end_month():
    entity_info = {
        "fiscal_year": None,
        "fiscal_period": None,
        "fiscal_year_end_month": 9,
    }
    first_quarter = {"form": "10-Q", "period_of_report": "2008-12-27"}
    third_quarter = {"form": "10-Q", "period_of_report": "2009-06-27"}

    assert sec_filings._filing_fiscal_year(entity_info, first_quarter) == 2009
    assert sec_filings._filing_fiscal_period(entity_info, "10-Q", first_quarter) == "Q1"
    assert sec_filings._filing_fiscal_year(entity_info, third_quarter) == 2009
    assert sec_filings._filing_fiscal_period(entity_info, "10-Q", third_quarter) == "Q3"


def test_filing_fact_end_falls_back_from_nan_duration_end_to_instant() -> None:
    assert sec_filings._filing_fact_end(
        {"period_end": float("nan"), "period_instant": "2011-09-30"}
    ) == "2011-09-30"


def test_filing_xbrl_extractor_exposes_parallel_worker_contract():
    result = sec_filings.extract_filing_xbrl_candidates(
        [],
        rules=[],
        canonical_names={},
        start_year=2006,
        end_year=2016,
        workers=2,
        log_progress=False,
    )

    assert result.candidates == []
    assert result.authoritative_periods == set()


def test_filing_xbrl_extractor_caps_repetitive_parse_warnings(
    tmp_path: Path, monkeypatch, capsys
):
    descriptors = [
        sec_filings.SecFilingBundleDescriptor(
            tmp_path / f"bundle-{index}",
            {
                "ticker": f"T{index}",
                "cik": str(1000 + index),
                "accession_number": f"accession-{index}",
            },
        )
        for index in range(sec_filings.MAX_FILING_XBRL_WARNING_EXAMPLES + 3)
    ]

    def fail_descriptor(descriptor, **_kwargs):
        return sec_filings.FilingXbrlDescriptorExtractResult(
            str(descriptor.path),
            [],
            error="ValueError: filing bundle has no valid XBRL instance document",
        )

    monkeypatch.setattr(sec_filings, "_extract_filing_xbrl_descriptor", fail_descriptor)

    sec_filings.extract_filing_xbrl_candidates(
        descriptors,
        rules=[],
        canonical_names={},
        start_year=2006,
        end_year=2016,
        workers=1,
        log_progress=True,
    )

    output = capsys.readouterr().out
    assert output.count("parse failed and lower-authority fallback is blocked") == (
        sec_filings.MAX_FILING_XBRL_WARNING_EXAMPLES
    )
    assert "warnings_suppressed=3" in output


def test_filing_xbrl_extractor_parallel_results_are_complete_and_ordered():
    with TemporaryDirectory() as tmpdir:
        descriptors = []
        for symbol, cik, value in (
            ("AAPL", "320193", 100),
            ("MSFT", "789019", 200),
        ):
            bundle = Path(tmpdir) / symbol
            bundle.mkdir()
            instance_name = f"{symbol.lower()}-2016.xml"
            (bundle / instance_name).write_text(
                f'''<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
  xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
  xmlns:dei="http://xbrl.sec.gov/dei/2016"
  xmlns:us-gaap="http://fasb.org/us-gaap/2016">
  <xbrli:context id="instant">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2016-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <dei:EntityRegistrantName contextRef="instant">{symbol}</dei:EntityRegistrantName>
  <dei:DocumentFiscalYearFocus contextRef="instant">2016</dei:DocumentFiscalYearFocus>
  <dei:DocumentFiscalPeriodFocus contextRef="instant">FY</dei:DocumentFiscalPeriodFocus>
  <dei:DocumentPeriodEndDate contextRef="instant">2016-12-31</dei:DocumentPeriodEndDate>
  <us-gaap:Assets contextRef="instant" unitRef="USD">{value}</us-gaap:Assets>
</xbrli:xbrl>
''',
                encoding="utf-8",
            )
            manifest = {
                "ticker": symbol,
                "cik": f"CIK{int(cik):010d}",
                "company_name": symbol,
                "form": "10-K",
                "filing_date": "2017-02-01",
                "period_of_report": "2016-12-31",
                "accession_number": f"{int(cik):010d}-17-000001",
                "source_authority": "SEC_10K_AUDITED",
                "xbrl_documents": [
                    {
                        "role": "instance",
                        "document_type": "EX-101.INS",
                        "document_name": instance_name,
                    }
                ],
            }
            descriptors.append(sec_filings.SecFilingBundleDescriptor(bundle, manifest))

        result = sec_filings.extract_filing_xbrl_candidates(
            descriptors,
            rules=[
                {
                    "canonical_id": "TOTAL_ASSETS",
                    "fs_type": "BS",
                    "primary_tags": ["us-gaap:Assets"],
                }
            ],
            canonical_names={"TOTAL_ASSETS": "Total assets"},
            start_year=2016,
            end_year=2016,
            workers=2,
            log_progress=False,
        )

        assert [(row.symbol, row.value) for row in result.candidates] == [
            ("AAPL", 100.0),
            ("MSFT", 200.0),
        ]

        expanded = sec_filings.fan_out_sec_candidates(
            result.candidates,
            symbols_by_cik={
                "320193": ("AAPL", "AAPL-A"),
                "789019": ("MSFT",),
            },
        )
        assert [(row.symbol, row.value) for row in expanded] == [
            ("AAPL", 100.0),
            ("AAPL-A", 100.0),
            ("MSFT", 200.0),
        ]

        authority = sec_filings.fan_out_sec_authority_keys(
            {("AAPL", 2016, 12)},
            symbol_to_cik={"AAPL": "320193"},
            symbols_by_cik={"320193": ("AAPL", "AAPL-A")},
        )
        assert authority == {("AAPL", 2016, 12), ("AAPL-A", 2016, 12)}


def test_companyfacts_rejects_period_end_more_than_one_year_from_fiscal_year():
    common = {"form": "10-K", "fy": 2016}

    assert sec_filings._companyfacts_period_matches_fiscal_year(
        {**common, "end": "2017-01-28"}
    )
    assert not sec_filings._companyfacts_period_matches_fiscal_year(
        {**common, "end": "2018-02-03"}
    )


def test_companyfacts_does_not_promote_comparative_when_current_period_is_invalid():
    accession = "0001177609-18-000008"
    data = {
        "cik": 1177609,
        "entityName": "Five Below, Inc.",
        "facts": {
            "us-gaap": {
                "Assets": {
                    "label": "Assets",
                    "units": {
                        "USD": [
                            {
                                "end": "2017-01-28",
                                "val": 100,
                                "accn": accession,
                                "fy": 2016,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2018-03-22",
                            },
                            {
                                "end": "2018-02-03",
                                "val": 200,
                                "accn": accession,
                                "fy": 2016,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2018-03-22",
                            },
                        ]
                    },
                }
            }
        },
    }

    rows = sec_filings.extract_companyfacts_candidates_from_data(
        data,
        companyfacts_path="CIK0001177609.json",
        symbol="FIVE",
        rules=[
            {
                "canonical_id": "TOTAL_ASSETS",
                "fs_type": "BS",
                "primary_tags": ["us-gaap:Assets"],
            }
        ],
        canonical_names={"TOTAL_ASSETS": "Total assets"},
        start_year=2016,
        end_year=2016,
    )

    assert rows == []
