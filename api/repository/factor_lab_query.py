from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
import math
import re
from typing import Any

from api.service.factor_identity import canonical_factor_id


NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
FACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")

MATH_UNARY_NODES = {"log", "abs", "sqrt", "negate"}
TEMPORAL_NODES = {"lag", "rolling_max", "rolling_min"}
UNARY_NODES = MATH_UNARY_NODES | TEMPORAL_NODES | {
    "winsorize",
    "zscore",
    "shrunk_zscore",
    "rank",
    "dense_rank",
    "percent_rank",
    "dense_score",
    "neutralize",
    "bucket",
}
ARITHMETIC_NODES = {"add", "sub", "mul", "div"}
COMPARISON_NODES = {"greater_than", "less_than"}
LOGICAL_NODES = {"and", "or"}
BINARY_NODES = ARITHMETIC_NODES | COMPARISON_NODES | LOGICAL_NODES
INPUT_NODES = {"factor_input", "constant"}
EVALUATE_NODES = {"ic", "bucket_return", "long_short", "turnover", "decay_test", "backtest"}
SUPPORTED_NODES = INPUT_NODES | UNARY_NODES | BINARY_NODES | {"condition", "condition_score", "weighted_score"} | EVALUATE_NODES

GROUP_BY_ALIASES = {
    ("trade_date",): ("trade_date",),
    ("trade_date", "sector"): ("trade_date", "sector"),
    ("trade_date", "sector_code"): ("trade_date", "sector"),
    ("trade_date", "industry_group"): ("trade_date", "industry_group"),
    ("trade_date", "industry_group_code"): ("trade_date", "industry_group"),
}

INVALID_REASONS = {
    "source_null",
    "source_non_finite",
    "division_by_zero",
    "log_non_positive",
    "sqrt_negative",
    "zscore_min_count",
    "zscore_zero_std",
    "winsor_empty_group",
    "missing_security_metadata",
    "missing_future_return",
    "non_finite_result",
    "lag_insufficient_history",
    "rolling_insufficient_history",
    "rolling_input_invalid",
    "condition_not_met",
    "missing_weight_input",
    "renormalize_zero_weight",
}


@dataclass(frozen=True)
class FactorLabIssue:
    code: str
    message: str
    node_id: str | None = None
    field: str | None = None


@dataclass(frozen=True)
class FactorLabValidationResult:
    valid: bool
    errors: list[FactorLabIssue] = field(default_factory=list)
    warnings: list[FactorLabIssue] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)
    final_node_id: str | None = None
    graph_hash: str = ""


@dataclass(frozen=True)
class FactorLabCompileResult:
    query: str
    parameters: dict[str, Any]
    final_node_id: str
    execution_order: list[str]
    graph_hash: str
    warnings: list[FactorLabIssue] = field(default_factory=list)


@dataclass(frozen=True)
class NodeTypeSpec:
    type: str
    group: str
    inputs: list[str]
    outputs: list[str]
    config_schema: dict[str, Any]


