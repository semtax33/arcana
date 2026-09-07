from __future__ import annotations

"""Search for PVGO-preserving strategies that are robust in KR and US.

Candidate selection uses only the declared train and validation windows.  The
2024-2026 U.S. and 2021-2026 Korean holdouts stay closed until a common design
is frozen.  Full-period results are produced only in the final stage.
"""

from dataclasses import asdict
import argparse
from datetime import date
import json
from pathlib import Path
from statistics import mean
from typing import Any

from api.service.dto import FactorLabBacktestRequestDto, FactorLabRunRequestDto
from api.service.factor_lab_service import FactorLabService
from scripts.discover_kr_pvgo_multifactor_strategy import (
    CandidateSpec,
    _weighted_sleeve,
    build_graph,
)
from scripts.optimize_kr_pvgo_expectations_alpha import (
    GateSpec,
    PortfolioSpec,
    _graph_hash,
    _jsonable,
    _write_checkpoint,
)


US_FULL = (date(2016, 1, 4), date(2026, 9, 4))
US_TRAIN = (date(2016, 1, 4), date(2019, 12, 31))
US_VALIDATION = (date(2020, 1, 2), date(2023, 12, 29))
US_HOLDOUT = (date(2024, 1, 2), date(2026, 9, 4))
KR_FULL = (date(2002, 4, 1), date(2026, 9, 4))
KR_TRAIN = (date(2002, 4, 1), date(2012, 12, 31))
KR_VALIDATION = (date(2016, 4, 1), date(2020, 12, 31))
KR_HOLDOUT = (date(2021, 1, 4), date(2026, 9, 4))
SELECTION_COST_BPS = 50.0
SCREEN_PORTFOLIO = PortfolioSpec(top_percent=30.0, max_positions=50)
US_SCREEN_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_screen_20260906.json"
)
US_PORTFOLIO_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_portfolio_screen_20260906.json"
)
US_COMBINATION_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_combinations_20260906.json"
)
US_GATE_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_gate_screen_20260906.json"
)
KR_TRANSFER_OUTPUT = Path(
    "deliverables/cross_market_pvgo_kr_transfer_screen_20260906.json"
)
US_COMMON_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_common_screen_20260906.json"
)
US_TREND_GATE_OUTPUT = Path(
    "deliverables/cross_market_pvgo_us_trend_gate_20260906.json"
)


US_SCREEN_SLEEVES = (
    "trend_quality",
    "price_momentum",
    "risk_adjusted_momentum",
    "near_52w_high",
    "gross_profitability",
    "fcf_to_ev_yield",
    "total_accruals_discipline",
    "roic_wacc_spread",
    "delta_economic_profit",
    "earnings_growth",
    "sales_growth",
    "inventory_growth_discipline",
    "operating_margin",
    "rpr_value",
    "market_cap_quality",
    "drawdown_control",
    "shareholder_yield",
    "economic_profit_yield",
    "external_financing_discipline",
    "cash_to_debt",
    "current_ratio",
    "rnd_to_market_cap",
    "operating_margin_growth",
)


def _us_screen_specs() -> dict[str, CandidateSpec]:
    specs = {
        "base_asset_lowvol": CandidateSpec(
            core_weight=0.40,
            sleeves=(
                _weighted_sleeve("asset_growth_discipline", 0.30),
                _weighted_sleeve("low_volatility", 0.30),
            ),
            beta_gate=1.5,
            min_market_cap_mil=1_000.0,
            require_positive_normalized_nopat=True,
        )
    }
    for key in US_SCREEN_SLEEVES:
        specs[key] = CandidateSpec(
            core_weight=0.20,
            sleeves=(
                _weighted_sleeve("asset_growth_discipline", 0.20),
                _weighted_sleeve("low_volatility", 0.20),
                _weighted_sleeve(key, 0.40),
            ),
            beta_gate=1.5,
            min_market_cap_mil=1_000.0,
            require_positive_normalized_nopat=True,
        )
    return specs


