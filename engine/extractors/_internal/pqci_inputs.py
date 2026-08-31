from __future__ import annotations

import json
import os
import re
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

from engine.core.paths import DATA_LAKE
from engine.core.source_storage import json_source_validator, write_source_bytes


SUPPORTED_SOURCES = ("bls", "census", "bea", "eia", "fdic", "nass", "fhfa")
PQCI_DIMENSIONS = {
    "P": "price_and_monetization",
    "Q": "quantity_activity_and_demand",
    "C": "cost_and_unit_economics",
    "I": "investment_capital_and_capacity",
}
REQUIRED_API_KEY_ENVS = {
    "census": "CENSUS_API_KEY",
    "bea": "BEA_API_KEY",
    "eia": "EIA_API_KEY",
    "nass": "NASS_API_KEY",
}
OPTIONAL_API_KEY_ENVS = {"bls": "BLS_API_KEY"}

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
CENSUS_BASE_URL = "https://api.census.gov/data"
BEA_URL = "https://apps.bea.gov/api/data/"
EIA_BASE_URL = "https://api.eia.gov/v2"
FDIC_BASE_URL = "https://api.fdic.gov/banks"
NASS_BASE_URL = "https://quickstats.nass.usda.gov/api"
FHFA_MASTER_HPI_URL = "https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv"

_SECRET_FIELD_NAMES = {
    "api-key",
    "api_key",
    "apikey",
    "key",
    "registrationkey",
    "user-id",
    "user_id",
    "userid",
}
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_SAFE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")


class PqciInputsError(RuntimeError):
    """Raised when a P/Q/C/I input source returns unusable data."""


class MissingApiKeyError(PqciInputsError):
    """Raised before a request when its environment-only API key is missing."""


@dataclass(frozen=True)
class CollectionResult:
    source: str
    dataset: str
    snapshot_path: Path
    latest_path: Path
    record_count: int
    retrieved_at: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["snapshot_path"] = str(self.snapshot_path)
        result["latest_path"] = str(self.latest_path)
        return result


