"""PIT quarter selection based on explicit report metadata, not elapsed days."""
from api.service.factor_identity import canonical_factor_id


def compile_fiscal_lag(node_id, config, source, params):
    base = f"node_{node_id}"
    params[f"{base}_factor"] = canonical_factor_id(source["config"]["factor_id"])
    params[f"{base}_period"] = int(config["period"])
    return [f"""{base}_snapshots AS (
        SELECT trade_date, security_id,
            argMax(tuple(factor_value, financial_period, source_trade_date), updated_at) AS cell
        FROM fact_daily_factor_snapshot
        WHERE factor_id = {{{base}_factor:String}} AND financial_basis = 'quarterly'
            AND trade_date <= {{temporal_end_date:Date}}
        GROUP BY trade_date, security_id
    )""", f"""{base}_periods AS (
        SELECT security_id, period_end_date, min(report_date) AS available_date,
            min(fiscal_year * 4 + intDiv(fiscal_month, 3) - 1) AS ordinal
        FROM dart_report_metadata
        WHERE fiscal_month IN (3, 6, 9, 12) AND report_date <= {{temporal_end_date:Date}}
        GROUP BY security_id, period_end_date
        HAVING uniqExact(fiscal_year * 4 + intDiv(fiscal_month, 3) - 1) = 1
    )""", f"""{base}_candidates AS (
        SELECT i.trade_date AS trade_date, i.security_id AS security_id,
            argMax(tuple(p.cell.1), p.trade_date).1 AS previous_value
        FROM node_{source['id']} AS i
        INNER JOIN {base}_snapshots AS c ON c.security_id = i.security_id AND c.trade_date = i.trade_date
        INNER JOIN {base}_periods AS cm ON cm.security_id = i.security_id AND cm.period_end_date = c.cell.2
        INNER JOIN {base}_snapshots AS p ON p.security_id = i.security_id
        INNER JOIN {base}_periods AS pm ON pm.security_id = i.security_id AND pm.period_end_date = p.cell.2
        WHERE cm.available_date <= i.trade_date AND pm.available_date <= i.trade_date
            AND pm.ordinal = cm.ordinal - {{{base}_period:UInt32}}
            AND p.trade_date <= i.trade_date AND p.cell.3 <= p.trade_date
            AND c.cell.3 <= i.trade_date
        GROUP BY i.trade_date, i.security_id
    )""", f"""{base} AS (
        SELECT i.trade_date AS trade_date, i.security_id AS security_id,
            if(isFinite(p.previous_value), p.previous_value, NULL) AS value,
            ifNull(isFinite(p.previous_value), false) AS is_valid,
            if(p.previous_value IS NULL, 'fiscal_period_unavailable', if(isFinite(p.previous_value), '', 'source_non_finite')) AS invalid_reason
        FROM node_{source['id']} AS i LEFT JOIN {base}_candidates AS p
            ON p.trade_date = i.trade_date AND p.security_id = i.security_id
    )"""]
