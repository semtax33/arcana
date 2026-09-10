"""Restore DART-reviewed Korean identities from pinned Marcap observations."""
import hashlib
from pathlib import Path
import re

import numpy as np
import pandas as pd

from engine.transformers.stock_splits import adjust_prices, load_events


def prepare_survivorship_marcap(bundle, *, end_date):
    requests = bundle.get("market_data_sources", [])
    if not requests:
        return []
    sources = {row["source_id"]: row for row in bundle["sources"]}
    episodes = {row["episode_id"]: row for row in bundle["listing_episodes"]}
    wanted = {row["symbol"] for row in requests}
    source_codes = wanted | {symbol.lstrip("0") or "0" for symbol in wanted}
    frames, restored, seen = {}, [], set()
    official = load_events("kr")
    columns = ["Code", "Date", "Open", "High", "Low", "Close", "Volume", "Stocks", "Marcap", "Market"]
    for request in requests:
        sid, symbol = request["security_id"], request["symbol"]
        if (request.get("identity_status") != "verified" or sid in seen
                or not re.fullmatch(r"[0-9]{6}", symbol) or sid != f"SEC_KR_{symbol}"):
            raise ValueError("Korean market data requires a reviewed security identity")
        seen.add(sid)
        refs = request.get("episode_ids", [])
        if not refs or any(ref not in episodes or episodes[ref]["security_id"] != sid for ref in refs):
            raise ValueError("Korean market data requires confirmed listing episodes")
        chosen = [episodes[ref] for ref in refs]
        if len({row["issuer_id"] for row in chosen}) != 1:
            raise ValueError("Reused Korean symbols require separate issuer identities")
        source_ids = request.get("source_ids", [])
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("Korean market data requires unique pinned sources")
        pieces = []
        for source_id in source_ids:
            source = sources.get(source_id)
            if source is None or source["provider"] != "MARCAP":
                raise ValueError("Korean market observations require a pinned Marcap source")
            if source_id not in frames:
                raw = pd.read_parquet(Path(source["path"]), columns=columns,
                                      filters=[("Code", "in", sorted(source_codes))])
                # Historical Marcap files use unpadded numeric tickers before
                # the six-character convention. Preserve their issuer mapping.
                raw["Code"] = raw.Code.astype(str).str.strip().str.zfill(6)
                frames[source_id] = raw
            pieces.append(frames[source_id].loc[frames[source_id].Code.eq(symbol)])
        frame = pd.concat(pieces, ignore_index=True).rename(columns={
            "Date": "trade_date", "Open": "open", "High": "high", "Low": "low", "Close": "close",
            "Volume": "volume", "Stocks": "shares", "Marcap": "vendor_market_cap", "Market": "exchange_code"})
        frame["trade_date"] = pd.to_datetime(frame.trade_date, errors="raise")
        mask = pd.Series(False, index=frame.index)
        numbers = pd.Series(-1, index=frame.index, dtype="int64")
        for number, episode in enumerate(sorted(chosen, key=lambda row: row["valid_from"])):
            interval = frame.trade_date.ge(pd.Timestamp(episode["valid_from"]))
            if episode.get("valid_until"):
                interval &= frame.trade_date.lt(pd.Timestamp(episode["valid_until"]))
            if (mask & interval).any():
                raise ValueError("Overlapping Korean listing episodes require review")
            if not frame.loc[interval, "exchange_code"].eq(episode["exchange_code"]).all():
                raise ValueError("Historical market differs from the DART-reviewed exchange")
            mask |= interval
            numbers.loc[interval] = number
        mask &= frame.trade_date.le(pd.Timestamp(end_date))
        excluded = int((~mask).sum())
        frame = frame.loc[mask].copy()
        frame["listing_episode"] = numbers.loc[mask].to_numpy()
        numeric = ["open", "high", "low", "close", "volume", "shares", "vendor_market_cap"]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        if (frame.empty or frame.trade_date.isna().any() or frame.trade_date.duplicated().any()
                or not np.isfinite(frame[numeric].to_numpy(dtype=float)).all()
                or not frame.close.gt(0).all() or not frame.shares.gt(0).all()
                or not frame.shares.mod(1).eq(0).all() or not frame.volume.ge(0).all()):
            raise ValueError("Invalid historical Korean price/share observations")
        # Raw files express these observations in won. Reconcile units from
        # the price/share identity rather than assuming a documentation label.
        frame["market_cap"] = frame.close * frame.shares
        if not np.allclose(frame.market_cap, frame.vendor_market_cap, rtol=1e-8, atol=1):
            raise ValueError("Marcap capitalization does not reconcile with raw close and listed shares")
        frame["security_id"], frame["currency"] = sid, "KRW"
        frame = frame.sort_values("trade_date").reset_index(drop=True)
        adjusted = adjust_prices(frame, [event for event in official if event.security_id == sid],
                                 as_of=end_date, price_basis="raw")
        signature = hashlib.sha256("\n".join(f"{ref}:{sources[ref]['source_sha256']}" for ref in sorted(source_ids)).encode()).hexdigest()
        metadata = {"security_id": sid, "symbol": symbol, "price_provider": "MARCAP", "survivorship_restored": True,
                    "share_basis": "daily_exchange_listed_shares", "source_ids": source_ids,
                    "source_sha256": signature, "episode_ids": refs, "as_of": end_date,
                    "rows": len(adjusted), "excluded_rows": excluded, "price_basis": "split_only",
                    "split_ledger_sha256": adjusted.split_ledger_sha256.iloc[0], "price_factor_rebuild_required": True}
        restored.append((metadata, adjusted))
    return restored