def default_pqci_config(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    current_year = (now or datetime.now(timezone.utc)).year
    recent_years = ",".join(str(year) for year in range(current_year - 4, current_year + 1))
    eits_fields = [
        "cell_value",
        "data_type_code",
        "time_slot_id",
        "error_data",
        "category_code",
        "seasonally_adj",
    ]
    return {
        "bls": {
            "datasets": [
                {
                    "name": "price_labor_activity",
                    "series_ids": [
                        "CUUR0000SA0",
                        "CUUR0000SAF11",
                        "CUUR0000SAH1",
                        "CUUR0000SETA01",
                        "CUUR0000SETB01",
                        "WPUFD4",
                        "WPUFD49104",
                        "WPU0573",
                        "CES0500000003",
                        "CES0000000001",
                        "LNS14000000",
                        "JTS000000000000000JOL",
                    ],
                    "start_year": current_year - 4,
                    "end_year": current_year,
                    "catalog": True,
                    "calculations": False,
                    "annual_average": False,
                    "aspects": False,
                    "series_metadata": {
                        "CUUR0000SA0": {"label": "CPI all items", "dimensions": ["P"]},
                        "CUUR0000SAF11": {"label": "CPI food at home", "dimensions": ["P", "C"]},
                        "CUUR0000SAH1": {"label": "CPI shelter", "dimensions": ["P", "C"]},
                        "CUUR0000SETA01": {"label": "CPI new vehicles", "dimensions": ["P"]},
                        "CUUR0000SETB01": {"label": "CPI gasoline", "dimensions": ["P", "C"]},
                        "WPUFD4": {"label": "PPI final demand", "dimensions": ["P", "C"]},
                        "WPUFD49104": {"label": "PPI final demand less food and energy", "dimensions": ["P", "C"]},
                        "WPU0573": {"label": "PPI light fuel oils", "dimensions": ["P", "C"]},
                        "CES0500000003": {"label": "Average hourly earnings, total private", "dimensions": ["C"]},
                        "CES0000000001": {"label": "Total nonfarm payroll employment", "dimensions": ["Q"]},
                        "LNS14000000": {"label": "Unemployment rate", "dimensions": ["Q"]},
                        "JTS000000000000000JOL": {"label": "Total job openings", "dimensions": ["Q"]},
                    },
                    "pqci": {
                        "dimensions": ["P", "Q", "C"],
                        "kpi_layers": ["activity", "monetization", "economics"],
                        "sectors": ["all"],
                    },
                }
            ]
        },
        "census": {
            "for": "us:*",
            "datasets": [
                {
                    "name": "advance_retail_sales",
                    "dataset": "timeseries/eits/marts",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["P", "Q"], "kpi_layers": ["demand", "activity"], "sectors": ["consumer_discretionary", "consumer_staples", "retail"]},
                },
                {
                    "name": "manufacturing_orders_shipments_inventories",
                    "dataset": "timeseries/eits/m3",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["Q", "I"], "kpi_layers": ["demand", "activity", "capital"], "sectors": ["industrials", "manufacturing"]},
                },
                {
                    "name": "manufacturing_trade_inventories_sales",
                    "dataset": "timeseries/eits/mtis",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["Q", "I"], "kpi_layers": ["demand", "activity", "capital"], "sectors": ["manufacturing", "wholesale", "retail"]},
                },
                {
                    "name": "residential_construction",
                    "dataset": "timeseries/eits/resconst",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["Q", "I"], "kpi_layers": ["demand", "activity", "capital"], "sectors": ["homebuilders", "real_estate", "construction"]},
                },
                {
                    "name": "new_residential_sales",
                    "dataset": "timeseries/eits/ressales",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["P", "Q"], "kpi_layers": ["demand", "activity", "monetization"], "sectors": ["homebuilders", "real_estate"]},
                },
                {
                    "name": "international_trade",
                    "dataset": "timeseries/eits/ftd",
                    "get": eits_fields,
                    "query": {"time": f"from {current_year - 4}-01"},
                    "pqci": {"dimensions": ["P", "Q"], "kpi_layers": ["demand", "activity"], "sectors": ["industrials", "manufacturing", "transportation"]},
                },
            ]
        },
        "bea": {
            "datasets": [
                {
                    "name": "regional_gdp",
                    "params": {"DataSetName": "Regional", "TableName": "SAGDP2", "LineCode": "1", "GeoFIPS": "STATE", "Year": "LAST5"},
                    "pqci": {"dimensions": ["P", "Q"], "kpi_layers": ["activity"], "sectors": ["all"]},
                },
                {
                    "name": "gdp_by_industry",
                    "params": {"DataSetName": "GDPByIndustry", "TableID": "ALL", "Frequency": "A,Q", "Year": recent_years, "Industry": "ALL"},
                    "pqci": {"dimensions": ["P", "Q", "C", "I"], "kpi_layers": ["activity", "monetization", "economics", "capital"], "sectors": ["all"]},
                },
                {
                    "name": "input_output_requirements",
                    "params": {"DataSetName": "InputOutput", "TableID": "56", "Year": "ALL"},
                    "pqci": {"dimensions": ["C"], "kpi_layers": ["economics"], "sectors": ["all"], "purpose": "industry_cost_graph"},
                },
            ]
        },
        "eia": {
            "page_size": 5000,
            "max_records": 50000,
            "datasets": [
                {"name": "wti_spot_price", "series_id": "PET.RWTC.D", "start": f"{current_year - 4}-01-01", "pqci": {"dimensions": ["P", "C"], "kpi_layers": ["monetization", "economics"], "sectors": ["energy", "transportation", "industrials"]}},
                {"name": "brent_spot_price", "series_id": "PET.RBRTE.D", "start": f"{current_year - 4}-01-01", "pqci": {"dimensions": ["P", "C"], "kpi_layers": ["monetization", "economics"], "sectors": ["energy", "transportation", "industrials"]}},
                {"name": "henry_hub_spot_price", "series_id": "NG.RNGWHHD.D", "start": f"{current_year - 4}-01-01", "pqci": {"dimensions": ["P", "C"], "kpi_layers": ["monetization", "economics"], "sectors": ["energy", "utilities", "chemicals"]}},
                {"name": "crude_oil_production", "series_id": "PET.WCRFPUS2.W", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["Q"], "kpi_layers": ["activity"], "sectors": ["energy"]}},
                {"name": "dry_natural_gas_production", "series_id": "NG.N9070US2.M", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["Q"], "kpi_layers": ["activity"], "sectors": ["energy", "utilities"]}},
                {"name": "crude_oil_inventory", "series_id": "PET.WCESTUS1.W", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["Q", "I"], "kpi_layers": ["demand", "capital"], "sectors": ["energy"]}},
                {"name": "refinery_utilization", "series_id": "PET.WPULEUS3.W", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["Q", "I"], "kpi_layers": ["activity", "capital"], "sectors": ["energy"]}},
                {"name": "electricity_sales", "series_id": "ELEC.SALES.US-ALL.M", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["Q"], "kpi_layers": ["activity", "demand"], "sectors": ["utilities"]}},
                {"name": "electricity_price", "series_id": "ELEC.PRICE.US-ALL.M", "start": f"{current_year - 4}-01", "pqci": {"dimensions": ["P", "C"], "kpi_layers": ["monetization", "economics"], "sectors": ["utilities"]}},
            ],
        },
        "fdic": {
            "page_size": 10000,
            "max_records": 150000,
            "datasets": [
                {
                    "name": "active_institutions",
                    "endpoint": "institutions",
                    "filters": "ACTIVE:1",
                    "fields": ["CERT", "NAME", "STALP", "CITY", "ASSET", "DEP", "ACTIVE", "DATEUPDT"],
                    "sort_by": "NAME",
                    "sort_order": "ASC",
                    "pqci": {"dimensions": ["Q"], "kpi_layers": ["activity"], "sectors": ["banks"], "purpose": "entity_map"},
                },
                {
                    "name": "quarterly_bank_financials",
                    "endpoint": "financials",
                    "filters": f"REPDTE:[{current_year - 4}-01-01 TO *]",
                    "fields": ["CERT", "NAME", "REPDTE", "ASSET", "DEP", "EQ", "ERNAST", "LNLSGR", "LNLSNET", "LNCI", "LNCON", "LNCRCD", "LNRE", "LNAUTO", "NETINCQ", "NIMYQ", "ROAQ", "ROEQ", "ELNATQ", "NTLNLSQ", "NTLNLSQR", "P3LNLS", "P9LNLS", "NALNLS", "NCLNLS"],
                    "sort_by": "REPDTE",
                    "sort_order": "ASC",
                    "pqci": {"dimensions": ["P", "Q", "C", "I"], "kpi_layers": ["activity", "monetization", "economics", "capital"], "sectors": ["banks"]},
                },
            ],
        },
        "nass": {
            "max_records": 50000,
            "datasets": [
                {
                    "name": commodity.lower(),
                    "query": {"source_desc": "SURVEY", "commodity_desc": commodity, "agg_level_desc": "STATE", "freq_desc": "ANNUAL", "year__GE": str(current_year - 4)},
                    "pqci": {"dimensions": ["P", "Q", "I"], "kpi_layers": ["activity", "monetization", "capital"], "sectors": ["agriculture", "food"]},
                }
                for commodity in ("CORN", "WHEAT", "SOYBEANS", "CATTLE")
            ],
        },
        "fhfa": {
            "datasets": [
                {
                    "name": "hpi_master",
                    "url": FHFA_MASTER_HPI_URL,
                    "pqci": {"dimensions": ["P", "I"], "kpi_layers": ["monetization", "capital"], "sectors": ["real_estate", "homebuilders", "banks"]},
                }
            ]
        },
    }