def _spec(
    core: float,
    *,
    beta_gate: float | None = 1.5,
    min_market_cap_mil: float | None = 1_000.0,
    require_positive_normalized_nopat: bool = True,
    require_complete_factor_case: bool = False,
    extra_gates: tuple[GateSpec, ...] = (),
    **sleeve_weights: float,
) -> CandidateSpec:
    return CandidateSpec(
        core_weight=core,
        sleeves=tuple(
            _weighted_sleeve(key, weight)
            for key, weight in sleeve_weights.items()
        ),
        beta_gate=beta_gate,
        min_market_cap_mil=min_market_cap_mil,
        require_positive_normalized_nopat=require_positive_normalized_nopat,
        require_complete_factor_case=require_complete_factor_case,
        extra_gates=extra_gates,
    )


def _us_combination_specs() -> dict[str, CandidateSpec]:
    """Small, theory-led combinations after the one-factor screen."""
    return {
        "sy_cash_balanced": _spec(
            0.20,
            asset_growth_discipline=0.10,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
        ),
        "sy_cash_no_asset": _spec(
            0.25,
            low_volatility=0.25,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
        ),
        "sy_cash_defensive": _spec(
            0.20,
            low_volatility=0.30,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
        ),
        "sy_cash_return_tilt": _spec(
            0.20,
            asset_growth_discipline=0.10,
            low_volatility=0.15,
            shareholder_yield=0.30,
            cash_to_debt=0.25,
        ),
        "sy_cash_core_tilt": _spec(
            0.30,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
        ),
        "sy_cash_only": _spec(
            0.30,
            shareholder_yield=0.35,
            cash_to_debt=0.35,
        ),
        "sy_cash_quality_change": _spec(
            0.20,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
            delta_economic_profit=0.10,
        ),
        "sy_cash_value": _spec(
            0.20,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
            economic_profit_yield=0.10,
        ),
        "sy_cash_earnings": _spec(
            0.20,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
            earnings_growth=0.10,
        ),
        "cash_quality_value": _spec(
            0.20,
            low_volatility=0.25,
            cash_to_debt=0.25,
            economic_profit_yield=0.15,
            delta_economic_profit=0.15,
        ),
        "sy_defensive": _spec(
            0.20,
            low_volatility=0.30,
            shareholder_yield=0.50,
        ),
        "cash_defensive": _spec(
            0.20,
            low_volatility=0.30,
            cash_to_debt=0.50,
        ),
    }


def _us_gate_specs() -> dict[str, CandidateSpec]:
    common = {"shareholder_yield": 0.35, "cash_to_debt": 0.35}
    return {
        "baseline": _spec(0.30, **common),
        "mcap_2500": _spec(0.30, min_market_cap_mil=2_500.0, **common),
        "mcap_5000": _spec(0.30, min_market_cap_mil=5_000.0, **common),
        "mcap_10000": _spec(0.30, min_market_cap_mil=10_000.0, **common),
        "beta_1_2": _spec(0.30, beta_gate=1.2, **common),
        "beta_1_0": _spec(0.30, beta_gate=1.0, **common),
        "no_beta": _spec(0.30, beta_gate=None, **common),
        "no_positive_nopat": _spec(
            0.30,
            require_positive_normalized_nopat=False,
            **common,
        ),
        "complete_case": _spec(
            0.30,
            require_complete_factor_case=True,
            **common,
        ),
    }


def _kr_transfer_specs() -> dict[str, CandidateSpec]:
    local = {
        "min_market_cap_mil": 100_000.0,
        "require_positive_normalized_nopat": True,
    }


def _us_common_specs() -> dict[str, CandidateSpec]:
    return {
        "core60_lowvol40_beta15": _spec(
            0.60, beta_gate=1.5, low_volatility=0.40
        ),
        "core60_lowvol40_beta12": _spec(
            0.60, beta_gate=1.2, low_volatility=0.40
        ),
        "core40_lowvol60_beta12": _spec(
            0.40, beta_gate=1.2, low_volatility=0.60
        ),
        "core70_lowvol30_beta12": _spec(
            0.70, beta_gate=1.2, low_volatility=0.30
        ),
        "core60_asset40_beta12": _spec(
            0.60, beta_gate=1.2, asset_growth_discipline=0.40
        ),
        "core40_asset30_lowvol30_beta12": _spec(
            0.40,
            beta_gate=1.2,
            asset_growth_discipline=0.30,
            low_volatility=0.30,
        ),
        "core55_lowvol35_sy10_beta12": _spec(
            0.55,
            beta_gate=1.2,
            low_volatility=0.35,
            shareholder_yield=0.10,
        ),
    }


