from __future__ import annotations

"""Limited second-loop confirmations around the positive-trend PVGO design."""

from dataclasses import asdict
import json
from pathlib import Path

from api.service.factor_lab_service import FactorLabService
from scripts.discover_kr_pvgo_multifactor_strategy import _weighted_sleeve, build_graph
from scripts.optimize_kr_pvgo_expectations_alpha import GateSpec, PortfolioSpec, _graph_hash, _jsonable, _write_checkpoint
from scripts.search_cross_market_pvgo_strategy import (
    CandidateSpec,
    SELECTION_COST_BPS,
    US_FULL,
    US_HOLDOUT,
    US_TRAIN,
    US_VALIDATION,
    _backtest,
    _run_history,
)


OUTPUT = Path("deliverables/cross_market_pvgo_us_trend_confirmations_20260906.json")
PORTFOLIO = PortfolioSpec(top_percent=5.0, max_positions=20)
TREND_GATE = (GateSpec("tr_12_1", "greater_than", 0.0, "annual"),)


def _spec(confirmation: str | None) -> CandidateSpec:
    sleeves = [
        _weighted_sleeve("low_volatility", 0.20),
        _weighted_sleeve("shareholder_yield", 0.20 if confirmation else 0.25),
        _weighted_sleeve("cash_to_debt", 0.20 if confirmation else 0.25),
        _weighted_sleeve("delta_economic_profit", 0.10),
    ]
    if confirmation:
        sleeves.append(_weighted_sleeve(confirmation, 0.10))
    return CandidateSpec(
        core_weight=0.20,
        sleeves=tuple(sleeves),
        beta_gate=1.2,
        min_market_cap_mil=1_000.0,
        require_positive_normalized_nopat=True,
        extra_gates=TREND_GATE,
    )


def candidate_specs() -> dict[str, CandidateSpec]:
    specs = {
        "baseline": _spec(None),
        "gross_profitability_10": _spec("gross_profitability"),
        "fcf_to_ev_yield_10": _spec("fcf_to_ev_yield"),
        "total_accruals_discipline_10": _spec("total_accruals_discipline"),
        "market_cap_quality_10": _spec("market_cap_quality"),
        "asset_growth_discipline_10": _spec("asset_growth_discipline"),
        "economic_profit_yield_10": _spec("economic_profit_yield"),
    }
    for key, confirmation in (
        ("gross_profitability_no_delta", "gross_profitability"),
        ("fcf_to_ev_yield_no_delta", "fcf_to_ev_yield"),
    ):
        specs[key] = CandidateSpec(
            core_weight=0.20,
            sleeves=(
                _weighted_sleeve("low_volatility", 0.20),
                _weighted_sleeve("shareholder_yield", 0.25),
                _weighted_sleeve("cash_to_debt", 0.25),
                _weighted_sleeve(confirmation, 0.10),
            ),
            beta_gate=1.2,
            min_market_cap_mil=1_000.0,
            require_positive_normalized_nopat=True,
            extra_gates=TREND_GATE,
        )
    return specs


def main() -> None:
    service = FactorLabService()
    periods = {
        "train": US_TRAIN,
        "validation": US_VALIDATION,
        "holdout": US_HOLDOUT,
        "full": US_FULL,
    }
    payload = {
        "design": {
            "status": "limited second loop after positive-trend candidate remained below Sharpe 1",
            "market": "US",
            "portfolio": asdict(PORTFOLIO),
            "transaction_cost_bps": SELECTION_COST_BPS,
            "candidate_count": len(candidate_specs()),
        },
        "candidates": {},
    }
    if OUTPUT.exists():
        previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking = []
    for key, spec in candidate_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US trend confirmation {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_TrendConfirmation__{key}",
            spec,
            start_date=US_FULL[0],
            end_date=US_FULL[1],
        )
        graph.experiment.market = "US"
        try:
            run = _run_history(service, graph, US_FULL)
            evidence = {
                label: _backtest(
                    service,
                    run.run_id,
                    market="US",
                    period=period,
                    portfolio=PORTFOLIO,
                )
                for label, period in periods.items()
            }
            full = evidence["full"]["metrics"]
            segment_sharpes = [
                float(evidence[label]["metrics"]["sharpe"])
                for label in ("train", "validation", "holdout")
            ]
            row = {
                "key": key,
                "full_sharpe": float(full["sharpe"]),
                "full_cagr": float(full["cagr"]),
                "full_abs_mdd": abs(float(full["max_drawdown"])),
                "worst_segment_sharpe": min(segment_sharpes),
            }
            row["score"] = (
                row["full_sharpe"]
                + 0.5 * row["worst_segment_sharpe"]
                + 0.25 * row["full_cagr"]
                - 0.35 * row["full_abs_mdd"]
            )
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "graph_hash_sha256": _graph_hash(graph),
                "evidence_50bps": evidence,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(OUTPUT, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "full_period_sharpe_constraint_feasible_count": sum(
            float(row["full_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(OUTPUT, payload)
    print(json.dumps(payload["selection"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