def node_type_specs() -> list[NodeTypeSpec]:
    return [
        NodeTypeSpec("factor_input", "input", [], ["out"], {"factor_id": "string", "financial_basis": "annual|quarterly|ttm", "missing_policy": "drop"}),
        NodeTypeSpec("constant", "input", [], ["out"], {"value": "finite number"}),
        NodeTypeSpec("add", "arithmetic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("sub", "arithmetic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("mul", "arithmetic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("div", "arithmetic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("greater_than", "logic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("less_than", "logic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("and", "logic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("or", "logic", ["left", "right"], ["out"], {}),
        NodeTypeSpec("log", "transform", ["input"], ["out"], {}),
        NodeTypeSpec("abs", "transform", ["input"], ["out"], {}),
        NodeTypeSpec("sqrt", "transform", ["input"], ["out"], {}),
        NodeTypeSpec("negate", "transform", ["input"], ["out"], {}),
        NodeTypeSpec("lag", "temporal", ["input"], ["out"], {"period": "positive integer"}),
        NodeTypeSpec("rolling_max", "temporal", ["input"], ["out"], {"window": "positive integer"}),
        NodeTypeSpec("rolling_min", "temporal", ["input"], ["out"], {"window": "positive integer"}),
        NodeTypeSpec("winsorize", "transform", ["input"], ["out"], {"group_by": ["trade_date"], "lower_quantile": 0.01, "upper_quantile": 0.99}),
        NodeTypeSpec("zscore", "transform", ["input"], ["out"], {"group_by": ["trade_date"], "stddev_method": "population", "min_count": 20, "zero_std_policy": "invalid", "direction": "as_is", "clip": None}),
        NodeTypeSpec("shrunk_zscore", "transform", ["input"], ["out"], {"group_key": "sector|industry_group", "min_market_count": 20, "min_group_count": 20, "shrinkage_strength": 20, "direction": "as_is", "clip": None}),
        NodeTypeSpec("neutralize", "transform", ["input"], ["out"], {"group_key": "sector|industry_group|market"}),
        NodeTypeSpec("rank", "score", ["input"], ["out"], {"group_by": ["trade_date"], "order": "desc"}),
        NodeTypeSpec("dense_rank", "score", ["input"], ["out"], {"group_by": ["trade_date"], "order": "desc"}),
        NodeTypeSpec("percent_rank", "score", ["input"], ["out"], {"group_by": ["trade_date"], "order": "desc"}),
        NodeTypeSpec("dense_score", "score", ["input"], ["out"], {"group_by": ["trade_date"], "order": "desc", "scale": "0_100"}),
        NodeTypeSpec("condition", "logic", ["condition", "if_true", "if_false"], ["out"], {}),
        NodeTypeSpec("condition_score", "logic", ["condition", "score"], ["out"], {}),
        NodeTypeSpec("weighted_score", "score", ["named inputs from weights"], ["out"], {"weights": {"node_handle": 1.0}, "missing_weight_renormalize": False}),
        NodeTypeSpec("bucket", "score", ["input"], ["out"], {"bucket_count": 5, "order": "desc"}),
        NodeTypeSpec("ic", "evaluate", ["score"], [], {"horizons": [1, 5, 20]}),
        NodeTypeSpec("bucket_return", "evaluate", ["score"], [], {"bucket_count": 5, "horizons": [1, 5, 20]}),
        NodeTypeSpec("long_short", "evaluate", ["score"], [], {"bucket_count": 5, "horizons": [1, 5, 20]}),
        NodeTypeSpec("turnover", "evaluate", ["score"], [], {"top_percent": 20}),
        NodeTypeSpec("decay_test", "evaluate", ["score"], [], {"horizons": [1, 5, 20]}),
        NodeTypeSpec("backtest", "evaluate", ["score"], [], {"top_percent": 20, "rebalance_frequency": "monthly|quarterly|semiannual|annual"}),
    ]


def validate_factor_lab_graph(
    graph: dict[str, Any],
    *,
    known_factor_ids: set[str] | None = None,
) -> FactorLabValidationResult:
    errors: list[FactorLabIssue] = []
    warnings: list[FactorLabIssue] = []
    graph_hash = _graph_hash(graph)

    nodes = _nodes_by_id(graph, errors)
    edges = _edges(graph, errors)
    experiment = _dict(graph.get("experiment"))
    outputs = _dict(graph.get("outputs"))
    final_node_id = str(outputs.get("final_node_id") or "")

    _validate_experiment(experiment, errors)
    _validate_nodes(nodes, known_factor_ids=known_factor_ids, errors=errors)
    incoming = _validate_edges(nodes, edges, errors)
    _validate_arity(nodes, incoming, errors)

    if not final_node_id:
        errors.append(FactorLabIssue("missing_final_node", "outputs.final_node_id is required", field="outputs.final_node_id"))
    elif final_node_id not in nodes:
        errors.append(FactorLabIssue("unknown_final_node", f"final node does not exist: {final_node_id}", field="outputs.final_node_id"))

    execution_order: list[str] = []
    if not errors and final_node_id:
        try:
            execution_order = _topological_order(nodes, edges, final_node_id)
        except ValueError as exc:
            errors.append(FactorLabIssue("cycle", str(exc)))
        else:
            reachable = set(execution_order)
            for node_id in nodes:
                if node_id not in reachable:
                    warnings.append(FactorLabIssue("disconnected_node", "node is not reachable from final node and will be skipped", node_id=node_id))

    return FactorLabValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        execution_order=execution_order,
        final_node_id=final_node_id or None,
        graph_hash=graph_hash,
    )


def compile_factor_lab_graph(
    graph: dict[str, Any],
    *,
    known_factor_ids: set[str] | None = None,
    trade_dates: list[str | date] | None = None,
    factor_table: str = "fact_daily_factors",
    factor_lab_table: str = "factor_lab_values",
    price_table: str = "price_daily",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
) -> FactorLabCompileResult:
    validation = validate_factor_lab_graph(graph, known_factor_ids=known_factor_ids)
    if not validation.valid:
        messages = "; ".join(issue.message for issue in validation.errors)
        raise ValueError(messages)

    for table_name in [factor_table, factor_lab_table, price_table, security_table, issuer_table]:
        _validate_identifier(table_name, "table_name")

    nodes = {node["id"]: node for node in graph["nodes"]}
    incoming = _incoming_by_handle(graph.get("edges") or [])
    experiment = _dict(graph.get("experiment"))
    has_temporal_nodes = any(
        nodes[node_id]["type"] in TEMPORAL_NODES
        for node_id in validation.execution_order
    )
    temporal_history_input_node_ids = _temporal_history_input_node_ids(
        nodes,
        graph.get("edges") or [],
    )
    has_renormalized_weighted_score = any(
        nodes[node_id]["type"] == "weighted_score"
        and bool(_dict(nodes[node_id].get("config")).get("missing_weight_renormalize", False))
        for node_id in validation.execution_order
    )
    params: dict[str, Any] = {
        "start_date": _resolve_date(experiment.get("start_date")),
        "end_date": _resolve_date(experiment.get("end_date")),
    }
    if trade_dates is not None:
        normalized_trade_dates = sorted({_resolve_date(value) for value in trade_dates})
        if not normalized_trade_dates:
            raise ValueError("trade_dates must not be empty")
        params["trade_dates"] = normalized_trade_dates
    if has_temporal_nodes:
        params["temporal_end_date"] = (
            max(params["trade_dates"])
            if "trade_dates" in params
            else params["end_date"]
        )
    direct_temporal_row_limits = _direct_temporal_factor_row_limits(
        nodes,
        graph.get("edges") or [],
        params,
    )

    ctes: list[str] = []
    if _needs_security_universe(nodes, validation.execution_order, experiment):
        ctes.append(_compile_security_universe_cte(experiment, security_table, issuer_table, params))
    base_universe_needs_history = any(
        nodes[node_id]["type"] == "constant"
        or (
            nodes[node_id]["type"] == "weighted_score"
            and bool(
                _dict(nodes[node_id].get("config")).get(
                    "missing_weight_renormalize", False
                )
            )
        )
        for node_id in temporal_history_input_node_ids
    )
    if (
        any(nodes[node_id]["type"] == "constant" for node_id in validation.execution_order)
        or has_renormalized_weighted_score
    ):
        ctes.append(
            _compile_base_universe_cte(
                price_table,
                use_trade_dates="trade_dates" in params,
                include_history=base_universe_needs_history,
                restrict_to_security_universe=has_renormalized_weighted_score,
            )
        )

    for node_id in validation.execution_order:
        node = nodes[node_id]
        node_type = node["type"]
        config = _dict(node.get("config"))
        input_map = incoming.get(node_id, {})
        if node_type == "factor_input":
            ctes.append(
                _compile_factor_input(
                    node_id,
                    config,
                    factor_table,
                    params,
                    include_history=node_id in temporal_history_input_node_ids,
                    history_row_limit=direct_temporal_row_limits.get(node_id),
                )
            )
        elif node_type == "constant":
            ctes.append(_compile_constant(node_id, config, params))
        elif node_type in BINARY_NODES:
            if node_type in ARITHMETIC_NODES:
                ctes.append(_compile_binary(node_id, node_type, input_map))
            else:
                ctes.append(_compile_boolean_binary(node_id, node_type, input_map))
        elif node_type in MATH_UNARY_NODES:
            ctes.append(_compile_unary_math(node_id, node_type, input_map["input"]))
        elif node_type == "lag":
            ctes.append(
                _compile_lag(
                    node_id,
                    config,
                    input_map["input"],
                    params,
                    limit_to_output_scope=node_id not in temporal_history_input_node_ids,
                )
            )
        elif node_type in {"rolling_max", "rolling_min"}:
            ctes.append(
                _compile_rolling(
                    node_id,
                    node_type,
                    config,
                    input_map["input"],
                    params,
                    limit_to_output_scope=node_id not in temporal_history_input_node_ids,
                )
            )
        elif node_type == "winsorize":
            ctes.extend(_compile_winsorize(node_id, config, input_map["input"], params))
        elif node_type == "zscore":
            ctes.append(_compile_zscore(node_id, config, input_map["input"], params))
        elif node_type == "shrunk_zscore":
            ctes.append(_compile_shrunk_zscore(node_id, config, input_map["input"], params))
        elif node_type in {"rank", "dense_rank", "percent_rank"}:
            ctes.append(_compile_rank(node_id, node_type, config, input_map["input"]))
        elif node_type == "dense_score":
            ctes.append(_compile_dense_score(node_id, config, input_map["input"]))
        elif node_type == "weighted_score":
            ctes.append(_compile_weighted_score(node_id, config, input_map, params))
        elif node_type == "condition":
            ctes.append(_compile_condition(node_id, input_map))
        elif node_type == "condition_score":
            ctes.append(_compile_condition_score(node_id, input_map))
        elif node_type == "neutralize":
            ctes.append(_compile_neutralize(node_id, config, input_map["input"]))
        elif node_type == "bucket":
            ctes.append(_compile_bucket(node_id, config, input_map["input"], params))
        elif node_type in EVALUATE_NODES:
            ctes.append(_compile_evaluate_passthrough(node_id, input_map["score"]))
        else:
            raise ValueError(f"unsupported node type: {node_type}")

    final_cte = _cte_name(validation.final_node_id or "")
    # ClickHouse 26.5 can drop a computed Bool alias from a joined CTE block when
    # that alias is used directly as the final filter (notably for weighted_score).
    # The explicit UInt8 predicate preserves the column through optimization while
    # retaining the same valid-row semantics.
    final_scope_filter = ""
    if has_temporal_nodes:
        final_scope_filter = (
            "\n    AND trade_date IN {trade_dates:Array(Date)}"
            if "trade_dates" in params
            else "\n    AND trade_date >= {start_date:Date}\n    AND trade_date <= {end_date:Date}"
        )
    query = (
        "WITH\n"
        + ",\n".join(ctes)
        + f"\nSELECT *\nFROM {final_cte}\nWHERE toUInt8(is_valid) = 1{final_scope_filter}"
    )
    return FactorLabCompileResult(
        query=query,
        parameters=params,
        final_node_id=validation.final_node_id or "",
        execution_order=validation.execution_order,
        graph_hash=validation.graph_hash,
        warnings=validation.warnings,
    )


def build_factor_lab_insert_query(
    compile_result: FactorLabCompileResult,
    *,
    factor_id: str,
    run_id: str,
    factor_lab_table: str = "factor_lab_values",
) -> tuple[str, dict[str, Any]]:
    _validate_factor_id(factor_id)
    _validate_identifier(factor_lab_table, "factor_lab_table")
    params = {
        **compile_result.parameters,
        "factor_id": factor_id,
        "run_id": run_id,
        "node_id": compile_result.final_node_id,
    }
    temporal_execution_settings = (
        "\nSETTINGS max_threads = 2"
        if "temporal_end_date" in compile_result.parameters
        else ""
    )
    query = f"""
INSERT INTO {factor_lab_table}
(
    security_id,
    trade_date,
    factor_id,
    financial_basis,
    factor_value,
    fiscal_year,
    financial_period,
    currency,
    run_id,
    node_id,
    is_valid,
    invalid_reason
)
SELECT
    security_id,
    trade_date,
    {{factor_id:String}} AS factor_id,
    'lab' AS financial_basis,
    value AS factor_value,
    NULL AS fiscal_year,
    NULL AS financial_period,
    '' AS currency,
    {{run_id:UUID}} AS run_id,
    {{node_id:String}} AS node_id,
    is_valid,
    invalid_reason
FROM (
{compile_result.query}
){temporal_execution_settings}
""".strip()
    return query, params


def build_quality_summary_query(
    *,
    run_id: str,
    node_id: str | None = None,
    factor_id: str | None = None,
    cache_table: str = "factor_lab_node_cache",
    values_table: str = "factor_lab_values",
) -> tuple[str, dict[str, Any]]:
    table = values_table if node_id is None else cache_table
    _validate_identifier(table, "table")
    params = {"run_id": run_id}
    node_filter = ""
    if node_id is not None:
        params["node_id"] = node_id
        node_filter = "\n    AND node_id = {node_id:String}"
    factor_filter = ""
    if factor_id is not None:
        if node_id is not None:
            raise ValueError("factor_id is only supported for final factor lab values")
        params["factor_id"] = _validate_factor_id(factor_id)
        factor_filter = "\n    AND factor_id = {factor_id:String}"
    query = f"""
SELECT
    count() AS input_rows,
    countIf(is_valid) AS valid_rows,
    countIf(NOT is_valid) AS invalid_rows,
    min(trade_date) AS min_trade_date,
    max(trade_date) AS max_trade_date,
    uniqExact(security_id) AS security_count
FROM {table}
WHERE run_id = {{run_id:UUID}}{factor_filter}{node_filter}
""".strip()
    return query, params


def build_invalid_reason_counts_query(
    *,
    run_id: str,
    node_id: str | None = None,
    factor_id: str | None = None,
    cache_table: str = "factor_lab_node_cache",
    values_table: str = "factor_lab_values",
) -> tuple[str, dict[str, Any]]:
    table = values_table if node_id is None else cache_table
    _validate_identifier(table, "table")
    params = {"run_id": run_id}
    node_filter = ""
    if node_id is not None:
        params["node_id"] = node_id
        node_filter = "\n    AND node_id = {node_id:String}"
    factor_filter = ""
    if factor_id is not None:
        if node_id is not None:
            raise ValueError("factor_id is only supported for final factor lab values")
        params["factor_id"] = _validate_factor_id(factor_id)
        factor_filter = "\n    AND factor_id = {factor_id:String}"
    query = f"""
SELECT
    invalid_reason,
    count() AS row_count
FROM {table}
WHERE run_id = {{run_id:UUID}}{factor_filter}{node_filter}
    AND NOT is_valid
GROUP BY invalid_reason
ORDER BY row_count DESC, invalid_reason ASC
""".strip()
    return query, params


def build_node_preview_query(
    *,
    run_id: str,
    node_id: str | None = None,
    limit: int = 100,
    cache_table: str = "factor_lab_node_cache",
    values_table: str = "factor_lab_values",
) -> tuple[str, dict[str, Any]]:
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    table = values_table if node_id is None else cache_table
    value_column = "factor_value" if node_id is None else "value"
    _validate_identifier(table, "table")
    params = {"run_id": run_id, "limit": int(limit)}
    node_filter = ""
    if node_id is not None:
        params["node_id"] = node_id
        node_filter = "\n    AND node_id = {node_id:String}"
    query = f"""
SELECT
    trade_date,
    security_id,
    {value_column} AS value,
    is_valid,
    invalid_reason
FROM {table}
WHERE run_id = {{run_id:UUID}}{node_filter}
ORDER BY trade_date DESC, security_id ASC
LIMIT {{limit:UInt64}}
""".strip()
    return query, params


def build_run_ranking_query(
    *,
    run_id: str,
    factor_id: str | None = None,
    effective_trade_date: str | date | None = None,
    limit: int = 100,
    values_table: str = "factor_lab_values",
    security_table: str = "security_master",
    issuer_table: str = "issuers",
    identifier_table: str = "identifiers",
) -> tuple[str, dict[str, Any]]:
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    for table in [values_table, security_table, issuer_table, identifier_table]:
        _validate_identifier(table, "table")
    params = {"run_id": run_id, "limit": int(limit)}
    factor_filter = ""
    if factor_id is not None:
        params["factor_id"] = _validate_factor_id(factor_id)
        factor_filter = "\n        AND factor_id = {factor_id:String}"
    if effective_trade_date is not None:
        params["effective_trade_date"] = _resolve_date(effective_trade_date)
        latest_trade_date_sql = "SELECT {effective_trade_date:Date} AS trade_date"
    else:
        latest_trade_date_sql = f"""SELECT max(trade_date) AS trade_date
    FROM {values_table}
    WHERE run_id = {{run_id:UUID}}
        AND is_valid{factor_filter}"""
    query = f"""
WITH
latest_trade_date AS (
    {latest_trade_date_sql}
),
ranked_values AS (
    SELECT
        security_id,
        trade_date,
        factor_id,
        factor_value,
        is_valid,
        invalid_reason,
        row_number() OVER (ORDER BY factor_value DESC, security_id ASC) AS rank,
        count() OVER () AS row_count
    FROM {values_table}
    WHERE run_id = {{run_id:UUID}}
        AND is_valid
        {factor_filter.strip()}
        AND trade_date = (SELECT trade_date FROM latest_trade_date)
),
security_metadata AS (
    SELECT
        sm.security_id AS security_id,
        any(id.ticker) AS ticker,
        if(empty(any(iss.legal_name_ko)), any(iss.legal_name_en), any(iss.legal_name_ko)) AS stock_name
    FROM {security_table} AS sm
    LEFT JOIN {issuer_table} AS iss
        ON iss.issuer_id = sm.issuer_id
    LEFT JOIN (
        SELECT
            security_id,
            any(id_value) AS ticker
        FROM {identifier_table}
        WHERE id_type = 'TICKER'
            AND is_primary
        GROUP BY security_id
    ) AS id
        ON id.security_id = sm.security_id
    GROUP BY sm.security_id
)
SELECT
    toUInt64(any(r.rank)) AS rank,
    r.security_id AS security_id,
    if(empty(any(m.ticker)), r.security_id, any(m.ticker)) AS ticker,
    any(m.stock_name) AS stock_name,
    r.trade_date AS trade_date,
    any(r.factor_id) AS factor_id,
    any(r.factor_value) AS factor_value,
    any(r.factor_value) AS score,
    if(
        any(r.row_count) <= 1,
        100.0,
        (any(r.row_count) - any(r.rank)) / (any(r.row_count) - 1) * 100.0
    ) AS percentile_score,
    any(r.is_valid) AS is_valid,
    any(r.invalid_reason) AS invalid_reason
FROM ranked_values AS r
LEFT JOIN security_metadata AS m
    ON m.security_id = r.security_id
GROUP BY
    r.security_id,
    r.trade_date
ORDER BY rank ASC, security_id ASC
LIMIT {{limit:UInt64}}
""".strip()
    return query, params


def _validate_experiment(experiment: dict[str, Any], errors: list[FactorLabIssue]) -> None:
    try:
        start_date = date.fromisoformat(str(experiment.get("start_date")))
        end_date = date.fromisoformat(str(experiment.get("end_date")))
    except Exception:
        errors.append(FactorLabIssue("invalid_date", "experiment.start_date and end_date must be ISO dates"))
        return
    if start_date > end_date:
        errors.append(
            FactorLabIssue(
                "invalid_date_range",
                "start_date must be earlier than or equal to end_date",
            )
        )


def _validate_nodes(
    nodes: dict[str, dict[str, Any]],
    *,
    known_factor_ids: set[str] | None,
    errors: list[FactorLabIssue],
) -> None:
    if not nodes:
        errors.append(FactorLabIssue("missing_nodes", "at least one node is required", field="nodes"))
        return

    for node_id, node in nodes.items():
        node_type = str(node.get("type") or "")
        config = _dict(node.get("config"))
        if not NODE_ID_RE.match(node_id):
            errors.append(FactorLabIssue("invalid_node_id", f"invalid node id: {node_id}", node_id=node_id))
        if node_type not in SUPPORTED_NODES:
            errors.append(FactorLabIssue("unknown_node_type", f"unsupported node type: {node_type}", node_id=node_id, field="type"))
            continue
        _validate_node_config(node_id, node_type, config, known_factor_ids, errors)


def _validate_node_config(
    node_id: str,
    node_type: str,
    config: dict[str, Any],
    known_factor_ids: set[str] | None,
    errors: list[FactorLabIssue],
) -> None:
    if node_type == "factor_input":
        raw_factor_id = str(config.get("factor_id") or "")
        factor_id = canonical_factor_id(raw_factor_id) if raw_factor_id else ""
        if not factor_id or not FACTOR_ID_RE.match(factor_id):
            errors.append(FactorLabIssue("invalid_factor_id", "factor_input.factor_id is invalid", node_id=node_id, field="config.factor_id"))
        elif known_factor_ids is not None and factor_id not in known_factor_ids:
            errors.append(FactorLabIssue("unknown_factor_id", f"factor_id was not found in factor_catalog: {factor_id}", node_id=node_id, field="config.factor_id"))
        basis = str(config.get("financial_basis") or "annual")
        if basis not in {"annual", "quarterly", "ttm", "lab"}:
            errors.append(FactorLabIssue("invalid_financial_basis", "financial_basis must be annual, quarterly, ttm, or lab", node_id=node_id, field="config.financial_basis"))
    elif node_type == "constant":
        value = config.get("value")
        if not _is_finite_number(value):
            errors.append(FactorLabIssue("invalid_constant", "constant.value must be a finite number", node_id=node_id, field="config.value"))
    elif node_type == "lag":
        _validate_positive_integer_config(node_id, config, "period", errors)
    elif node_type in {"rolling_max", "rolling_min"}:
        _validate_positive_integer_config(node_id, config, "window", errors)
    elif node_type == "winsorize":
        lower = config.get("lower_quantile", 0.01)
        upper = config.get("upper_quantile", 0.99)
        if not _is_finite_number(lower) or not _is_finite_number(upper) or not (0 <= float(lower) < float(upper) <= 1):
            errors.append(FactorLabIssue("invalid_quantile", "winsorize requires 0 <= lower_quantile < upper_quantile <= 1", node_id=node_id))
        _validate_group_by(node_id, config, errors)
    elif node_type == "zscore":
        min_count = config.get("min_count", 20)
        if not isinstance(min_count, int) or min_count < 2:
            errors.append(FactorLabIssue("invalid_min_count", "zscore.min_count must be an integer >= 2", node_id=node_id, field="config.min_count"))
        if str(config.get("stddev_method", "population")) not in {"population", "sample"}:
            errors.append(FactorLabIssue("invalid_stddev_method", "zscore.stddev_method must be population or sample", node_id=node_id))
        if str(config.get("zero_std_policy", "invalid")) not in {"invalid", "zero"}:
            errors.append(FactorLabIssue("invalid_zero_std_policy", "zscore.zero_std_policy must be invalid or zero", node_id=node_id))
        if str(config.get("direction", "as_is")) not in {"as_is", "higher_better", "lower_better"}:
            errors.append(FactorLabIssue("invalid_direction", "zscore.direction must be as_is, higher_better, or lower_better", node_id=node_id))
        clip = config.get("clip")
        if clip is not None and (not _is_finite_number(clip) or float(clip) <= 0):
            errors.append(FactorLabIssue("invalid_clip", "zscore.clip must be null or a positive finite number", node_id=node_id))
        _validate_group_by(node_id, config, errors)
    elif node_type == "shrunk_zscore":
        if str(config.get("group_key", "sector")) not in {
            "sector",
            "sector_code",
            "industry_group",
            "industry_group_code",
        }:
            errors.append(FactorLabIssue("invalid_group_key", "shrunk_zscore.group_key must be sector or industry_group", node_id=node_id))
        for field_name, minimum in [
            ("min_market_count", 2),
            ("min_group_count", 2),
        ]:
            value = config.get(field_name, 20)
            if not isinstance(value, int) or value < minimum:
                errors.append(FactorLabIssue("invalid_min_count", f"shrunk_zscore.{field_name} must be an integer >= {minimum}", node_id=node_id, field=f"config.{field_name}"))
        strength = config.get("shrinkage_strength", 20)
        if not _is_finite_number(strength) or float(strength) <= 0:
            errors.append(FactorLabIssue("invalid_shrinkage_strength", "shrunk_zscore.shrinkage_strength must be a positive finite number", node_id=node_id, field="config.shrinkage_strength"))
        if str(config.get("direction", "as_is")) not in {"as_is", "higher_better", "lower_better"}:
            errors.append(FactorLabIssue("invalid_direction", "shrunk_zscore.direction must be as_is, higher_better, or lower_better", node_id=node_id))
        clip = config.get("clip")
        if clip is not None and (not _is_finite_number(clip) or float(clip) <= 0):
            errors.append(FactorLabIssue("invalid_clip", "shrunk_zscore.clip must be null or a positive finite number", node_id=node_id))
    elif node_type in {"rank", "dense_rank", "percent_rank", "dense_score"}:
        if str(config.get("order", "desc")) not in {"asc", "desc"}:
            errors.append(FactorLabIssue("invalid_order", "order must be asc or desc", node_id=node_id))
        _validate_group_by(node_id, config, errors)
    elif node_type == "bucket":
        bucket_count = config.get("bucket_count", 5)
        if not isinstance(bucket_count, int) or bucket_count < 2:
            errors.append(FactorLabIssue("invalid_bucket_count", "bucket_count must be an integer >= 2", node_id=node_id))
    elif node_type == "neutralize":
        if str(config.get("group_key", "sector")) not in {"market", "sector", "sector_code", "industry_group", "industry_group_code"}:
            errors.append(FactorLabIssue("invalid_group_key", "neutralize.group_key must be market, sector, or industry_group", node_id=node_id))
    elif node_type == "weighted_score":
        weights = config.get("weights")
        if not isinstance(weights, dict) or not weights:
            errors.append(FactorLabIssue("invalid_weights", "weighted_score.weights must be a non-empty object", node_id=node_id))
            return
        total = 0.0
        for handle, weight in weights.items():
            if not NODE_ID_RE.match(str(handle)):
                errors.append(FactorLabIssue("invalid_weight_handle", f"invalid weighted_score input handle: {handle}", node_id=node_id))
            if not _is_finite_number(weight):
                errors.append(FactorLabIssue("invalid_weight", f"weight must be finite for handle: {handle}", node_id=node_id))
            else:
                total += float(weight)
        if total == 0:
            errors.append(FactorLabIssue("invalid_weight_sum", "weighted_score weight sum must not be zero", node_id=node_id))
        renormalize = config.get("missing_weight_renormalize", False)
        if not isinstance(renormalize, bool):
            errors.append(
                FactorLabIssue(
                    "invalid_missing_weight_renormalize",
                    "missing_weight_renormalize must be a boolean",
                    node_id=node_id,
                    field="config.missing_weight_renormalize",
                )
            )


def _validate_positive_integer_config(
    node_id: str,
    config: dict[str, Any],
    field_name: str,
    errors: list[FactorLabIssue],
) -> None:
    value = config.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(
            FactorLabIssue(
                f"invalid_{field_name}",
                f"{field_name} must be an integer >= 1",
                node_id=node_id,
                field=f"config.{field_name}",
            )
        )


def _validate_group_by(node_id: str, config: dict[str, Any], errors: list[FactorLabIssue]) -> None:
    try:
        _normalize_group_by(config.get("group_by", ["trade_date"]))
    except ValueError as exc:
        errors.append(FactorLabIssue("invalid_group_by", str(exc), node_id=node_id, field="config.group_by"))


def _validate_edges(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    errors: list[FactorLabIssue],
) -> dict[str, dict[str, str]]:
    incoming: dict[str, dict[str, str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        target_handle = str(edge.get("target_handle") or "input")
        if source not in nodes:
            errors.append(FactorLabIssue("unknown_source", f"edge source does not exist: {source}"))
            continue
        if target not in nodes:
            errors.append(FactorLabIssue("unknown_target", f"edge target does not exist: {target}"))
            continue
        handles = incoming.setdefault(target, {})
        if target_handle in handles:
            errors.append(FactorLabIssue("duplicate_handle", f"target handle already has an input: {target}.{target_handle}", node_id=target))
        handles[target_handle] = source
    return incoming


def _validate_arity(
    nodes: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, str]],
    errors: list[FactorLabIssue],
) -> None:
    for node_id, node in nodes.items():
        node_type = str(node.get("type") or "")
        handles = incoming.get(node_id, {})
        if node_type in INPUT_NODES and handles:
            errors.append(FactorLabIssue("invalid_arity", f"{node_type} does not accept inputs", node_id=node_id))
        elif node_type in UNARY_NODES:
            _require_exact_handles(node_id, handles, {"input"}, errors)
        elif node_type in BINARY_NODES:
            _require_exact_handles(node_id, handles, {"left", "right"}, errors)
        elif node_type == "condition":
            _require_exact_handles(node_id, handles, {"condition", "if_true", "if_false"}, errors)
        elif node_type == "condition_score":
            _require_exact_handles(node_id, handles, {"condition", "score"}, errors)
        elif node_type in EVALUATE_NODES:
            _require_exact_handles(node_id, handles, {"score"}, errors)
        elif node_type == "weighted_score":
            weights = _dict(_dict(node.get("config")).get("weights"))
            _require_exact_handles(node_id, handles, set(str(key) for key in weights), errors)


def _require_exact_handles(
    node_id: str,
    actual: dict[str, str],
    expected: set[str],
    errors: list[FactorLabIssue],
) -> None:
    actual_set = set(actual)
    missing = sorted(expected - actual_set)
    extra = sorted(actual_set - expected)
    if missing:
        errors.append(FactorLabIssue("missing_input", f"missing input handle(s): {', '.join(missing)}", node_id=node_id))
    if extra:
        errors.append(FactorLabIssue("unknown_handle", f"unknown input handle(s): {', '.join(extra)}", node_id=node_id))


def _topological_order(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    final_node_id: str,
) -> list[str]:
    incoming = _incoming_by_handle(edges)
    visiting: set[str] = set()
    visited: set[str] = set()
    order: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ValueError("graph contains a cycle")
        visiting.add(node_id)
        for source in incoming.get(node_id, {}).values():
            visit(source)
        visiting.remove(node_id)
        visited.add(node_id)
        order.append(node_id)

    visit(final_node_id)
    return order


def _compile_security_universe_cte(
    experiment: dict[str, Any],
    security_table: str,
    issuer_table: str,
    params: dict[str, Any],
) -> str:
    market = str(experiment.get("market") or "").strip().upper()
    universe = _dict(experiment.get("universe"))
    filters = []
    if market and market != "ALL":
        params["market_country"] = market
        filters.append("sm.country = {market_country:String}")
    sector_codes = _as_string_list(universe.get("sector_codes"))
    if sector_codes:
        params["sector_codes"] = sector_codes
        filters.append("has({sector_codes:Array(String)}, iss.sector_code)")
    industry_group_codes = _as_string_list(universe.get("industry_group_codes"))
    if industry_group_codes:
        params["industry_group_codes"] = industry_group_codes
        filters.append("has({industry_group_codes:Array(String)}, iss.industry_group_code)")
    where_clause = "\n    WHERE " + "\n        AND ".join(filters) if filters else ""
    return f"""
security_universe AS (
    SELECT
        sm.security_id AS security_id,
        any(sm.country) AS market,
        any(iss.sector_code) AS sector_code,
        any(iss.industry_group_code) AS industry_group_code
    FROM {security_table} AS sm
    LEFT JOIN {issuer_table} AS iss
        ON iss.issuer_id = sm.issuer_id{where_clause}
    GROUP BY sm.security_id
)""".strip()


def _compile_base_universe_cte(
    price_table: str,
    *,
    use_trade_dates: bool = False,
    include_history: bool = False,
    restrict_to_security_universe: bool = False,
) -> str:
    if include_history:
        date_filter = "p.trade_date <= {temporal_end_date:Date}"
    elif use_trade_dates:
        date_filter = "p.trade_date IN {trade_dates:Array(Date)}"
    else:
        date_filter = "p.trade_date >= {start_date:Date}\n        AND p.trade_date <= {end_date:Date}"
    universe_join = (
        "\n    INNER JOIN security_universe AS u\n        ON u.security_id = p.security_id"
        if restrict_to_security_universe
        else ""
    )
    return f"""
lab_base_universe AS (
    SELECT DISTINCT
        p.trade_date AS trade_date,
        p.security_id AS security_id
    FROM {price_table} AS p{universe_join}
    WHERE {date_filter}
)""".strip()


def _compile_factor_input(
    node_id: str,
    config: dict[str, Any],
    factor_table: str,
    params: dict[str, Any],
    *,
    include_history: bool = False,
    history_row_limit: int | None = None,
) -> str:
    factor_id = canonical_factor_id(str(config["factor_id"]))
    _validate_factor_id(factor_id)
    param_prefix = _param_prefix(node_id)
    params[f"{param_prefix}_factor_id"] = factor_id
    params[f"{param_prefix}_financial_basis"] = str(config.get("financial_basis") or "annual")
    if include_history:
        date_filter = "f.trade_date <= {temporal_end_date:Date}"
    elif "trade_dates" in params:
        date_filter = "f.trade_date IN {trade_dates:Array(Date)}"
    else:
        date_filter = "f.trade_date >= {start_date:Date}\n        AND f.trade_date <= {end_date:Date}"
    source_select = f"""
SELECT
    f.trade_date AS trade_date,
    f.security_id AS security_id,
    if(f.factor_value IS NULL OR NOT isFinite(toFloat64(f.factor_value)), NULL, toFloat64(f.factor_value)) AS value,
    f.factor_value IS NOT NULL AND isFinite(toFloat64(f.factor_value)) AS is_valid,
    multiIf(
        f.factor_value IS NULL, 'source_null',
        NOT isFinite(toFloat64(f.factor_value)), 'source_non_finite',
        ''
    ) AS invalid_reason
FROM {factor_table} AS f
INNER JOIN security_universe AS u
    ON u.security_id = f.security_id
WHERE f.factor_id = {{{param_prefix}_factor_id:String}}
    AND f.financial_basis = {{{param_prefix}_financial_basis:String}}
    AND {date_filter}""".strip()
    row_limit_sql = ""
    if history_row_limit is not None:
        params[f"{param_prefix}_history_row_limit"] = history_row_limit
        row_limit_sql = f"""
    ORDER BY security_id ASC, trade_date DESC
    LIMIT {{{param_prefix}_history_row_limit:UInt32}} BY security_id"""
    return f"""
{_cte_name(node_id)} AS (
    {source_select}{row_limit_sql}
)""".strip()


def _compile_constant(node_id: str, config: dict[str, Any], params: dict[str, Any]) -> str:
    param_prefix = _param_prefix(node_id)
    params[f"{param_prefix}_value"] = float(config["value"])
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        {{{param_prefix}_value:Float64}} AS value,
        true AS is_valid,
        '' AS invalid_reason
    FROM lab_base_universe
)""".strip()


def _compile_binary(node_id: str, node_type: str, input_map: dict[str, str]) -> str:
    left_cte = _cte_name(input_map["left"])
    right_cte = _cte_name(input_map["right"])
    op_expr = {
        "add": "l.value + r.value",
        "sub": "l.value - r.value",
        "mul": "l.value * r.value",
        "div": "l.value / nullIf(r.value, 0)",
    }[node_type]
    div_guard = " OR r.value = 0" if node_type == "div" else ""
    div_reason = "r.value = 0, 'division_by_zero'," if node_type == "div" else ""
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        l.trade_date AS trade_date,
        l.security_id AS security_id,
        if(NOT l.is_valid OR NOT r.is_valid{div_guard}, NULL, {op_expr}) AS value,
        l.is_valid AND r.is_valid{'' if node_type != 'div' else ' AND r.value != 0'} AND isFinite({op_expr}) AS is_valid,
        multiIf(
            NOT l.is_valid, l.invalid_reason,
            NOT r.is_valid, r.invalid_reason,
            {div_reason}
            NOT isFinite({op_expr}), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM {left_cte} AS l
    INNER JOIN {right_cte} AS r
        ON l.trade_date = r.trade_date
       AND l.security_id = r.security_id
)""".strip()


def _compile_boolean_binary(node_id: str, node_type: str, input_map: dict[str, str]) -> str:
    left_cte = _cte_name(input_map["left"])
    right_cte = _cte_name(input_map["right"])
    predicate = {
        "greater_than": "l.value > r.value",
        "less_than": "l.value < r.value",
        "and": "l.value != 0 AND r.value != 0",
        "or": "l.value != 0 OR r.value != 0",
    }[node_type]
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        l.trade_date AS trade_date,
        l.security_id AS security_id,
        if(NOT l.is_valid OR NOT r.is_valid, NULL, toFloat64({predicate})) AS value,
        l.is_valid AND r.is_valid AS is_valid,
        multiIf(
            NOT l.is_valid, l.invalid_reason,
            NOT r.is_valid, r.invalid_reason,
            ''
        ) AS invalid_reason
    FROM {left_cte} AS l
    INNER JOIN {right_cte} AS r
        ON l.trade_date = r.trade_date
       AND l.security_id = r.security_id
)""".strip()


def _compile_lag(
    node_id: str,
    config: dict[str, Any],
    input_node_id: str,
    params: dict[str, Any],
    *,
    limit_to_output_scope: bool,
) -> str:
    input_cte = _cte_name(input_node_id)
    param_prefix = _param_prefix(node_id)
    params[f"{param_prefix}_period"] = int(config["period"])
    window = """PARTITION BY i.security_id
                ORDER BY i.trade_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"""
    output_scope_filter = _time_series_output_scope_filter(params) if limit_to_output_scope else ""
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        if(row_position <= {{{param_prefix}_period:UInt32}} OR NOT lag_is_valid, NULL, lag_value) AS value,
        row_position > {{{param_prefix}_period:UInt32}}
            AND lag_is_valid
            AND isFinite(lag_value) AS is_valid,
        multiIf(
            row_position <= {{{param_prefix}_period:UInt32}}, 'lag_insufficient_history',
            NOT lag_is_valid, lag_invalid_reason,
            NOT isFinite(lag_value), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM (
        SELECT
            i.trade_date,
            i.security_id,
            row_number() OVER (PARTITION BY i.security_id ORDER BY i.trade_date) AS row_position,
            lagInFrame(i.value, {{{param_prefix}_period:UInt32}}, NULL) OVER ({window}) AS lag_value,
            lagInFrame(i.is_valid, {{{param_prefix}_period:UInt32}}, false) OVER ({window}) AS lag_is_valid,
            lagInFrame(i.invalid_reason, {{{param_prefix}_period:UInt32}}, 'lag_insufficient_history') OVER ({window}) AS lag_invalid_reason
        FROM {input_cte} AS i
    )
    {output_scope_filter}
)""".strip()


def _compile_rolling(
    node_id: str,
    node_type: str,
    config: dict[str, Any],
    input_node_id: str,
    params: dict[str, Any],
    *,
    limit_to_output_scope: bool,
) -> str:
    input_cte = _cte_name(input_node_id)
    param_prefix = _param_prefix(node_id)
    window_size = int(config["window"])
    params[f"{param_prefix}_window"] = window_size
    params[f"{param_prefix}_window_preceding"] = window_size - 1
    aggregate = "max" if node_type == "rolling_max" else "min"
    frame = f"""PARTITION BY i.security_id
                ORDER BY i.trade_date
                ROWS BETWEEN {{{param_prefix}_window_preceding:UInt32}} PRECEDING AND CURRENT ROW"""
    output_scope_filter = _time_series_output_scope_filter(params) if limit_to_output_scope else ""
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        if(
            row_count < {{{param_prefix}_window:UInt32}}
                OR valid_count < {{{param_prefix}_window:UInt32}},
            NULL,
            rolling_value
        ) AS value,
        row_count >= {{{param_prefix}_window:UInt32}}
            AND valid_count >= {{{param_prefix}_window:UInt32}}
            AND isFinite(rolling_value) AS is_valid,
        multiIf(
            row_count < {{{param_prefix}_window:UInt32}}, 'rolling_insufficient_history',
            valid_count < {{{param_prefix}_window:UInt32}}, 'rolling_input_invalid',
            NOT isFinite(rolling_value), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM (
        SELECT
            i.trade_date,
            i.security_id,
            count() OVER ({frame}) AS row_count,
            countIf(i.is_valid) OVER ({frame}) AS valid_count,
            {aggregate}(i.value) OVER ({frame}) AS rolling_value
        FROM {input_cte} AS i
    )
    {output_scope_filter}
)""".strip()


def _time_series_output_scope_filter(params: dict[str, Any]) -> str:
    if "trade_dates" in params:
        return "WHERE trade_date IN {trade_dates:Array(Date)}"
    return """WHERE trade_date >= {start_date:Date}
        AND trade_date <= {end_date:Date}"""


def _compile_condition(node_id: str, input_map: dict[str, str]) -> str:
    condition_cte = _cte_name(input_map["condition"])
    true_cte = _cte_name(input_map["if_true"])
    false_cte = _cte_name(input_map["if_false"])
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        c.trade_date AS trade_date,
        c.security_id AS security_id,
        if(
            NOT c.is_valid
                OR if(c.value != 0, NOT ifNull(t.is_valid, false), NOT ifNull(f.is_valid, false)),
            NULL,
            if(c.value != 0, t.value, f.value)
        ) AS value,
        c.is_valid AND if(c.value != 0, ifNull(t.is_valid, false), ifNull(f.is_valid, false)) AS is_valid,
        multiIf(
            NOT c.is_valid, c.invalid_reason,
            c.value != 0 AND NOT ifNull(t.is_valid, false), ifNull(t.invalid_reason, 'source_null'),
            c.value = 0 AND NOT ifNull(f.is_valid, false), ifNull(f.invalid_reason, 'source_null'),
            ''
        ) AS invalid_reason
    FROM {condition_cte} AS c
    LEFT JOIN {true_cte} AS t
        ON c.trade_date = t.trade_date
       AND c.security_id = t.security_id
    LEFT JOIN {false_cte} AS f
        ON c.trade_date = f.trade_date
       AND c.security_id = f.security_id
)""".strip()


def _compile_condition_score(node_id: str, input_map: dict[str, str]) -> str:
    condition_cte = _cte_name(input_map["condition"])
    score_cte = _cte_name(input_map["score"])
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        c.trade_date AS trade_date,
        c.security_id AS security_id,
        if(NOT c.is_valid OR c.value = 0 OR NOT ifNull(s.is_valid, false), NULL, s.value) AS value,
        c.is_valid AND c.value != 0 AND ifNull(s.is_valid, false) AS is_valid,
        multiIf(
            NOT c.is_valid, c.invalid_reason,
            c.value = 0, 'condition_not_met',
            NOT ifNull(s.is_valid, false), ifNull(s.invalid_reason, 'source_null'),
            ''
        ) AS invalid_reason
    FROM {condition_cte} AS c
    LEFT JOIN {score_cte} AS s
        ON c.trade_date = s.trade_date
       AND c.security_id = s.security_id
)""".strip()


def _compile_unary_math(node_id: str, node_type: str, input_node_id: str) -> str:
    input_cte = _cte_name(input_node_id)
    if node_type == "log":
        value_expr = "log(i.value)"
        value_guard = "i.value > 0"
        invalid_reason = "i.value <= 0, 'log_non_positive',"
    elif node_type == "sqrt":
        value_expr = "sqrt(i.value)"
        value_guard = "i.value >= 0"
        invalid_reason = "i.value < 0, 'sqrt_negative',"
    elif node_type == "abs":
        value_expr = "abs(i.value)"
        value_guard = "true"
        invalid_reason = ""
    else:
        value_expr = "-i.value"
        value_guard = "true"
        invalid_reason = ""
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        i.trade_date AS trade_date,
        i.security_id AS security_id,
        if(NOT i.is_valid OR NOT ({value_guard}), NULL, {value_expr}) AS value,
        i.is_valid AND ({value_guard}) AND isFinite({value_expr}) AS is_valid,
        multiIf(
            NOT i.is_valid, i.invalid_reason,
            {invalid_reason}
            NOT isFinite({value_expr}), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM {input_cte} AS i
)""".strip()


