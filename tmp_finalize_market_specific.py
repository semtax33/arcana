from __future__ import annotations

import json
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any

import requests

import tmp_market_specific_factorlab as lab


API = lab.API
START = lab.START
END = lab.END
FINAL = {
    "US": {
        "candidate": "rim90_k10",
        "weights": {"rim": 0.9, "k": 0.1},
        "top_percent": 15.0,
        "name": "Ungdroo_US_RIM_TrendStability_Robust_v6_20260824_FactorLab",
        "benchmark_end": "2026-07-22",
        "benchmarks": ["US_SP500", "US_NASDAQ"],
    },
    "KR": {
        "candidate": "bpr80_accrual20",
        "weights": {"bpr": 0.8, "accrual": 0.2},
        "top_percent": 20.0,
        "name": "Ungdroo_KR_Value_AccrualQuality_Robust_v6_20260824_FactorLab",
        "benchmark_end": "2026-07-24",
        "benchmarks": ["KOSPI200", "KOSDAQ"],
    },
}


def post(path: str, body: dict[str, Any], timeout: int = 900) -> dict[str, Any]:
    response = requests.post(API + path, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


def build_final_graph(market: str) -> dict[str, Any]:
    spec = FINAL[market]
    lab.CANDIDATES.setdefault(market, {})[spec["candidate"]] = spec["weights"]
    graph = lab.build_graph(market, spec["candidate"])
    graph["experiment"]["name"] = spec["name"]
    graph["experiment"]["start_date"] = START
    graph["experiment"]["end_date"] = END
    for node in graph["nodes"]:
        if node["id"] == "core_score":
            node["config"]["portfolio_hint"]["top_percent"] = spec["top_percent"]
            node["config"]["research_design"] = (
                "us_residual_income_value_with_small_price_path_stability_and_solvency_liquidity_guards"
                if market == "US"
                else "kr_book_to_price_with_accrual_quality_and_solvency_liquidity_guards"
            )
        if node["id"] == "market_alpha":
            node["config"]["research_design"] = (
                "us_rim_90_k_ratio_10"
                if market == "US"
                else "kr_bpr_80_low_accrual_20"
            )
    return graph


def run_history(graph: dict[str, Any], *, start: str = START, end: str = END) -> dict[str, Any]:
    return post(
        "/api/factor-lab/runs",
        {
            "graph": graph,
            "mode": "history",
            "history_start_date": start,
            "history_end_date": end,
            "history_rebalance_frequency": "semiannual",
        },
    )


def backtest(
    market: str,
    run_id: str,
    *,
    top_percent: float,
    start: str = START,
    end: str = END,
    cost: float = 20,
) -> dict[str, Any]:
    return post(
        f"/api/factor-lab/runs/{run_id}/backtest",
        {
            "top_percent": top_percent,
            "start_date": start,
            "end_date": end,
            "rebalance_frequency": "semiannual",
            "market": market.lower(),
            "benchmarks": FINAL[market]["benchmarks"],
            "max_positions": 100,
            "transaction_cost_bps": cost,
        },
    )


def compact_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": payload["summary"],
        "position_counts": [len(item.get("positions") or []) for item in payload.get("rebalance_history", [])],
        "warnings": payload.get("warnings", []),
    }


def nav_summary(points: list[tuple[str, float]]) -> dict[str, float | None]:
    clean = [(day, float(nav)) for day, nav in points if nav is not None]
    if len(clean) < 2:
        return {"cumulative_return": None, "cagr": None, "max_drawdown": None, "volatility": None, "sharpe": None}
    navs = [nav for _, nav in clean]
    returns = [navs[index] / navs[index - 1] - 1 for index in range(1, len(navs)) if navs[index - 1] != 0]
    years = max((date.fromisoformat(clean[-1][0]) - date.fromisoformat(clean[0][0])).days / 365.25, 1 / 365.25)
    peak = navs[0]
    drawdowns = []
    for nav in navs:
        peak = max(peak, nav)
        drawdowns.append(nav / peak - 1 if peak else 0.0)
    stddev = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    average = statistics.fmean(returns) if returns else 0.0
    return {
        "cumulative_return": navs[-1] - 1,
        "cagr": navs[-1] ** (1 / years) - 1,
        "max_drawdown": min(drawdowns),
        "volatility": stddev * math.sqrt(252) if stddev else None,
        "sharpe": average / stddev * math.sqrt(252) if stddev else None,
    }


def benchmark_summaries(payload: dict[str, Any], benchmark_ids: list[str]) -> dict[str, Any]:
    result = {"strategy": payload["summary"]}
    for benchmark_id in benchmark_ids:
        result[benchmark_id] = nav_summary(
            [
                (str(point["trade_date"]), point.get("benchmark_navs", {}).get(benchmark_id))
                for point in payload.get("equity_curve", [])
            ]
        )
    return result