def _us_trend_gate_specs() -> dict[str, CandidateSpec]:
    foundation = {
        "low_volatility": 0.20,
        "shareholder_yield": 0.25,
        "cash_to_debt": 0.25,
        "delta_economic_profit": 0.10,
    }
    return {
        "baseline_beta12": _spec(0.20, beta_gate=1.2, **foundation),
        "positive_tr_12_1": _spec(
            0.20,
            beta_gate=1.2,
            extra_gates=(GateSpec("tr_12_1", "greater_than", 0.0, "annual"),),
            **foundation,
        ),
        "positive_risk_adjusted_momentum": _spec(
            0.20,
            beta_gate=1.2,
            extra_gates=(
                GateSpec("risk_adj_mom", "greater_than", 0.0, "annual"),
            ),
            **foundation,
        ),
        "within_20pct_of_52w_high": _spec(
            0.20,
            beta_gate=1.2,
            extra_gates=(
                GateSpec("high52w_gap_pct", "greater_than", -20.0, "annual"),
            ),
            **foundation,
        ),
        "one_year_drawdown_above_minus40": _spec(
            0.20,
            beta_gate=1.2,
            extra_gates=(
                GateSpec("mdd1yr_12_1_pct", "greater_than", -40.0, "annual"),
            ),
            **foundation,
        ),
    }
    return {
        "sy_cash_beta_1_2": _spec(
            0.30,
            beta_gate=1.2,
            shareholder_yield=0.35,
            cash_to_debt=0.35,
            **local,
        ),
        "sy_cash_beta_1_5": _spec(
            0.30,
            beta_gate=1.5,
            shareholder_yield=0.35,
            cash_to_debt=0.35,
            **local,
        ),
        "sy_cash_value_beta_1_2": _spec(
            0.20,
            beta_gate=1.2,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
            economic_profit_yield=0.10,
            **local,
        ),
        "sy_cash_quality_change_beta_1_2": _spec(
            0.20,
            beta_gate=1.2,
            low_volatility=0.20,
            shareholder_yield=0.25,
            cash_to_debt=0.25,
            delta_economic_profit=0.10,
            **local,
        ),
    }


def _run_history(service: FactorLabService, graph: Any, period: tuple[date, date]):
    return service.run_graph(
        FactorLabRunRequestDto(
            graph=graph,
            mode="history",
            history_start_date=period[0],
            history_end_date=period[1],
            history_rebalance_frequency="quarterly",
        )
    )


def _backtest(
    service: FactorLabService,
    run_id: str,
    *,
    market: str,
    period: tuple[date, date],
    portfolio: PortfolioSpec,
    cost_bps: float = SELECTION_COST_BPS,
) -> dict[str, Any]:
    benchmarks = (
        ["US_NASDAQ", "US_SP500"]
        if market == "US"
        else ["KOSPI200", "KOSDAQ"]
    )
    result = service.run_backtest(
        run_id,
        FactorLabBacktestRequestDto(
            top_percent=portfolio.top_percent,
            start_date=period[0],
            end_date=period[1],
            rebalance_frequency="quarterly",
            market=market,
            benchmarks=benchmarks,
            max_positions=portfolio.max_positions,
            transaction_cost_bps=cost_bps,
        ),
    )
    return {
        "period": [str(period[0]), str(period[1])],
        "metrics": _jsonable(result.summary),
        "rebalance_count": len(result.rebalance_history),
        "warnings": list(result.warnings),
    }


def _selection_row(key: str, train: dict[str, Any], validation: dict[str, Any]):
    metrics = [train["metrics"], validation["metrics"]]
    sharpes = [float(value["sharpe"]) for value in metrics]
    cagrs = [float(value["cagr"]) for value in metrics]
    mdds = [abs(float(value["max_drawdown"])) for value in metrics]
    return {
        "key": key,
        "worst_sharpe": min(sharpes),
        "mean_sharpe": mean(sharpes),
        "mean_cagr": mean(cagrs),
        "worst_abs_mdd": max(mdds),
        "score": min(sharpes) + 0.4 * mean(sharpes) + 0.25 * mean(cagrs) - 0.35 * max(mdds),
    }