def _compile_winsorize(
    node_id: str,
    config: dict[str, Any],
    input_node_id: str,
    params: dict[str, Any],
) -> list[str]:
    input_cte = _cte_name(input_node_id)
    bounds_cte = f"{_cte_name(node_id)}_bounds"
    param_prefix = _param_prefix(node_id)
    params[f"{param_prefix}_lower_quantile"] = float(config.get("lower_quantile", 0.01))
    params[f"{param_prefix}_upper_quantile"] = float(config.get("upper_quantile", 0.99))
    partition_cols, join_using = _group_sql(config.get("group_by", ["trade_date"]))
    return [
        f"""
{bounds_cte} AS (
    SELECT
        {partition_cols},
        quantileExact({{{param_prefix}_lower_quantile:Float64}})(value) AS lower_bound,
        quantileExact({{{param_prefix}_upper_quantile:Float64}})(value) AS upper_bound,
        count() AS n
    FROM {_group_source_sql(input_cte, config.get("group_by", ["trade_date"]))}
    WHERE is_valid
    GROUP BY {partition_cols}
)""".strip(),
        f"""
{_cte_name(node_id)} AS (
    SELECT
        i.trade_date AS trade_date,
        i.security_id AS security_id,
        if(NOT i.is_valid OR ifNull(b.n, 0) = 0, NULL, greatest(least(i.value, b.upper_bound), b.lower_bound)) AS value,
        i.is_valid AND ifNull(b.n, 0) > 0 AS is_valid,
        multiIf(
            NOT i.is_valid, i.invalid_reason,
            ifNull(b.n, 0) = 0, 'winsor_empty_group',
            ''
        ) AS invalid_reason
    FROM {_group_source_sql(input_cte, config.get("group_by", ["trade_date"]))} AS i
    LEFT JOIN {bounds_cte} AS b
        USING ({join_using})
)""".strip(),
    ]