def load_pqci_config(
    path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    config = default_pqci_config(now)
    if path is None:
        return config
    supplied = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(supplied, dict):
        raise ValueError("PQCI inputs config must be a JSON object")
    unknown_sources = sorted(set(supplied) - set(SUPPORTED_SOURCES))
    if unknown_sources:
        raise ValueError(f"unsupported config sources: {', '.join(unknown_sources)}")
    _reject_config_secrets(supplied)
    for source, values in supplied.items():
        if not isinstance(values, dict):
            raise ValueError(f"config for {source} must be a JSON object")
        config[source] = _deep_merge(config[source], values)
    return config


def collect_sources(
    sources: Sequence[str] | None = None,
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
    data_lake_root: str | Path = DATA_LAKE.root,
    session: requests.Session | None = None,
    skip_missing_keys: bool = False,
    now: datetime | None = None,
) -> list[CollectionResult]:
    selected = _normalize_sources(sources)
    resolved_config = deepcopy(config) if config is not None else default_pqci_config(now)
    _reject_config_secrets(resolved_config)

    missing = [
        (source, REQUIRED_API_KEY_ENVS[source])
        for source in selected
        if source in REQUIRED_API_KEY_ENVS and not _environment_key(REQUIRED_API_KEY_ENVS[source])
    ]
    if missing and not skip_missing_keys:
        names = ", ".join(env_name for _, env_name in missing)
        raise MissingApiKeyError(f"required API key environment variables are missing: {names}")
    if skip_missing_keys:
        missing_sources = {source for source, _ in missing}
        selected = [source for source in selected if source not in missing_sources]

    http = session or requests.Session()
    close_session = session is None
    try:
        results: list[CollectionResult] = []
        for source in selected:
            source_config = resolved_config.get(source)
            if not isinstance(source_config, Mapping):
                raise ValueError(f"config for {source} must be a mapping")
            results.extend(
                collect_source(
                    source,
                    config=source_config,
                    data_lake_root=data_lake_root,
                    session=http,
                    now=now,
                )
            )
        _save_catalog(resolved_config, data_lake_root=data_lake_root, now=now)
        return results
    finally:
        if close_session:
            http.close()


def collect_source(
    source: str,
    *,
    config: Mapping[str, Any] | None = None,
    data_lake_root: str | Path = DATA_LAKE.root,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[CollectionResult]:
    normalized = str(source).strip().lower()
    if normalized not in SUPPORTED_SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SUPPORTED_SOURCES)}")
    source_config = deepcopy(dict(config or default_pqci_config(now)[normalized]))
    _reject_config_secrets(source_config)
    http = session or requests.Session()
    close_session = session is None
    try:
        collector = globals()[f"collect_{normalized}"]
        return collector(
            source_config,
            data_lake_root=data_lake_root,
            session=http,
            now=now,
        )
    finally:
        if close_session:
            http.close()


