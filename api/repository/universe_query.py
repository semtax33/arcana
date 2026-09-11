"""One dated universe for screening, graph cross sections and backtests.

Market cap is always a same-day observation, independent of the score's table
and financial basis. Current listing classification is intentionally explicit.
"""
from __future__ import annotations

import re
from datetime import date
from api.model.universe import has_size_filters, normalize_universe
from api.repository.listing_history import listing_history_table, trading_halt_history_table, security_source_sql


def build_universe_ctes(*, dates_sql: str, universe=None, market=None,
                        sector_codes=None, industry_group_codes=None,
                        security_table="security_master", issuer_table="issuers",
                        cap_table="fact_daily_factors", listing_table=None, trading_halt_table=None):
    for table in (security_table, issuer_table, cap_table, *([trading_halt_table] if trading_halt_table else [])):
        if not re.fullmatch(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?", table):
            raise ValueError("invalid universe table name")
    filters = normalize_universe(universe, market)
    params = {"uv_market": str(market or "").upper(), "uv_exchanges": filters["exchange_codes"],
              "uv_sectors": sector_codes or [], "uv_industries": industry_group_codes or []}
    where = ["({uv_market:String} IN ('', 'ALL') OR s.country = {uv_market:String})",
             "(empty({uv_exchanges:Array(String)}) OR has({uv_exchanges:Array(String)}, s.exchange_code))",
             "(empty({uv_sectors:Array(String)}) OR has({uv_sectors:Array(String)}, i.sector_code))",
             "(empty({uv_industries:Array(String)}) OR has({uv_industries:Array(String)}, i.industry_group_code))"]
    conditions = []
    for key, operator in (("market_cap_min_mil", ">="), ("market_cap_max_mil", "<=")):
        if filters[key] is not None:
            params["uv_" + key] = filters[key]
            conditions.append(f"r.market_cap {operator} {{uv_{key}:Float64}}")
    if filters["size_percentile"]:
        params["uv_percent"] = filters["size_percentile"]["percent"]
        rank = "size_rank_high" if filters["size_percentile"]["side"] == "top" else "size_rank_low"
        conditions.append(f"r.{rank} <= ceil(r.size_count * {{uv_percent:Float64}} / 100.0)")
    if listing_table:
        # Confirmed listing intervals describe what actually existed on each
        # date. A later report can verify an earlier IPO/delisting; its receipt
        # date is audit provenance, not the start of exchange membership.
        # Financial observations retain their separate publication-date gates.
        # Exchange classification for a covered security comes from its dated
        # episode below, not the latest master row.
        where.pop(1)
    ctes = [f"""uv_securities AS (
    SELECT s.security_id AS security_id, s.country AS country,
           s.exchange_code AS exchange_code, s.classification_date AS classification_date
    FROM (SELECT security_id, argMax(country, updated_at) AS country,
                 argMax(issuer_id, updated_at) AS issuer_id,
                 argMax(exchange_code, updated_at) AS exchange_code,
                 toDate(max(updated_at)) AS classification_date
          FROM {security_source_sql(security_table, listing_table)} GROUP BY security_id) s
    LEFT JOIN (SELECT issuer_id, argMax(sector_code, updated_at) AS sector_code,
                      argMax(industry_group_code, updated_at) AS industry_group_code
               FROM {issuer_table} GROUP BY issuer_id) i ON s.issuer_id = i.issuer_id
    WHERE {' AND '.join(where)}
)""", f"uv_dates AS ({dates_sql})"]
    dated_cte = "uv_listed_securities" if trading_halt_table else "uv_dated_securities"
    if listing_table:
        ctes.append(f"""{dated_cte} AS (
    SELECT d.trade_date AS trade_date, s.security_id AS security_id,
           s.country AS country, s.exchange_code AS exchange_code,
           s.classification_date AS classification_date, 0 AS listing_verified
    FROM uv_dates d CROSS JOIN uv_securities s
    WHERE s.security_id NOT IN (SELECT security_id FROM {listing_table} WHERE status = 'confirmed')
      AND (empty({{uv_exchanges:Array(String)}}) OR has({{uv_exchanges:Array(String)}}, s.exchange_code))
    UNION DISTINCT
    SELECT d.trade_date AS trade_date, s.security_id AS security_id,
           h.country AS country, h.exchange_code AS exchange_code,
           h.published_date AS classification_date, 1 AS listing_verified
    FROM uv_dates d CROSS JOIN {listing_table} h
    INNER JOIN uv_securities s ON s.security_id = h.security_id
    WHERE h.status = 'confirmed' AND h.security_type IN ('common_stock', 'provider_stock')
      AND d.trade_date >= h.valid_from AND (h.valid_until IS NULL OR d.trade_date < h.valid_until)
      AND (empty({{uv_exchanges:Array(String)}}) OR has({{uv_exchanges:Array(String)}}, h.exchange_code))
)""")
    else:
        ctes.append(f"""{dated_cte} AS (
    SELECT d.trade_date AS trade_date, s.security_id AS security_id,
           s.country AS country, s.exchange_code AS exchange_code,
           s.classification_date AS classification_date, 0 AS listing_verified
    FROM uv_dates d CROSS JOIN uv_securities s
)""")
    if trading_halt_table:
        ctes.append(f"""uv_dated_securities AS (
    SELECT s.* FROM uv_listed_securities s
    WHERE (s.trade_date, s.security_id) NOT IN (
        SELECT d.trade_date, h.security_id
        FROM uv_dates d CROSS JOIN {trading_halt_table} h
        WHERE h.status = 'confirmed' AND d.trade_date >= h.start_date
          AND (h.end_date IS NULL OR d.trade_date < h.end_date)
    )
)""")
    ctes.extend([f"""uv_cap_observations AS (
    SELECT f.trade_date AS trade_date, f.security_id AS security_id,
           f.financial_basis AS financial_basis, s.country AS country,
           argMax(tuple(f.factor_value, f.currency), f.updated_at) AS observation
    FROM {cap_table} f
    INNER JOIN uv_dated_securities s ON s.security_id = f.security_id AND s.trade_date = f.trade_date
    WHERE f.trade_date IN (SELECT trade_date FROM uv_dates)
      AND f.factor_id = 'mcap_mil' AND f.financial_basis IN ('annual', 'ttm', 'quarterly')
    GROUP BY f.trade_date, f.security_id, f.financial_basis, s.country
)""", """uv_caps AS (
    SELECT trade_date, security_id,
           argMax(toFloat64(tupleElement(observation, 1)),
               multiIf(financial_basis = 'annual', 3, financial_basis = 'ttm', 2, 1)) AS market_cap
    FROM uv_cap_observations
    WHERE isFinite(tupleElement(observation, 1)) AND tupleElement(observation, 1) > 0
      AND tupleElement(observation, 2) = if(country = 'KR', 'KRW', 'USD')
    GROUP BY trade_date, security_id
)""", """uv_ranked_caps AS (
    SELECT trade_date, security_id, market_cap,
           row_number() OVER (PARTITION BY trade_date ORDER BY market_cap DESC, security_id ASC) AS size_rank_high,
           row_number() OVER (PARTITION BY trade_date ORDER BY market_cap ASC, security_id ASC) AS size_rank_low,
           count() OVER (PARTITION BY trade_date) AS size_count
    FROM uv_caps
)"""])
    if has_size_filters(filters):
        eligible = "SELECT r.trade_date AS trade_date, r.security_id AS security_id FROM uv_ranked_caps r"
        if conditions:
            eligible += " WHERE " + " AND ".join(conditions)
    else:
        eligible = "SELECT trade_date, security_id FROM uv_dated_securities"
    ctes.append(f"uv_eligible AS ({eligible})")
    return ctes, params


def filter_factor_query(query: str, parameters: dict, *, dates_sql: str, universe,
                        market=None, sector_codes=None, industry_group_codes=None,
                        security_table="security_master", issuer_table="issuers",
                        cap_table="fact_daily_factors", batch=False, exact_signal_values=False,
                        listing_table=None, trading_halt_table=None):
    ctes, params = build_universe_ctes(dates_sql=dates_sql, universe=universe, market=market,
        sector_codes=sector_codes, industry_group_codes=industry_group_codes,
        security_table=security_table, issuer_table=issuer_table, cap_table=cap_table,
        listing_table=listing_table, trading_halt_table=trading_halt_table)
    parameters.update(params)
    query, n = re.subn(r"(?<!\w)latest_factor_values AS \(", "uv_unfiltered_values AS (", query, count=1)
    if n != 1:
        raise ValueError("factor query has no candidate CTE")
    day = "v.signal_date" if batch else "(SELECT trade_date FROM uv_dates LIMIT 1)"
    exact = f" AND v.trade_date = {day}" if exact_signal_values else ""
    ctes.append(f"""latest_factor_values AS (
    SELECT v.* FROM uv_unfiltered_values v
    WHERE ({day}, v.security_id) IN (SELECT trade_date, security_id FROM uv_eligible){exact}
)""")
    marker = "scored_factors AS (" if "scored_factors AS (" in query else "ranked_factor_values AS ("
    return query.replace(marker, ",\n".join(ctes) + ",\n" + marker, 1), parameters


def load_universe_details(client, *, dates, universe=None, market=None, sector_codes=None,
                          industry_group_codes=None, security_ids=None, listing_table=None, trading_halt_table=None):
    if listing_table is None:
        listing_table = listing_history_table(client)
    if trading_halt_table is None:
        trading_halt_table = trading_halt_history_table(client)
    days = sorted({str(d)[:10] for d in dates if d is not None})
    if not days:
        return None, {}
    filters = normalize_universe(universe, market)
    ctes, params = build_universe_ctes(dates_sql="SELECT arrayJoin({uv_days:Array(Date)}) AS trade_date",
        universe=filters, market=market, sector_codes=sector_codes, industry_group_codes=industry_group_codes,
        listing_table=listing_table, trading_halt_table=trading_halt_table)
    params["uv_days"] = days
    prefix = "WITH\n" + ",\n".join(ctes)
    summary_sql = prefix + """
SELECT d.trade_date AS trade_date,
       ifNull(s.before_count, 0) AS before_count,
       ifNull(c.cap_count, 0) AS valid_market_cap_count,
       before_count - valid_market_cap_count AS missing_market_cap_count,
       ifNull(e.eligible_count, 0) AS after_count,
       s.classification_date AS classification_date,
       ifNull(s.listing_verified_count, 0) AS listing_verified_count
FROM uv_dates d
LEFT JOIN (SELECT trade_date, count() before_count, max(classification_date) classification_date,
           countIf(listing_verified = 1) listing_verified_count
           FROM uv_dated_securities GROUP BY trade_date) s ON s.trade_date = d.trade_date
LEFT JOIN (SELECT trade_date, count() cap_count FROM uv_caps GROUP BY trade_date) c ON c.trade_date = d.trade_date
LEFT JOIN (SELECT trade_date, count() eligible_count FROM uv_eligible GROUP BY trade_date) e ON e.trade_date = d.trade_date
ORDER BY d.trade_date
"""
    rows = client.query_df(summary_sql, parameters=params).to_dict("records")
    classification_dates = [str(r.pop("classification_date"))[:10] for r in rows]
    for row in rows:
        row["trade_date"] = str(row["trade_date"])[:10]
        for key in ("before_count", "valid_market_cap_count", "missing_market_cap_count", "after_count"):
            row[key] = int(row[key])
        row["listing_verified_count"] = int(row.get("listing_verified_count", 0))
        row["listing_unverified_count"] = row["before_count"] - row["listing_verified_count"]
    summary = {"applied_filters": {**filters, "sector_codes": sector_codes or [],
                                  "industry_group_codes": industry_group_codes or []},
               "market": market, "classification_basis": "current",
               "listing_basis": "dated_with_current_fallback" if listing_table else "current_unverified",
               "trading_halt_basis": "confirmed_intervals_before_size_ranking" if trading_halt_table else "unverified",
               "sector_classification_basis": "current",
               "classification_as_of": max(classification_dates, default=date.today().isoformat()),
               "dates": rows}
    metadata = {}
    if security_ids:
        params["uv_ids"] = sorted(set(security_ids))
        sql = prefix + """
SELECT d.trade_date AS trade_date, s.security_id AS security_id, s.exchange_code AS exchange_code,
       nullIf(c.market_cap, 0) AS market_cap, s.country AS country
FROM uv_dates d CROSS JOIN uv_securities s
LEFT JOIN uv_caps c ON c.trade_date = d.trade_date AND c.security_id = s.security_id
WHERE has({uv_ids:Array(String)}, s.security_id)
"""
        for r in client.query_df(sql, parameters=params).to_dict("records"):
            metadata[(str(r["trade_date"])[:10], str(r["security_id"]))] = r
    return summary, metadata


def load_row_metadata(client, rows):
    days = sorted({str(r.get("evaluation_date") or r.get("trade_date") or r.get("latest_trade_date"))[:10] for r in rows})
    ids = sorted({str(r["security_id"]) for r in rows})
    if not ids:
        return {}
    ctes, params = build_universe_ctes(dates_sql="SELECT arrayJoin({uv_days:Array(Date)}) AS trade_date",
        listing_table=listing_history_table(client), trading_halt_table=trading_halt_history_table(client))
    params.update(uv_days=days, uv_ids=ids)
    query = "WITH\n" + ",\n".join(ctes) + """
/* universe_row_metadata */
SELECT s.trade_date AS trade_date, s.security_id AS security_id,
       s.exchange_code AS exchange_code, s.country AS country, nullIf(c.market_cap, 0) AS market_cap
FROM uv_dated_securities s
LEFT JOIN uv_caps c ON c.trade_date = s.trade_date AND c.security_id = s.security_id
WHERE has({uv_ids:Array(String)}, s.security_id)
"""
    result = {}
    for row in client.query_df(query, parameters=params).to_dict("records"):
        if "security_id" not in row or "trade_date" not in row:
            continue
        key = (str(row.pop("trade_date"))[:10], str(row.pop("security_id")))
        result[key] = row
    return result
