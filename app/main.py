"""FastAPI entrypoint for the internal admin app (weeks 1-6).

Coordinators and the owner only. There is no candidate-facing surface here and
no employer surface yet -- see the build order in CLAUDE.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import Residency, get_settings
from app.security_headers import SecurityHeaders
from app.db import session_scope
from app.routers import (
    auth,
    catalogue,
    cohorts,
    data_rights,
    follow_up_reports,
    identity,
    inbound,
    lmis,
    operations,
    pay,
    registry,
    requests,
    staff,
)
from app.web import employer_router
from app.web import router as web_router
from app.web.deps import (
    LoginRequired,
    PasswordChangeRequired,
    redirect_to_login,
)
from app.web.employer_deps import (
    EmployerLoginRequired,
    EmployerPasswordChangeRequired,
)

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)

# Before anything else, so every response carries them -- including error
# responses and redirects, which are exactly the ones that get forgotten.
app.add_middleware(SecurityHeaders)

# One stylesheet, served rather than inlined. That is what lets the policy be
# style-src 'self' instead of 'unsafe-inline' -- see app/security_headers.py.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
    name="static",
)
app.include_router(auth.router)
app.include_router(identity.router)
app.include_router(catalogue.router)
app.include_router(cohorts.router)
app.include_router(data_rights.router)
app.include_router(follow_up_reports.router)
app.include_router(registry.router)
app.include_router(requests.router)
app.include_router(inbound.router)
app.include_router(lmis.router)
app.include_router(pay.router)
app.include_router(staff.router)
app.include_router(web_router.router)
app.include_router(employer_router.router)
app.include_router(operations.router)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    """Send a browser to the sign-in page instead of a bare 401 body."""
    return redirect_to_login(request)


@app.exception_handler(PasswordChangeRequired)
def _password_change_required(request: Request, exc: PasswordChangeRequired):
    """Send them to the change page rather than a dead end."""
    return RedirectResponse("/ui/password", status_code=303)


@app.exception_handler(EmployerLoginRequired)
def _employer_login_required(request: Request, exc: EmployerLoginRequired):
    return RedirectResponse("/employer/login", status_code=303)


@app.exception_handler(EmployerPasswordChangeRequired)
def _employer_password_change(
    request: Request, exc: EmployerPasswordChangeRequired
):
    return RedirectResponse("/employer/password", status_code=303)


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

    # Reported, never turned into an HTTP failure. A stalled cron does not
    # mean this container is unwell, and a 503 would have an orchestrator
    # restart the one part that is still working. Monitoring alerts on the
    # field; the orchestrator reads the status code.
    messaging: dict[str, object] = {"state": "unknown"}
    backups: dict[str, object] = {"state": "unknown"}
    if database == "up":
        try:
            from app.operations.jobs import backup_status, messaging_status

            with session_scope() as session:
                messaging = messaging_status(session)
                backups = backup_status(session)
        except Exception as exc:  # pragma: no cover - defensive
            messaging = {"state": "unknown", "reason": str(exc)}
            backups = {"state": "unknown", "reason": str(exc)}

    return {
        "status": "ok" if database == "up" else "degraded",
        "database": database,
        "messaging": messaging,
        "backups": backups,
        "data_residency": settings.data_residency.value,
        # Surfaced so a misconfigured deployment is visible in monitoring, not
        # only at startup.
        "holds_real_personal_data": (
            settings.data_residency is not Residency.LOCAL_DEV
        ),
    }