def run_us_screen(output_path: Path = US_SCREEN_OUTPUT) -> dict[str, Any]:
    service = FactorLabService()
    payload: dict[str, Any] = {
        "design": {
            "market": "US",
            "full_period": [str(value) for value in US_FULL],
            "train": [str(value) for value in US_TRAIN],
            "validation": [str(value) for value in US_VALIDATION],
            "holdout": [str(value) for value in US_HOLDOUT],
            "holdout_policy": "not accessed during screen",
            "fixed_pvgo_core_relative_weights": {"gap": 0.45, "quality": 0.30, "compression": 0.25},
            "common_controls": {
                "beta_gate": 1.5,
                "minimum_market_cap_usd_millions": 1_000.0,
                "positive_normalized_nopat": True,
                "portfolio": asdict(SCREEN_PORTFOLIO),
            },
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    for key, spec in _us_screen_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US screen {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_Screen__{key}",
            spec,
            start_date=US_FULL[0],
            end_date=US_FULL[1],
        )
        graph.experiment.market = "US"
        try:
            run = _run_history(service, graph, US_FULL)
            train = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_TRAIN,
                portfolio=SCREEN_PORTFOLIO,
            )
            validation = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_VALIDATION,
                portfolio=SCREEN_PORTFOLIO,
            )
            row = _selection_row(key, train, validation)
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_us_portfolio_screen(
    *,
    source_path: Path = US_SCREEN_OUTPUT,
    output_path: Path = US_PORTFOLIO_OUTPUT,
) -> dict[str, Any]:
    """Test breadth only on leading pre-holdout factor designs."""
    source = json.loads(source_path.read_text(encoding="utf-8"))
    candidate_keys = (
        "cash_to_debt",
        "shareholder_yield",
        "rnd_to_market_cap",
        "earnings_growth",
        "economic_profit_yield",
        "delta_economic_profit",
    )
    portfolios = tuple(
        PortfolioSpec(top_percent=top_percent, max_positions=max_positions)
        for top_percent in (5.0, 10.0, 20.0, 30.0, 40.0)
        for max_positions in (20, 50, 100)
    )
    payload: dict[str, Any] = {
        "design": {
            "source": str(source_path),
            "candidate_keys": list(candidate_keys),
            "periods": {
                "train": [str(value) for value in US_TRAIN],
                "validation": [str(value) for value in US_VALIDATION],
            },
            "holdout_policy": "not accessed during portfolio screen",
            "transaction_cost_bps": SELECTION_COST_BPS,
        },
        "candidates": {},
    }
    service = FactorLabService()
    ranking: list[dict[str, Any]] = []
    for key in candidate_keys:
        run_id = source["candidates"][key]["history_run_id"]
        for portfolio in portfolios:
            label = (
                f"{key}__top{portfolio.top_percent:g}__max{portfolio.max_positions}"
            )
            print(f"[CROSS-PVGO] US portfolio {label}", flush=True)
            train = _backtest(
                service,
                run_id,
                market="US",
                period=US_TRAIN,
                portfolio=portfolio,
            )
            validation = _backtest(
                service,
                run_id,
                market="US",
                period=US_VALIDATION,
                portfolio=portfolio,
            )
            row = _selection_row(label, train, validation)
            row["factor_design"] = key
            row["portfolio"] = asdict(portfolio)
            ranking.append(row)
            payload["candidates"][label] = {
                "history_run_id": run_id,
                "factor_design": key,
                "portfolio": asdict(portfolio),
                "train": train,
                "validation": validation,
                "selection": row,
            }
            _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_us_combination_screen(
    output_path: Path = US_COMBINATION_OUTPUT,
) -> dict[str, Any]:
    """Combine only the economically complementary pre-holdout leaders."""
    service = FactorLabService()
    portfolio = PortfolioSpec(top_percent=5.0, max_positions=20)
    payload: dict[str, Any] = {
        "design": {
            "market": "US",
            "periods": {
                "train": [str(value) for value in US_TRAIN],
                "validation": [str(value) for value in US_VALIDATION],
            },
            "holdout_policy": "not accessed during combination screen",
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": SELECTION_COST_BPS,
            "economic_hypothesis": (
                "undervalued PVGO is confirmed by balance-sheet liquidity, "
                "cash returned to owners, and low idiosyncratic risk"
            ),
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    for key, spec in _us_combination_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US combination {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_Combination__{key}",
            spec,
            start_date=US_FULL[0],
            end_date=US_FULL[1],
        )
        graph.experiment.market = "US"
        try:
            run = _run_history(service, graph, US_FULL)
            train = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_TRAIN,
                portfolio=portfolio,
            )
            validation = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_VALIDATION,
                portfolio=portfolio,
            )
            row = _selection_row(key, train, validation)
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_us_gate_screen(output_path: Path = US_GATE_OUTPUT) -> dict[str, Any]:
    """Change one eligibility gate at a time around the leading combination."""
    service = FactorLabService()
    portfolio = PortfolioSpec(top_percent=5.0, max_positions=20)
    payload: dict[str, Any] = {
        "design": {
            "market": "US",
            "base_factor_design": "PVGO 30%, shareholder yield 35%, cash/debt 35%",
            "periods": {
                "train": [str(value) for value in US_TRAIN],
                "validation": [str(value) for value in US_VALIDATION],
            },
            "holdout_policy": "not accessed during gate screen",
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": SELECTION_COST_BPS,
            "test_policy": "one eligibility rule differs from baseline per candidate",
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    for key, spec in _us_gate_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US gate {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_Gate__{key}",
            spec,
            start_date=US_FULL[0],
            end_date=US_FULL[1],
        )
        graph.experiment.market = "US"
        try:
            run = _run_history(service, graph, US_FULL)
            train = _backtest(
                service, run.run_id, market="US", period=US_TRAIN, portfolio=portfolio
            )
            validation = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_VALIDATION,
                portfolio=portfolio,
            )
            row = _selection_row(key, train, validation)
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_kr_transfer_screen(
    output_path: Path = KR_TRANSFER_OUTPUT,
) -> dict[str, Any]:
    """Transfer frozen U.S. leaders to Korean pre-holdout windows."""
    service = FactorLabService()
    portfolio = PortfolioSpec(top_percent=5.0, max_positions=20)
    payload: dict[str, Any] = {
        "design": {
            "market": "KR",
            "source": "U.S. pre-holdout leaders",
            "periods": {
                "train": [str(value) for value in KR_TRAIN],
                "validation": [str(value) for value in KR_VALIDATION],
            },
            "holdout_policy": "2021-2026 not accessed during transfer screen",
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": SELECTION_COST_BPS,
            "local_currency_size_floor": "KRW 100 billion",
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    for key, spec in _kr_transfer_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] KR transfer {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_KR_Transfer__{key}",
            spec,
            start_date=KR_FULL[0],
            end_date=KR_FULL[1],
        )
        graph.experiment.market = "KR"
        try:
            run = _run_history(service, graph, KR_FULL)
            train = _backtest(
                service, run.run_id, market="KR", period=KR_TRAIN, portfolio=portfolio
            )
            validation = _backtest(
                service,
                run.run_id,
                market="KR",
                period=KR_VALIDATION,
                portfolio=portfolio,
            )
            row = _selection_row(key, train, validation)
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_us_common_screen(output_path: Path = US_COMMON_OUTPUT) -> dict[str, Any]:
    """Test the leading Korean common factors on U.S. pre-holdout data."""
    service = FactorLabService()
    portfolio = PortfolioSpec(top_percent=30.0, max_positions=50)
    payload: dict[str, Any] = {
        "design": {
            "market": "US",
            "source": "Korean pre-holdout leaders",
            "periods": {
                "train": [str(value) for value in US_TRAIN],
                "validation": [str(value) for value in US_VALIDATION],
            },
            "holdout_policy": "2024-2026 not accessed during common-factor screen",
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": SELECTION_COST_BPS,
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    for key, spec in _us_common_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US common {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_Common__{key}",
            spec,
            start_date=US_FULL[0],
            end_date=US_FULL[1],
        )
        graph.experiment.market = "US"
        try:
            run = _run_history(service, graph, US_FULL)
            train = _backtest(
                service, run.run_id, market="US", period=US_TRAIN, portfolio=portfolio
            )
            validation = _backtest(
                service,
                run.run_id,
                market="US",
                period=US_VALIDATION,
                portfolio=portfolio,
            )
            row = _selection_row(key, train, validation)
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "train": train,
                "validation": validation,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "sharpe_constraint_feasible_count": sum(
            float(row["worst_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


def run_us_trend_gate_screen(
    output_path: Path = US_TREND_GATE_OUTPUT,
) -> dict[str, Any]:
    """Audit economically neutral value-trap gates after the first holdout failed."""
    service = FactorLabService()
    portfolio = PortfolioSpec(top_percent=5.0, max_positions=20)
    payload: dict[str, Any] = {
        "design": {
            "market": "US",
            "status": "second research loop after the first frozen holdout candidate failed",
            "multiple_testing_note": "one baseline plus four theory-set thresholds",
            "portfolio": asdict(portfolio),
            "transaction_cost_bps": SELECTION_COST_BPS,
        },
        "candidates": {},
    }
    if output_path.exists():
        previous = json.loads(output_path.read_text(encoding="utf-8"))
        payload["candidates"].update(previous.get("candidates", {}))
    ranking: list[dict[str, Any]] = []
    periods = {
        "train": US_TRAIN,
        "validation": US_VALIDATION,
        "holdout": US_HOLDOUT,
        "full": US_FULL,
    }
    for key, spec in _us_trend_gate_specs().items():
        cached = payload["candidates"].get(key, {})
        if cached.get("status") == "completed":
            ranking.append(cached["selection"])
            continue
        print(f"[CROSS-PVGO] US trend gate {key}", flush=True)
        graph = build_graph(
            f"Arcana_CrossMarket_PVGO_US_TrendGate__{key}",
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
                    portfolio=portfolio,
                )
                for label, period in periods.items()
            }
            full_metrics = evidence["full"]["metrics"]
            segment_sharpes = [
                float(evidence[label]["metrics"]["sharpe"])
                for label in ("train", "validation", "holdout")
            ]
            row = {
                "key": key,
                "full_sharpe": float(full_metrics["sharpe"]),
                "full_cagr": float(full_metrics["cagr"]),
                "full_abs_mdd": abs(float(full_metrics["max_drawdown"])),
                "worst_segment_sharpe": min(segment_sharpes),
                "score": (
                    float(full_metrics["sharpe"])
                    + 0.5 * min(segment_sharpes)
                    + 0.25 * float(full_metrics["cagr"])
                    - 0.35 * abs(float(full_metrics["max_drawdown"]))
                ),
            }
            ranking.append(row)
            payload["candidates"][key] = {
                "status": "completed",
                "spec": asdict(spec),
                "graph_hash_sha256": _graph_hash(graph),
                "history_run_id": run.run_id,
                "history_quality": _jsonable(run.quality),
                "evidence_50bps": evidence,
                "selection": row,
            }
        except Exception as exc:
            payload["candidates"][key] = {
                "status": "failed",
                "spec": asdict(spec),
                "error": str(exc),
            }
        _write_checkpoint(output_path, payload)
    ranking.sort(key=lambda row: float(row["score"]), reverse=True)
    payload["selection"] = {
        "ranking": ranking,
        "full_period_sharpe_constraint_feasible_count": sum(
            float(row["full_sharpe"]) > 1.0 for row in ranking
        ),
    }
    _write_checkpoint(output_path, payload)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        nargs="?",
        choices=(
            "us-screen",
            "us-portfolio",
            "us-combinations",
            "us-gates",
            "kr-transfer",
            "us-common",
            "us-trend-gates",
        ),
        default="us-screen",
    )
    args = parser.parse_args()
    if args.stage == "us-screen":
        result = run_us_screen()
    elif args.stage == "us-portfolio":
        result = run_us_portfolio_screen()
    elif args.stage == "us-combinations":
        result = run_us_combination_screen()
    elif args.stage == "us-gates":
        result = run_us_gate_screen()
    elif args.stage == "kr-transfer":
        result = run_kr_transfer_screen()
    elif args.stage == "us-common":
        result = run_us_common_screen()
    else:
        result = run_us_trend_gate_screen()
    print(json.dumps(result["selection"], ensure_ascii=False, indent=2))
