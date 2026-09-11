"""Read confirmed historical identities without current-master membership bias."""
import re


def _table_name(value):
    if not re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?", value):
        raise ValueError("invalid listing history table name")
    return value


def listing_history_table(client):
    query = getattr(client, "query", None)
    if not callable(query):
        return None
    for prefix in ("", "TEMPORARY "):
        rows = query(f"EXISTS {prefix}TABLE security_listing_episodes").result_rows
        if rows and rows[0][0]:
            return "security_listing_episodes"
    return None


def trading_halt_history_table(client):
    query = getattr(client, "query", None)
    if not callable(query):
        return None
    for prefix in ("", "TEMPORARY "):
        rows = query(f"EXISTS {prefix}TABLE security_trading_halts").result_rows
        if rows and rows[0][0]:
            return "security_trading_halts"
    return None


def security_source_sql(security_table="security_master", listing_table=None):
    security_table = _table_name(security_table)
    if listing_table is None:
        return security_table
    listing_table = _table_name(listing_table)
    return f"""(
        SELECT security_id, issuer_id, country, exchange_code,
               toDateTime64(updated_at, 3) AS updated_at
        FROM {security_table}
        UNION ALL
        SELECT security_id, argMax(issuer_id, published_date) AS issuer_id,
               argMax(country, published_date) AS country,
               argMax(exchange_code, published_date) AS exchange_code,
               toDateTime64(max(published_date), 3) AS updated_at
        FROM {listing_table}
        WHERE status = 'confirmed' AND security_type = 'common_stock'
            AND security_id NOT IN (SELECT security_id FROM {security_table})
        GROUP BY security_id
    )"""
