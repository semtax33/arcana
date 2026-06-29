from __future__ import annotations

from datetime import date, datetime
import json
import uuid
from typing import Any, Callable

from api.config.clickhouse import get_clickhouse_client
from api.repository.factor_lab_query import (
    FactorLabCompileResult,
    FactorLabIssue,
    build_factor_lab_insert_query,
    build_invalid_reason_counts_query,
    build_node_preview_query,
    build_quality_summary_query,
    build_run_ranking_query,
    compile_factor_lab_graph,
    node_type_specs,
    validate_factor_lab_graph,
)
from api.service.factor_identity import canonical_factor_id
from api.service.backtest_service import BacktestService
from api.service.dto import (
    FactorBacktestRequestDto,
    FactorConditionDto,
    FactorLabBacktestRequestDto,
    FactorLabCompileResponseDto,
    FactorLabExperimentDeleteResponseDto,
    FactorLabExperimentResponseDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabIssueDto,
    FactorLabNodePreviewResponseDto,
    FactorLabNodeTypeDto,
    FactorLabPreviewRowDto,
    FactorLabQualitySummaryDto,
    FactorLabRunRequestDto,
    FactorLabRunResponseDto,
    FactorLabRunRowDto,
    FactorLabValidationResponseDto,
)


FACTOR_LAB_DDL = [
    """
CREATE TABLE IF NOT EXISTS factor_lab_experiment
(
    experiment_id UUID,
    name String,
    graph_json String,
    final_node_id String,
    market LowCardinality(String),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3),
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY experiment_id
""".strip(),
    """
CREATE TABLE IF NOT EXISTS factor_lab_run
(
    run_id UUID,
    experiment_id Nullable(UUID),
    graph_hash String,
    status LowCardinality(String),
    start_date Date,
    end_date Date,
    error String,
    started_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3),
    finished_at Nullable(DateTime64(3, 'Asia/Seoul'))
)
ENGINE = ReplacingMergeTree(started_at)
ORDER BY run_id
""".strip(),
    """
CREATE TABLE IF NOT EXISTS factor_lab_node_cache
(
    run_id UUID,
    node_id String,
    trade_date Date,
    security_id String,
    value Nullable(Float64),
    is_valid Bool,
    invalid_reason String,
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (run_id, node_id, trade_date, security_id)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS factor_lab_values
(
    security_id String,
    trade_date Date,
    factor_id LowCardinality(String),
    financial_basis LowCardinality(String) DEFAULT 'lab',
    factor_value Nullable(Float64),
    fiscal_year Nullable(UInt16),
    financial_period Nullable(Date),
    currency LowCardinality(String) DEFAULT '',
    run_id UUID,
    node_id String,
    is_valid Bool,
    invalid_reason String,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (factor_id, trade_date, security_id)
""".strip(),
]


