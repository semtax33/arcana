"""Persist and evaluate immutable FactorLab score runs independently of signals."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import uuid
from zoneinfo import ZoneInfo

import pandas as pd

from api.config.clickhouse import get_clickhouse_client
from api.repository.factor_lab_query import compile_factor_lab_graph
from api.service.factor_identity import canonical_factor_id
from api.service.factor_lab_outcomes import evaluate_forward_outcome
from api.service.factor_lab_earnings import evaluate_earnings_outcome


EVALUATION_DDL = [
    """CREATE TABLE IF NOT EXISTS factor_lab_run_definition (
        run_id UUID, graph_hash String, graph_json String,
        created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
    ) ENGINE=MergeTree ORDER BY run_id""",
    """CREATE TABLE IF NOT EXISTS factor_lab_evaluation (
        evaluation_id UUID, run_id UUID, as_of Date, result_json String, inputs_json String,
        created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
    ) ENGINE=MergeTree ORDER BY (run_id, created_at, evaluation_id)""",
]


def _records(frame):
    if frame is None:
        return []
    if isinstance(frame, pd.DataFrame):
        rows = frame.astype(object).where(pd.notna(frame), None).to_dict("records")
        for row in rows:
            for key in ("trade_date", "source_trade_date"):
                if isinstance(row.get(key), datetime):
                    row[key] = row[key].date()
        return rows
    return list(frame)


def _json(value):
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def prepare_evaluation_run(client, run_id, graph, *, factor_table, trade_dates=None):
    """Freeze selected score inputs before any future labels are queried."""
    evaluation_ids = graph.get("outputs", {}).get("evaluation_node_ids", [])
    for ddl in EVALUATION_DDL:
        client.command(ddl)
    score_ids = sorted({e["source"] for e in graph["edges"] if e["target"] in evaluation_ids and e.get("target_handle") == "score"})
    for score_id in score_ids:
        score_graph = deepcopy(graph)
        score_graph["outputs"] = {"final_node_id": score_id, "evaluation_node_ids": []}
        compiled = compile_factor_lab_graph(score_graph, trade_dates=trade_dates, factor_table=factor_table)
        client.command(f"""INSERT INTO factor_lab_node_cache
            (run_id, node_id, trade_date, security_id, value, is_valid, invalid_reason)
            SELECT {{evaluation_run_id:UUID}}, {{evaluation_score_id:String}},
                trade_date, security_id, value, is_valid, invalid_reason
            FROM ({compiled.query})""", parameters={**compiled.parameters,
                "evaluation_run_id": run_id, "evaluation_score_id": score_id})
    # Keep the actual execution dates/source modes, not a mutable experiment reference.
    compiled = compile_factor_lab_graph(graph, trade_dates=trade_dates, factor_table=factor_table)
    client.command("""INSERT INTO factor_lab_run_definition (run_id, graph_hash, graph_json)
        VALUES ({run_id:UUID}, {graph_hash:String}, {graph_json:String})""",
        parameters={"run_id": run_id, "graph_hash": compiled.graph_hash, "graph_json": _json(graph)})


class FactorLabEvaluationService:
    def __init__(self, client_factory=get_clickhouse_client):
        self._client_factory = client_factory

    def evaluate(self, run_id: str, *, as_of: date | None = None) -> dict:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        as_of = as_of or today
        if as_of > today:
            raise ValueError("evaluation as_of cannot be in the future")
        client = self._client_factory()
        try:
            for ddl in EVALUATION_DDL:
                client.command(ddl)
            definitions = _records(client.query_df("""SELECT graph_json, graph_hash
                FROM factor_lab_run_definition WHERE run_id = {run_id:UUID}
                ORDER BY created_at DESC LIMIT 1""", parameters={"run_id": run_id}))
            if not definitions:
                raise KeyError("run has no frozen evaluation definition; execute a graph v2 evaluation first")
            runs = _records(client.query_df("""SELECT status FROM factor_lab_run FINAL
                WHERE run_id = {run_id:UUID} ORDER BY started_at DESC LIMIT 1""",
                parameters={"run_id": run_id}))
            if not runs or runs[0]["status"] != "completed":
                raise ValueError("evaluation requires a completed score run")
            definition = definitions[0]
            graph = json.loads(definition["graph_json"])
            nodes = {n["id"]: n for n in graph["nodes"]}
            inputs = {e["target"]: e["source"] for e in graph["edges"] if e.get("target_handle") == "score"}
            evaluations, manifests = [], []
            score_cache = {}
            for node_id in graph["outputs"]["evaluation_node_ids"]:
                source_id = inputs[node_id]
                if source_id not in score_cache:
                    score_cache[source_id] = _records(client.query_df("""SELECT trade_date, security_id, value
                        FROM factor_lab_node_cache WHERE run_id = {run_id:UUID} AND node_id = {node_id:String}
                        AND is_valid ORDER BY trade_date, security_id LIMIT 1000001""",
                        parameters={"run_id": run_id, "node_id": source_id}))
                scores = score_cache[source_id]
                if len(scores) > 1_000_000:
                    raise ValueError("evaluation exceeds 1,000,000 score rows; reduce the signal date range")
                if any(r["trade_date"] > as_of for r in scores):
                    raise ValueError("evaluation as_of cannot precede the latest signal date")
                config = nodes[node_id]["config"]
                start = min((r["trade_date"] for r in scores), default=as_of)
                if nodes[node_id]["type"] == "earnings_outcome":
                    field = {k: k for k in ("surprise_pct", "reported_eps", "estimated_eps")}[config["target_field"]]
                    events = _records(client.query_df(f"""SELECT security_id, event_date,
                        event.1 AS availability_date, event.2 AS fiscal_period_end,
                        if(event.1 <= {{as_of:Date}}, event.3, NULL) AS {field}, event.4 AS raw_path, event.5 AS snapshot_date
                        FROM (SELECT security_id, event_date,
                            argMin(tuple(availability_date, fiscal_period_end, {field}, raw_path, snapshot_date), tuple(snapshot_date, raw_path)) AS event
                            FROM us_consensus_events
                            WHERE provider = {{provider:String}} AND event_type = 'EARNINGS_RELEASE'
                                AND security_id IN {{security_ids:Array(String)}}
                                AND event_date > {{start:Date}} AND event_date <= {{as_of:Date}}
                            GROUP BY security_id, event_date)
                        ORDER BY security_id, event_date LIMIT 2000001""",
                        parameters={"provider": config["provider"], "security_ids": sorted({r["security_id"] for r in scores}), "start": start, "as_of": as_of})) if scores else []
                    if len(events) > 2_000_000:
                        raise ValueError("evaluation exceeds 2,000,000 event rows")
                    result = evaluate_earnings_outcome(config, scores, events, as_of)
                    manifests.append({"node_id": node_id, "scores": scores, "events": events, "observations": result.pop("observations")})
                    evaluations.append({"node_id": node_id, "node_version": nodes[node_id].get("version", 1), "score_node_id": source_id, **result})
                    continue
                market = str(graph["experiment"].get("market") or "").strip().upper()
                calendar = _records(client.query_df("""SELECT DISTINCT p.trade_date AS trade_date
                    FROM price_daily AS p INNER JOIN (
                        SELECT security_id, argMax(country, updated_at) AS country
                        FROM security_master GROUP BY security_id
                    ) AS sm ON sm.security_id = p.security_id
                    WHERE p.trade_date >= {start:Date} AND p.trade_date <= {as_of:Date}
                        AND ({market:String} = 'ALL' OR sm.country = {market:String})
                    ORDER BY trade_date""", parameters={"start": start, "as_of": as_of, "market": market}))
                days = [r["trade_date"] for r in calendar]
                positions = {d: i for i, d in enumerate(days)}
                required_dates = set()
                for score in scores:
                    d = score["trade_date"]
                    if d <= as_of:
                        required_dates.add(d)
                    for h in config["horizons"]:
                        if config["unit"] == "calendar_day":
                            target = d + timedelta(days=h)
                        else:
                            pos = positions.get(d)
                            target = days[pos + h] if pos is not None and pos + h < len(days) else None
                        if target is not None and target <= as_of:
                            required_dates.add(target)
                targets = _records(client.query_df("""SELECT trade_date, security_id,
                        tupleElement(argMax(tuple(factor_value), updated_at), 1) AS value,
                        tupleElement(argMax(tuple(toString(financial_period)), updated_at), 1) AS financial_period,
                        argMax(source_trade_date, updated_at) AS source_trade_date
                    FROM fact_daily_factor_snapshot
                    WHERE factor_id = {target_factor_id:String} AND financial_basis = {financial_basis:String}
                        AND trade_date IN {target_dates:Array(Date)} AND security_id IN {security_ids:Array(String)}
                    GROUP BY trade_date, security_id ORDER BY trade_date, security_id LIMIT 2000001""",
                    parameters={"target_factor_id": canonical_factor_id(config["target_factor_id"]),
                        "financial_basis": config["financial_basis"], "target_dates": sorted(required_dates),
                        "security_ids": sorted({r["security_id"] for r in scores})})) if scores else []
                if len(targets) > 2_000_000:
                    raise ValueError("evaluation exceeds 2,000,000 target snapshots; reduce the date range")
                result = evaluate_forward_outcome(config, scores, targets, days, as_of)
                manifests.append({"node_id": node_id, "scores": scores, "targets": targets,
                                  "calendar": days, "observations": result.pop("observations")})
                evaluations.append({"node_id": node_id, "node_version": nodes[node_id].get("version", 1),
                                    "score_node_id": source_id, **result})
            result = {"evaluation_id": str(uuid.uuid4()), "run_id": run_id, "as_of": as_of.isoformat(),
                      "graph_hash": definition["graph_hash"], "evaluations": evaluations}
            client.command("""INSERT INTO factor_lab_evaluation
                (evaluation_id, run_id, as_of, result_json, inputs_json)
                VALUES ({evaluation_id:UUID}, {run_id:UUID}, {as_of:Date}, {result_json:String}, {inputs_json:String})""",
                parameters={"evaluation_id": result["evaluation_id"], "run_id": run_id, "as_of": as_of,
                            "result_json": _json(result), "inputs_json": _json(manifests)})
            return result
        finally:
            client.close()

    def list_evaluations(self, run_id: str) -> dict:
        client = self._client_factory()
        try:
            for ddl in EVALUATION_DDL:
                client.command(ddl)
            rows = _records(client.query_df("""SELECT result_json FROM factor_lab_evaluation
                WHERE run_id = {run_id:UUID} ORDER BY created_at DESC, evaluation_id DESC LIMIT 100""",
                parameters={"run_id": run_id}))
            return {"run_id": run_id, "evaluations": [json.loads(r["result_json"]) for r in rows]}
        finally:
            client.close()
