from __future__ import annotations

import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


API = "http://127.0.0.1:8000"
START = "2022-07-01"
END = "2026-08-24"
SECTORS = ["10", "15", "20", "25", "30", "35", "45", "50", "55", "60"]

FACTOR_SPECS = {
    "rim": ("rim_upside_potential", "higher_better"),
    "k": ("k_ratio_3y", "higher_better"),
    "lowvol": ("vol_12_1_ann", "lower_better"),
    "bpr": ("bpr", "higher_better"),
    "accrual": ("accrual_ratio", "lower_better"),
    "assetgrowth": ("asset_yoy_pct", "lower_better"),
    "rnd_margin": ("rnd_margin", "higher_better"),
    "iroe": ("iroe", "higher_better"),
    "rpr": ("rpr", "higher_better"),
    "duration": ("equity_duration_20y", "lower_better"),
}

CANDIDATES = {
    "US": {
        "rim90_k10": {"rim": 0.9, "k": 0.1},
        "rim80_k10_iroe10": {"rim": 0.8, "k": 0.1, "iroe": 0.1},
        "rim80_k10_rndmargin10": {"rim": 0.8, "k": 0.1, "rnd_margin": 0.1},
        "rim80_k10_rpr10": {"rim": 0.8, "k": 0.1, "rpr": 0.1},
        "rim80_k10_duration10": {"rim": 0.8, "k": 0.1, "duration": 0.1},
        "rim80_k10_iroe05_rpr05": {"rim": 0.8, "k": 0.1, "iroe": 0.05, "rpr": 0.05},
    },
    "KR": {
        "bpr80_accrual20": {"bpr": 0.8, "accrual": 0.2},
        "bpr70_accrual20_iroe10": {"bpr": 0.7, "accrual": 0.2, "iroe": 0.1},
        "bpr70_accrual20_rndmargin10": {"bpr": 0.7, "accrual": 0.2, "rnd_margin": 0.1},
        "bpr70_accrual20_rpr10": {"bpr": 0.7, "accrual": 0.2, "rpr": 0.1},
        "bpr70_accrual20_duration10": {"bpr": 0.7, "accrual": 0.2, "duration": 0.1},
        "bpr60_accrual20_iroe10_rpr10": {"bpr": 0.6, "accrual": 0.2, "iroe": 0.1, "rpr": 0.1},
    },
}


def node(node_id: str, node_type: str, x: float, y: float, config: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "config": config}


def edge(source: str, target: str, handle: str = "input") -> dict[str, str]:
    return {
        "id": f"edge_{source}_{target}_{handle}",
        "source": source,
        "source_handle": "out",
        "target": target,
        "target_handle": handle,
    }


def build_graph(market: str, candidate: str) -> dict[str, Any]:
    weights = CANDIDATES[market][candidate]
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for index, handle in enumerate(weights):
        factor_id, direction = FACTOR_SPECS[handle]
        y = 80 + index * 170
        nodes.extend(
            [
                node(f"{handle}_input", "factor_input", 60, y, {"factor_id": factor_id, "financial_basis": "annual", "missing_policy": "drop"}),
                node(f"{handle}_winsor", "winsorize", 280, y, {"group_by": ["trade_date"], "lower_quantile": 0.01, "upper_quantile": 0.99}),
                node(
                    f"{handle}_score",
                    "shrunk_zscore",
                    500,
                    y,
                    {
                        "group_key": "sector",
                        "min_market_count": 20,
                        "min_group_count": 20,
                        "shrinkage_strength": 20,
                        "direction": direction,
                        "clip": 3.0,
                    },
                ),
            ]
        )
        edges.extend([edge(f"{handle}_input", f"{handle}_winsor"), edge(f"{handle}_winsor", f"{handle}_score")])

    if len(weights) == 1:
        alpha_node = f"{next(iter(weights))}_score"
    else:
        alpha_node = "market_alpha"
        nodes.append(
            node(
                alpha_node,
                "weighted_score",
                770,
                180,
                {
                    "weights": weights,
                    "missing_weight_renormalize": False,
                    "research_design": f"market_specific_economic_{market.lower()}_{candidate}",
                },
            )
        )
        for handle in weights:
            edges.append(edge(f"{handle}_score", alpha_node, handle))

    nodes.extend(
        [
            node("solvency_input", "factor_input", 60, 650, {"factor_id": "interest_coverage", "financial_basis": "annual", "missing_policy": "drop"}),
            node("solvency_winsor", "winsorize", 280, 650, {"group_by": ["trade_date"], "lower_quantile": 0.01, "upper_quantile": 0.99}),
            node("solvency_percentile", "dense_score", 500, 650, {"group_by": ["trade_date"], "order": "desc", "scale": "0_100", "tie_method": "average", "missing_score": None}),
            node("solvency_floor", "constant", 500, 760, {"value": 50.0}),
            node("solvency_ok", "greater_than", 730, 700, {}),
            node("mcap_input", "factor_input", 60, 850, {"factor_id": "mcap_mil", "financial_basis": "annual", "missing_policy": "drop"}),
            node("mcap_percentile", "dense_score", 280, 850, {"group_by": ["trade_date"], "order": "desc", "scale": "0_100", "tie_method": "average", "missing_score": None}),
            node("mcap_floor", "constant", 280, 960, {"value": 20.0}),
            node("mcap_ok", "greater_than", 520, 900, {}),
            node("investable", "and", 950, 800, {}),
            node(
                "core_score",
                "condition_score",
                1190,
                500,
                {
                    "research_design": f"market_specific_economic_{market.lower()}_{candidate}",
                    "portfolio_hint": {
                        "top_percent": 20,
                        "max_positions": 100,
                        "rebalance_frequency": "semiannual",
                        "transaction_cost_bps": 20,
                    },
                },
            ),
        ]
    )
    edges.extend(
        [
            edge("solvency_input", "solvency_winsor"),
            edge("solvency_winsor", "solvency_percentile"),
            edge("solvency_percentile", "solvency_ok", "left"),
            edge("solvency_floor", "solvency_ok", "right"),
            edge("mcap_input", "mcap_percentile"),
            edge("mcap_percentile", "mcap_ok", "left"),
            edge("mcap_floor", "mcap_ok", "right"),
            edge("solvency_ok", "investable", "left"),
            edge("mcap_ok", "investable", "right"),
            edge("investable", "core_score", "condition"),
            edge(alpha_node, "core_score", "score"),
        ]
    )
    return {
        "version": 1,
        "experiment": {
            "name": f"research_market_specific_{market}_{candidate}",
            "market": market,
            "start_date": START,
            "end_date": END,
            "factor_data_mode": "point_in_time_snapshot",
            "universe": {"type": "market", "sector_codes": SECTORS, "industry_group_codes": []},
            "rebalance": {"frequency": "semiannual", "signal_lag_days": 1, "transaction_cost_bps": 20},
        },
        "nodes": nodes,
        "edges": edges,
        "outputs": {"final_node_id": "core_score"},
    }