def _compile_zscore(
    node_id: str,
    config: dict[str, Any],
    input_node_id: str,
    params: dict[str, Any],
) -> str:
    input_cte = _cte_name(input_node_id)
    param_prefix = _param_prefix(node_id)
    min_count = int(config.get("min_count", 20))
    params[f"{param_prefix}_min_count"] = min_count
    group_by = config.get("group_by", ["trade_date"])
    partition_by = _partition_by_sql(group_by, alias="s")
    stddev_func = "stddevSamp" if str(config.get("stddev_method", "population")) == "sample" else "stddevPop"
    zero_policy = str(config.get("zero_std_policy", "invalid"))
    direction = -1 if str(config.get("direction", "as_is")) == "lower_better" else 1
    clip = config.get("clip")
    z_expr = f"(({direction}) * ((value - mu) / sigma))"
    if clip is not None:
        params[f"{param_prefix}_clip"] = float(clip)
        z_expr = f"greatest(least({z_expr}, {{{param_prefix}_clip:Float64}}), -{{{param_prefix}_clip:Float64}})"
    invalid_when_zero = "false" if zero_policy == "zero" else "sigma = 0"
    zero_value = "0" if zero_policy == "zero" else "NULL"
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        if(
            NOT is_valid OR n < {{{param_prefix}_min_count:UInt32}} OR sigma = 0,
            {zero_value},
            {z_expr}
        ) AS value,
        is_valid
            AND n >= {{{param_prefix}_min_count:UInt32}}
            AND NOT ({invalid_when_zero}) AS is_valid,
        multiIf(
            NOT is_valid, invalid_reason,
            n < {{{param_prefix}_min_count:UInt32}}, 'zscore_min_count',
            {invalid_when_zero}, 'zscore_zero_std',
            ''
        ) AS invalid_reason
    FROM (
        SELECT
            s.trade_date,
            s.security_id,
            s.value,
            s.is_valid,
            s.invalid_reason,
            countIf(s.is_valid) OVER (PARTITION BY {partition_by}) AS n,
            avg(if(s.is_valid, s.value, NULL)) OVER (PARTITION BY {partition_by}) AS mu,
            {stddev_func}(if(s.is_valid, s.value, NULL)) OVER (PARTITION BY {partition_by}) AS sigma
        FROM {_group_source_sql(input_cte, group_by)} AS s
    )
)""".strip()


def _compile_shrunk_zscore(
    node_id: str,
    config: dict[str, Any],
    input_node_id: str,
    params: dict[str, Any],
) -> str:
    """Blend a group Z-score with the market Z-score when groups are small."""
    input_cte = _cte_name(input_node_id)
    param_prefix = _param_prefix(node_id)
    group_key = str(config.get("group_key", "sector"))
    group_column = {
        "sector": "u.sector_code",
        "sector_code": "u.sector_code",
        "industry_group": "u.industry_group_code",
        "industry_group_code": "u.industry_group_code",
    }[group_key]
    min_market_count = int(config.get("min_market_count", 20))
    min_group_count = int(config.get("min_group_count", 20))
    params[f"{param_prefix}_min_market_count"] = min_market_count
    params[f"{param_prefix}_min_group_count"] = min_group_count
    params[f"{param_prefix}_shrinkage_strength"] = float(
        config.get("shrinkage_strength", 20)
    )
    direction = -1 if str(config.get("direction", "as_is")) == "lower_better" else 1
    clip = config.get("clip")
    adjusted_z = f"""(
        ({direction}) * if(
            group_n >= {{{param_prefix}_min_group_count:UInt32}}
                AND group_sigma != 0
                AND NOT empty(group_code),
            (group_n / (group_n + {{{param_prefix}_shrinkage_strength:Float64}}))
                * ((value - group_mu) / group_sigma)
                + ({{{param_prefix}_shrinkage_strength:Float64}}
                    / (group_n + {{{param_prefix}_shrinkage_strength:Float64}}))
                * ((value - market_mu) / market_sigma),
            ((value - market_mu) / market_sigma)
        )
    )"""
    if clip is not None:
        params[f"{param_prefix}_clip"] = float(clip)
        adjusted_z = f"greatest(least({adjusted_z}, {{{param_prefix}_clip:Float64}}), -{{{param_prefix}_clip:Float64}})"
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        if(
            NOT is_valid
                OR market_n < {{{param_prefix}_min_market_count:UInt32}}
                OR market_sigma = 0,
            NULL,
            {adjusted_z}
        ) AS value,
        is_valid
            AND market_n >= {{{param_prefix}_min_market_count:UInt32}}
            AND market_sigma != 0 AS is_valid,
        multiIf(
            NOT is_valid, invalid_reason,
            market_n < {{{param_prefix}_min_market_count:UInt32}}, 'zscore_min_count',
            market_sigma = 0, 'zscore_zero_std',
            ''
        ) AS invalid_reason
    FROM (
        SELECT
            x.trade_date,
            x.security_id,
            x.value,
            x.is_valid,
            x.invalid_reason,
            {group_column} AS group_code,
            countIf(x.is_valid) OVER (PARTITION BY x.trade_date) AS market_n,
            avg(if(x.is_valid, x.value, NULL)) OVER (PARTITION BY x.trade_date) AS market_mu,
            stddevPop(if(x.is_valid, x.value, NULL)) OVER (PARTITION BY x.trade_date) AS market_sigma,
            countIf(x.is_valid) OVER (
                PARTITION BY x.trade_date, {group_column}
            ) AS group_n,
            avg(if(x.is_valid, x.value, NULL)) OVER (
                PARTITION BY x.trade_date, {group_column}
            ) AS group_mu,
            stddevPop(if(x.is_valid, x.value, NULL)) OVER (
                PARTITION BY x.trade_date, {group_column}
            ) AS group_sigma
        FROM {input_cte} AS x
        INNER JOIN security_universe AS u
            ON u.security_id = x.security_id
    )
)""".strip()


