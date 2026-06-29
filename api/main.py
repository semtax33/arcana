from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from api.controller.backtest_controller import router as backtest_router
from api.controller.chart_controller import router as chart_router
from api.controller.factor_controller import router as factor_router
from api.controller.factor_lab_controller import router as factor_lab_router
from api.controller.factor_screen_controller import router as factor_screen_router
from api.controller.financials_controller import router as financials_router
from api.controller.introduction_controller import router as introduction_router
from api.controller.operating_metrics_controller import router as operating_metrics_router
from api.controller.estimate_controller import router as estimate_router
from api.controller.sector_leader_controller import router as sector_leader_router
from api.controller.sector_controller import router as sector_router
from api.controller.style_score_controller import router as style_score_router
from api.controller.valuation_controller import router as valuation_router


app = FastAPI(title="Arcana API")

app.include_router(backtest_router)
app.include_router(chart_router)
app.include_router(sector_router)
app.include_router(sector_leader_router)
app.include_router(factor_router)
app.include_router(factor_lab_router)
app.include_router(factor_screen_router)
app.include_router(introduction_router)
app.include_router(financials_router)
app.include_router(operating_metrics_router)
app.include_router(estimate_router)
app.include_router(style_score_router)
app.include_router(valuation_router)


@app.get("/", include_in_schema=False)
def mcp_http_root() -> dict[str, Any]:
    return {
        "name": "arcana-api",
        "transport": "http",
        "protocol": "mcp",
        "endpoint": "/",
    }


@app.post("/", include_in_schema=False)
async def mcp_http_transport(request: Request) -> Response:
    from api.mcp import handle_http_message

    message = await request.json()
    if isinstance(message, list):
        responses = [
            response
            for item in message
            if (response := handle_http_message(item)) is not None
        ]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    response = handle_http_message(message)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)

@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _flatten_included_routers_for_test_compatibility() -> None:
    flattened_routes = []
    for route in app.router.routes:
        if hasattr(route, "path"):
            flattened_routes.append(route)
            continue
        effective_candidates = getattr(route, "effective_candidates", None)
        if callable(effective_candidates):
            flattened_routes.extend(
                getattr(candidate, "original_route", candidate)
                for candidate in effective_candidates()
            )
            continue
        flattened_routes.append(route)
    app.router.routes = flattened_routes


_flatten_included_routers_for_test_compatibility()