def latest_top10(run: dict[str, Any]) -> dict[str, Any]:
    rows = run.get("rankings") or run.get("rows") or run.get("results") or []
    valid = [row for row in rows if row.get("is_valid", True) and row.get("score") is not None]
    valid.sort(key=lambda row: (row.get("rank") is None, row.get("rank") or 10**9, -(row.get("score") or -10**9)))
    return {
        "signal_date": max((str(row["trade_date"]) for row in valid), default=None),
        "rows": [
            {
                "rank": row.get("rank"),
                "ticker": row.get("ticker"),
                "stock_name": row.get("stock_name"),
                "score": row.get("score"),
            }
            for row in valid[:10]
        ],
    }


def sensitivity_task(task: tuple[str, str, dict[str, float], float]) -> dict[str, Any]:
    market, label, weights, solvency_floor = task
    candidate = f"sensitivity_{label}"
    lab.CANDIDATES.setdefault(market, {})[candidate] = weights
    graph = lab.build_graph(market, candidate)
    for node in graph["nodes"]:
        if node["id"] == "solvency_floor":
            node["config"]["value"] = solvency_floor
    validation = post("/api/factor-lab/validate", graph, 60)
    if not validation.get("valid"):
        return {"market": market, "label": label, "stage": "validate", "validation": validation}
    run = run_history(graph)
    result = backtest(market, run["run_id"], top_percent=FINAL[market]["top_percent"])
    return {
        "market": market,
        "label": label,
        "weights": weights,
        "solvency_floor": solvency_floor,
        "summary": result["summary"],
    }


def finalize_market(market: str) -> dict[str, Any]:
    spec = FINAL[market]
    graph = build_final_graph(market)
    validation = post("/api/factor-lab/validate", graph, 60)
    if not validation.get("valid"):
        raise RuntimeError(json.dumps(validation, ensure_ascii=False))
    saved = requests.put(API + "/api/factor-lab/experiments/by-name", json={"graph": graph}, timeout=120)
    saved.raise_for_status()
    saved_payload = saved.json()
    verified = requests.get(
        API + "/api/factor-lab/experiments/by-name",
        params={"name": spec["name"]},
        timeout=60,
    )
    verified.raise_for_status()
    verified_payload = verified.json()
    verified_validation = post("/api/factor-lab/validate", verified_payload["graph"], 60)

    run = run_history(verified_payload["graph"])
    top = spec["top_percent"]
    robustness = {
        "base": compact_backtest(backtest(market, run["run_id"], top_percent=top)),
        "early": compact_backtest(backtest(market, run["run_id"], top_percent=top, end="2024-06-30")),
        "late": compact_backtest(backtest(market, run["run_id"], top_percent=top, start="2024-07-01")),
        "cost50": compact_backtest(backtest(market, run["run_id"], top_percent=top, cost=50)),
    }
    if market == "US":
        robustness["top12_5"] = compact_backtest(backtest(market, run["run_id"], top_percent=12.5))
        robustness["top17_5"] = compact_backtest(backtest(market, run["run_id"], top_percent=17.5))
        robustness["top20"] = compact_backtest(backtest(market, run["run_id"], top_percent=20))
    else:
        robustness["top15"] = compact_backtest(backtest(market, run["run_id"], top_percent=15))
        robustness["top25"] = compact_backtest(backtest(market, run["run_id"], top_percent=25))

    common = backtest(market, run["run_id"], top_percent=top, end=spec["benchmark_end"])
    return {
        "market": market,
        "name": spec["name"],
        "experiment_id": saved_payload["experiment_id"],
        "validation": validation,
        "verified_experiment_id": verified_payload["experiment_id"],
        "verified_validation": verified_validation,
        "run_id": run["run_id"],
        "quality": run["quality"],
        "robustness": robustness,
        "benchmarks_common_period": benchmark_summaries(common, spec["benchmarks"]),
        "top10": latest_top10(run),
    }


def main() -> None:
    finalized = [finalize_market(market) for market in ("US", "KR")]
    tasks = [
        ("US", "k05_g50", {"rim": 0.95, "k": 0.05}, 50.0),
        ("US", "k15_g50", {"rim": 0.85, "k": 0.15}, 50.0),
        ("US", "k10_g40", {"rim": 0.9, "k": 0.1}, 40.0),
        ("US", "k10_g60", {"rim": 0.9, "k": 0.1}, 60.0),
        ("KR", "accrual10_g50", {"bpr": 0.9, "accrual": 0.1}, 50.0),
        ("KR", "accrual30_g50", {"bpr": 0.7, "accrual": 0.3}, 50.0),
        ("KR", "accrual20_g40", {"bpr": 0.8, "accrual": 0.2}, 40.0),
        ("KR", "accrual20_g60", {"bpr": 0.8, "accrual": 0.2}, 60.0),
    ]
    sensitivities: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(sensitivity_task, task) for task in tasks]
        for future in as_completed(futures):
            sensitivities.append(future.result())
    print(json.dumps({"finalized": finalized, "sensitivities": sensitivities}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