class FactorLabService:
    def __init__(self, client_factory: Callable[[], Any] = get_clickhouse_client) -> None:
        self._client_factory = client_factory

    def node_types(self) -> list[FactorLabNodeTypeDto]:
        return [
            FactorLabNodeTypeDto(
                type=spec.type,
                group=spec.group,
                inputs=spec.inputs,
                outputs=spec.outputs,
                config_schema=spec.config_schema,
            )
            for spec in node_type_specs()
        ]

    def validate_graph(self, graph: FactorLabGraphDto) -> FactorLabValidationResponseDto:
        graph_dict = _model_dump(graph)
        client = self._client_factory()
        try:
            known_factor_ids = _load_known_factor_ids(client, graph_dict)
            result = validate_factor_lab_graph(graph_dict, known_factor_ids=known_factor_ids)
        finally:
            _close(client)
        return _validation_response(result)

    def compile_graph(self, graph: FactorLabGraphDto) -> FactorLabCompileResponseDto:
        graph_dict = _model_dump(graph)
        client = self._client_factory()
        try:
            known_factor_ids = _load_known_factor_ids(client, graph_dict)
            result = compile_factor_lab_graph(graph_dict, known_factor_ids=known_factor_ids)
        finally:
            _close(client)
        return _compile_response(result)

    def save_experiment(
        self,
        request: FactorLabExperimentSaveRequestDto,
    ) -> FactorLabExperimentResponseDto:
        experiment_id = str(uuid.uuid4())
        self._write_experiment(experiment_id, request.graph, require_existing=False)
        return FactorLabExperimentResponseDto(experiment_id=experiment_id, graph=request.graph)

    def update_experiment(
        self,
        experiment_id: str,
        request: FactorLabExperimentSaveRequestDto,
    ) -> FactorLabExperimentResponseDto:
        self._write_experiment(experiment_id, request.graph, require_existing=True)
        return FactorLabExperimentResponseDto(experiment_id=experiment_id, graph=request.graph)

    def delete_experiment(self, experiment_id: str) -> FactorLabExperimentDeleteResponseDto:
        client = self._client_factory()
        try:
            _ensure_tables(client)
            if not _experiment_exists(client, experiment_id):
                raise KeyError(experiment_id)
            run_ids = _load_experiment_run_ids(client, experiment_id)
            factor_ids = [_lab_factor_id(run_id) for run_id in run_ids]
            _execute(
                client,
                """
ALTER TABLE factor_lab_experiment
DELETE WHERE experiment_id = {experiment_id:UUID}
""".strip(),
                {"experiment_id": experiment_id},
            )
            _execute(
                client,
                """
ALTER TABLE factor_lab_run
DELETE WHERE experiment_id = {experiment_id:UUID}
""".strip(),
                {"experiment_id": experiment_id},
            )
            if run_ids:
                _execute(
                    client,
                    """
ALTER TABLE factor_lab_node_cache
DELETE WHERE has({run_ids:Array(UUID)}, run_id)
""".strip(),
                    {"run_ids": run_ids},
                )
                _execute(
                    client,
                    """
ALTER TABLE factor_lab_values
DELETE WHERE has({run_ids:Array(UUID)}, run_id)
""".strip(),
                    {"run_ids": run_ids},
                )
            if factor_ids:
                _execute(
                    client,
                    """
ALTER TABLE factor_catalog
DELETE WHERE has({factor_ids:Array(String)}, factor_id)
    AND factor_type = 'lab'
    AND factor_group = 'factor_lab'
""".strip(),
                    {"factor_ids": factor_ids},
                )
        finally:
            _close(client)
        return FactorLabExperimentDeleteResponseDto(deleted=True)

    def _write_experiment(
        self,
        experiment_id: str,
        graph: FactorLabGraphDto,
        *,
        require_existing: bool,
    ) -> None:
        validation = self.validate_graph(graph)
        if not validation.valid:
            raise ValueError(_format_validation_errors(validation.errors))

        graph_dict = _model_dump(graph)
        client = self._client_factory()
        try:
            _ensure_tables(client)
            if require_existing and not _experiment_exists(client, experiment_id):
                raise KeyError(experiment_id)
            _execute(
                client,
                """
INSERT INTO factor_lab_experiment
(
    experiment_id,
    name,
    graph_json,
    final_node_id,
    market
)
VALUES
(
    {experiment_id:UUID},
    {name:String},
    {graph_json:String},
    {final_node_id:String},
    {market:String}
)
""".strip(),
                {
                    "experiment_id": experiment_id,
                    "name": str(graph_dict["experiment"].get("name") or "factor_lab_experiment"),
                    "graph_json": json.dumps(graph_dict, sort_keys=True, default=str),
                    "final_node_id": graph_dict["outputs"]["final_node_id"],
                    "market": str(graph_dict["experiment"].get("market") or ""),
                },
            )
        finally:
            _close(client)

    def get_experiment(self, experiment_id: str) -> FactorLabExperimentResponseDto:
        client = self._client_factory()
        try:
            rows = _records(
                client.query_df(
                    """
SELECT
    experiment_id,
    graph_json
FROM factor_lab_experiment FINAL
WHERE experiment_id = {experiment_id:UUID}
ORDER BY updated_at DESC
LIMIT 1
""".strip(),
                    parameters={"experiment_id": experiment_id},
                )
            )
        finally:
            _close(client)
        if not rows:
            raise KeyError(experiment_id)
        graph = FactorLabGraphDto(**json.loads(str(rows[0]["graph_json"])))
        return FactorLabExperimentResponseDto(experiment_id=str(rows[0]["experiment_id"]), graph=graph)

    def run_graph(self, request: FactorLabRunRequestDto) -> FactorLabRunResponseDto:
        experiment_id = request.experiment_id
        graph = request.graph
        if graph is None:
            if not experiment_id:
                raise ValueError("graph or experiment_id is required")
            graph = self.get_experiment(experiment_id).graph

        graph_dict = _model_dump(graph)
        run_id = str(uuid.uuid4())
        factor_id = _lab_factor_id(run_id)
        client = self._client_factory()
        try:
            _ensure_tables(client)
            known_factor_ids = _load_known_factor_ids(client, graph_dict)
            compile_result = compile_factor_lab_graph(graph_dict, known_factor_ids=known_factor_ids)
            _insert_run_status(
                client,
                run_id=run_id,
                experiment_id=experiment_id,
                graph_hash=compile_result.graph_hash,
                status="running",
                graph_dict=graph_dict,
                error="",
            )
            insert_query, params = build_factor_lab_insert_query(
                compile_result,
                factor_id=factor_id,
                run_id=run_id,
            )
            _execute(client, insert_query, params)
            _insert_factor_catalog(client, factor_id=factor_id, run_id=run_id)
            _insert_run_status(
                client,
                run_id=run_id,
                experiment_id=experiment_id,
                graph_hash=compile_result.graph_hash,
                status="completed",
                graph_dict=graph_dict,
                error="",
            )
            quality = _load_quality(client, run_id=run_id, node_id=None)
            run_rows = _load_run_rows(client, run_id=run_id)
        except Exception as exc:
            try:
                _insert_run_status(
                    client,
                    run_id=run_id,
                    experiment_id=experiment_id,
                    graph_hash="",
                    status="failed",
                    graph_dict=graph_dict,
                    error=str(exc),
                )
            except Exception:
                pass
            raise
        finally:
            _close(client)

        return FactorLabRunResponseDto(
            run_id=run_id,
            experiment_id=experiment_id,
            factor_id=factor_id,
            status="completed",
            final_node_id=compile_result.final_node_id,
            graph_hash=compile_result.graph_hash,
            quality=quality,
            warnings=[issue.message for issue in compile_result.warnings],
            rows=run_rows,
            results=run_rows,
            rankings=run_rows,
            positions=run_rows,
        )

    def get_run(self, run_id: str) -> FactorLabRunResponseDto:
        client = self._client_factory()
        try:
            rows = _records(
                client.query_df(
                    """
SELECT
    run_id,
    experiment_id,
    graph_hash,
    status
FROM factor_lab_run FINAL
WHERE run_id = {run_id:UUID}
ORDER BY started_at DESC
LIMIT 1
""".strip(),
                    parameters={"run_id": run_id},
                )
            )
            quality = _load_quality(client, run_id=run_id, node_id=None)
            run_rows = _load_run_rows(client, run_id=run_id)
        finally:
            _close(client)
        if not rows:
            raise KeyError(run_id)
        row = rows[0]
        factor_id = _lab_factor_id(run_id)
        return FactorLabRunResponseDto(
            run_id=str(row["run_id"]),
            experiment_id=_optional_str(row.get("experiment_id")),
            factor_id=factor_id,
            status=str(row["status"]),
            final_node_id="",
            graph_hash=str(row["graph_hash"]),
            quality=quality,
            rows=run_rows,
            results=run_rows,
            rankings=run_rows,
            positions=run_rows,
        )

    def preview_node(
        self,
        run_id: str,
        *,
        node_id: str | None = None,
        limit: int = 100,
    ) -> FactorLabNodePreviewResponseDto:
        client = self._client_factory()
        try:
            query, params = build_node_preview_query(run_id=run_id, node_id=node_id, limit=limit)
            rows = _records(client.query_df(query, parameters=params))
            quality = _load_quality(client, run_id=run_id, node_id=node_id)
        finally:
            _close(client)
        return FactorLabNodePreviewResponseDto(
            run_id=run_id,
            node_id=node_id,
            rows=[
                FactorLabPreviewRowDto(
                    trade_date=row["trade_date"],
                    security_id=str(row["security_id"]),
                    value=_float_or_none(row.get("value")),
                    is_valid=bool(row.get("is_valid", True)),
                    invalid_reason=str(row.get("invalid_reason") or ""),
                )
                for row in rows
            ],
            quality=quality,
        )

    def run_backtest(self, run_id: str, request: FactorLabBacktestRequestDto):
        client = self._client_factory()
        try:
            _ensure_tables(client)
            run_status = _load_run_status(client, run_id)
            if run_status is None:
                raise KeyError(run_id)
            if run_status != "completed":
                raise ValueError(f"factor lab run is not completed: {run_status}")
            if _load_run_value_count(client, run_id) <= 0:
                raise ValueError("factor lab run has no completed factor values")
        finally:
            _close(client)

        factor_id = _lab_factor_id(run_id)
        backtest_request = FactorBacktestRequestDto(
            conditions=[
                FactorConditionDto(
                    factor_id=factor_id,
                    mode="top_percent",
                    top_percent=request.top_percent,
                    rank_direction="higher",
                    percentile_side="top",
                )
            ],
            start_date=request.start_date,
            end_date=request.end_date,
            rebalance_frequency=request.rebalance_frequency,
            market=request.market,
            financial_basis="lab",
            benchmarks=request.benchmarks,
            max_positions=request.max_positions,
            transaction_cost_bps=request.transaction_cost_bps,
            factor_table="factor_lab_values",
        )
        return BacktestService(client_factory=self._client_factory).run_factor_backtest(backtest_request)