def collect_bls(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    key = _environment_key(OPTIONAL_API_KEY_ENVS["bls"])
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="timeseries"):
        series_ids = [str(value).strip() for value in job.get("series_ids", []) if str(value).strip()]
        if not series_ids:
            raise ValueError("bls dataset series_ids must contain at least one series ID")
        body: dict[str, Any] = {
            "seriesid": series_ids,
            "startyear": str(job["start_year"]),
            "endyear": str(job["end_year"]),
            "catalog": bool(job.get("catalog", True)),
            "calculations": bool(job.get("calculations", False)),
            "annualaverage": bool(job.get("annual_average", False)),
            "aspects": bool(job.get("aspects", False)),
        }
        if key:
            body["registrationkey"] = key
        payload = _request_json(session, "bls", "POST", BLS_URL, json_body=body, secrets=[key])
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise PqciInputsError(f"BLS request failed: {_safe_api_message(payload, [key])}")
        series = payload.get("Results", {}).get("series", [])
        record_count = sum(len(item.get("data", [])) for item in series if isinstance(item, dict))
        request_metadata = {name: value for name, value in body.items() if name != "registrationkey"}
        results.append(
            _save_json_result(
                "bls",
                str(job["name"]),
                payload,
                request={"method": "POST", "endpoint": BLS_URL, "parameters": request_metadata},
                pqci=_job_metadata(job, extra_names=("series_metadata",)),
                secrets=[key],
                record_count=record_count,
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_census(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    key = _required_environment_key("census")
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="census"):
        dataset = str(job["dataset"]).strip().strip("/")
        year = str(job.get("year", "")).strip()
        endpoint = f"{CENSUS_BASE_URL}/{year + '/' if year else ''}{dataset}"
        get_fields = job.get("get", [])
        fields = ",".join(str(value).strip() for value in get_fields) if not isinstance(get_fields, str) else get_fields
        params: dict[str, Any] = {"get": fields, "key": key}
        if job.get("for"):
            params["for"] = str(job["for"])
        for optional_name in ("in", "ucgid"):
            if job.get(optional_name):
                params[optional_name] = str(job[optional_name])
        extra = job.get("query", {})
        if extra:
            if not isinstance(extra, Mapping):
                raise ValueError("census dataset query must be an object")
            params.update(extra)
        payload = _request_json(session, "census", "GET", endpoint, params=params, secrets=[key])
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            raise PqciInputsError("Census response did not contain the expected row array")
        safe_params = {name: value for name, value in params.items() if name != "key"}
        results.append(
            _save_json_result(
                "census",
                str(job["name"]),
                payload,
                request={"method": "GET", "endpoint": endpoint, "parameters": safe_params},
                pqci=_job_metadata(job),
                secrets=[key],
                record_count=max(0, len(payload) - 1),
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_bea(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    key = _required_environment_key("bea")
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="bea"):
        configured_params = job.get("params")
        if configured_params is None:
            configured_params = {
                "DataSetName": str(job["dataset"]),
                "TableName": str(job["table_name"]),
                "LineCode": str(job["line_code"]),
                "GeoFIPS": str(job["geo_fips"]),
                "Year": str(job["year"]),
            }
        if not isinstance(configured_params, Mapping):
            raise ValueError("bea dataset params must be an object")
        params: dict[str, Any] = {"UserID": key, "method": "GetData", **configured_params, "ResultFormat": "JSON"}
        extra = job.get("query", {})
        if extra:
            if not isinstance(extra, Mapping):
                raise ValueError("bea dataset query must be an object")
            params.update(extra)
        payload = _request_json(session, "bea", "GET", BEA_URL, params=params, secrets=[key])
        bea_api = payload.get("BEAAPI", {}) if isinstance(payload, dict) else {}
        raw_results = bea_api.get("Results", {}) if isinstance(bea_api, Mapping) else {}
        if isinstance(raw_results, Mapping):
            result_nodes = [raw_results]
        elif isinstance(raw_results, list):
            result_nodes = [node for node in raw_results if isinstance(node, Mapping)]
        else:
            result_nodes = []
        if bea_api.get("Error") or any(node.get("Error") for node in result_nodes):
            raise PqciInputsError(f"BEA request failed: {_safe_api_message(payload, [key])}")
        data: list[Any] = []
        for node in result_nodes:
            rows = node.get("Data", [])
            if isinstance(rows, list):
                data.extend(rows)
        safe_params = {name: value for name, value in params.items() if name.lower() != "userid"}
        results.append(
            _save_json_result(
                "bea",
                str(job["name"]),
                payload,
                request={"method": "GET", "endpoint": BEA_URL, "parameters": safe_params},
                pqci=_job_metadata(job),
                secrets=[key],
                record_count=len(data) if isinstance(data, list) else 0,
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_eia(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    key = _required_environment_key("eia")
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="eia"):
        page_size = min(5000, max(1, int(job.get("page_size", 5000))))
        max_records = max(1, int(job.get("max_records", 50000)))
        base_params: list[tuple[str, Any]] = [("api_key", key)]
        if job.get("series_id"):
            series_id = str(job["series_id"]).strip()
            endpoint = f"{EIA_BASE_URL}/seriesid/{series_id}"
            base_params.extend([("sort[0][column]", "period"), ("sort[0][direction]", "asc")])
        else:
            route = str(job["route"]).strip().strip("/")
            endpoint = f"{EIA_BASE_URL}/{route}/data/"
            if job.get("frequency"):
                base_params.append(("frequency", str(job["frequency"])))
            for data_column in job.get("data", []):
                base_params.append(("data[]", str(data_column)))
            facets = job.get("facets", {})
            if not isinstance(facets, Mapping):
                raise ValueError("eia dataset facets must be an object")
            for facet_name, values in facets.items():
                facet_values = values if isinstance(values, list) else [values]
                for value in facet_values:
                    base_params.append((f"facets[{facet_name}][]", str(value)))
            for index, item in enumerate(job.get("sort", [])):
                base_params.append((f"sort[{index}][column]", str(item["column"])))
                base_params.append((f"sort[{index}][direction]", str(item["direction"])))
        for name in ("start", "end"):
            if job.get(name):
                base_params.append((name, str(job[name])))

        pages: list[dict[str, Any]] = []
        offset = 0
        total = 0
        while True:
            params = [*base_params, ("offset", offset), ("length", page_size)]
            payload = _request_json(session, "eia", "GET", endpoint, params=params, secrets=[key])
            if isinstance(payload, Mapping) and payload.get("error"):
                raise PqciInputsError(f"EIA request failed: {_safe_api_message(payload, [key])}")
            response = payload.get("response", {}) if isinstance(payload, dict) else {}
            rows = response.get("data", [])
            if not isinstance(rows, list):
                raise PqciInputsError("EIA response did not contain response.data")
            pages.append(payload)
            total = _as_int(response.get("total"), default=len(rows))
            if total > max_records:
                raise PqciInputsError(
                    f"EIA query returns {total} rows, exceeding max_records={max_records}; narrow the query or raise the limit"
                )
            offset += len(rows)
            if not rows or offset >= total:
                break

        safe_params = _pairs_without_secrets(base_params, {"api_key"})
        results.append(
            _save_json_result(
                "eia",
                str(job["name"]),
                {"total": total, "pages": pages},
                request={"method": "GET", "endpoint": endpoint, "parameters": safe_params},
                pqci=_job_metadata(job),
                secrets=[key],
                record_count=offset,
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_fdic(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="fdic"):
        endpoint_name = str(job["endpoint"]).strip().strip("/")
        endpoint = f"{FDIC_BASE_URL}/{endpoint_name}"
        page_size = max(1, int(job.get("page_size", 10000)))
        max_records = max(1, int(job.get("max_records", 50000)))
        base_params: dict[str, Any] = {
            "filters": str(job.get("filters", "")),
            "fields": ",".join(str(value) for value in job.get("fields", [])),
            "sort_by": str(job.get("sort_by", "NAME")),
            "sort_order": str(job.get("sort_order", "ASC")),
            "format": "json",
        }
        base_params = {name: value for name, value in base_params.items() if value}
        pages: list[dict[str, Any]] = []
        offset = 0
        total = 0
        while True:
            params = {**base_params, "offset": offset, "limit": page_size}
            payload = _request_json(session, "fdic", "GET", endpoint, params=params)
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                raise PqciInputsError("FDIC response did not contain data")
            pages.append(payload)
            total = _as_int(payload.get("meta", {}).get("total"), default=len(rows))
            if total > max_records:
                raise PqciInputsError(
                    f"FDIC query returns {total} rows, exceeding max_records={max_records}; narrow the query or raise the limit"
                )
            offset += len(rows)
            if not rows or offset >= total:
                break
        results.append(
            _save_json_result(
                "fdic",
                str(job["name"]),
                {"total": total, "pages": pages},
                request={"method": "GET", "endpoint": endpoint, "parameters": base_params},
                pqci=_job_metadata(job),
                record_count=offset,
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_nass(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    key = _required_environment_key("nass")
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="quickstats"):
        query = job.get("query", {})
        if not isinstance(query, Mapping) or not query:
            raise ValueError("nass dataset query must be a non-empty object")
        params = {str(name): value for name, value in query.items()}
        count_payload = _request_json(
            session, "nass", "GET", f"{NASS_BASE_URL}/get_counts/", params={"key": key, **params}, secrets=[key]
        )
        if isinstance(count_payload, dict) and count_payload.get("error"):
            raise PqciInputsError(f"NASS count request failed: {_safe_api_message(count_payload, [key])}")
        count = _as_int(count_payload.get("count"), default=0)
        max_records = min(50000, max(1, int(job.get("max_records", 50000))))
        if count > max_records:
            raise PqciInputsError(
                f"NASS query returns {count} rows, exceeding max_records={max_records}; narrow the dataset query"
            )
        payload = _request_json(
            session,
            "nass",
            "GET",
            f"{NASS_BASE_URL}/api_GET/",
            params={"key": key, **params, "format": "JSON"},
            secrets=[key],
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise PqciInputsError(f"NASS request failed: {_safe_api_message(payload, [key])}")
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise PqciInputsError("NASS response did not contain data")
        results.append(
            _save_json_result(
                "nass",
                str(job["name"]),
                payload,
                request={"method": "GET", "endpoint": f"{NASS_BASE_URL}/api_GET/", "parameters": {**params, "format": "JSON"}},
                pqci=_job_metadata(job),
                secrets=[key],
                record_count=len(rows),
                data_lake_root=data_lake_root,
                now=now,
            )
        )
    return results


def collect_fhfa(
    config: Mapping[str, Any],
    *,
    data_lake_root: str | Path,
    session: requests.Session,
    now: datetime | None = None,
) -> list[CollectionResult]:
    results: list[CollectionResult] = []
    for job in _dataset_jobs(config, default_name="hpi_master"):
        dataset = str(job["name"])
        safe_dataset = _safe_name(dataset)
        url = str(job.get("url", FHFA_MASTER_HPI_URL)).strip()
        response = _request(session, "fhfa", "GET", url)
        payload = response.content
        if not payload or b"," not in payload[:4096]:
            raise PqciInputsError("FHFA response was not a non-empty CSV file")
        retrieved = _utc_now(now)
        timestamp = _timestamp_for_filename(retrieved)
        output_dir = Path(data_lake_root) / "bronze" / "pqci" / "fhfa"
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = output_dir / f"{safe_dataset}_{timestamp}.csv"
        latest_path = output_dir / f"latest_{safe_dataset}.csv"
        write_source_bytes(snapshot_path, payload, source="fhfa")
        write_source_bytes(latest_path, payload, source="fhfa")
        metadata = {
            "schema_version": 2,
            "source": "fhfa",
            "dataset": dataset,
            "retrieved_at": retrieved.isoformat(),
            "request": {"method": "GET", "endpoint": url, "parameters": {}},
            "pqci": _job_metadata(job),
            "record_count": max(0, payload.count(b"\n") - 1),
        }
        metadata_payload = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        write_source_bytes(output_dir / f"{safe_dataset}_{timestamp}.metadata.json", metadata_payload, source="fhfa", validator=json_source_validator)
        write_source_bytes(output_dir / f"latest_{safe_dataset}.metadata.json", metadata_payload, source="fhfa", validator=json_source_validator)
        results.append(
            CollectionResult(
                source="fhfa",
                dataset=dataset,
                snapshot_path=snapshot_path,
                latest_path=latest_path,
                record_count=metadata["record_count"],
                retrieved_at=retrieved.isoformat(),
            )
        )
    return results


def _request_json(
    session: requests.Session,
    source: str,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    json_body: Mapping[str, Any] | None = None,
    secrets: Sequence[str | None] = (),
    timeout: int = 120,
    retries: int = 3,
) -> Any:
    response = _request(
        session,
        source,
        method,
        url,
        params=params,
        json_body=json_body,
        secrets=secrets,
        timeout=timeout,
        retries=retries,
    )
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        content_type = response.headers.get("content-type", "unknown")
        raise PqciInputsError(f"{source.upper()} returned invalid JSON ({content_type})") from exc


def _request(
    session: requests.Session,
    source: str,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    json_body: Mapping[str, Any] | None = None,
    secrets: Sequence[str | None] = (),
    timeout: int = 120,
    retries: int = 3,
) -> requests.Response:
    headers = {"User-Agent": "Arcana pqci-inputs-collector/1.0", "Accept": "application/json"}
    for attempt in range(retries + 1):
        try:
            response = session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            if attempt >= retries:
                message = _redact_text(str(exc), secrets)
                raise PqciInputsError(f"{source.upper()} request failed: {message}") from exc
            time.sleep(min(2**attempt, 8))
            continue
        if response.status_code < 400:
            return response
        if response.status_code in _RETRYABLE_STATUS_CODES and attempt < retries:
            retry_after = _as_float(response.headers.get("Retry-After"), default=2**attempt)
            time.sleep(min(max(retry_after, 0.0), 30.0))
            continue
        detail = _redact_text(response.text[:1000], secrets).strip()
        raise PqciInputsError(
            f"{source.upper()} returned HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )
    raise AssertionError("request retry loop exited unexpectedly")


def _save_json_result(
    source: str,
    dataset: str,
    response_payload: Any,
    *,
    request: Mapping[str, Any],
    pqci: Mapping[str, Any] | None = None,
    secrets: Sequence[str | None] = (),
    record_count: int,
    data_lake_root: str | Path,
    now: datetime | None,
) -> CollectionResult:
    retrieved = _utc_now(now)
    timestamp = _timestamp_for_filename(retrieved)
    safe_dataset = _safe_name(dataset)
    output_dir = Path(data_lake_root) / "bronze" / "pqci" / source
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / f"{safe_dataset}_{timestamp}.json"
    latest_path = output_dir / f"latest_{safe_dataset}.json"
    envelope = {
        "schema_version": 2,
        "source": source,
        "dataset": dataset,
        "retrieved_at": retrieved.isoformat(),
        "request": _redact_value(request, secrets),
        "pqci": _redact_value(dict(pqci or {}), secrets),
        "record_count": int(record_count),
        "response": _redact_value(response_payload, secrets),
    }
    encoded = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    write_source_bytes(snapshot_path, encoded, source=source, validator=json_source_validator)
    write_source_bytes(latest_path, encoded, source=source, validator=json_source_validator)
    return CollectionResult(
        source=source,
        dataset=dataset,
        snapshot_path=snapshot_path,
        latest_path=latest_path,
        record_count=int(record_count),
        retrieved_at=retrieved.isoformat(),
    )


def _dataset_jobs(config: Mapping[str, Any], *, default_name: str) -> list[dict[str, Any]]:
    datasets = config.get("datasets")
    common = {key: deepcopy(value) for key, value in config.items() if key != "datasets"}
    if datasets is None:
        legacy = deepcopy(common)
        legacy.setdefault("name", default_name)
        return [legacy]
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("source datasets must be a non-empty array")
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, Mapping):
            raise ValueError(f"source datasets[{index}] must be an object")
        job = _deep_merge(dataset, common)
        name = str(job.get("name", "")).strip()
        if not name:
            raise ValueError(f"source datasets[{index}].name is required")
        safe_name = _safe_name(name)
        if safe_name in seen:
            raise ValueError(f"duplicate dataset name after normalization: {name}")
        seen.add(safe_name)
        job["name"] = name
        jobs.append(job)
    return jobs


def _job_metadata(job: Mapping[str, Any], *, extra_names: Sequence[str] = ()) -> dict[str, Any]:
    metadata = deepcopy(dict(job.get("pqci", {})))
    dimensions = metadata.get("dimensions", [])
    if not isinstance(dimensions, list):
        raise ValueError("dataset pqci.dimensions must be an array")
    unknown = sorted({str(value) for value in dimensions} - set(PQCI_DIMENSIONS))
    if unknown:
        raise ValueError(f"unknown PQCI dimensions: {', '.join(unknown)}")
    metadata["dimension_definitions"] = {
        dimension: PQCI_DIMENSIONS[dimension] for dimension in dimensions
    }
    for name in extra_names:
        if name in job:
            metadata[name] = deepcopy(job[name])
    return metadata


def _save_catalog(
    config: Mapping[str, Mapping[str, Any]],
    *,
    data_lake_root: str | Path,
    now: datetime | None,
) -> Path:
    retrieved = _utc_now(now)
    sources: dict[str, Any] = {}
    for source in SUPPORTED_SOURCES:
        source_config = config.get(source)
        if not isinstance(source_config, Mapping):
            continue
        sources[source] = [
            {
                "name": job["name"],
                "pqci": _job_metadata(
                    job,
                    extra_names=("series_metadata",) if source == "bls" else (),
                ),
            }
            for job in _dataset_jobs(source_config, default_name=source)
        ]
    catalog = {
        "schema_version": 1,
        "generated_at": retrieved.isoformat(),
        "pqci_dimensions": PQCI_DIMENSIONS,
        "sources": sources,
    }
    output_path = Path(data_lake_root) / "bronze" / "pqci" / "catalog.json"
    encoded = json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    write_source_bytes(output_path, encoded, source="pqci", validator=json_source_validator)
    return output_path


def _normalize_sources(sources: Sequence[str] | None) -> list[str]:
    if not sources:
        return list(SUPPORTED_SOURCES)
    normalized: list[str] = []
    for source in sources:
        value = str(source).strip().lower()
        if value == "all":
            return list(SUPPORTED_SOURCES)
        if value not in SUPPORTED_SOURCES:
            raise ValueError(f"unsupported PQCI input source: {source}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def _required_environment_key(source: str) -> str:
    env_name = REQUIRED_API_KEY_ENVS[source]
    value = _environment_key(env_name)
    if not value:
        raise MissingApiKeyError(f"{env_name} environment variable is required for {source.upper()}")
    return value


def _environment_key(env_name: str) -> str:
    return os.getenv(env_name, "").strip()


def _reject_config_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace(" ", "_")
            if normalized in _SECRET_FIELD_NAMES:
                raise ValueError(
                    f"API keys must be supplied through environment variables, not {path}.{key}"
                )
            _reject_config_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_config_secrets(nested, f"{path}[{index}]")


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _pairs_without_secrets(
    pairs: Sequence[tuple[str, Any]],
    secret_names: set[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in secret_names:
            continue
        if name in result:
            current = result[name]
            result[name] = [*current, value] if isinstance(current, list) else [current, value]
        else:
            result[name] = value
    return result


def _safe_api_message(payload: Any, secrets: Sequence[str | None]) -> str:
    return _redact_text(json.dumps(payload, ensure_ascii=False)[:2000], secrets)


def _redact_text(text: str, secrets: Sequence[str | None]) -> str:
    result = str(text)
    for secret in secrets:
        if secret:
            result = result.replace(str(secret), "[REDACTED]")
    return result


def _redact_value(value: Any, secrets: Sequence[str | None]) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact_value(nested, secrets) for key, nested in value.items()}
    if isinstance(value, list):
        return [_redact_value(nested, secrets) for nested in value]
    if isinstance(value, tuple):
        return [_redact_value(nested, secrets) for nested in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _safe_name(value: Any) -> str:
    return _SAFE_NAME_PATTERN.sub("_", str(value).strip()).strip("._") or "data"


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_for_filename(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
