from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.service.dto import (
    BacktestAnnualReturnDto,
    BacktestEquityCurvePointDto,
    BacktestPositionDto,
    BacktestRebalanceDto,
    BacktestSummaryDto,
    FactorBacktestResponseDto,
    FactorLabBacktestRequestDto,
    FactorLabCompileResponseDto,
    FactorLabExperimentDeleteResponseDto,
    FactorLabExperimentListResponseDto,
    FactorLabExperimentResponseDto,
    FactorLabExperimentSaveRequestDto,
    FactorLabGraphDto,
    FactorLabNodePreviewResponseDto,
    FactorLabNodeTypeDto,
    FactorLabRunRequestDto,
    FactorLabRunResponseDto,
    FactorLabValidationResponseDto,
)
from api.service.factor_lab_service import FactorLabService


router = APIRouter(prefix="/api/factor-lab", tags=["factor-lab"])


@router.get("/node-types", response_model=list[FactorLabNodeTypeDto])
def get_factor_lab_node_types() -> list[FactorLabNodeTypeDto]:
    return FactorLabService().node_types()


@router.post("/validate", response_model=FactorLabValidationResponseDto)
def validate_factor_lab_graph(graph: FactorLabGraphDto) -> FactorLabValidationResponseDto:
    try:
        return FactorLabService().validate_graph(graph)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_validate_failed", str(exc))) from exc


@router.post("/compile", response_model=FactorLabCompileResponseDto)
def compile_factor_lab_graph(graph: FactorLabGraphDto) -> FactorLabCompileResponseDto:
    try:
        return FactorLabService().compile_graph(graph)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_graph", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_compile_failed", str(exc))) from exc


@router.post("/experiments", response_model=FactorLabExperimentResponseDto)
def save_factor_lab_experiment(
    request: FactorLabExperimentSaveRequestDto,
) -> FactorLabExperimentResponseDto:
    try:
        return FactorLabService().save_experiment(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_graph", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_save_failed", str(exc))) from exc


@router.get("/experiments", response_model=FactorLabExperimentListResponseDto)
def list_factor_lab_experiments() -> FactorLabExperimentListResponseDto:
    try:
        return FactorLabService().list_experiments()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_list_failed", str(exc))) from exc


@router.get("/experiments/by-name", response_model=FactorLabExperimentResponseDto)
def get_factor_lab_experiment_by_name(
    name: str = Query(..., min_length=1),
) -> FactorLabExperimentResponseDto:
    try:
        return FactorLabService().get_experiment_by_name(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_get_failed", str(exc))) from exc


@router.put("/experiments/by-name", response_model=FactorLabExperimentResponseDto)
def save_factor_lab_experiment_by_name(
    request: FactorLabExperimentSaveRequestDto,
) -> FactorLabExperimentResponseDto:
    try:
        return FactorLabService().save_experiment_by_name(request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_graph", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_save_failed", str(exc))) from exc


@router.delete("/experiments/by-name", response_model=FactorLabExperimentDeleteResponseDto)
def delete_factor_lab_experiment_by_name(
    name: str = Query(..., min_length=1),
) -> FactorLabExperimentDeleteResponseDto:
    try:
        return FactorLabService().delete_experiment_by_name(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_delete_failed", str(exc))) from exc


@router.get("/experiments/{experiment_id}", response_model=FactorLabExperimentResponseDto)
def get_factor_lab_experiment(experiment_id: str) -> FactorLabExperimentResponseDto:
    try:
        return FactorLabService().get_experiment(experiment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_get_failed", str(exc))) from exc


@router.put("/experiments/{experiment_id}", response_model=FactorLabExperimentResponseDto)
def update_factor_lab_experiment(
    experiment_id: str,
    request: FactorLabExperimentSaveRequestDto,
) -> FactorLabExperimentResponseDto:
    try:
        return FactorLabService().update_experiment(experiment_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_graph", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_update_failed", str(exc))) from exc


@router.delete("/experiments/{experiment_id}", response_model=FactorLabExperimentDeleteResponseDto)
def delete_factor_lab_experiment(experiment_id: str) -> FactorLabExperimentDeleteResponseDto:
    try:
        return FactorLabService().delete_experiment(experiment_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_delete_failed", str(exc))) from exc


@router.post("/runs", response_model=FactorLabRunResponseDto)
def run_factor_lab_graph(request: FactorLabRunRequestDto) -> FactorLabRunResponseDto:
    try:
        return FactorLabService().run_graph(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_run", str(exc))) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_experiment_not_found", "experiment not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_run_failed", str(exc))) from exc


@router.get("/runs/{run_id}", response_model=FactorLabRunResponseDto)
def get_factor_lab_run(run_id: str) -> FactorLabRunResponseDto:
    try:
        return FactorLabService().get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_run_not_found", "run not found")) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_get_run_failed", str(exc))) from exc


@router.get("/runs/{run_id}/nodes/{node_id}/preview", response_model=FactorLabNodePreviewResponseDto)
def preview_factor_lab_node(
    run_id: str,
    node_id: str,
    limit: int = Query(default=100, gt=0, le=1000),
) -> FactorLabNodePreviewResponseDto:
    try:
        return FactorLabService().preview_node(run_id, node_id=node_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_preview", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_preview_failed", str(exc))) from exc


@router.get("/runs/{run_id}/preview", response_model=FactorLabNodePreviewResponseDto)
def preview_factor_lab_final(
    run_id: str,
    limit: int = Query(default=100, gt=0, le=1000),
) -> FactorLabNodePreviewResponseDto:
    try:
        return FactorLabService().preview_node(run_id, node_id=None, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_preview", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_preview_failed", str(exc))) from exc


@router.post("/runs/{run_id}/backtest", response_model=FactorBacktestResponseDto)
def run_factor_lab_backtest(
    run_id: str,
    request: FactorLabBacktestRequestDto,
) -> FactorBacktestResponseDto:
    try:
        result = FactorLabService().run_backtest(run_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error("factor_lab_run_not_found", "run not found")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_error("factor_lab_invalid_backtest", str(exc))) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_error("factor_lab_backtest_failed", str(exc))) from exc

    return FactorBacktestResponseDto(
        summary=BacktestSummaryDto(**result.summary.__dict__),
        equity_curve=[
            BacktestEquityCurvePointDto(**point.__dict__)
            for point in result.equity_curve
        ],
        rebalance_history=[
            BacktestRebalanceDto(
                rebalance_date=rebalance.rebalance_date,
                signal_date=rebalance.signal_date,
                positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.positions
                ],
                entered_positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.entered_positions
                ],
                exited_positions=[
                    BacktestPositionDto(**position.__dict__)
                    for position in rebalance.exited_positions
                ],
            )
            for rebalance in result.rebalance_history
        ],
        annual_returns=[
            BacktestAnnualReturnDto(**annual_return.__dict__)
            for annual_return in result.annual_returns
        ],
        warnings=result.warnings,
    )


def _error(code: str, message: str, details=None) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "details": details or {},
    }
