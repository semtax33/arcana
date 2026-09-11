"""Public refresh command consumes source-pinned lifecycle review manifests."""
import hashlib
import json
from datetime import date

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_survivorship_default_storage(tmp_path, monkeypatch):
    from engine.core.paths import DataLakePaths
    from engine.workflows import survivorship
    monkeypatch.setattr(survivorship, 'DATA_LAKE', DataLakePaths(tmp_path / 'data-lake'))


def test_survivorship_user_summary_uses_gold_and_retains_incomplete_status(tmp_path):
    from engine.workflows.survivorship import run_survivorship_refresh
    result = run_survivorship_refresh(market='us', end_date='2026-09-10',
                                      download=False, load_clickhouse=False)
    path = tmp_path / 'data-lake/gold/survivorship/us/summary.json'
    assert path.exists()
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['status'] == result['status'] == 'awaiting_review'
    assert saved['coverage_complete'] is False


def test_kr_refresh_retains_dart_primary_evidence_and_separate_exchange_corroboration(tmp_path):
    from engine.workflows.survivorship import run_survivorship_refresh

    lake = tmp_path / "data-lake"
    sources = []
    for source_id, provider, day, url in [
        ("dart", "DART", "2026-01-07", "https://opendart.fss.or.kr/api/document.xml?rcept_no=20260107000001"),
        ("kind", "KIND", "2026-01-08", "https://kind.krx.co.kr/external/2026/01/08/000001/20260108000001/68051.htm"),
    ]:
        original = lake / "bronze" / provider.lower() / "synthetic_listing.html"
        original.parent.mkdir(parents=True)
        original.write_text("Synthetic fixture: an old common stock was listed in 2000 and removed on Jan 6, 2026.", encoding="utf-8")
        sources.append(dict(source_id=source_id, provider=provider, published_date=day,
            path=original.relative_to(lake).as_posix(), source_url=url,
            source_sha256=hashlib.sha256(original.read_bytes()).hexdigest()))
    manifest = lake / "silver" / "review" / "reviewed.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(dict(schema_version=1, market="kr", review_status="verified", source_root="../..",
        sources=sources, listing_episodes=[dict(episode_id="kr-old", security_id="SEC_KR_009994", issuer_id="OLD",
            symbol="009994", country="KR", exchange_code="KOSPI", security_type="common_stock", status="confirmed",
            valid_from="2000-01-01", valid_until="2026-01-06", published_date="2026-01-08",
            source_ids=["dart"], supporting_source_ids=["kind"])], events=[], entitlements=[])), encoding="utf-8")

    run_survivorship_refresh(market="kr", end_date="2026-01-09", manifest_path=manifest,
        download=False, load_clickhouse=False)

    user = json.loads((lake / "gold/survivorship/kr/listing_episodes.json").read_text("utf-8"))
    episode, = user["rows"]
    assert episode["source_ids"] == ["dart"]
    assert episode["source_url"] == sources[0]["source_url"]
    assert episode["source_sha256"] == sources[0]["source_sha256"]
    assert episode["supporting_sources"] == [dict(source_id="kind", provider="KIND", published_date="2026-01-08",
        source_url=sources[1]["source_url"], source_sha256=sources[1]["source_sha256"])]
    assert episode["published_date"] == "2026-01-08"
    assert episode["valid_until"] == "2026-01-06"
    assert user["coverage_complete"] is False