def post(path: str, body: dict[str, Any], timeout: int = 900) -> requests.Response:
    return requests.post(API + path, json=body, timeout=timeout)


def evaluate(task: tuple[str, str]) -> dict[str, Any]:
    market, candidate = task
    graph = build_graph(market, candidate)
    started = time.monotonic()
    validation = post("/api/factor-lab/validate", graph, 60)
    if validation.status_code != 200 or not validation.json().get("valid"):
        return {"market": market, "candidate": candidate, "stage": "validate", "detail": validation.json()}
    response = post(
        "/api/factor-lab/runs",
        {"graph": graph, "mode": "history", "history_start_date": START, "history_end_date": END, "history_rebalance_frequency": "semiannual"},
    )
    if response.status_code != 200:
        return {"market": market, "candidate": candidate, "stage": "run", "status": response.status_code, "detail": response.json()}
    run = response.json()
    top_percent = 15 if market == "US" else 20
    tests = [("base", top_percent, 20, START, END)]
    backtests: dict[str, Any] = {}
    for label, top_percent, cost, test_start, test_end in tests:
        result = post(
            f"/api/factor-lab/runs/{run['run_id']}/backtest",
            {
                "top_percent": top_percent,
                "start_date": test_start,
                "end_date": test_end,
                "rebalance_frequency": "semiannual",
                "market": market.lower(),
                "benchmarks": ["US_SP500", "US_NASDAQ"] if market == "US" else ["KOSPI200", "KOSDAQ"],
                "max_positions": 100,
                "transaction_cost_bps": cost,
            },
        )
        if result.status_code != 200:
            backtests[label] = {"status": result.status_code, "detail": result.json()}
            continue
        payload = result.json()
        backtests[label] = {
            "summary": payload["summary"],
            "position_counts": [len(item.get("positions") or []) for item in payload.get("rebalance_history", [])],
        }
    return {
        "market": market,
        "candidate": candidate,
        "weights": CANDIDATES[market][candidate],
        "stage": "complete",
        "run_id": run["run_id"],
        "quality": run["quality"],
        "backtests": backtests,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main() -> None:
    tasks = [(market, candidate) for market, candidates in CANDIDATES.items() for candidate in candidates]
    output = Path(tempfile.gettempdir()) / "arcana_market_specific_innovation_duration_20260824.jsonl"
    with output.open("w", encoding="utf-8") as handle:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(evaluate, task) for task in tasks]
            for future in as_completed(futures):
                result = future.result()
                line = json.dumps(result, ensure_ascii=False, default=str)
                handle.write(line + "\n")
                handle.flush()
                print(line)
    print(json.dumps({"output": str(output), "tasks": len(tasks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
