from __future__ import annotations

from fastapi import FastAPI

from api.controller.factor_controller import router as factor_router
from api.controller.factor_screen_controller import router as factor_screen_router
from api.controller.sector_controller import router as sector_router


app = FastAPI(title="StatementParsing API")

app.include_router(sector_router)
app.include_router(factor_router)
app.include_router(factor_screen_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
