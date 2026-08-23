"""FastAPI entrypoint for the internal admin app (weeks 1-6).

Coordinators and the owner only. There is no candidate-facing surface here and
no employer surface yet -- see the build order in CLAUDE.md.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from app.config import Residency, get_settings
from app.db import session_scope
from app.routers import operations

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(operations.router)


@app.get("/health")
def health() -> dict[str, object]:
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        database = "up"
    except Exception:
        database = "down"

    return {
        "status": "ok" if database == "up" else "degraded",
        "database": database,
        "data_residency": settings.data_residency.value,
        # Surfaced so a misconfigured deployment is visible in monitoring, not
        # only at startup.
        "holds_real_personal_data": (
            settings.data_residency is not Residency.LOCAL_DEV
        ),
    }
