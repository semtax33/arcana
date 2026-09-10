"""Restore reviewed historical identities using retained Alpha Vantage prices."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import re

import pandas as pd

from engine.extractors.alpha_vantage_prices import parse_daily_payload
from engine.transformers.stock_splits import SplitEvent, adjust_prices, load_events


def prepare_survivorship_prices(bundle, *, market, end_date):
    """Validate all requested identities before returning any loadable frame.

    Source bytes stay untouched. Only accepted listing intervals are loadable;
    quotes outside those intervals remain in the retained original response.
    """
    sources = {source["source_id"]: source for source in bundle["sources"]}
    episodes = {row["episode_id"]: row for row in bundle["listing_episodes"]}
    official = load_events(market) if bundle.get("price_sources") else []
    restored, seen = [], set()
    for request in bundle.get("price_sources", []):
        sid, symbol = request["security_id"], request["symbol"]
        if (market != "us" or request.get("identity_status") != "verified" or sid in seen
                or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", symbol)):
            raise ValueError("Historical price identity requires review")
        seen.add(sid)
        refs = request.get("episode_ids", [])
        if not refs or any(ref not in episodes or episodes[ref]["security_id"] != sid for ref in refs):
            raise ValueError("Historical prices require confirmed listing episode identities")
        chosen = [episodes[ref] for ref in refs]
        if len({row["issuer_id"] for row in chosen}) != 1:
            raise ValueError("Reused symbol price histories require separate issuer identities")
        source = sources.get(request["source_id"])
        if source is None or source["provider"] != "ALPHA_VANTAGE":
            raise ValueError("US historical prices must use a pinned Alpha Vantage source")
        payload = json.loads(Path(source["path"]).read_text(encoding="utf-8-sig"))
        frame = parse_daily_payload(payload, symbol=symbol)
        mask = pd.Series(False, index=frame.index)
        episode_numbers = pd.Series(-1, index=frame.index, dtype="int64")
        for number, episode in enumerate(sorted(chosen, key=lambda row: row["valid_from"])):
            interval = frame.trade_date.ge(pd.Timestamp(episode["valid_from"]))
            if episode.get("valid_until"):
                interval &= frame.trade_date.lt(pd.Timestamp(episode["valid_until"]))
            if (mask & interval).any():
                raise ValueError("Overlapping listing intervals require review")
            episode_numbers.loc[interval] = number
            mask |= interval
        mask &= frame.trade_date.le(pd.Timestamp(end_date))
        excluded = int((~mask).sum())
        frame = frame.loc[mask].copy()
        frame["listing_episode"] = episode_numbers.loc[mask].to_numpy()
        if frame.empty:
            raise ValueError(f"No historical prices overlap the reviewed listing: {sid}")
        frame["security_id"] = sid
        first, last = frame.trade_date.min().date().isoformat(), frame.trade_date.max().date().isoformat()
        selected = {event.effective_date: event for event in official
                    if event.security_id == sid and first <= event.effective_date <= last and event.status == "confirmed"}
        daily = payload["Time Series (Daily)"]
        vendor_days = set()
        for day in frame.trade_date.dt.date.astype(str):
            ratio = Decimal(str(daily[day]["8. split coefficient"]))
            if ratio == 1:
                continue
            vendor_days.add(day)
            if day in selected:
                if abs(selected[day].ratio - ratio) / ratio > Decimal("0.0001"):
                    raise ValueError(f"Official/Alpha split ratio conflict for {sid} on {day}")
            else:
                selected[day] = SplitEvent(
                    security_id=sid, effective_date=day, new_shares=str(ratio), old_shares="1",
                    source="ALPHA_VANTAGE", source_id=f"{symbol}:{day}", source_url=source["source_url"],
                    source_sha256=source["source_sha256"], published_date=source["published_date"],
                    action_type="split" if ratio > 1 else "reverse_split",
                    evidence="Retained executed daily split coefficient; no price-jump inference.")
        if set(selected) - vendor_days:
            raise ValueError(f"Official split dates absent from historical Alpha actions require review: {sid}")
        adjusted = adjust_prices(frame, list(selected.values()), as_of=end_date, price_basis="raw")
        metadata = {"security_id": sid, "symbol": symbol, "price_provider": "ALPHA_VANTAGE",
                    "source_path": source["path"], "source_sha256": source["source_sha256"],
                    "episode_ids": refs, "as_of": end_date, "rows": len(adjusted), "excluded_rows": excluded,
                    "price_basis": "split_only", "split_ledger_sha256": adjusted.split_ledger_sha256.iloc[0],
                    "price_factor_rebuild_required": True}
        restored.append((metadata, adjusted))
    return restored


def prepare_survivorship_shares(bundle, prices, *, market, end_date):
    """Use reviewed single-class DEI cover counts, available after filing day."""
    from engine.transformers.sec_shares import parse_share_observations, align_disclosed_shares
    sources = {row["source_id"]: row for row in bundle["sources"]}
    price_frames = {metadata["security_id"]: frame for metadata, frame in prices}
    restored, seen = [], set()
    for request in bundle.get("share_sources", []):
        sid = request["security_id"]
        if market != "us" or request.get("single_common_class_confirmed") is not True or sid in seen or sid not in price_frames:
            raise ValueError("Disclosed share counts require reviewed single-class identity and prices")
        seen.add(sid)
        issuer_ids = {str(row.get("cik", "")).lstrip("0") for row in bundle["listing_episodes"] if row["security_id"] == sid}
        if issuer_ids != {str(request["cik"]).lstrip("0")}:
            raise ValueError("Disclosed share CIK does not match the reviewed listing identity")
        source = sources.get(request["source_id"])
        if source is None or source["provider"] != "SEC":
            raise ValueError("Disclosed historical US shares require retained SEC evidence")
        payload = json.loads(Path(source["path"]).read_text(encoding="utf-8-sig"))
        if int(payload.get("cik", -1)) != int(request["cik"]):
            raise ValueError("CompanyFacts issuer identity mismatch")
        dei = payload.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
        groups = {}
        for row in dei.get("units", {}).get("shares", []):
            if row.get("form") not in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}:
                continue
            key = (row.get("filed"), row.get("end"), row.get("accn"))
            groups.setdefault(key, set()).add(row.get("val"))
        if any(len(values) > 1 for values in groups.values()):
            raise ValueError("Conflicting SEC cover share counts require class/context review")
        # DEI cover counts identify shares on a particular measurement day.
        # Do not substitute weighted EPS denominators or restated comparative
        # balance-sheet share counts whose split basis needs separate review.
        observations = parse_share_observations({"cik": payload["cik"], "facts": {"dei": {
            "EntityCommonStockSharesOutstanding": dei}}}, security_id=sid,
            source_sha256=source["source_sha256"], as_of=end_date)
        if observations.empty:
            raise ValueError(f"No verified DEI cover share counts: {sid}")
        observations["filed_date"] = observations.trade_date
        observations["trade_date"] = observations.trade_date + pd.Timedelta(days=1)
        observations = observations.loc[observations.trade_date.le(pd.Timestamp(end_date))].copy()
        if observations.empty:
            raise ValueError(f"No share count was available before the cutoff: {sid}")
        aligned = align_disclosed_shares(price_frames[sid], observations)
        metadata = {"security_id": sid, "symbol": sid.removeprefix("SEC_US_"),
                    "cik": request["cik"], "source_path": source["path"], "source_sha256": source["source_sha256"],
                    "observations": len(observations), "valid_price_days": int(aligned.shares.notna().sum()),
                    "missing_price_days": int(aligned.shares.isna().sum()),
                    "availability_policy": "Calendar day after filed date; carried forward only on observed trading dates.",
                    "unit_basis": "DEI measurement date, adjusted only for subsequent executed splits."}
        restored.append((metadata, observations, aligned))
    return restored