@pytest.mark.parametrize("invalid_change", ["replace_dart", "backdate", "missing_reference", "changed_original", "unofficial_domain"])
def test_kr_corroboration_rejects_invalid_evidence_without_replacing_the_user_generation(tmp_path, invalid_change):
    from engine.workflows.survivorship import run_survivorship_refresh

    lake = tmp_path / "data-lake"
    bronze = lake / "bronze" / "synthetic"
    bronze.mkdir(parents=True)
    sources = []
    for identifier, provider, day, url in [
        ("dart", "DART", "2026-01-07", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260107000001"),
        ("kind", "KIND", "2026-01-08", "https://kind.krx.co.kr/external/2026/01/08/synthetic.html"),
    ]:
        source = bronze / f"{identifier}.html"
        source.write_text("Synthetic confirmed listing-removal fixture", encoding="utf-8")
        sources.append(dict(source_id=identifier, provider=provider, published_date=day,
            path=source.relative_to(lake).as_posix(), source_url=url, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    manifest = lake / "silver" / "review" / "reviewed.json"
    manifest.parent.mkdir(parents=True)
    reviewed = dict(schema_version=1, market="kr", review_status="verified", source_root="../..", sources=sources,
        listing_episodes=[dict(episode_id="kr-old", security_id="SEC_KR_009994", issuer_id="OLD", symbol="009994",
            country="KR", exchange_code="KOSPI", security_type="common_stock", status="confirmed", valid_from="2000-01-01",
            valid_until="2026-01-06", published_date="2026-01-08", source_ids=["dart"], supporting_source_ids=["kind"])],
        events=[], entitlements=[])
    manifest.write_text(json.dumps(reviewed), encoding="utf-8")
    run_survivorship_refresh(market="kr", end_date="2026-01-09", manifest_path=manifest, download=False, load_clickhouse=False)
    gold = lake / "gold/survivorship/kr"
    previous = {path.name:path.read_bytes() for path in gold.iterdir() if path.is_file()}
    episode = reviewed["listing_episodes"][0]
    if invalid_change == "replace_dart":
        episode["source_ids"], episode["supporting_source_ids"] = ["kind"], []
    elif invalid_change == "backdate":
        episode["published_date"] = "2026-01-07"
    elif invalid_change == "missing_reference":
        episode["supporting_source_ids"] = ["absent"]
    elif invalid_change == "changed_original":
        (bronze / "kind.html").write_text("A different original response", encoding="utf-8")
    elif invalid_change == "unofficial_domain":
        sources[1]["source_url"] = "https://example.test/unofficial-notice.html"
    manifest.write_text(json.dumps(reviewed), encoding="utf-8")
    with pytest.raises(ValueError):
        run_survivorship_refresh(market="kr", end_date="2026-01-09", manifest_path=manifest, download=False, load_clickhouse=False)
    assert {path.name:path.read_bytes() for path in gold.iterdir() if path.is_file()} == previous


@pytest.mark.parametrize("reader_mode", ["cached", "csv"])
def test_dart_reviewed_kr_history_restores_pinned_marcap_prices_and_capitalization(tmp_path, monkeypatch, reader_mode):
    import pandas as pd
    from engine.workflows import refresh
    evidence = tmp_path / "dart.html"
    evidence.write_text("Synthetic DART fixture: common shares listed in 1980; cash exchange occurred Jan 6, 2026.", encoding="utf-8")
    market_data = tmp_path / "market.parquet"
    pd.DataFrame([
        {"Code": "9993" if day == "2026-01-02" else "009993", "Date": pd.Timestamp(day), "Open": close, "High": close,
         "Low": close, "Close": close, "Volume": 100, "Stocks": 1000,
         "Marcap": close * 1000, "Market": "KOSPI"}
        for day, close in [("2026-01-02", 10.), ("2026-01-05", 12.), ("2026-01-07", 999.)]
    ]).to_parquet(market_data, index=False)
    raw = market_data.read_bytes()
    blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps({"schema_version": 1, "market": "kr", "review_status": "verified",
        "sources": [
            {"source_id": "dart", "path": evidence.name, "provider": "DART", "published_date": "2026-01-06",
             "source_url": "https://opendart.fss.or.kr/api/document.xml?rcept_no=20260106000001",
             "source_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()},
            {"source_id": "market", "path": market_data.name, "provider": "MARCAP", "published_date": "2026-01-08",
             "source_url": f"https://api.github.com/repos/FinanceData/marcap/git/blobs/{blob}",
             "source_sha256": hashlib.sha256(raw).hexdigest()}],
        "listing_episodes": [{"episode_id": "kr-old", "security_id": "SEC_KR_009993", "issuer_id": "OLD",
            "symbol": "009993", "country": "KR", "exchange_code": "KOSPI", "security_type": "common_stock",
            "valid_from": "1980-01-01", "valid_until": "2026-01-06", "status": "confirmed",
            "published_date": "2026-01-06", "source_ids": ["dart"]}],
        "events": [], "entitlements": [],
        "market_data_sources": [{"security_id": "SEC_KR_009993", "symbol": "009993",
            "identity_status": "verified", "episode_ids": ["kr-old"], "source_ids": ["market"]}]
    }), encoding="utf-8")
    from engine.core.paths import DataLakePaths
    from engine.loaders import factors
    from engine.transformers import factors as metrics
    lake = DataLakePaths(tmp_path / "lake")
    output, panels = lake.silver("survivorship", "kr"), lake.silver("corporate_actions", "prices", "kr")
    panels.mkdir(parents=True)
    (panels / "kr_009993.metadata.json").write_text(json.dumps({
        "source_sha256": "a" * 64, "unresolved_extreme_moves": 3,
        "official_vendor_discrepancies": [{"reason": "unresolved ratio"}], "coverage_complete": False}), encoding="utf-8")
    monkeypatch.setattr(factors, "DATA_LAKE", lake)
    monkeypatch.setattr(metrics, "DATA_LAKE", lake)
    monkeypatch.setattr(metrics, "SHARES_PATH", tmp_path / "no-shares.csv")
    monkeypatch.setattr(metrics, "LEGACY_SHARES_PATHS", ())
    args = refresh.build_arg_parser().parse_args([
        "--market", "kr", "--targets", "survivorship", "--end-date", "2026-01-07",
        "--survivorship-manifest", str(manifest), "--survivorship-output", str(output),
        "--survivorship-panel-dir", str(panels), "--survivorship-no-download", "--skip-clickhouse"])
    refresh.run_refresh(args)
    restored = pd.read_parquet(panels / "kr_009993.parquet")
    metadata = json.loads((panels / "kr_009993.metadata.json").read_text(encoding="utf-8"))
    assert metadata["unresolved_extreme_moves"] == 3
    assert metadata["official_vendor_discrepancies"] == [{"reason": "unresolved ratio"}]
    assert metadata["coverage_complete"] is False
    assert metadata["quality_review_basis"]["source_sha256"] == "a" * 64
    assert restored.close.tolist() == [10., 12.]
    assert restored.shares.tolist() == [1000, 1000]
    user_prices = pd.read_parquet(tmp_path / 'data-lake/gold/survivorship/kr/prices/kr_009993.parquet')
    assert user_prices.close.tolist() == user_prices.adj_close.tolist() == [10., 12.]
    caps = pd.read_parquet(output / "market_cap_factors.parquet")
    assert caps.factor_value.tolist() == [.01, .012]
    assert caps.currency.tolist() == ["KRW", "KRW"]
    assert not (panels.parent / "us_disclosed_shares.parquet").exists()
    dividends = tmp_path / "empty-dividends.csv"
    pd.DataFrame(columns=["security_id", "trade_date", "dividend"]).to_csv(dividends, index=False)
    calculated = factors.create_daily_factor_rows(
        stock_codes=["009993"], market="kr", financial_basis="annual", factor_ids=["mcap_mil"],
        start_date="2026-01-02", end_date="2026-01-05", reader_mode=reader_mode,
        dividend_path=dividends, financial_dir=tmp_path / "no-financials",
        report_metadata_path=tmp_path / "no-reports.csv", wacc_online_backfill=False)
    assert calculated.factor_value.tolist() == [.01, .012]


def test_refresh_target_publishes_closed_episode_once_from_verified_sources(tmp_path):
    from engine.workflows import refresh

    listing = tmp_path / "listing.csv"
    listing.write_text(
        "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
        "OLD,Historical Company,NYSE,Stock,2000-01-01,null,Active\n", encoding="utf-8")
    event = tmp_path / "merger.html"
    event.write_text("Synthetic fixture: OLD merged on 2026-01-06 for USD 120 per common share, paid that day.", encoding="utf-8")
    listing_hash = hashlib.sha256(listing.read_bytes()).hexdigest()
    event_hash = hashlib.sha256(event.read_bytes()).hexdigest()
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "market": "us", "review_status": "verified",
        "sources": [
            {"source_id": "listing", "path": "listing.csv", "provider": "ALPHA_VANTAGE",
             "source_url": "https://www.alphavantage.co/query?function=LISTING_STATUS&date=2026-01-02&state=active",
             "source_sha256": listing_hash, "published_date": "2026-01-02"},
            {"source_id": "merger", "path": "merger.html", "provider": "SEC",
             "source_url": "https://www.sec.gov/Archives/edgar/data/1/synthetic-fixture.html",
             "source_sha256": event_hash, "published_date": "2026-01-06"},
        ],
        "listing_episodes": [{"episode_id": "old-2000", "security_id": "SEC_US_OLD",
            "issuer_id": "OLD", "symbol": "OLD", "country": "US", "exchange_code": "NYSE",
            "security_type": "common_stock", "valid_from": "2000-01-01", "valid_until": None,
            "status": "confirmed", "published_date": "2026-01-02", "source_ids": ["listing"]}],
        "events": [{"event_id": "old-merger", "security_id": "SEC_US_OLD", "event_type": "cash_merger",
            "effective_date": "2026-01-06", "cash_per_share": 120, "cash_payment_date": "2026-01-06",
            "currency": "USD", "status": "confirmed", "entitlements_complete": True,
            "published_date": "2026-01-06", "source_ids": ["merger"]}],
        "entitlements": [],
    }), encoding="utf-8")
    output = tmp_path / "silver"
    args = refresh.build_arg_parser().parse_args([
        "--market", "us", "--targets", "survivorship", "--end-date", "2026-01-07",
        "--survivorship-manifest", str(manifest), "--survivorship-output", str(output),
        "--survivorship-no-download", "--skip-clickhouse",
    ])

    refresh.run_refresh(args)
    refresh.run_refresh(args)

    episodes = json.loads((output / "listing_episodes.json").read_text(encoding="utf-8"))
    events = json.loads((output / "events.json").read_text(encoding="utf-8"))
    assert len(episodes["rows"]) == len(events["rows"]) == 1
    assert episodes["rows"][0]["valid_until"] == "2026-01-06"
    assert set(episodes["rows"][0]["source_ids"]) == {"listing", "merger"}
    assert events["rows"][0]["source_sha256"] == event_hash

    before = (output / "events.json").read_bytes()
    event.write_text("Changed evidence must invalidate the review.", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        refresh.run_refresh(args)
    assert (output / "events.json").read_bytes() == before


def test_clickhouse_publication_replaces_one_market_atomically_without_duplicate_rows():
    from datetime import date
    from uuid import uuid4
    import pandas as pd
    from engine.core.clickhouse import get_clickhouse_client
    from engine.loaders.survivorship import load_survivorship

    client = get_clickhouse_client()
    prefix = "test_survivorship_" + uuid4().hex + "_"
    bundle = {"listing_episodes": [{
        "episode_id": "old", "security_id": "SEC_US_OLD", "issuer_id": "OLD", "symbol": "OLD",
        "country": "US", "exchange_code": "NYSE", "security_type": "common_stock",
        "valid_from": "1956-07-02", "valid_until": "2026-01-06", "status": "confirmed",
        "published_date": "2020-01-01", "source_url": "https://www.sec.gov/fixture", "source_sha256": "a" * 64,
    }], "events": [], "entitlements": [], "sources": [], "unresolved": []}
    try:
        load_survivorship(bundle, market="us", client=client, table_prefix=prefix)
        assert client.query(f"SELECT valid_from FROM {prefix}security_listing_episodes").result_rows == [(date(1956, 7, 2),)]
        # Simulate an existing installation using the old Date projection.
        client.command(f"""CREATE OR REPLACE VIEW {prefix}security_listing_episodes AS
            SELECT toDate(JSONExtractString(payload, 'valid_from')) AS valid_from
            FROM {prefix}survivorship_rows WHERE kind='listing_episodes'""")
        unchanged = load_survivorship(bundle, market="us", client=client, table_prefix=prefix)
        assert unchanged["status"] == "unchanged"
        assert client.query(f"SELECT valid_from FROM {prefix}security_listing_episodes").result_rows == [(date(1956, 7, 2),)]
        assert client.query(f"SELECT security_id, valid_until FROM {prefix}security_listing_episodes").result_rows == [
            ("SEC_US_OLD", date(2026, 1, 6))]
        kr = dict(bundle, listing_episodes=[dict(bundle["listing_episodes"][0],
            episode_id="kr-old", security_id="SEC_KR_OLD", country="KR")])
        load_survivorship(kr, market="kr", client=client, table_prefix=prefix)
        replacement = dict(bundle, listing_episodes=[dict(bundle["listing_episodes"][0], valid_until="2026-01-05")])
        load_survivorship(replacement, market="us", client=client, table_prefix=prefix)
        assert client.query(f"SELECT country, valid_until FROM {prefix}security_listing_episodes ORDER BY country").result_rows == [
            ("KR", date(2026, 1, 6)), ("US", date(2026, 1, 5))]
        for invalid_start in ("invalid-date", "1899-12-31", "2300-01-01"):
            invalid = dict(bundle, listing_episodes=[dict(bundle["listing_episodes"][0], valid_from=invalid_start)])
            with pytest.raises(Exception, match="(parse|date|Date|convert)"):
                load_survivorship(invalid, market="us", client=client, table_prefix=prefix)
        assert client.query(f"SELECT country, valid_until FROM {prefix}security_listing_episodes ORDER BY country").result_rows == [
            ("KR", date(2026, 1, 6)), ("US", date(2026, 1, 5))]
        client.command(f"""CREATE TABLE {prefix}price_daily (
            security_id String, trade_date Date, open Float64, high Float64, low Float64,
            close Float64, adj_close Float64, volume UInt64, currency String,
            updated_at DateTime64(3) DEFAULT now64(3)
        ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (security_id, trade_date)""")
        prices = [({"security_id": "SEC_US_OLD", "source_sha256": "b" * 64}, pd.DataFrame([
            {"security_id": "SEC_US_OLD", "trade_date": "2026-01-02", "open": 100., "high": 100., "low": 100.,
             "close": 100., "split_adj_close": 50., "volume": 100, "currency": "USD"},
            {"security_id": "SEC_US_OLD", "trade_date": "2026-01-05", "open": 60., "high": 60., "low": 60.,
             "close": 60., "split_adj_close": 60., "volume": 200, "currency": "USD"},
        ]))]
        load_survivorship(bundle, market="us", client=client, table_prefix=prefix, prices=prices)
        load_survivorship(bundle, market="us", client=client, table_prefix=prefix, prices=prices)
        review_update = dict(bundle, unresolved=[{"scope": "fixture", "reason": "New review note; prices are unchanged."}])
        publication = load_survivorship(review_update, market="us", client=client, table_prefix=prefix, prices=prices)
        assert publication["price_rows_written"] == 0
        assert client.query(f"SELECT close, adj_close FROM {prefix}price_daily ORDER BY trade_date").result_rows == [(100., 50.), (60., 60.)]
        client.command(f"""CREATE TABLE {prefix}fact_daily_factors (
            security_id String, trade_date Date, factor_id String, financial_basis String,
            factor_value Float64, fiscal_year Nullable(Int32), financial_period Nullable(String),
            currency String, updated_at DateTime64(3)
        ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (security_id, trade_date, factor_id, financial_basis)""")
        from engine.loaders.factors import prepare_daily_factor_rows
        cap_input = pd.DataFrame([{"security_id": "SEC_US_OLD", "trade_date": "2026-01-05", "mcap_mil": .012, "currency": "USD"}])
        caps = prepare_daily_factor_rows(cap_input, financial_basis="annual", factor_ids=["mcap_mil"])
        publication = load_survivorship(bundle, market="us", client=client, table_prefix=prefix, prices=prices, market_cap_factors=caps)
        assert publication["market_cap_rows_written"] == 1
        caps["updated_at"] = pd.Timestamp("2026-02-01")
        publication = load_survivorship(review_update, market="us", client=client, table_prefix=prefix, prices=prices, market_cap_factors=caps)
        assert publication["market_cap_rows_written"] == 0
        assert publication["price_rows_written"] == 0
        assert client.query(f"SELECT factor_id, factor_value, currency FROM {prefix}fact_daily_factors").result_rows == [("mcap_mil", .012, "USD")]
        caps["currency"] = "KRW"
        with pytest.raises(ValueError, match="market.cap"):
            load_survivorship(bundle, market="us", client=client, table_prefix=prefix, market_cap_factors=caps)
    finally:
        for name in ("security_listing_episodes", "security_lifecycle_events", "security_lifecycle_entitlements", "security_trading_halts", "survivorship_rows", "survivorship_publications", "price_daily", "fact_daily_factors"):
            client.command(f"DROP TABLE IF EXISTS {prefix}{name}")
        client.close()


def test_refresh_downloads_both_us_listing_states_without_promoting_unreviewed_identities(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.workflows import refresh
    import requests

    calls = []
    def provider_response(url, *, params, timeout):
        assert params["function"] == "LISTING_STATUS"
        calls.append(params["state"])
        return SimpleNamespace(status_code=200, content=(
            "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
            "REUSE,Old Issuer,NYSE,Stock,2000-01-01,2020-01-01,Delisted\n"
        ).encode())
    monkeypatch.setattr(requests, "get", provider_response)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "synthetic-key")
    args = refresh.build_arg_parser().parse_args([
        "--market", "us", "--targets", "survivorship", "--end-date", "2026-01-07",
        "--survivorship-source-dir", str(tmp_path / "bronze"),
        "--survivorship-output", str(tmp_path / "silver"),
        "--survivorship-manifest", str(tmp_path / "not-yet-reviewed.json"), "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    assert sorted(calls) == ["active", "delisted"]
    summary = json.loads((tmp_path / "silver" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "awaiting_review"
    assert not (tmp_path / "silver" / "listing_episodes.json").exists()
    assert (tmp_path / "bronze" / "snapshot_date=2026-01-07" / "delisted.csv").exists()


def test_us_refresh_reports_changed_provider_delisting_date_and_preserves_snapshots(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import requests
    from engine.workflows import refresh

    header = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
    originals = {}
    calls = []

    def provider_response(url, *, params, timeout):
        day, state = params["date"], params["state"]
        calls.append((day, state))
        if state == "active":
            body = "LIVE,Listed Issuer,NYSE,Stock,2000-01-01,,Active\n"
        else:
            terminal = "2026-01-05" if day == "2026-01-06" else "2026-01-06"
            body = (f"MOVE,Former Issuer,NYSE,Stock,2000-01-01,{terminal},Delisted\n"
                    "FIXED,Stable Issuer,NASDAQ,Stock,2001-01-01,2020-01-02,Delisted\n")
        raw = (header + body).encode()
        originals[day, state] = raw
        return SimpleNamespace(status_code=200, content=raw)

    monkeypatch.setattr(requests, "get", provider_response)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "synthetic-key")
    lake = tmp_path / "data-lake"

    def run(day):
        args = refresh.build_arg_parser().parse_args([
            "--market", "us", "--targets", "survivorship", "--end-date", day,
            "--survivorship-source-dir", str(lake / "bronze/alpha-vantage/listings"),
            "--survivorship-output", str(lake / "silver/survivorship/us"),
            "--survivorship-manifest", str(lake / "silver/not-reviewed.json"), "--skip-clickhouse",
        ])
        refresh.run_refresh(args)
        return json.loads((lake / "gold/survivorship/us/summary.json").read_text("utf-8"))

    run("2026-01-06")
    summary = run("2026-01-07")
    quality = json.loads((lake / "gold/survivorship/us/listing_source_quality.json").read_text("utf-8"))
    assert summary["listing_source_quality"]["sha256"] == hashlib.sha256(
        (lake / "gold/survivorship/us/listing_source_quality.json").read_bytes()).hexdigest()
    assert quality["status"] == "source_observations_require_review"
    assert quality["previous_snapshot_dates"]["delisted"] == "2026-01-06"
    assert quality["changed_delisting_dates"] == [{
        "symbol": "MOVE", "exchange": "NYSE", "assetType": "Stock", "ipoDate": "2000-01-01",
        "previous_delisting_date": "2026-01-05", "current_delisting_date": "2026-01-06",
    }]
    assert quality["coverage_complete"] is False
    assert summary["status"] == "awaiting_review"
    assert not (lake / "gold/survivorship/us/listing_episodes.json").exists()
    for (day, state), raw in originals.items():
        path = lake / f"bronze/alpha-vantage/listings/snapshot_date={day}/{state}.csv"
        assert path.read_bytes() == raw
    before = (lake / "gold/survivorship/us/listing_source_quality.json").read_bytes()
    run("2026-01-07")
    assert len(calls) == 4
    assert (lake / "gold/survivorship/us/listing_source_quality.json").read_bytes() == before
    assert list((lake / "silver/survivorship/us/listing_source_quality").rglob("audit.json"))


def test_us_refresh_separates_name_changes_duplicate_rows_and_conflicting_listing_states(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import requests
    from engine.extractors.alpha_vantage_prices import download_alpha_vantage_listings
    from engine.workflows import refresh

    header = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
    duplicate = "DUP,Same Issuer,NYSE,Stock,2000-01-01,,Active\n"
    overlap = "BOTH,Overlap Issuer,NYSE,Stock,2000-01-01"
    source_rows = {
        ("2026-06-30", "active"): duplicate * 2,
        ("2026-09-09", "delisted"): (
            "NAME,Old Fund,NYSE ARCA,ETF,2014-10-23,2020-01-02,Delisted\n"
            "AMBIG,First Issuer,NYSE,Stock,2001-01-01,2020-01-02,Delisted\n"
            "AMBIG,Second Issuer,NYSE,Stock,2001-01-01,2021-01-02,Delisted\n"),
        ("2026-09-10", "active"): overlap + ",,Active\n",
        ("2026-09-10", "delisted"): (
            "NAME,New Fund,NYSE ARCA,ETF,2014-10-23,2020-01-02,Delisted\n"
            "AMBIG,First Issuer,NYSE,Stock,2001-01-01,2022-01-02,Delisted\n"
            + overlap + ",2020-01-02,Delisted\n"),
    }

    def provider_response(url, *, params, timeout):
        return SimpleNamespace(status_code=200, content=(header + source_rows[params["date"], params["state"]]).encode())

    monkeypatch.setattr(requests, "get", provider_response)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "synthetic-key")
    lake = tmp_path / "data-lake"
    root = lake / "bronze/alpha-vantage/listings"
    download_alpha_vantage_listings(dates=["2026-06-30"], states=("active",), output_dir=root)
    download_alpha_vantage_listings(dates=["2026-09-09"], states=("delisted",), output_dir=root)
    args = refresh.build_arg_parser().parse_args([
        "--market", "us", "--targets", "survivorship", "--end-date", "2026-09-10",
        "--survivorship-source-dir", str(root),
        "--survivorship-output", str(lake / "silver/survivorship/us"),
        "--survivorship-manifest", str(lake / "silver/not-reviewed.json"), "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    quality = json.loads((lake / "gold/survivorship/us/listing_source_quality.json").read_text("utf-8"))
    assert quality["previous_snapshot_dates"] == {"active": "2026-06-30", "delisted": "2026-09-09"}
    assert quality["changed_historical_names"] == [{
        "symbol": "NAME", "exchange": "NYSE ARCA", "assetType": "ETF", "ipoDate": "2014-10-23",
        "previous_name": "Old Fund", "current_name": "New Fund",
    }]
    assert quality["same_provider_key_in_active_and_delisted"] == [{
        "symbol": "BOTH", "exchange": "NYSE", "assetType": "Stock", "ipoDate": "2000-01-01",
    }]
    assert quality["changed_delisting_dates"] == []  # Ambiguous prior rows cannot select a date.
    assert quality["ambiguous_provider_keys"] == [{
        "symbol": "AMBIG", "exchange": "NYSE", "assetType": "Stock", "ipoDate": "2001-01-01",
        "snapshot_date": "2026-09-09", "state": "delisted", "distinct_records": 2,
    }]
    assert quality["duplicate_records"] == [{
        "symbol": "DUP", "exchange": "NYSE", "assetType": "Stock", "ipoDate": "2000-01-01",
        "name": "Same Issuer", "delistingDate": "", "status": "Active",
        "snapshot_date": "2026-06-30", "state": "active", "occurrences": 2,
    }]
    assert (root / "snapshot_date=2026-06-30/active.csv").read_text("utf-8") == header + duplicate * 2
    assert quality["coverage_complete"] is False
    assert not (lake / "gold/survivorship/us/events.json").exists()


def test_dart_refresh_retains_actual_receipt_date_and_unavailable_response(tmp_path, monkeypatch):
    import requests
    from engine.workflows import refresh

    def provider_response(self, method, url, **kwargs):
        params = kwargs["params"]
        response = requests.Response()
        response.status_code = 200
        if url.endswith("list.json"):
            if params["pblntf_detail_ty"] == "I003":
                payload = {"status": "000", "total_count": 1, "total_page": 1, "list": [{
                    "rcept_no": "20170202000370", "rcept_dt": "20170203", "corp_code": "00123456",
                    "stock_code": "035480", "report_nm": "[정정]상장폐지", "corp_cls": "K", "corp_name": "Fixture"}]}
            else:
                payload = {"status": "013", "message": "조회된 데이터가 없습니다."}
            response._content = json.dumps(payload).encode()
        else:
            assert url.endswith("document.xml")
            response._content = b"<result><status>014</status><message>unavailable</message></result>"
        return response
    monkeypatch.setattr(requests.Session, "request", provider_response)
    monkeypatch.setenv("DART_API_KEY", "synthetic-key")
    args = refresh.build_arg_parser().parse_args([
        "--market", "kr", "--targets", "survivorship", "--end-date", "2017-02-03",
        "--survivorship-start-date", "2017-02-03", "--survivorship-source-dir", str(tmp_path / "bronze"),
        "--survivorship-output", str(tmp_path / "silver"),
        "--survivorship-manifest", str(tmp_path / "not-yet-reviewed.json"), "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    report = json.loads((tmp_path / "bronze" / "collection_report.json").read_text(encoding="utf-8"))
    candidate = report["candidates"][0]
    assert candidate["published_date"] == "2017-02-03"
    assert candidate["review_status"] == "pending"
    assert candidate["document_status"] == "014"
    raw = (tmp_path / "bronze" / candidate["document_path"]).read_bytes()
    assert b"<status>014</status>" in raw
    assert hashlib.sha256(raw).hexdigest() == candidate["document_sha256"]


def test_dart_refresh_collects_liquidation_and_dissolution_without_approving_payouts(tmp_path, monkeypatch):
    import io
    from zipfile import ZipFile
    import requests
    from engine.workflows import refresh

    documents = {}
    for receipt in ("20260608900348", "20260618000252"):
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(receipt + ".xml", "<DOCUMENT>Synthetic fixture: liquidation distribution remains planned.</DOCUMENT>")
        documents[receipt] = buffer.getvalue()
    calls = []

    def provider_response(self, method, url, **kwargs):
        params = kwargs["params"]
        calls.append(url.rsplit("/", 1)[-1])
        response = requests.Response()
        response.status_code = 200
        if url.endswith("list.json"):
            fixtures = {
                "I001": ("20260608900348", "20260608", "기타경영사항(자율공시) (청산관련 주요사항 안내)"),
                "B001": ("20260618000252", "20260618", "주요사항보고서(해산사유발생)"),
            }
            if params["pblntf_detail_ty"] in fixtures:
                receipt, day, title = fixtures[params["pblntf_detail_ty"]]
                payload = dict(status="000", total_count=1, total_page=1, list=[dict(
                    rcept_no=receipt, rcept_dt=day, corp_code="01775952", stock_code="464440",
                    report_nm=title, corp_cls="K", corp_name="Synthetic liquidation issuer")])
            else:
                payload = dict(status="013", message="조회된 데이터가 없습니다.")
            response._content = json.dumps(payload, ensure_ascii=False).encode()
        else:
            assert url.endswith("document.xml")
            response._content = documents[params["rcept_no"]]
        return response

    monkeypatch.setattr(requests.Session, "request", provider_response)
    monkeypatch.setenv("DART_API_KEY", "synthetic-key")
    lake = tmp_path / "data-lake"
    args = refresh.build_arg_parser().parse_args([
        "--market", "kr", "--targets", "survivorship", "--end-date", "2026-06-30",
        "--survivorship-start-date", "2026-06-01", "--survivorship-source-dir", str(lake / "bronze/dart"),
        "--survivorship-output", str(lake / "silver/survivorship/kr"),
        "--survivorship-manifest", str(lake / "silver/not-yet-reviewed.json"), "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    report_path = lake / "bronze/dart/collection_report.json"
    report = json.loads(report_path.read_text("utf-8"))
    candidates = {row["source_id"]: row for row in report["candidates"]}
    assert set(candidates) == {"20260608900348", "20260618000252"}
    for receipt, candidate in candidates.items():
        assert candidate["document_status"] == "available"
        assert candidate["review_status"] == "pending"
        raw = (lake / "bronze/dart" / candidate["document_path"]).read_bytes()
        assert raw == documents[receipt]
        assert candidate["document_sha256"] == hashlib.sha256(raw).hexdigest()
    assert candidates["20260608900348"]["published_date"] == "2026-06-08"
    assert candidates["20260618000252"]["published_date"] == "2026-06-18"
    gold = lake / "gold/survivorship/kr"
    summary = json.loads((gold / "summary.json").read_text("utf-8"))
    assert summary["status"] == "awaiting_review"
    assert summary["coverage_complete"] is False
    assert not (gold / "events.json").exists()
    assert not (gold / "listing_episodes.json").exists()
    assert calls.count("document.xml") == 2
    previous_calls = len(calls)
    refresh.run_refresh(args)
    assert len(calls) == previous_calls
    assert json.loads(report_path.read_text("utf-8"))["candidates"] == report["candidates"]


@pytest.mark.parametrize("title", ["투자유의안내", "기타시장안내 (상장적격성 실질심사 사유 발생 안내)", "주권매매거래정지"])
def test_dart_refresh_retains_halt_review_notices_without_approving_a_halt_interval(tmp_path, monkeypatch, title):
    import io
    from zipfile import ZipFile
    import requests
    from engine.workflows import refresh

    receipt = "20240202800801"
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(receipt + ".xml", "<DOCUMENT>Synthetic notice: review exact execution halt timing.</DOCUMENT>")
    raw_document = buffer.getvalue()

    def response(self, method, url, **kwargs):
        params = kwargs["params"]
        result = requests.Response()
        result.status_code = 200
        if url.endswith("list.json"):
            payload = dict(status="013")
            if params["pblntf_detail_ty"] == "I003":
                payload = dict(status="000", total_count=1, total_page=1, list=[dict(
                    rcept_no=receipt, rcept_dt="20240202", corp_code="00860730", stock_code="140910",
                    report_nm=title, corp_cls="Y", corp_name="Synthetic halt issuer")])
            result._content = json.dumps(payload, ensure_ascii=False).encode()
        else:
            assert url.endswith("document.xml") and params["rcept_no"] == receipt
            result._content = raw_document
        return result

    monkeypatch.setattr(requests.Session, "request", response)
    monkeypatch.setenv("DART_API_KEY", "synthetic-key")
    lake = tmp_path / "data-lake"
    source_root = lake / "bronze/dart/listings"
    args = refresh.build_arg_parser().parse_args([
        "--market", "kr", "--targets", "survivorship", "--end-date", "2024-02-02",
        "--survivorship-start-date", "2024-02-02", "--survivorship-source-dir", str(source_root),
        "--survivorship-manifest", str(lake / "silver/not-reviewed.json"), "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    report = json.loads((source_root / "collection_report.json").read_text("utf-8"))
    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    assert candidate["review_status"] == "pending" and candidate["document_status"] == "available"
    assert (source_root / candidate["document_path"]).read_bytes() == raw_document
    assert not (lake / "gold/survivorship/kr/trading_halts.json").exists()


def test_regular_market_data_cli_includes_survivorship_stage(tmp_path, monkeypatch, capsys):
    import sys
    from engine.workflows import refresh
    monkeypatch.setattr(sys, "argv", [
        "refresh", "--market", "us", "--targets", "market-data", "--symbols", "TEST",
        "--end-date", "2026-01-07", "--dry-run", "--skip-clickhouse", "--no-resume",
        "--resume-state-path", str(tmp_path / "resume.json"),
    ])
    refresh.main()
    output = capsys.readouterr().out
    assert "[DRY-RUN] US market-data" in output
    assert "[DRY-RUN] survivorship market=us" in output


def test_reviewed_dart_delisting_closes_listing_but_does_not_zero_private_shares(tmp_path):
    from pathlib import Path
    from engine.workflows import refresh
    manifest = Path(__file__).resolve().parents[1] / "data-lake/meta/survivorship/kr_reviewed.json"
    if not manifest.exists():
        pytest.skip("Retained DART evidence and reviewed local manifest are required")
    args = refresh.build_arg_parser().parse_args([
        "--market", "kr", "--targets", "survivorship", "--end-date", "2026-09-10",
        "--survivorship-manifest", str(manifest), "--survivorship-output", str(tmp_path),
        "--survivorship-panel-dir", str(tmp_path / "prices"),
        "--survivorship-no-download", "--skip-clickhouse",
    ])
    refresh.run_refresh(args)
    episode = json.loads((tmp_path / "listing_episodes.json").read_text(encoding="utf-8"))["rows"][0]
    event = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))["rows"][0]
    assert episode["valid_until"] == "2019-12-12"
    assert event["cash_per_share"] is None
    assert event["entitlements_complete"] is False


@pytest.mark.parametrize("market,symbol", [("us", "TEST"), ("kr", "005930")])
def test_all_refresh_restores_historical_inputs_before_factor_computation(tmp_path, monkeypatch, capsys, market, symbol):
    import sys
    from engine.workflows import refresh
    if market == "kr":
        from pykrx import stock
        from engine.core.paths import DataLakePaths
        monkeypatch.setattr(refresh, "DATA_LAKE", DataLakePaths(tmp_path / "data-lake"))
        monkeypatch.setattr(refresh.dividend_loader, "silver_dividend_dir", tmp_path / "dividends")
        monkeypatch.setattr(stock, "get_nearest_business_day_in_a_week", lambda requested, prev: requested)
    monkeypatch.setattr(sys, "argv", [
        "refresh", "--market", market, "--targets", "all", "--symbols", symbol,
        "--end-date", "2026-01-07", "--dry-run", "--skip-clickhouse", "--no-resume",
        "--resume-state-path", str(tmp_path / "resume.json"),
    ])
    refresh.main()
    output = capsys.readouterr().out
    assert output.count(f"[DRY-RUN] survivorship market={market}") == 1
    assert output.index(f"[DRY-RUN] survivorship market={market}") < output.index("[SKIP] factors require ClickHouse")


def test_reviewed_historical_alpha_prices_and_shares_use_split_units_and_available_dates(tmp_path, monkeypatch):
    import pandas as pd
    from engine.workflows import refresh
    listing = tmp_path / "listing.csv"
    listing.write_text("symbol,name,exchange,assetType,ipoDate,delistingDate,status\nOLD,Historical Company,NYSE,Stock,2000-01-01,null,Active\n", encoding="utf-8")
    raw = tmp_path / "prices.json"
    raw.write_text(json.dumps({"Meta Data": {"2. Symbol": "OLD"}, "Time Series (Daily)": {
        "2026-01-05": {"1. open": "100", "2. high": "100", "3. low": "100", "4. close": "100",
                       "5. adjusted close": "49", "6. volume": "100", "7. dividend amount": "0", "8. split coefficient": "1"},
        "2026-01-06": {"1. open": "60", "2. high": "60", "3. low": "60", "4. close": "60",
                       "5. adjusted close": "59", "6. volume": "200", "7. dividend amount": "1", "8. split coefficient": "2"},
    }}), encoding="utf-8")
    manifest = tmp_path / "reviewed.json"
    manifest.write_text(json.dumps({"schema_version": 1, "market": "us", "review_status": "verified",
        "sources": [
            {"source_id": "listing", "path": "listing.csv", "provider": "ALPHA_VANTAGE", "published_date": "2026-01-02",
             "source_url": "https://www.alphavantage.co/query?function=LISTING_STATUS&date=2026-01-02&state=active",
             "source_sha256": hashlib.sha256(listing.read_bytes()).hexdigest()},
            {"source_id": "prices", "path": "prices.json", "provider": "ALPHA_VANTAGE", "published_date": "2026-01-07",
             "source_url": "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol=OLD",
             "source_sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}],
        "listing_episodes": [{"episode_id": "old-2000", "security_id": "SEC_US_OLD", "issuer_id": "OLD", "symbol": "OLD",
            "country": "US", "exchange_code": "NYSE", "security_type": "common_stock", "valid_from": "2000-01-01",
            "valid_until": None, "status": "confirmed", "published_date": "2026-01-02", "source_ids": ["listing"]}],
        "events": [], "entitlements": [],
        "price_sources": [{"security_id": "SEC_US_OLD", "symbol": "OLD", "source_id": "prices",
                           "identity_status": "verified", "episode_ids": ["old-2000"]}],
    }), encoding="utf-8")
    output = tmp_path / "silver"
    args = refresh.build_arg_parser().parse_args([
        "--market", "us", "--targets", "survivorship", "--end-date", "2026-01-07",
        "--survivorship-manifest", str(manifest), "--survivorship-output", str(output),
        "--survivorship-panel-dir", str(tmp_path / "ready-prices"),
        "--survivorship-no-download", "--skip-clickhouse"])
    refresh.run_refresh(args)
    panel = pd.read_parquet(output / "prices" / "us_OLD.parquet")
    assert panel.close.tolist() == [100, 60]
    assert panel.split_adj_close.tolist() == [50, 60]
    assert panel.vendor_adj_close.tolist() == [49, 59]
    ready = json.loads((tmp_path / "ready-prices" / "us_OLD.metadata.json").read_text(encoding="utf-8"))
    assert ready["status"] == "ready"
    assert (tmp_path / "ready-prices" / "us_OLD.parquet").exists()
    rebuild = json.loads((tmp_path / "us_price_rebuild_required.json").read_text(encoding="utf-8"))
    assert rebuild["items"]["SEC_US_OLD"]["from_date"] == "2026-01-05"
    assert rebuild["items"]["SEC_US_OLD"]["completed_bases"] == []
    shares_path = tmp_path / "shares.json"
    shares_path.write_text(json.dumps({"cik": 123, "facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [{
            "end": "2026-01-05", "filed": "2026-01-05", "val": 100, "form": "10-Q", "accn": "0000000123-26-000001"
        }]}}}, "us-gaap": {"WeightedAverageNumberOfDilutedSharesOutstanding": {
            "units": {"shares": [{"end": "2026-01-05", "filed": "2026-01-05", "val": 999999, "form": "10-Q", "accn": "0000000123-26-000001"}]}}}}}), encoding="utf-8")
    reviewed = json.loads(manifest.read_text(encoding="utf-8"))
    reviewed["listing_episodes"][0]["cik"] = "0000000123"
    reviewed["sources"].append({"source_id": "shares", "path": "shares.json", "provider": "SEC", "published_date": "2026-01-05",
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json", "source_sha256": hashlib.sha256(shares_path.read_bytes()).hexdigest()})
    reviewed["share_sources"] = [{"security_id": "SEC_US_OLD", "cik": "0000000123", "source_id": "shares",
                                   "single_common_class_confirmed": True}]
    manifest.write_text(json.dumps(reviewed), encoding="utf-8")
    refresh.run_refresh(args)
    shares = pd.read_parquet(output / "shares" / "us_OLD.parquet")
    # Publication time is unknown: the Jan 5 filing is usable from Jan 6.
    # Jan 6 has a 2-for-1 split, so the disclosed 100 shares become 200.
    assert pd.isna(shares.shares.iloc[0])
    assert shares.shares.iloc[1] == 200
    assert shares.market_cap.iloc[1] == 12000
    factor_observations = pd.read_parquet(tmp_path / "us_disclosed_shares.parquet")
    assert str(factor_observations.trade_date.iloc[0].date()) == "2026-01-06"
    assert factor_observations.shares.iloc[0] == 100
    caps = pd.read_parquet(output / "market_cap_factors.parquet")
    assert caps.factor_id.tolist() == ["mcap_mil"]
    assert caps.factor_value.tolist() == [.012]
    assert caps.currency.tolist() == ["USD"]
    # The regular factor loader must discover the restored issuer even though
    # no normalized financial statement exists for its retired ticker.
    from engine.core.paths import DataLakePaths
    from engine.loaders import factors
    lake = DataLakePaths(tmp_path / "factor-lake")
    monkeypatch.setattr(factors, "DATA_LAKE", lake)
    episodes_path = lake.silver("survivorship", "us", "listing_episodes.json")
    episodes_path.parent.mkdir(parents=True)
    episodes_path.write_bytes((output / "listing_episodes.json").read_bytes())
    price_csv, share_csv = tmp_path / "factor-prices.csv", tmp_path / "factor-shares.csv"
    panel.rename(columns={"split_adj_close": "adj_close"}).to_csv(price_csv, index=False)
    shares.to_csv(share_csv, index=False)
    dividend_csv = tmp_path / "dividends.csv"
    pd.DataFrame(columns=["security_id", "trade_date", "dividend"]).to_csv(dividend_csv, index=False)
    fallback_calls = []
    def current_ticker_provider(*args):
        fallback_calls.append(args[0])
        return []
    calculated = factors.create_daily_factor_rows(
        stock_codes=None, market="us", financial_basis="annual", factor_ids=["mcap_mil"],
        start_date="2026-01-05", end_date="2026-01-06", price_path=price_csv,
        shares_path=share_csv, dividend_path=dividend_csv,
        financial_dir=tmp_path / "no-financials", report_metadata_path=tmp_path / "no-reports.csv",
        use_edgartools=True, edgartools_provider=current_ticker_provider, wacc_online_backfill=False,
    )
    assert calculated.security_id.tolist() == ["SEC_US_OLD"]
    assert calculated.factor_value.tolist() == [.012]
    assert fallback_calls == []
    with_abstentions = factors.create_daily_factor_rows(
        stock_codes=None, market="us", financial_basis="annual", factor_ids=["mcap_mil"],
        start_date="2026-01-05", end_date="2026-01-06", price_path=price_csv,
        shares_path=share_csv, dividend_path=dividend_csv,
        financial_dir=tmp_path / "no-financials", report_metadata_path=tmp_path / "no-reports.csv",
        use_edgartools=True, edgartools_provider=current_ticker_provider, wacc_online_backfill=False,
        include_abstentions=True,
    )
    assert pd.to_datetime(with_abstentions.trade_date).dt.date.tolist() == [date(2026, 1, 5), date(2026, 1, 6)]
    assert pd.isna(with_abstentions.factor_value.iloc[0])
    assert with_abstentions.factor_value.iloc[1] == .012
    assert fallback_calls == []
