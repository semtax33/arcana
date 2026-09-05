from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.semantic import audit_portfolio_factor_drift
from scripts.rebuild_capex_daily_factors_v5 import CAPEX_FACTOR_IDS, load_targets


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = DATA_LAKE.meta("kr_capex_semantic_v5_factor_targets.csv")
DEFAULT_STATUS = DATA_LAKE.meta("kr_capex_semantic_v5_factor_rebuild_status.json")
DEFAULT_OUTPUT = ROOT / "deliverables" / "capex_daily_portfolio_drift_v5.json"
DRIFT_QUERY_SETTINGS = {
    # FULL OUTER JOIN must preserve absent old/new rows as NULL. ClickHouse
    # otherwise substitutes type defaults (0/empty string), corrupting both
    # coverage and drift.
    "join_use_nulls": 1,
    "max_bytes_before_external_sort": 512 * 1024 * 1024,
    "max_bytes_before_external_group_by": 512 * 1024 * 1024,
    "max_threads": 4,
}


def build_factor_drift_query(
    backup_table: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    if not re.fullmatch(
        r"fact_daily_factors_semantic_v5_capex_backup_[0-9a-f]{12}", backup_table
    ):
        raise ValueError("invalid CAPEX backup table name")
    date_scope_parts = []
    if start_date:
        date_scope_parts.append("      AND trade_date >= {start_date:Date}")
    if end_date:
        date_scope_parts.append("      AND trade_date <= {end_date:Date}")
    date_scope = "\n".join(date_scope_parts)
    return f"""
WITH
calendar_month_ends AS
(
    SELECT
        toYYYYMM(trade_date) AS year_month,
        max(trade_date) AS month_end_date
    FROM price_daily
    WHERE startsWith(security_id, 'SEC_KR_')
{date_scope}
    GROUP BY year_month
),
affected_old_rows AS
(
    SELECT factor_id, security_id, trade_date, argMax(factor_value, updated_at) AS old_value
    FROM {backup_table}
    WHERE financial_basis = 'annual'
      AND factor_id IN {{factor_ids:Array(String)}}
      AND security_id IN {{security_ids:Array(String)}}
      AND trade_date IN (SELECT month_end_date FROM calendar_month_ends)
{date_scope}
    GROUP BY factor_id, security_id, trade_date
),
current_rows AS
(
    SELECT factor_id, security_id, trade_date, argMax(factor_value, updated_at) AS new_value
    FROM fact_daily_factors
    WHERE financial_basis = 'annual'
      AND factor_id IN {{factor_ids:Array(String)}}
      AND startsWith(security_id, 'SEC_KR_')
      AND trade_date IN (SELECT month_end_date FROM calendar_month_ends)
{date_scope}
    GROUP BY factor_id, security_id, trade_date
),
merged AS
(
    SELECT
        factor_id,
        coalesce(current_rows.security_id, affected_old_rows.security_id) AS security_id,
        coalesce(current_rows.trade_date, affected_old_rows.trade_date) AS trade_date,
        multiIf(
            isNotNull(affected_old_rows.security_id), affected_old_rows.old_value,
            isNotNull(current_rows.security_id)
                AND current_rows.security_id NOT IN {{security_ids:Array(String)}},
            current_rows.new_value,
            NULL
        ) AS old_value,
        current_rows.new_value AS new_value
    FROM current_rows
    FULL OUTER JOIN affected_old_rows USING (factor_id, security_id, trade_date)
),
prices AS
(
    SELECT
        security_id,
        trade_date,
        if(
            isNull(close) OR isNull(forward_close) OR close = 0,
            NULL,
            toFloat64(forward_close / close - 1)
        ) AS forward_return
    FROM
    (
        SELECT
            security_id,
            trade_date,
            close,
            leadInFrame(toNullable(close), 21) OVER (
                PARTITION BY security_id ORDER BY trade_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            ) AS forward_close
        FROM price_daily FINAL
        WHERE startsWith(security_id, 'SEC_KR_')
    )
    WHERE trade_date IN (SELECT month_end_date FROM calendar_month_ends)
)
SELECT
    merged.factor_id AS factor_id,
    merged.trade_date AS trade_date,
    merged.security_id AS security_id,
    old_value,
    new_value,
    forward_return
FROM merged
LEFT JOIN prices USING (security_id, trade_date)
ORDER BY factor_id, trade_date, security_id
""".strip()


def build_daily_cell_audit_query(
    backup_table: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    if not re.fullmatch(
        r"fact_daily_factors_semantic_v5_capex_backup_[0-9a-f]{12}", backup_table
    ):
        raise ValueError("invalid CAPEX backup table name")
    date_scope_parts = []
    if start_date:
        date_scope_parts.append("      AND trade_date >= {start_date:Date}")
    if end_date:
        date_scope_parts.append("      AND trade_date <= {end_date:Date}")
    date_scope = "\n".join(date_scope_parts)
    return f"""
WITH
old_rows AS
(
    SELECT factor_id, security_id, trade_date, argMax(factor_value, updated_at) AS old_value
    FROM {backup_table}
    WHERE financial_basis = 'annual'
      AND factor_id IN {{factor_ids:Array(String)}}
      AND security_id IN {{security_ids:Array(String)}}
{date_scope}
    GROUP BY factor_id, security_id, trade_date
),
new_rows AS
(
    SELECT factor_id, security_id, trade_date, argMax(factor_value, updated_at) AS new_value
    FROM fact_daily_factors
    WHERE financial_basis = 'annual'
      AND factor_id IN {{factor_ids:Array(String)}}
      AND security_id IN {{security_ids:Array(String)}}
{date_scope}
    GROUP BY factor_id, security_id, trade_date
),
merged AS
(
    SELECT factor_id, old_value, new_value
    FROM old_rows
    FULL OUTER JOIN new_rows USING (factor_id, security_id, trade_date)
)
SELECT
    factor_id,
    countIf(isNotNull(old_value)) AS old_available_cell_count,
    countIf(isNotNull(new_value)) AS new_available_cell_count,
    countIf(isNull(old_value) AND isNotNull(new_value)) AS coverage_gain_cell_count,
    countIf(isNotNull(old_value) AND isNull(new_value)) AS coverage_loss_cell_count,
    countIf(
        isNotNull(old_value) AND isNotNull(new_value) AND old_value != new_value
    ) AS value_changed_cell_count
FROM merged
GROUP BY factor_id
ORDER BY factor_id
""".strip()


def build_report(targets_path: Path, status_path: Path) -> dict[str, object]:
    symbols, security_ids, target_digest = load_targets(targets_path)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("target_sha256") != target_digest:
        raise ValueError("CAPEX rebuild status belongs to another target set")
    if set(status.get("completed_symbols", [])) != set(symbols):
        raise RuntimeError("CAPEX daily factor rebuild is not complete")
    backup_table = str(status.get("backup_table") or "")
    if not re.fullmatch(
        r"fact_daily_factors_semantic_v5_capex_backup_[0-9a-f]{12}", backup_table
    ):
        raise ValueError("invalid CAPEX backup table name")

    client = get_clickhouse_client(send_receive_timeout=3_600)
    factor_reports = []
    try:
        directions = {
            str(row[0]): str(row[1])
            for row in client.query(
                """
SELECT factor_id, argMax(value_direction, updated_at)
FROM factor_catalog FINAL
WHERE factor_id IN {factor_ids:Array(String)}
GROUP BY factor_id
""".strip(),
                parameters={"factor_ids": list(CAPEX_FACTOR_IDS)},
            ).result_rows
        }
        start_date = status.get("start_date")
        end_date = status.get("end_date")
        parameters = {
            "factor_ids": list(CAPEX_FACTOR_IDS),
            "security_ids": security_ids,
        }
        if start_date:
            parameters["start_date"] = str(start_date)
        if end_date:
            parameters["end_date"] = str(end_date)
        daily_count_rows = client.query(
            build_daily_cell_audit_query(
                backup_table,
                start_date=str(start_date) if start_date else None,
                end_date=str(end_date) if end_date else None,
            ),
            parameters=parameters,
            settings=DRIFT_QUERY_SETTINGS,
        ).result_rows
        daily_counts_by_factor = {
            str(row[0]): {
                "old_available_cell_count": int(row[1]),
                "new_available_cell_count": int(row[2]),
                "coverage_gain_cell_count": int(row[3]),
                "coverage_loss_cell_count": int(row[4]),
                "value_changed_cell_count": int(row[5]),
            }
            for row in daily_count_rows
        }
        frame = client.query_df(
            build_factor_drift_query(
                backup_table,
                start_date=str(start_date) if start_date else None,
                end_date=str(end_date) if end_date else None,
            ),
            parameters=parameters,
            settings=DRIFT_QUERY_SETTINGS,
        )
        for factor_id in CAPEX_FACTOR_IDS:
            daily_counts = daily_counts_by_factor.get(
                factor_id,
                {
                    "old_available_cell_count": 0,
                    "new_available_cell_count": 0,
                    "coverage_gain_cell_count": 0,
                    "coverage_loss_cell_count": 0,
                    "value_changed_cell_count": 0,
                },
            )
            factor_frame = frame.loc[frame["factor_id"].eq(factor_id)].drop(
                columns=["factor_id"]
            )
            direction = directions.get(factor_id, "NEUTRAL")
            drift = audit_portfolio_factor_drift(
                factor_frame,
                higher_is_better=direction != "LOWER_BETTER",
            )
            factor_reports.append(
                {
                    "factor_id": factor_id,
                    "direction": direction,
                    "daily_affected_cells": {
                        **daily_counts,
                        "coverage_cell_delta": daily_counts[
                            "new_available_cell_count"
                        ]
                        - daily_counts["old_available_cell_count"],
                    },
                    "monthly_full_universe_portfolio_drift": drift,
                }
            )
    finally:
        client.close()

    return {
        "semantic_engine_version": 5,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rebuild_contract": "capex_daily_factor_rebuild/v5",
        "point_in_time_policy": status["point_in_time_policy"],
        "target_count": len(symbols),
        "factor_count": len(CAPEX_FACTOR_IDS),
        "backup_table": backup_table,
        "forward_return_horizon_trading_days": 21,
        "portfolio_sampling_policy": (
            "last KR price date per calendar month, ranked across the full KR "
            "factor universe; exact value/coverage counts remain daily and limited "
            "to the affected target set"
        ),
        "factor_reports": factor_reports,
        "totals": {
            "old_available_cell_count": sum(
                row["daily_affected_cells"]["old_available_cell_count"]
                for row in factor_reports
            ),
            "new_available_cell_count": sum(
                row["daily_affected_cells"]["new_available_cell_count"]
                for row in factor_reports
            ),
            "coverage_cell_delta": sum(
                row["daily_affected_cells"]["coverage_cell_delta"]
                for row in factor_reports
            ),
            "value_changed_cell_count": sum(
                row["daily_affected_cells"]["value_changed_cell_count"]
                for row in factor_reports
            ),
            "percentile_changed_cell_count": sum(
                row["monthly_full_universe_portfolio_drift"][
                    "percentile_changed_cell_count"
                ]
                for row in factor_reports
            ),
            "decile_changed_cell_count": sum(
                row["monthly_full_universe_portfolio_drift"][
                    "decile_changed_cell_count"
                ]
                for row in factor_reports
            ),
            "top_decile_membership_changed_cell_count": sum(
                row["monthly_full_universe_portfolio_drift"][
                    "top_decile_membership_changed_cell_count"
                ]
                for row in factor_reports
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare materialized CAPEX factors through value, rank, portfolio, IC, spread, and turnover."
    )
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report(args.targets, args.status)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), **report["totals"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
