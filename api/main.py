from __future__ import annotations

from fastapi import FastAPI

from api.controller.backtest_controller import router as backtest_router
from api.controller.chart_controller import router as chart_router
from api.controller.factor_controller import router as factor_router
from api.controller.factor_screen_controller import router as factor_screen_router
from api.controller.financials_controller import router as financials_router
from api.controller.introduction_controller import router as introduction_router
from api.controller.sector_leader_controller import router as sector_leader_router
from api.controller.sector_controller import router as sector_router
from api.controller.style_score_controller import router as style_score_router


app = FastAPI(title="Arcana API")

app.include_router(backtest_router)
app.include_router(chart_router)
app.include_router(sector_router)
app.include_router(sector_leader_router)
app.include_router(factor_router)
app.include_router(factor_screen_router)
app.include_router(introduction_router)
app.include_router(financials_router)
app.include_router(style_score_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
