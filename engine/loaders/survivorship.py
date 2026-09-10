"""Publish a complete per-market lifecycle generation without partial readers."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import time

import numpy as np
import pandas as pd

from engine.core.clickhouse import get_clickhouse_client


SCHEMAS = {
    "listing_episodes": ("security_listing_episodes", {
        **dict.fromkeys(("episode_id", "security_id", "issuer_id", "symbol", "country",
                        "exchange_code", "security_type", "status", "source_url", "source_sha256"), "String"),
        "valid_from": "Date32", "valid_until": "Nullable(Date32)", "published_date": "Date32",
    }),
    "events": ("security_lifecycle_events", {
        **dict.fromkeys(("event_id", "security_id", "event_type", "currency", "status", "source_url", "source_sha256"), "String"),
        "effective_date": "Date32", "cash_payment_date": "Nullable(Date32)", "published_date": "Date32",
        "cash_per_share": "Nullable(Float64)", "entitlements_complete": "Bool",
    }),
    "entitlements": ("security_lifecycle_entitlements", {
        **dict.fromkeys(("event_id", "component_id", "component_type", "recipient_security_id", "currency"), "String"),
        "units_per_share": "Float64", "delivery_date": "Nullable(Date32)",
        "tradable_date": "Nullable(Date32)",
    }),
}


def load_survivorship(bundle, *, market, client=None, table_prefix="", prices=(), market_cap_factors=None):
    """Append immutable rows, then expose them with a single commit marker.

    A failed insertion leaves the previous market generation visible. All
    categories, including an emptied category, switch together at publication.
    Other markets are untouched; retained generations preserve review history.
    """
    if market not in {"us", "kr"} or (table_prefix and not re.fullmatch(r"[A-Za-z_]\w*", table_prefix)):
        raise ValueError("Invalid survivorship market or table prefix")
    serialized = []
    keys = {"listing_episodes": "episode_id", "events": "event_id", "entitlements": "component_id"}
    for kind, values in bundle.items():
        seen = set()
        for index, row in enumerate(values):
            if kind in SCHEMAS:
                for column, datatype in SCHEMAS[kind][1].items():
                    if "Date32" not in datatype:
                        continue
                    value = row.get(column)
                    if value is None and datatype.startswith("Nullable"):
                        continue
                    try:
                        parsed = date.fromisoformat(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"Invalid date in survivorship {kind}.{column}") from exc
                    # ClickHouse silently clamps out-of-range Date32 values.
                    # Reject them instead of changing an evidence-backed date.
                    if not date(1900, 1, 1) <= parsed <= date(2299, 12, 31):
                        raise ValueError(f"Date32 range exceeded in survivorship {kind}.{column}")
            identity = str(row.get(keys.get(kind, "source_id"), index))
            if kind == "entitlements":
                identity = row["event_id"] + ":" + identity
            if identity in seen:
                raise ValueError(f"Duplicate survivorship {kind} identity")
            seen.add(identity)
            serialized.append((kind, identity, json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)))
    price_frames = []
    price_columns = ["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "currency"]
    for metadata, panel in prices:
        frame = panel.rename(columns={"split_adj_close": "adj_close"})[price_columns].copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise")
        numeric = frame[["open", "high", "low", "close", "adj_close", "volume"]]
        if (frame.empty or frame.trade_date.isna().any() or frame.duplicated(["security_id", "trade_date"]).any()
                or set(frame.security_id) != {metadata["security_id"]}
                or not frame.currency.eq("USD" if market == "us" else "KRW").all()
                or not np.isfinite(numeric.to_numpy(dtype=float)).all()
                or not frame.close.gt(0).all() or not frame.adj_close.gt(0).all()
                or not frame.volume.ge(0).all() or not frame.volume.mod(1).eq(0).all()):
            raise ValueError("Invalid reviewed historical price frame")
        frame = frame.sort_values(["security_id", "trade_date"])
        digest = hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest()
        descriptor = {**{key: value for key, value in metadata.items() if key != "as_of"},
                      "normalized_price_sha256": digest, "rows": len(frame)}
        serialized.append(("price_publication", metadata["security_id"], json.dumps(descriptor, sort_keys=True, allow_nan=False)))
        price_frames.append((metadata["security_id"], digest, frame))
    factor_frames = []
    factor_columns = ["security_id", "trade_date", "factor_id", "financial_basis", "factor_value",
                      "fiscal_year", "financial_period", "currency", "updated_at"]
    if market_cap_factors is not None and not market_cap_factors.empty:
        factors = market_cap_factors[factor_columns].copy()
        factors["trade_date"] = pd.to_datetime(factors.trade_date, errors="raise")
        factors["updated_at"] = pd.to_datetime(factors.updated_at, errors="raise")
        accepted_ids = {row["security_id"] for row in bundle.get("listing_episodes", [])}
        if (factors.trade_date.isna().any() or factors.updated_at.isna().any()
                or factors.duplicated(["security_id", "trade_date"]).any()
                or not set(factors.security_id).issubset(accepted_ids)
                or not factors.factor_id.eq("mcap_mil").all() or not factors.financial_basis.eq("annual").all()
                or not factors.currency.eq("USD" if market == "us" else "KRW").all()
                or not np.isfinite(factors.factor_value.to_numpy(dtype=float)).all()
                or not factors.factor_value.gt(0).all()):
            raise ValueError("Invalid reviewed historical market-cap factor frame")
        for sid, frame in factors.groupby("security_id", sort=True):
            frame = frame.sort_values("trade_date")
            digest = hashlib.sha256(frame.drop(columns="updated_at").to_csv(index=False).encode()).hexdigest()
            serialized.append(("factor_publication", sid, json.dumps({
                "normalized_factor_sha256": digest, "rows": len(frame), "factor_id": "mcap_mil",
                "financial_basis": "annual"}, sort_keys=True)))
            factor_frames.append((sid, digest, frame))
    serialized.sort()
    fingerprint = hashlib.sha256(json.dumps(serialized, ensure_ascii=False).encode()).hexdigest()
    owned = client is None
    client = client or get_clickhouse_client()
    rows_table, publications = table_prefix + "survivorship_rows", table_prefix + "survivorship_publications"
    try:
        projections = {}
        client.command(f"""CREATE TABLE IF NOT EXISTS {rows_table} (
            market LowCardinality(String), generation UInt64, kind LowCardinality(String),
            identity String, payload String
        ) ENGINE = MergeTree ORDER BY (market, generation, kind, identity)""")
        client.command(f"""CREATE TABLE IF NOT EXISTS {publications} (
            market LowCardinality(String), generation UInt64, fingerprint String
        ) ENGINE = MergeTree ORDER BY (market, generation)""")
        for kind, (name, schema) in SCHEMAS.items():
            fields = []
            for column, datatype in schema.items():
                if datatype == "Date32":
                    expression = f"toDate32(JSONExtractString(payload, '{column}'))"
                elif datatype == "Nullable(Date32)":
                    expression = f"toDate32OrNull(JSONExtractString(payload, '{column}'))"
                else:
                    expression = f"JSONExtract(payload, '{column}', '{datatype}')"
                fields.append(f"{expression} AS {column}")
            projections[kind] = ", ".join(fields)
            # Replace projections even for an unchanged source generation so
            # existing installations also preserve pre-1970 listing dates.
            client.command(f"""CREATE OR REPLACE VIEW {table_prefix}{name} AS
                SELECT {', '.join(fields)} FROM {rows_table}
                WHERE kind = '{kind}' AND (market, generation) IN (
                    SELECT market, max(generation) FROM {publications} GROUP BY market
                )""")
        previous = client.query(f"SELECT fingerprint FROM {publications} WHERE market = {{market:String}} ORDER BY generation DESC LIMIT 1",
                                parameters={"market": market}).result_rows
        if previous and previous[0][0] == fingerprint:
            return {"status": "unchanged", "fingerprint": fingerprint, "price_rows_written": 0, "market_cap_rows_written": 0}
        generation = time.time_ns()
        if serialized:
            client.insert(rows_table, [(market, generation, *row) for row in serialized],
                          column_names=["market", "generation", "kind", "identity", "payload"])
        # Evaluate typed fields before switching readers to this generation.
        for kind, fields in projections.items():
            client.query(f"SELECT {fields} FROM {rows_table} WHERE market = {{market:String}} "
                         "AND generation = {generation:UInt64} AND kind = {kind:String}",
                         parameters={"market": market, "generation": generation, "kind": kind})
        written = 0
        if price_frames:
            prior_prices = dict(client.query(f"""SELECT identity,
                argMax(JSONExtractString(payload, 'normalized_price_sha256'), generation)
                FROM {rows_table} WHERE market = {{market:String}} AND kind = 'price_publication'
                AND generation IN (SELECT generation FROM {publications} WHERE market = {{market:String}})
                GROUP BY identity""", parameters={"market": market}).result_rows)
            changed_frames = [frame for sid, digest, frame in price_frames if prior_prices.get(sid) != digest]
        else:
            changed_frames = []
        if changed_frames:
            combined = pd.concat(changed_frames, ignore_index=True)
            months = combined.trade_date.dt.strftime("%Y%m")
            ordered = sorted(months.unique())
            # Batch across securities while keeping fewer than 100 monthly
            # partitions per insert, avoiding one tiny part per issuer/month.
            for offset in range(0, len(ordered), 80):
                chunk = combined.loc[months.isin(ordered[offset:offset + 80])].copy()
                chunk["trade_date"] = chunk.trade_date.dt.date
                chunk["volume"] = chunk.volume.astype("uint64")
                client.insert_df(table_prefix + "price_daily", chunk, column_names=price_columns)
                written += len(chunk)
        factor_written = 0
        if factor_frames:
            prior_factors = dict(client.query(f"""SELECT identity,
                argMax(JSONExtractString(payload, 'normalized_factor_sha256'), generation)
                FROM {rows_table} WHERE market = {{market:String}} AND kind = 'factor_publication'
                AND generation IN (SELECT generation FROM {publications} WHERE market = {{market:String}})
                GROUP BY identity""", parameters={"market": market}).result_rows)
            changed = [frame for sid, digest, frame in factor_frames if prior_factors.get(sid) != digest]
            if changed:
                combined = pd.concat(changed, ignore_index=True)
                months = combined.trade_date.dt.strftime("%Y%m")
                ordered = sorted(months.unique())
                for offset in range(0, len(ordered), 80):
                    chunk = combined.loc[months.isin(ordered[offset:offset + 80])].copy()
                    chunk["trade_date"] = chunk.trade_date.dt.date
                    client.insert_df(table_prefix + "fact_daily_factors", chunk, column_names=factor_columns)
                    factor_written += len(chunk)
        client.insert(publications, [(market, generation, fingerprint)],
                      column_names=["market", "generation", "fingerprint"])
        return {"status": "published", "generation": generation, "fingerprint": fingerprint,
                "price_rows_written": written, "market_cap_rows_written": factor_written}
    finally:
        if owned:
            client.close()
