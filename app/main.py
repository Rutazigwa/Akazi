"""FastAPI entrypoint for the internal admin app (weeks 1-6).

Coordinators and the owner only. There is no candidate-facing surface here and
no employer surface yet -- see the build order in CLAUDE.md.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.config import Residency, get_settings
from app.db import session_scope
from app.routers import (
    auth,
    data_rights,
    identity,
    inbound,
    operations,
    pay,
    registry,
    requests,
    staff,
)
from app.web import employer_router
from app.web import router as web_router
from app.web.deps import LoginRequired, redirect_to_login
from app.web.employer_deps import EmployerLoginRequired

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(auth.router)
app.include_router(identity.router)
app.include_router(data_rights.router)
app.include_router(registry.router)
app.include_router(requests.router)
app.include_router(inbound.router)
app.include_router(pay.router)
app.include_router(staff.router)
app.include_router(web_router.router)
app.include_router(employer_router.router)
app.include_router(operations.router)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    """Send a browser to the sign-in page instead of a bare 401 body."""
    return redirect_to_login(request)


@app.exception_handler(EmployerLoginRequired)
def _employer_login_required(request: Request, exc: EmployerLoginRequired):
    return RedirectResponse("/employer/login", status_code=303)


@app.get("/")
def root():
    return RedirectResponse("/ui/", status_code=307)


@app.get("/health")
def health(response: Response) -> dict[str, object]:
    """Liveness and readiness.

    Returns 503 when the database is unreachable. A 200 with
    {"database": "down"} would let an orchestrator keep routing traffic to a
    container that cannot serve a single real request.
    """
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        database = "up"
    except Exception:
        database = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

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