def _load_known_factor_ids(client: Any, graph: dict[str, Any]) -> set[str]:
    factor_ids = sorted(
        {
            canonical_factor_id(str(node.get("config", {}).get("factor_id") or ""))
            for node in graph.get("nodes", [])
            if node.get("type") == "factor_input" and node.get("config", {}).get("factor_id")
        }
    )
    if not factor_ids:
        return set()
    rows = _records(
        client.query_df(
            """
SELECT factor_id
FROM factor_catalog
WHERE has({factor_ids:Array(String)}, factor_id)
""".strip(),
            parameters={"factor_ids": factor_ids},
        )
    )
    known = {str(row["factor_id"]) for row in rows}
    known.update(factor_id for factor_id in factor_ids if factor_id.startswith("lab_"))
    return known


def _experiment_exists(client: Any, experiment_id: str) -> bool:
    rows = _records(
        client.query_df(
            """
SELECT 1 AS found
FROM factor_lab_experiment FINAL
WHERE experiment_id = {experiment_id:UUID}
LIMIT 1
""".strip(),
            parameters={"experiment_id": experiment_id},
        )
    )
    return bool(rows)


def _load_experiment_run_ids(client: Any, experiment_id: str) -> list[str]:
    rows = _records(
        client.query_df(
            """
SELECT run_id
FROM factor_lab_run FINAL
WHERE experiment_id = {experiment_id:UUID}
""".strip(),
            parameters={"experiment_id": experiment_id},
        )
    )
    return [str(row["run_id"]) for row in rows]


