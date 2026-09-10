"""Historical capitalization enters the regular public market-data pipeline."""
import hashlib
import json

import pandas as pd
import pytest

from engine.core.paths import DataLakePaths
from engine.transformers.market_data import normalize_shares


def _lake(tmp_path, monkeypatch):
    from engine.transformers._internal import krx_market_data

    lake = DataLakePaths(tmp_path / "data-lake")
    monkeypatch.setattr(krx_market_data, "DATA_LAKE", lake)
    raw = lake.bronze("krx", "shares", "kr_005930.csv")
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "날짜,상장주식수,시가총액\n2013-01-02,10,1000\n2026-09-04,20,2400\n",
        encoding="utf-8",
    )
    return lake, raw


def _register(lake, rows):
    source = lake.bronze("marcap", "data", "marcap-2013.parquet")
    source.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["Code", "Date", "Close", "Stocks", "Marcap"]).to_parquet(source, index=False)
    manifest = lake.silver("krx", "shares", "historical_sources.json")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema_version": 1, "sources": [{
        "year": 2013,
        "path": source.relative_to(lake.root).as_posix(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]}), encoding="utf-8")
    return source, manifest


def test_regular_normalization_keeps_registered_history_on_every_refresh(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    source, _ = _register(lake, [
        ("5930", "2013-01-02", 100, 10, 1000),
        ("660", "2013-01-02", 200, 30, 6000),
        ("00341A", "2013-01-02", 50, 4, 200),
    ])
    original = source.read_bytes()
    expected = pd.DataFrame({
        "security_id": ["SEC_KR_000660", "SEC_KR_00341A", "SEC_KR_005930", "SEC_KR_005930"],
        "trade_date": pd.to_datetime(["2013-01-02", "2013-01-02", "2013-01-02", "2026-09-04"]),
        "shares": [30, 4, 10, 20],
        "market_cap": [6000, 200, 1000, 2400],
    })
    for _ in range(2):
        actual = normalize_shares(str(raw.parent / "*.csv"))
        pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
        saved = pd.read_csv(lake.silver("krx", "shares", "kr_normalized_shares.csv"), parse_dates=["trade_date"])
        pd.testing.assert_frame_equal(saved, expected, check_dtype=False)
    assert source.read_bytes() == original


def test_conflicting_historical_observation_keeps_last_published_input(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    _register(lake, [("005930", "2013-01-02", 100, 11, 1100)])
    with pytest.raises(ValueError, match="conflict"):
        normalize_shares(str(raw.parent / "*.csv"))
    assert published.read_bytes() == before


@pytest.mark.parametrize("rows", [
    [("005930", "2013-01-02", 100, 10, 990)],
    [("005930", "2013-01-02", 100, None, 1000)],
    [("005930", "2013-01-02", 100, 0, 0)],
    [("005930", "2013-01-02", 100, 10.5, 1050)],
    [("005930", "2014-01-02", 100, 10, 1000)],
    [("bad-code", "2013-01-02", 100, 10, 1000)],
    [("005930", "2013-01-02", 100, 10, 1000)] * 2,
])
def test_invalid_registered_source_cannot_replace_published_input(tmp_path, monkeypatch, rows):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    _register(lake, rows)
    with pytest.raises(ValueError):
        normalize_shares(str(raw.parent / "*.csv"))
    assert published.read_bytes() == before


@pytest.mark.parametrize("change", ["source_bytes", "outside_bronze", "duplicate_year", "empty_sources"])
def test_registration_requires_unchanged_bronze_sources(tmp_path, monkeypatch, change):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    source, manifest = _register(lake, [("000660", "2013-01-02", 200, 30, 6000)])
    config = json.loads(manifest.read_text("utf-8"))
    if change == "source_bytes":
        source.write_bytes(source.read_bytes() + b"changed")
    elif change == "outside_bronze":
        misplaced = lake.silver("krx", "shares", "source.parquet")
        misplaced.write_bytes(source.read_bytes())
        config["sources"][0]["path"] = misplaced.relative_to(lake.root).as_posix()
    elif change == "duplicate_year":
        config["sources"] *= 2
    else:
        config["sources"] = []
    manifest.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        normalize_shares(str(raw.parent / "*.csv"))
    assert published.read_bytes() == before


def test_failed_file_replacement_preserves_last_published_input(tmp_path, monkeypatch):
    import os

    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    _register(lake, [("000660", "2013-01-02", 200, 30, 6000)])

    def unavailable_replace(*args, **kwargs):
        raise OSError("destination busy")

    with monkeypatch.context() as filesystem:
        filesystem.setattr(os, "replace", unavailable_replace)
        with pytest.raises(OSError, match="destination busy"):
            normalize_shares(str(raw.parent / "*.csv"))
    assert published.read_bytes() == before
    assert not list(published.parent.glob("*.tmp"))


def test_staging_output_can_be_verified_before_replacing_default_input(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    _register(lake, [("000660", "2013-01-02", 200, 30, 6000)])
    stage = lake.silver("survivorship", "capitalization", "candidate.csv")
    result = normalize_shares(str(raw.parent / "*.csv"), output_path=stage)
    assert len(result) == 3
    assert stage.exists()
    assert published.read_bytes() == before


def test_registered_quarantine_retains_invalid_originals_without_publishing_their_observations(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    source, manifest = _register(lake, [
        ("005930", "2013-01-02", 100, 10, 1000),
        ("000660", "2013-01-02", 0, 30, 6000),
        ("000660", "2013-01-03", 200, 30, 6000),
    ])
    original = source.read_bytes()
    quarantine = lake.silver("krx", "shares", "quarantine", "2013.parquet")
    quarantine.parent.mkdir(parents=True)
    pd.DataFrame({"security_id": ["SEC_KR_000660"], "trade_date": pd.to_datetime(["2013-01-02"]),
        "raw_close": [0], "shares": [30], "market_cap": [6000]}).to_parquet(quarantine, index=False)
    config = json.loads(manifest.read_text("utf-8"))
    config["sources"][0]["quarantine"] = {"path": quarantine.relative_to(lake.root).as_posix(),
        "sha256": hashlib.sha256(quarantine.read_bytes()).hexdigest()}
    manifest.write_text(json.dumps(config), "utf-8")

    actual = normalize_shares(str(raw.parent / "*.csv"))

    assert actual.security_id.tolist() == ["SEC_KR_000660", "SEC_KR_005930", "SEC_KR_005930"]
    assert actual.trade_date.dt.strftime("%Y-%m-%d").tolist() == ["2013-01-03", "2013-01-02", "2026-09-04"]
    assert actual.shares.tolist() == [30, 10, 20]
    assert actual.attrs["quarantined_share_source_rows"] == 1
    assert source.read_bytes() == original
    saved = pd.read_csv(lake.silver("krx", "shares", "kr_normalized_shares.csv"))
    assert not ((saved.security_id == "SEC_KR_000660") & (saved.trade_date == "2013-01-02")).any()


@pytest.mark.parametrize("evidence_change", ["usable_observation", "different_value", "different_bytes"])
def test_quarantine_cannot_hide_usable_or_unmatched_source_observations(tmp_path, monkeypatch, evidence_change):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    published = lake.silver("krx", "shares", "kr_normalized_shares.csv")
    before = published.read_bytes()
    _, manifest = _register(lake, [("000660", "2013-01-02", 0, 30, 6000),
        ("000660", "2013-01-03", 200, 30, 6000)])
    rejected = pd.DataFrame({"security_id": ["SEC_KR_000660"], "trade_date": pd.to_datetime(["2013-01-02"]),
        "raw_close": [0], "shares": [30], "market_cap": [6000]})
    if evidence_change == "usable_observation":
        rejected.loc[0, "trade_date"] = pd.Timestamp("2013-01-03")
        rejected.loc[0, "raw_close"] = 200
    elif evidence_change == "different_value":
        rejected.loc[0, "shares"] = 31
    quarantine = lake.silver("krx", "shares", "quarantine.parquet")
    rejected.to_parquet(quarantine, index=False)
    config = json.loads(manifest.read_text("utf-8"))
    config["sources"][0]["quarantine"] = {"path": quarantine.relative_to(lake.root).as_posix(),
        "sha256": hashlib.sha256(quarantine.read_bytes()).hexdigest()}
    manifest.write_text(json.dumps(config), "utf-8")
    if evidence_change == "different_bytes":
        quarantine.write_bytes(quarantine.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="quarantine"):
        normalize_shares(str(raw.parent / "*.csv"))

    assert published.read_bytes() == before


def test_refresh_combines_only_registered_dates_from_recent_raw_sources(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    _, manifest = _register(lake, [
        ("005930", "2013-01-01", 90, 10, 900),
        ("005930", "2013-01-03", 110, 10, 1100),
    ])
    registration = json.loads(manifest.read_text("utf-8"))
    registration["sources"][0].update(start_date="2013-01-03", end_date="2013-01-03")
    listing = lake.bronze("finance-datareader", "krx-listing", "2013-01-04.csv")
    listing.parent.mkdir(parents=True)
    listing.write_text("Code,Close,Stocks,Marcap\n005930,120,10,1200\n", encoding="utf-8")
    registration["sources"].append(dict(year=2013, format="fdr_listing_csv", observation_date="2013-01-04",
        path=listing.relative_to(lake.root).as_posix(), sha256=hashlib.sha256(listing.read_bytes()).hexdigest()))
    manifest.write_text(json.dumps(registration), encoding="utf-8")
    actual = normalize_shares(str(raw.parent / "*.csv"))
    assert actual.trade_date.dt.strftime("%Y-%m-%d").tolist() == ["2013-01-02", "2013-01-03", "2013-01-04", "2026-09-04"]
    assert actual.market_cap.tolist() == [1000, 1100, 1200, 2400]


def test_added_historical_share_input_reports_its_actual_recalculation_start(tmp_path, monkeypatch):
    lake, raw = _lake(tmp_path, monkeypatch)
    normalize_shares(str(raw.parent / "*.csv"))
    _register(lake, [("000660", "2013-01-02", 200, 30, 6000)])

    result = normalize_shares(str(raw.parent / "*.csv"))

    report_path = result.attrs["share_input_change_report"]
    from pathlib import Path
    assert Path(report_path).is_relative_to(lake.root / "silver")
    report = json.loads(Path(report_path).read_text("utf-8"))
    assert report["status"] == "published"
    assert set(report["changed_securities"]) == {"SEC_KR_000660"}
    changed = report["changed_securities"]["SEC_KR_000660"]
    assert changed["from_date"] == changed["last_changed_date"] == "2013-01-02"
    assert (changed["added_rows"], changed["changed_rows"], changed["removed_rows"]) == (1, 0, 0)


def test_share_publication_runs_inside_the_refresh_commands_market_lock(tmp_path, monkeypatch):
    from engine.core.source_storage import SourceRefreshLock
    lake, raw = _lake(tmp_path, monkeypatch)
    with SourceRefreshLock("kr", data_lake_root=lake.root):
        result = normalize_shares(str(raw.parent / "*.csv"))
    assert result.shares.tolist() == [10, 20]