def _compile_rank(node_id: str, node_type: str, config: dict[str, Any], input_node_id: str) -> str:
    input_cte = _cte_name(input_node_id)
    function_name = {
        "rank": "rank",
        "dense_rank": "dense_rank",
        "percent_rank": "percent_rank",
    }[node_type]
    order_sql = _order_sql(config)
    partition_by = _partition_by_sql(config.get("group_by", ["trade_date"]), alias="s")
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        s.trade_date AS trade_date,
        s.security_id AS security_id,
        toFloat64({function_name}() OVER (PARTITION BY {partition_by} ORDER BY s.value {order_sql}, s.security_id ASC)) AS value,
        s.is_valid,
        s.invalid_reason
    FROM {_group_source_sql(input_cte, config.get("group_by", ["trade_date"]))} AS s
    WHERE s.is_valid
)""".strip()


def _compile_dense_score(node_id: str, config: dict[str, Any], input_node_id: str) -> str:
    input_cte = _cte_name(input_node_id)
    order_sql = _order_sql(config)
    partition_by = _partition_by_sql(config.get("group_by", ["trade_date"]), alias="s")
    scale = 100.0 if str(config.get("scale", "0_100")) == "0_100" else 1.0
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        trade_date,
        security_id,
        if(max_rank <= 1, {scale}, ({scale}) * (max_rank - dense_value) / (max_rank - 1)) AS value,
        is_valid,
        invalid_reason
    FROM (
        SELECT
            s.trade_date,
            s.security_id,
            toFloat64(dense_rank() OVER (PARTITION BY {partition_by} ORDER BY s.value {order_sql}, s.security_id ASC)) AS dense_value,
            toFloat64(count() OVER (PARTITION BY {partition_by})) AS max_rank,
            s.is_valid,
            s.invalid_reason
        FROM {_group_source_sql(input_cte, config.get("group_by", ["trade_date"]))} AS s
        WHERE s.is_valid
    )
)""".strip()