def _load_run_status(client: Any, run_id: str) -> str | None:
    rows = _records(
        client.query_df(
            """
SELECT status
FROM factor_lab_run FINAL
WHERE run_id = {run_id:UUID}
ORDER BY started_at DESC
LIMIT 1
""".strip(),
            parameters={"run_id": run_id},
        )
    )
    if not rows:
        return None
    return str(rows[0].get("status") or "")


def _load_run_value_count(client: Any, run_id: str) -> int:
    rows = _records(
        client.query_df(
            """
SELECT count() AS row_count
FROM factor_lab_values
WHERE run_id = {run_id:UUID}
    AND is_valid
""".strip(),
            parameters={"run_id": run_id},
        )
    )
    if not rows:
        return 0
    return int(_float_or_none(rows[0].get("row_count")) or 0)


def _ensure_tables(client: Any) -> None:
    for ddl in FACTOR_LAB_DDL:
        _execute(client, ddl, {})


def _insert_run_status(
    client: Any,
    *,
    run_id: str,
    experiment_id: str | None,
    graph_hash: str,
    status: str,
    graph_dict: dict[str, Any],
    error: str,
) -> None:
    experiment = graph_dict.get("experiment", {})
    _execute(
        client,
        """
INSERT INTO factor_lab_run
(
    run_id,
    experiment_id,
    graph_hash,
    status,
    start_date,
    end_date,
    error,
    finished_at
)
VALUES
(
    {run_id:UUID},
    {experiment_id:Nullable(UUID)},
    {graph_hash:String},
    {status:String},
    {start_date:Date},
    {end_date:Date},
    {error:String},
    {finished_at:Nullable(DateTime64(3, 'Asia/Seoul'))}
)
""".strip(),
        {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "graph_hash": graph_hash,
            "status": status,
            "start_date": _date_iso(experiment.get("start_date")),
            "end_date": _date_iso(experiment.get("end_date")),
            "error": error,
            "finished_at": datetime.now() if status in {"completed", "failed"} else None,
        },
    )


