"""Source-verified listing episodes and corporate-action rights for backtests.

Provider listings are candidate identities, not automatic evidence of merger
proceeds or cancellation. Reviewed manifests bind normalized facts to retained
source bytes. Unknown rights remain explicitly incomplete.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from engine.core.paths import DATA_LAKE


def _day(value):
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text).isoformat()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_reviewed_manifest(path, *, market, end_date):
    """Validate all evidence before publishing any accepted row."""
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("market") != market
            or manifest.get("review_status") != "verified"):
        raise ValueError("Survivorship manifest requires a verified review for this market")
    source_root = (path.parent / manifest.get("source_root", ".")).resolve()
    sources = {}
    domains = {"ALPHA_VANTAGE": {"www.alphavantage.co", "alphavantage.co"},
               "DART": {"dart.fss.or.kr", "opendart.fss.or.kr"},
               "KIND": {"kind.krx.co.kr"},
               "SEC": {"www.sec.gov", "sec.gov", "data.sec.gov"}, "MARCAP": {"api.github.com"}}
    for source in manifest.get("sources", []):
        identifier = source["source_id"]
        location = (source_root / source["path"]).resolve()
        url = urlparse(source["source_url"])
        if (identifier in sources or not location.is_relative_to(source_root)
                or url.scheme != "https" or url.hostname not in domains.get(source["provider"], set())
                or {key.lower() for key in parse_qs(url.query)} & {"apikey", "crtfc_key", "api_key"}):
            raise ValueError("Invalid or duplicated survivorship source provenance")
        raw = location.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source["source_sha256"]:
            raise ValueError(f"Survivorship source SHA256 mismatch: {identifier}")
        if source["provider"] == "MARCAP":
            blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            if url.path != f"/repos/FinanceData/marcap/git/blobs/{blob}":
                raise ValueError("Marcap source must match its immutable published Git blob")
        sources[identifier] = dict(source, path=str(location), source_sha256=digest,
                                   published_date=_day(source["published_date"]))
        if source["provider"] == "KIND" and market != "kr":
            raise ValueError("KIND corroboration is only supported for Korean lifecycle facts")

    rows = {}
    keys = {"listing_episodes": "episode_id", "events": "event_id", "entitlements": "component_id",
            "trading_halts": "halt_id"}
    for category, key in keys.items():
        unique = {}
        for original in manifest.get(category, []):
            row = dict(original)
            identifier = (row.get("event_id"), row[key]) if category == "entitlements" else row[key]
            if identifier in unique:
                raise ValueError(f"Duplicate {category} identity: {identifier}")
            if category != "entitlements":
                refs = row.get("source_ids", [])
                if not refs or any(ref not in sources for ref in refs):
                    raise ValueError(f"Missing source evidence for {identifier}")
                if market == "kr" and any(sources[ref]["provider"] != "DART" for ref in refs):
                    raise ValueError("Korean listing and lifecycle facts require DART evidence")
                supporting = row.get("supporting_source_ids", [])
                if (not isinstance(supporting, list) or any(not isinstance(ref, str) for ref in supporting)
                        or len(set(supporting)) != len(supporting)
                        or any(ref not in sources for ref in supporting)):
                    raise ValueError("Missing or duplicated lifecycle corroborating evidence")
                if supporting and (market != "kr" or any(sources[ref]["provider"] != "KIND" for ref in supporting)):
                    raise ValueError("Korean exchange corroboration must cite separately retained KIND notices")
                row.pop("supporting_sources", None)
                if supporting:
                    row["supporting_sources"] = [{key:sources[ref][key] for key in
                        ("source_id", "provider", "published_date", "source_url", "source_sha256")} for ref in supporting]
                if row.get("status") != "confirmed":
                    raise ValueError("Unconfirmed rows belong in the unresolved inventory")
                row["published_date"] = _day(row["published_date"])
                if row["published_date"] < max(sources[ref]["published_date"] for ref in [*refs, *supporting]):
                    raise ValueError("Fact availability precedes its cited evidence")
                # A later correction cannot be silently backdated by the receipt ID.
                if row["published_date"] > end_date:
                    continue
                primary = sources[refs[-1]]
                row.update(source_url=primary["source_url"], source_sha256=primary["source_sha256"])
            unique[identifier] = row
        rows[category] = list(unique.values())

    events = {row["event_id"]: row for row in rows["events"]}
    for component in rows["entitlements"]:
        if component["event_id"] not in events:
            raise ValueError("Entitlement refers to an absent event")
        units = component.get("units_per_share")
        if (component.get("component_type") != "security" or not component.get("recipient_security_id")
                or not isinstance(units, (int, float)) or not math.isfinite(units) or units <= 0):
            raise ValueError("Unresolved security entitlement")
        if component.get("delivery_date"):
            component["delivery_date"] = _day(component["delivery_date"])
        if component.get("tradable_date"):
            component["tradable_date"] = _day(component["tradable_date"])
    for event in events.values():
        event["effective_date"] = _day(event["effective_date"])
        if event.get("cash_payment_date"):
            event["cash_payment_date"] = _day(event["cash_payment_date"])
        amount = event.get("cash_per_share")
        if amount is not None and (not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0):
            raise ValueError("Invalid lifecycle cash amount")
        if not isinstance(event.get("entitlements_complete"), bool):
            raise ValueError("Explicit entitlement completeness is required")
    for episode in rows["listing_episodes"]:
        if episode["country"] != market.upper():
            raise ValueError("Listing episode country does not match the manifest")
        episode["valid_from"] = _day(episode["valid_from"])
        if episode.get("valid_until"):
            episode["valid_until"] = _day(episode["valid_until"])
        closures = [event for event in events.values() if event["security_id"] == episode["security_id"]
                    and event["event_type"] in {"cash_merger", "cash_exchange", "share_exchange", "cancellation", "delisting"}
                    and event["effective_date"] >= episode["valid_from"]]
        if len(closures) > 1:
            raise ValueError("Conflicting terminal events require review")
        if closures:
            event = closures[0]
            # An earlier exchange delisting can precede cancellation/OTC trading.
            if not episode.get("valid_until") or event["effective_date"] < episode["valid_until"]:
                episode["valid_until"] = event["effective_date"]
                episode["source_ids"] = sorted(set(episode["source_ids"] + event["source_ids"]))
                episode["closure_published_date"] = event["published_date"]
                episode["closure_source_ids"] = event["source_ids"]
                if event.get("supporting_source_ids"):
                    episode["closure_supporting_source_ids"] = event["supporting_source_ids"]
                    episode["closure_supporting_sources"] = event["supporting_sources"]
        if episode.get("valid_until") and episode["valid_until"] <= episode["valid_from"]:
            raise ValueError("Listing episode must have a positive lifetime")
    # Listing lifetime and exchange execution restrictions are distinct facts.
    # A halt ends on the first session whose closing trade is executable again.
    halt_intervals = {}
    for halt in rows["trading_halts"]:
        halt["start_date"] = _day(halt["start_date"])
        halt["end_date"] = _day(halt["end_date"]) if halt.get("end_date") else None
        if halt["end_date"] is not None and halt["end_date"] <= halt["start_date"]:
            raise ValueError("Trading halt must end after its start")
        if not any(episode["security_id"] == halt["security_id"]
                   and episode["valid_from"] <= halt["start_date"]
                   and (episode.get("valid_until") is None or halt["start_date"] < episode["valid_until"])
                   for episode in rows["listing_episodes"]):
            raise ValueError("Trading halt requires a matching confirmed listing lifetime")
        prior = halt_intervals.setdefault(halt["security_id"], [])
        stop = halt["end_date"] or "9999-12-31"
        if any(halt["start_date"] < old_end and old_start < stop for old_start, old_end in prior):
            raise ValueError("Overlapping trading halt intervals require review")
        prior.append((halt["start_date"], stop))
    return {**rows, "sources": list(sources.values()), "unresolved": manifest.get("unresolved", []),
            "price_sources": manifest.get("price_sources", []), "share_sources": manifest.get("share_sources", []),
            "market_data_sources": manifest.get("market_data_sources", [])}


def run_survivorship_refresh(*, market, end_date, manifest_path=None, output_dir=None,
                            source_dir=None, start_date=None, panel_dir=None, download=True, load_clickhouse=True, force=False,
                            gold_dir=None):
    end_date = _day(end_date)
    if market not in {"us", "kr"}:
        raise ValueError("market must be us or kr")
    output = Path(output_dir or DATA_LAKE.silver("survivorship", market))
    gold = Path(gold_dir or DATA_LAKE.gold("survivorship", market))
    from engine.core.serving_storage import export_json, export_frame, export_prices
    manifest_path = Path(manifest_path or DATA_LAKE.meta("survivorship", f"{market}_reviewed.json"))
    collection = None
    if download:
        from engine.extractors.survivorship import download_survivorship_sources
        collection = download_survivorship_sources(market=market, end_date=end_date, start_date=start_date,
                                                  force=force, output_dir=source_dir)
    listing_quality = None
    if download and market == "us":
        from engine.transformers.listing_source_quality import audit_alpha_vantage_listing_snapshots
        listing_quality, audit_artifact = audit_alpha_vantage_listing_snapshots(
            root=source_dir or DATA_LAKE.bronze("alpha-vantage", "listings"), end_date=end_date,
            output_dir=output / "listing_source_quality")
        collection["source_quality_audit"] = audit_artifact
    if not manifest_path.exists():
        summary = {"status": "awaiting_review", "market": market, "as_of": end_date,
                   "coverage_complete": False, "collection": collection,
                   "manifest_path": str(manifest_path.resolve()), "gold_dir": str(gold.resolve()), "artifacts": []}
        if listing_quality is not None:
            summary["listing_source_quality"] = export_json(gold / "listing_source_quality.json", listing_quality)
        _write_json(output / "summary.json", summary)
        export_json(gold / "summary.json", summary)
        return summary
    result = read_reviewed_manifest(manifest_path, market=market, end_date=end_date)
    if market == "us" and result["market_data_sources"]:
        raise ValueError("US historical prices must use Alpha Vantage price sources")
    from engine.transformers.survivorship_prices import prepare_survivorship_prices, prepare_survivorship_shares
    if market == "kr" and result["market_data_sources"]:
        from engine.transformers.survivorship_marcap import prepare_survivorship_marcap
        prices = prepare_survivorship_marcap(result, end_date=end_date)
    else:
        prices = prepare_survivorship_prices(result, market=market, end_date=end_date)
    shares = prepare_survivorship_shares(result, prices, market=market, end_date=end_date)
    import pandas as pd
    from engine.loaders.factors import prepare_daily_factor_rows
    capitalizations = pd.concat([aligned for _, _, aligned in shares], ignore_index=True) if shares else pd.DataFrame()
    if market == "kr" and prices:
        capitalizations = pd.concat([frame for _, frame in prices], ignore_index=True)
    if not capitalizations.empty:
        capitalizations["mcap_mil"] = capitalizations.market_cap / 1_000_000
        capitalizations["currency"] = "USD" if market == "us" else "KRW"
    market_cap_factors = prepare_daily_factor_rows(capitalizations, financial_basis="annual", factor_ids=["mcap_mil"])
    for category in ("listing_episodes", "events", "entitlements", "trading_halts", "sources", "unresolved"):
        _write_json(output / f"{category}.json", {"schema_version": 1, "market": market,
                                                "as_of": end_date, "rows": result[category]})
    for metadata, frame in prices:
        path = output / "prices" / f"{market}_{metadata['symbol']}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        frame.to_parquet(staged, index=False)
        staged.replace(path)
        _write_json(path.with_suffix(".metadata.json"), metadata)
    for metadata, observations, aligned in shares:
        folder = output / "shares"
        folder.mkdir(parents=True, exist_ok=True)
        for suffix, frame in (("", aligned), ("_observations", observations)):
            target = folder / f"{market}_{metadata['symbol']}{suffix}.parquet"
            staged = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            frame.to_parquet(staged, index=False)
            staged.replace(target)
        _write_json(folder / f"{market}_{metadata['symbol']}.metadata.json", metadata)
    publication = None
    cap_target = output / "market_cap_factors.parquet"
    staged = cap_target.with_name(f".{cap_target.name}.{uuid4().hex}.tmp")
    market_cap_factors.to_parquet(staged, index=False)
    staged.replace(cap_target)
    if load_clickhouse:
        from engine.loaders.survivorship import load_survivorship
        publication = load_survivorship(result, market=market, prices=prices, market_cap_factors=market_cap_factors)
    if prices and (load_clickhouse or panel_dir is not None):
        from engine.workflows.stock_splits import price_panel_dir, rebuild_manifest_path
        destination = Path(panel_dir) if panel_dir is not None else price_panel_dir(market)
        if shares:
            import pandas as pd
            from engine.transformers.sec_shares import disclosed_shares_path
            share_target = destination.parent / "us_disclosed_shares.parquet" if panel_dir is not None else disclosed_shares_path()
            restored_observations = pd.concat([observations for _, observations, _ in shares], ignore_index=True)
            combined = restored_observations
            if share_target.exists():
                existing = pd.read_parquet(share_target)
                existing = existing.loc[~existing.security_id.isin(restored_observations.security_id.unique())]
                combined = pd.concat([existing, restored_observations], ignore_index=True)
            share_target.parent.mkdir(parents=True, exist_ok=True)
            staged = share_target.with_name(f".{share_target.name}.{uuid4().hex}.tmp")
            combined.to_parquet(staged, index=False)
            staged.replace(share_target)
            share_report_path = share_target.with_suffix(".json")
            share_report = json.loads(share_report_path.read_text(encoding="utf-8")) if share_report_path.exists() else {}
            share_report["survivorship_restoration"] = {"as_of": end_date, "sources": [metadata for metadata, _, _ in shares],
                "availability_policy": "Restored rows begin on the day after filing; existing securities are preserved."}
            _write_json(share_report_path, share_report)
        rebuild_path = (destination.parent / f"{market}_price_rebuild_required.json"
                        if panel_dir is not None else rebuild_manifest_path(market))
        dirty = json.loads(rebuild_path.read_text(encoding="utf-8")) if rebuild_path.exists() else {"market": market, "items": {}}
        for metadata, frame in prices:
            target = destination / f"{market}_{metadata['symbol']}.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            metadata_path = target.with_suffix(".metadata.json")
            prior_metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            # Restoring a listing and its prices does not resolve previously
            # identified price/action anomalies. Keep that review and its
            # original source basis until a separate price review supersedes it.
            quality_keys = ("unresolved_extreme_moves", "official_vendor_discrepancies",
                            "return_review_events", "price_semantics")
            prior_quality = {key: prior_metadata[key] for key in quality_keys if key in prior_metadata}
            if prior_quality:
                prior_quality["quality_review_basis"] = prior_metadata.get("quality_review_basis", {
                    "source_sha256": prior_metadata.get("source_sha256"),
                    "event_sha256": prior_metadata.get("event_sha256"),
                    "note": "Retained preceding panel review; restoration does not resolve price anomalies."})
            staged = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            frame.to_parquet(staged, index=False)
            staged.replace(target)
            inputs = ["security_id", "trade_date", "open", "high", "low", "close", "volume",
                      "split_adj_close", "split_adjustment_factor", "currency"]
            if "listing_episode" in frame:
                inputs.append("listing_episode")
            for column in ("shares", "market_cap"):
                if column in frame:
                    inputs.append(column)
            signature_input = frame[inputs].to_csv(index=False)
            for share_metadata, _, aligned in shares:
                if share_metadata["security_id"] == metadata["security_id"]:
                    signature_input += aligned.to_csv(index=False)
            signature = hashlib.sha256(signature_input.encode()).hexdigest()
            first_date = str(frame.trade_date.min().date())
            _write_json(metadata_path, {**prior_quality, **metadata, "status": "ready", "coverage_complete": False,
                "panel_path": str(target.resolve()), "recompute_from": first_date,
                "event_sha256": metadata["split_ledger_sha256"], "normalization_sha256": signature})
            previous = dirty["items"].get(metadata["security_id"], {})
            if previous.get("signature") != signature:
                dirty["items"][metadata["security_id"]] = {
                    "from_date": min(first_date, previous.get("from_date", "9999-12-31")),
                    "signature": signature, "completed_bases": [], "snapshots_rebuilt": False,
                }
        _write_json(rebuild_path, dirty)
    artifacts = []
    for category in ("listing_episodes", "events", "entitlements", "trading_halts", "unresolved"):
        artifacts.append(export_json(gold / f"{category}.json", {
            "schema_version": 1, "market": market, "as_of": end_date,
            "coverage_complete": False, "rows": result[category]}))
    for metadata, frame in prices:
        artifacts.append(export_prices(gold / "prices" / f"{market}_{metadata['symbol']}.parquet", frame))
    artifacts.append(export_frame(gold / "market_cap_factors.parquet", market_cap_factors))
    summary = {"market": market, "as_of": end_date,
               **{key: len(value) for key, value in result.items()},
               "restored_price_rows": sum(len(frame) for _, frame in prices),
               "restored_share_rows": (len(capitalizations) if market == "kr" else sum(metadata["valid_price_days"] for metadata, _, _ in shares)),
               "restored_market_cap_rows": len(market_cap_factors),
               "database_publication": publication,
               "coverage_complete": False, "output_dir": str(output.resolve()),
               "gold_dir": str(gold.resolve()), "artifacts": artifacts}
    if listing_quality is not None:
        summary["collection"] = collection
        summary["listing_source_quality"] = export_json(gold / "listing_source_quality.json", listing_quality)
    _write_json(output / "summary.json", summary)
    export_json(gold / "summary.json", summary)
    return summary
