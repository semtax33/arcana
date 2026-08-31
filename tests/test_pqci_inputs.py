from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine.extractors.pqci_inputs import (
    MissingApiKeyError,
    collect_source,
    collect_sources,
    default_pqci_config,
    load_pqci_config,
)


NOW = datetime(2026, 8, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        content: bytes | None = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.content = content if content is not None else json.dumps(payload).encode("utf-8")
        self.text = text if text is not None else self.content.decode("utf-8", errors="replace")

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _stored_envelope(result):
    return json.loads(result.snapshot_path.read_text(encoding="utf-8"))


def _single_dataset(source_config, name=None):
    datasets = source_config["datasets"]
    selected = next(
        dataset for dataset in datasets if name is None or dataset["name"] == name
    )
    return {**source_config, "datasets": [selected]}


def test_required_keys_are_preflighted_before_any_request(tmp_path, monkeypatch):
    for name in ("CENSUS_API_KEY", "BEA_API_KEY", "EIA_API_KEY", "NASS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    session = FakeSession()

    with pytest.raises(MissingApiKeyError) as exc_info:
        collect_sources(
            ["census", "bea", "eia", "nass"],
            data_lake_root=tmp_path,
            session=session,
            now=NOW,
        )

    message = str(exc_info.value)
    assert "CENSUS_API_KEY" in message
    assert "BEA_API_KEY" in message
    assert "EIA_API_KEY" in message
    assert "NASS_API_KEY" in message
    assert session.calls == []


def test_skip_missing_keys_still_collects_keyless_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    response = FakeResponse(
        {"data": [{"NAME": "Bank"}], "meta": {"total": 1}}
    )
    session = FakeSession(response)
    config = default_pqci_config(NOW)
    config["fdic"]["page_size"] = 10
    config["fdic"] = _single_dataset(config["fdic"], "active_institutions")

    results = collect_sources(
        ["census", "fdic"],
        config=config,
        data_lake_root=tmp_path,
        session=session,
        skip_missing_keys=True,
        now=NOW,
    )

    assert [result.source for result in results] == ["fdic"]
    catalog = json.loads(
        (tmp_path / "bronze" / "pqci" / "catalog.json").read_text(encoding="utf-8")
    )
    assert set(catalog["pqci_dimensions"]) == {"P", "Q", "C", "I"}


def test_bls_reads_optional_key_from_environment_and_redacts_storage(tmp_path, monkeypatch):
    secret = "bls-runtime-secret"
    monkeypatch.setenv("BLS_API_KEY", secret)
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": [{"value": "1"}]}]},
    }
    session = FakeSession(FakeResponse(payload))
    config = _single_dataset(default_pqci_config(NOW)["bls"])
    config["datasets"][0]["series_ids"] = ["CUUR0000SA0"]

    result = collect_source(
        "bls", config=config, data_lake_root=tmp_path, session=session, now=NOW
    )[0]

    assert session.calls[0]["json"]["registrationkey"] == secret
    assert result.record_count == 1
    assert result.snapshot_path.parent == tmp_path / "bronze" / "pqci" / "bls"
    assert result.latest_path.exists()
    assert secret not in result.snapshot_path.read_text(encoding="utf-8")
    envelope = _stored_envelope(result)
    assert envelope["pqci"]["dimensions"] == ["P", "Q", "C"]
    assert envelope["pqci"]["series_metadata"]["CUUR0000SA0"]["label"] == "CPI all items"


def test_census_uses_official_data_api_and_environment_key(tmp_path, monkeypatch):
    secret = "census-runtime-secret"
    monkeypatch.setenv("CENSUS_API_KEY", secret)
    session = FakeSession(FakeResponse([["NAME", "state"], ["California", "06"]]))
    config = {
        "datasets": [
            {
                "name": "acs_state",
                "year": 2024,
                "dataset": "acs/acs5",
                "get": ["NAME"],
                "for": "state:06",
                "pqci": {"dimensions": ["Q"]},
            }
        ]
    }

    result = collect_source(
        "census", config=config, data_lake_root=tmp_path, session=session, now=NOW
    )[0]

    call = session.calls[0]
    assert call["url"] == "https://api.census.gov/data/2024/acs/acs5"
    assert call["params"]["key"] == secret
    assert result.record_count == 1
    envelope = _stored_envelope(result)
    assert "key" not in envelope["request"]["parameters"]
    assert secret not in json.dumps(envelope)


def test_bea_uses_environment_key_and_get_data_parameters(tmp_path, monkeypatch):
    secret = "bea-runtime-secret"
    monkeypatch.setenv("BEA_API_KEY", secret)
    payload = {
        "BEAAPI": {
            "Request": {
                "RequestParam": [
                    {"ParameterName": "USERID", "ParameterValue": secret}
                ]
            },
            "Results": {"Data": [{"GeoName": "Alabama"}]},
        }
    }
    session = FakeSession(FakeResponse(payload))

    result = collect_source(
        "bea",
        config=_single_dataset(default_pqci_config(NOW)["bea"], "regional_gdp"),
        data_lake_root=tmp_path,
        session=session,
        now=NOW,
    )[0]

    params = session.calls[0]["params"]
    assert params["UserID"] == secret
    assert params["method"] == "GetData"
    assert params["DataSetName"] == "Regional"
    assert result.record_count == 1
    assert secret not in result.snapshot_path.read_text(encoding="utf-8")


def test_bea_counts_list_shaped_results(tmp_path, monkeypatch):
    monkeypatch.setenv("BEA_API_KEY", "bea-runtime-secret")
    payload = {
        "BEAAPI": {
            "Results": [
                {"Data": [{"Industry": "11"}, {"Industry": "21"}]},
                {"Data": [{"Industry": "22"}]},
            ]
        }
    }
    session = FakeSession(FakeResponse(payload))

    result = collect_source(
        "bea",
        config=_single_dataset(default_pqci_config(NOW)["bea"], "gdp_by_industry"),
        data_lake_root=tmp_path,
        session=session,
        now=NOW,
    )[0]

    assert result.record_count == 3


def test_eia_paginates_and_never_persists_key(tmp_path, monkeypatch):
    secret = "eia-runtime-secret"
    monkeypatch.setenv("EIA_API_KEY", secret)
    page_1 = {
        "response": {"total": "2", "data": [{"period": "2026-01"}]},
        "request": {"params": {"api_key": secret}},
    }
    page_2 = {"response": {"total": "2", "data": [{"period": "2026-02"}]}}
    session = FakeSession(FakeResponse(page_1), FakeResponse(page_2))
    config = _single_dataset(default_pqci_config(NOW)["eia"], "wti_spot_price")
    config["page_size"] = 1

    result = collect_source(
        "eia", config=config, data_lake_root=tmp_path, session=session, now=NOW
    )[0]

    assert result.record_count == 2
    assert len(session.calls) == 2
    first_params = session.calls[0]["params"]
    assert ("api_key", secret) in first_params
    assert ("offset", 0) in first_params
    assert ("offset", 1) in session.calls[1]["params"]
    assert secret not in result.snapshot_path.read_text(encoding="utf-8")


def test_fdic_paginates_without_an_api_key(tmp_path):
    page_1 = {"data": [{"CERT": "1"}], "meta": {"total": 2}}
    page_2 = {"data": [{"CERT": "2"}], "meta": {"total": 2}}
    session = FakeSession(FakeResponse(page_1), FakeResponse(page_2))
    config = _single_dataset(default_pqci_config(NOW)["fdic"], "active_institutions")
    config["page_size"] = 1

    result = collect_source(
        "fdic", config=config, data_lake_root=tmp_path, session=session, now=NOW
    )[0]

    assert result.record_count == 2
    assert session.calls[0]["url"] == "https://api.fdic.gov/banks/institutions"
    assert session.calls[1]["params"]["offset"] == 1


def test_nass_checks_count_then_downloads_with_environment_key(tmp_path, monkeypatch):
    secret = "nass-runtime-secret"
    monkeypatch.setenv("NASS_API_KEY", secret)
    session = FakeSession(
        FakeResponse({"count": "1"}),
        FakeResponse({"data": [{"commodity_desc": "CORN", "Value": "1"}]}),
    )

    result = collect_source(
        "nass",
        config=_single_dataset(default_pqci_config(NOW)["nass"], "corn"),
        data_lake_root=tmp_path,
        session=session,
        now=NOW,
    )[0]

    assert session.calls[0]["url"].endswith("/get_counts/")
    assert session.calls[1]["url"].endswith("/api_GET/")
    assert session.calls[1]["params"]["key"] == secret
    assert result.record_count == 1
    assert secret not in result.snapshot_path.read_text(encoding="utf-8")


def test_fhfa_saves_master_csv_and_metadata(tmp_path):
    csv_bytes = b"hpi_type,hpi_flavor,frequency\ntraditional,purchase-only,monthly\n"
    session = FakeSession(
        FakeResponse(None, content=csv_bytes, headers={"content-type": "text/csv"})
    )

    result = collect_source(
        "fhfa",
        config=default_pqci_config(NOW)["fhfa"],
        data_lake_root=tmp_path,
        session=session,
        now=NOW,
    )[0]

    assert result.record_count == 1
    assert result.snapshot_path.read_bytes() == csv_bytes
    assert result.latest_path == tmp_path / "bronze" / "pqci" / "fhfa" / "latest_hpi_master.csv"
    metadata = json.loads(
        (tmp_path / "bronze" / "pqci" / "fhfa" / "latest_hpi_master.metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["dataset"] == "hpi_master"


def test_config_file_cannot_contain_api_keys(tmp_path):
    config_path = tmp_path / "pqci-inputs.json"
    config_path.write_text(
        json.dumps({"census": {"query": {"api_key": "do-not-store"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="environment variables"):
        load_pqci_config(config_path, now=NOW)


def test_default_config_can_be_overridden_without_mutating_other_defaults(tmp_path):
    config_path = tmp_path / "pqci-inputs.json"
    config_path.write_text(
        json.dumps({"census": {"year": 2023, "get": ["NAME"]}}),
        encoding="utf-8",
    )

    config = load_pqci_config(config_path, now=NOW)

    assert config["census"]["year"] == 2023
    assert len(config["census"]["datasets"]) == 6
    assert config["bls"]["datasets"][0]["series_ids"][0] == "CUUR0000SA0"


def test_default_catalog_covers_reference_nowcasting_inputs():
    config = default_pqci_config(NOW)

    assert config["census"]["for"] == "us:*"
    census_names = {job["name"] for job in config["census"]["datasets"]}
    assert {
        "advance_retail_sales",
        "manufacturing_orders_shipments_inventories",
        "manufacturing_trade_inventories_sales",
        "residential_construction",
        "new_residential_sales",
        "international_trade",
    } <= census_names

    bea_names = {job["name"] for job in config["bea"]["datasets"]}
    assert {"regional_gdp", "gdp_by_industry", "input_output_requirements"} <= bea_names
    regional = next(job for job in config["bea"]["datasets"] if job["name"] == "regional_gdp")
    assert regional["params"]["TableName"] == "SAGDP2"

    eia_series = {job["series_id"] for job in config["eia"]["datasets"]}
    assert {
        "PET.RWTC.D",
        "PET.RBRTE.D",
        "NG.RNGWHHD.D",
        "PET.WCRFPUS2.W",
        "NG.N9070US2.M",
        "PET.WCESTUS1.W",
        "PET.WPULEUS3.W",
        "ELEC.SALES.US-ALL.M",
        "ELEC.PRICE.US-ALL.M",
    } <= eia_series

    financials = next(
        job for job in config["fdic"]["datasets"] if job["name"] == "quarterly_bank_financials"
    )
    assert financials["endpoint"] == "financials"
    assert {"ERNAST", "LNLSGR", "DEP", "NIMYQ", "ELNATQ", "EQ"} <= set(financials["fields"])
    assert {job["query"]["commodity_desc"] for job in config["nass"]["datasets"]} == {
        "CORN",
        "WHEAT",
        "SOYBEANS",
        "CATTLE",
    }


def test_latest_files_are_dataset_specific(tmp_path):
    session = FakeSession(
        FakeResponse({"data": [{"CERT": "1"}], "meta": {"total": 1}}),
        FakeResponse({"data": [{"CERT": "1", "REPDTE": "2026-06-30"}], "meta": {"total": 1}}),
    )

    results = collect_source(
        "fdic",
        config=default_pqci_config(NOW)["fdic"],
        data_lake_root=tmp_path,
        session=session,
        now=NOW,
    )

    assert [result.dataset for result in results] == [
        "active_institutions",
        "quarterly_bank_financials",
    ]
    assert results[0].latest_path.name == "latest_active_institutions.json"
    assert results[1].latest_path.name == "latest_quarterly_bank_financials.json"
    assert results[0].latest_path != results[1].latest_path