def _insert_factor_catalog(client: Any, *, factor_id: str, run_id: str) -> None:
    _execute(
        client,
        """
INSERT INTO factor_catalog
(
    factor_id,
    factor_name,
    factor_type,
    factor_group,
    unit,
    value_direction,
    description,
    is_active,
    created_at,
    updated_at
)
VALUES
(
    {factor_id:String},
    {factor_name:String},
    'lab',
    'factor_lab',
    'score',
    'HIGHER_BETTER',
    {description:String},
    true,
    now(),
    now()
)
""".strip(),
        {
            "factor_id": factor_id,
            "factor_name": f"Factor Lab {run_id}",
            "description": f"Generated by factor lab run {run_id}",
        },
    )


def _load_quality(client: Any, *, run_id: str, node_id: str | None) -> FactorLabQualitySummaryDto:
    summary_query, summary_params = build_quality_summary_query(run_id=run_id, node_id=node_id)
    reason_query, reason_params = build_invalid_reason_counts_query(run_id=run_id, node_id=node_id)
    summary_rows = _records(client.query_df(summary_query, parameters=summary_params))
    reason_rows = _records(client.query_df(reason_query, parameters=reason_params))
    if not summary_rows:
        return FactorLabQualitySummaryDto()
    row = summary_rows[0]
    return FactorLabQualitySummaryDto(
        input_rows=int(_float_or_none(row.get("input_rows")) or 0),
        valid_rows=int(_float_or_none(row.get("valid_rows")) or 0),
        invalid_rows=int(_float_or_none(row.get("invalid_rows")) or 0),
        dropped_rows=0,
        invalid_reason_counts={
            str(reason.get("invalid_reason") or ""): int(_float_or_none(reason.get("row_count")) or 0)
            for reason in reason_rows
        },
        date_coverage={
            "min": row.get("min_trade_date"),
            "max": row.get("max_trade_date"),
        },
        security_coverage=int(_float_or_none(row.get("security_count")) or 0),
    )


def _load_run_rows(client: Any, *, run_id: str, limit: int = 100) -> list[FactorLabRunRowDto]:
    query, params = build_run_ranking_query(run_id=run_id, limit=limit)
    rows = _records(client.query_df(query, parameters=params))
    return [
        FactorLabRunRowDto(
            rank=int(_float_or_none(row.get("rank")) or index + 1),
            security_id=str(row["security_id"]),
            ticker=_optional_str(row.get("ticker")),
            stock_name=_optional_str(row.get("stock_name")),
            trade_date=row["trade_date"],
            factor_id=_optional_str(row.get("factor_id")),
            factor_value=_float_or_none(row.get("factor_value")),
            value=_float_or_none(row.get("factor_value")),
            score=_float_or_none(row.get("score")),
            percentile_score=_float_or_none(row.get("percentile_score")),
            is_valid=bool(row.get("is_valid", True)),
            invalid_reason=str(row.get("invalid_reason") or ""),
        )
        for index, row in enumerate(rows)
    ]


def _validation_response(result) -> FactorLabValidationResponseDto:
    return FactorLabValidationResponseDto(
        valid=result.valid,
        errors=[_issue_dto(issue) for issue in result.errors],
        warnings=[_issue_dto(issue) for issue in result.warnings],
        execution_order=result.execution_order,
        final_node_id=result.final_node_id,
        graph_hash=result.graph_hash,
    )


def _compile_response(result: FactorLabCompileResult) -> FactorLabCompileResponseDto:
    return FactorLabCompileResponseDto(
        query=result.query,
        parameters=result.parameters,
        final_node_id=result.final_node_id,
        execution_order=result.execution_order,
        graph_hash=result.graph_hash,
        warnings=[_issue_dto(issue) for issue in result.warnings],
    )


def _issue_dto(issue: FactorLabIssue) -> FactorLabIssueDto:
    return FactorLabIssueDto(
        code=issue.code,
        message=issue.message,
        node_id=issue.node_id,
        field=issue.field,
    )


def _execute(client: Any, query: str, parameters: dict[str, Any] | None = None) -> None:
    parameters = parameters or {}
    command = getattr(client, "command", None)
    if callable(command):
        command(query, parameters=parameters)
        return
    query_method = getattr(client, "query", None)
    if callable(query_method):
        query_method(query, parameters=parameters)
        return
    client.query_df(query, parameters=parameters)


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    return list(frame)


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _lab_factor_id(run_id: str) -> str:
    return f"lab_{str(run_id).replace('-', '')}"


def _date_iso(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _format_validation_errors(errors: list[FactorLabIssueDto]) -> str:
    return "; ".join(f"{error.code}: {error.message}" for error in errors)