def _compile_weighted_score(
    node_id: str,
    config: dict[str, Any],
    input_map: dict[str, str],
    params: dict[str, Any],
) -> str:
    if bool(config.get("missing_weight_renormalize", False)):
        return _compile_weighted_score_renormalized(node_id, config, input_map, params)

    weights = {str(key): float(value) for key, value in _dict(config.get("weights")).items()}
    handles = list(weights)
    base_handle = handles[0]
    from_sql = f"{_cte_name(input_map[base_handle])} AS {base_handle}"
    joins = []
    value_parts = []
    valid_parts = []
    reason_parts = []
    for handle in handles:
        param_name = f"{_param_prefix(node_id)}_{handle}_weight"
        params[param_name] = weights[handle]
        alias = handle
        if handle != base_handle:
            joins.append(
                f"""INNER JOIN {_cte_name(input_map[handle])} AS {alias}
        ON {base_handle}.trade_date = {alias}.trade_date
       AND {base_handle}.security_id = {alias}.security_id"""
            )
        value_parts.append(f"({{{param_name}:Float64}} * {alias}.value)")
        valid_parts.append(f"{alias}.is_valid")
        reason_parts.append(f"NOT {alias}.is_valid, {alias}.invalid_reason")
    value_expr = " + ".join(value_parts)
    valid_expr = " AND ".join(valid_parts)
    reason_expr = ",\n            ".join(reason_parts)
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        {base_handle}.trade_date AS trade_date,
        {base_handle}.security_id AS security_id,
        if(NOT ({valid_expr}), NULL, {value_expr}) AS value,
        ({valid_expr}) AND isFinite({value_expr}) AS is_valid,
        multiIf(
            {reason_expr},
            NOT isFinite({value_expr}), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM {from_sql}
    {' '.join(joins)}
)""".strip()


def _compile_weighted_score_renormalized(
    node_id: str,
    config: dict[str, Any],
    input_map: dict[str, str],
    params: dict[str, Any],
) -> str:
    weights = {str(key): float(value) for key, value in _dict(config.get("weights")).items()}
    param_prefix = _param_prefix(node_id)
    total_weight = sum(weights.values())
    params[f"{param_prefix}_total_weight"] = total_weight
    joins = []
    weighted_value_parts = []
    active_weight_parts = []
    valid_parts = []
    for handle, weight in weights.items():
        param_name = f"{param_prefix}_{handle}_weight"
        params[param_name] = weight
        alias = handle
        joins.append(
            f"""LEFT JOIN {_cte_name(input_map[handle])} AS {alias}
        ON base.trade_date = {alias}.trade_date
       AND base.security_id = {alias}.security_id"""
        )
        is_valid = f"ifNull({alias}.is_valid, false)"
        weighted_value_parts.append(
            f"if({is_valid}, {{{param_name}:Float64}} * {alias}.value, 0.0)"
        )
        active_weight_parts.append(
            f"if({is_valid}, {{{param_name}:Float64}}, 0.0)"
        )
        valid_parts.append(is_valid)

    weighted_value = " + ".join(weighted_value_parts)
    active_weight = " + ".join(active_weight_parts)
    any_valid = " OR ".join(valid_parts)
    normalized_value = (
        f"(({weighted_value}) * {{{param_prefix}_total_weight:Float64}} "
        f"/ nullIf(({active_weight}), 0.0))"
    )
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        base.trade_date AS trade_date,
        base.security_id AS security_id,
        if(
            NOT ({any_valid}) OR ({active_weight}) = 0.0,
            NULL,
            {normalized_value}
        ) AS value,
        ({any_valid})
            AND ({active_weight}) != 0.0
            AND isFinite({normalized_value}) AS is_valid,
        multiIf(
            NOT ({any_valid}), 'missing_weight_input',
            ({active_weight}) = 0.0, 'renormalize_zero_weight',
            NOT isFinite({normalized_value}), 'non_finite_result',
            ''
        ) AS invalid_reason
    FROM lab_base_universe AS base
    {' '.join(joins)}
)""".strip()


def _compile_neutralize(node_id: str, config: dict[str, Any], input_node_id: str) -> str:
    input_cte = _cte_name(input_node_id)
    group_key = str(config.get("group_key", "sector"))
    group_column = {
        "market": "u.market",
        "sector": "u.sector_code",
        "sector_code": "u.sector_code",
        "industry_group": "u.industry_group_code",
        "industry_group_code": "u.industry_group_code",
    }[group_key]
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        x.trade_date AS trade_date,
        x.security_id AS security_id,
        if(empty({group_column}), NULL, x.value - avg(x.value) OVER (
            PARTITION BY x.trade_date, {group_column}
        )) AS value,
        x.is_valid AND NOT empty({group_column}) AS is_valid,
        multiIf(
            NOT x.is_valid, x.invalid_reason,
            empty({group_column}), 'missing_security_metadata',
            ''
        ) AS invalid_reason
    FROM {input_cte} AS x
    INNER JOIN security_universe AS u
        ON u.security_id = x.security_id
)""".strip()


def _compile_bucket(node_id: str, config: dict[str, Any], input_node_id: str, params: dict[str, Any]) -> str:
    input_cte = _cte_name(input_node_id)
    param_prefix = _param_prefix(node_id)
    params[f"{param_prefix}_bucket_count"] = int(config.get("bucket_count", 5))
    order_sql = _order_sql(config)
    return f"""
{_cte_name(node_id)} AS (
    SELECT
        i.trade_date AS trade_date,
        i.security_id AS security_id,
        toFloat64(ntile({{{param_prefix}_bucket_count:UInt32}}) OVER (
            PARTITION BY i.trade_date ORDER BY i.value {order_sql}, i.security_id ASC
        )) AS value,
        i.is_valid,
        i.invalid_reason
    FROM {input_cte} AS i
    WHERE i.is_valid
)""".strip()


def _compile_evaluate_passthrough(node_id: str, input_node_id: str) -> str:
    return f"""
{_cte_name(node_id)} AS (
    SELECT *
    FROM {_cte_name(input_node_id)}
)""".strip()


def _needs_security_universe(
    nodes: dict[str, dict[str, Any]],
    execution_order: list[str],
    experiment: dict[str, Any],
) -> bool:
    universe = _dict(experiment.get("universe"))
    if experiment.get("market") or universe.get("sector_codes") or universe.get("industry_group_codes"):
        return True
    for node_id in execution_order:
        node = nodes[node_id]
        node_type = node["type"]
        config = _dict(node.get("config"))
        if node_type in {"factor_input", "neutralize", "shrunk_zscore"}:
            return True
        if (
            node_type == "weighted_score"
            and bool(config.get("missing_weight_renormalize", False))
        ):
            return True
        if _normalize_group_by(config.get("group_by", ["trade_date"])) in {
            ("trade_date", "sector"),
            ("trade_date", "industry_group"),
        }:
            return True
    return False


def _group_source_sql(input_cte: str, raw_group_by: Any) -> str:
    group_by = _normalize_group_by(raw_group_by)
    if group_by == ("trade_date", "sector"):
        return f"""(
        SELECT x.*, u.sector_code AS sector_code, u.industry_group_code AS industry_group_code
        FROM {input_cte} AS x
        INNER JOIN security_universe AS u ON u.security_id = x.security_id
    )"""
    if group_by == ("trade_date", "industry_group"):
        return f"""(
        SELECT x.*, u.sector_code AS sector_code, u.industry_group_code AS industry_group_code
        FROM {input_cte} AS x
        INNER JOIN security_universe AS u ON u.security_id = x.security_id
    )"""
    return input_cte


def _group_sql(raw_group_by: Any) -> tuple[str, str]:
    group_by = _normalize_group_by(raw_group_by)
    if group_by == ("trade_date",):
        return "trade_date", "trade_date"
    if group_by == ("trade_date", "sector"):
        return "trade_date, sector_code", "trade_date, sector_code"
    return "trade_date, industry_group_code", "trade_date, industry_group_code"


def _partition_by_sql(raw_group_by: Any, *, alias: str) -> str:
    group_by = _normalize_group_by(raw_group_by)
    if group_by == ("trade_date",):
        return f"{alias}.trade_date"
    if group_by == ("trade_date", "sector"):
        return f"{alias}.trade_date, {alias}.sector_code"
    return f"{alias}.trade_date, {alias}.industry_group_code"


def _normalize_group_by(raw_group_by: Any) -> tuple[str, ...]:
    if raw_group_by is None:
        raw_group_by = ["trade_date"]
    if not isinstance(raw_group_by, list) or not raw_group_by:
        raise ValueError("group_by must be a non-empty list")
    key = tuple(str(item) for item in raw_group_by)
    if key not in GROUP_BY_ALIASES:
        raise ValueError("group_by must be trade_date, trade_date+sector, or trade_date+industry_group")
    return GROUP_BY_ALIASES[key]


def _order_sql(config: dict[str, Any]) -> str:
    return "ASC" if str(config.get("order", "desc")).lower() == "asc" else "DESC"


def _nodes_by_id(graph: dict[str, Any], errors: list[FactorLabIssue]) -> dict[str, dict[str, Any]]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        errors.append(FactorLabIssue("invalid_nodes", "nodes must be a list", field="nodes"))
        return {}
    result = {}
    for node in nodes:
        if not isinstance(node, dict):
            errors.append(FactorLabIssue("invalid_node", "node must be an object"))
            continue
        node_id = str(node.get("id") or "")
        if node_id in result:
            errors.append(FactorLabIssue("duplicate_node_id", f"duplicate node id: {node_id}", node_id=node_id))
            continue
        result[node_id] = node
    return result


def _edges(graph: dict[str, Any], errors: list[FactorLabIssue]) -> list[dict[str, Any]]:
    edges = graph.get("edges", [])
    if not isinstance(edges, list):
        errors.append(FactorLabIssue("invalid_edges", "edges must be a list", field="edges"))
        return []
    return [edge for edge in edges if isinstance(edge, dict)]


def _incoming_by_handle(edges: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    incoming: dict[str, dict[str, str]] = {}
    for edge in edges:
        target = str(edge.get("target") or "")
        source = str(edge.get("source") or "")
        handle = str(edge.get("target_handle") or "input")
        incoming.setdefault(target, {})[handle] = source
    return incoming


def _temporal_history_input_node_ids(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> set[str]:
    """Return the strict upstream closure of temporal nodes.

    A temporal CTE needs history from its inputs, but a graph that merely contains
    a temporal node must not widen unrelated factor-input CTEs to all history.
    """
    upstream: dict[str, set[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in nodes and target in nodes:
            upstream.setdefault(target, set()).add(source)

    pending = [
        source
        for node_id, node in nodes.items()
        if node["type"] in TEMPORAL_NODES
        for source in upstream.get(node_id, set())
    ]
    history_input_node_ids: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in history_input_node_ids:
            continue
        history_input_node_ids.add(node_id)
        pending.extend(upstream.get(node_id, set()))
    return history_input_node_ids


def _direct_temporal_factor_row_limits(
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, int]:
    """Bound direct factor→temporal inputs for a single screen date.

    `lag(k)` only requires the target observation plus its preceding k
    observations, and a rolling window only requires its own window.  Applying
    this exact bound to a direct factor input avoids sorting its entire history
    for a one-date screen.  Multi-date history runs retain the complete history.
    """
    if "trade_dates" in params or params["start_date"] != params["end_date"]:
        return {}

    history_input_node_ids = _temporal_history_input_node_ids(nodes, edges)
    downstream: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in nodes and target in nodes:
            downstream.setdefault(source, []).append(edge)

    row_limits: dict[str, int] = {}
    for node_id, node in nodes.items():
        if node["type"] != "factor_input":
            continue
        direct_temporal_edges = [
            edge
            for edge in downstream.get(node_id, [])
            if nodes[str(edge["target"])]["type"] in TEMPORAL_NODES
            and str(edge.get("target_handle") or "") == "input"
        ]
        if not direct_temporal_edges:
            continue
        # If this input also reaches a temporal node through an intermediate
        # calculation, its observation requirement is graph-dependent; retain
        # the full history for that general case.
        if any(
            str(edge["target"]) in history_input_node_ids
            for edge in downstream.get(node_id, [])
            if nodes[str(edge["target"])]["type"] not in TEMPORAL_NODES
        ):
            continue
        required_rows = []
        for edge in direct_temporal_edges:
            target = nodes[str(edge["target"])]
            config = _dict(target.get("config"))
            if target["type"] == "lag":
                required_rows.append(int(config["period"]) + 1)
            else:
                required_rows.append(int(config["window"]))
        row_limits[node_id] = max(required_rows)
    return row_limits


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _resolve_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _graph_hash(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cte_name(node_id: str) -> str:
    if not NODE_ID_RE.match(node_id):
        raise ValueError(f"invalid node id: {node_id}")
    return f"node_{node_id}"


def _param_prefix(node_id: str) -> str:
    return f"node_{node_id}"


def _validate_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise ValueError(f"invalid {field_name}: {value!r}")
    return value


def _validate_factor_id(value: str) -> str:
    if not isinstance(value, str) or not FACTOR_ID_RE.match(value):
        raise ValueError(f"invalid factor_id: {value!r}")
    return value


def _is_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)
