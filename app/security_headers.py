"""Response headers the browser enforces on our behalf.

The application had none. For a system holding national ID numbers, home
locations and assessment scores -- used by coordinators on laptops that may
be shared or borrowed -- several of these are not decoration.

The policy can be unusually strict because of a decision made early: no build
step, no framework, no JavaScript at all. `script-src 'none'` means an
injected script tag does not execute even if one ever gets past Jinja's
escaping. Very few web applications can say that, and it is worth not giving
up lightly -- adding one line of JavaScript costs this protection for every
page.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import Residency, get_settings

# style-src still needs 'unsafe-inline': the pages carry a <style> block and
# 118 style attributes. That is a real weakness but a much smaller one than
# inline script would be, and with script-src 'none' there is no obvious path
# from injected CSS to anything worse than defacement. Moving those attributes
# into classes would let this be tightened.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'none'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    # Where a form may post. Stops an injected form exfiltrating a national ID
    # to somebody else's server.
    "form-action 'self'",
    # Clickjacking: a coordinator's session framed inside an attacker's page
    # could be made to click "Send Chantal", or worse, an erasure.
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])

# Paths that may be cached. Everything else carries personal data or an
# authenticated view of it, and a cached page of national ID numbers on a
# shared laptop outlives the session that fetched it.
CACHEABLE = frozenset({"/health"})


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Redundant with frame-ancestors for modern browsers, kept for older
        # ones. The cost is one header.
        response.headers["X-Frame-Options"] = "DENY"
        # URLs here carry candidate UUIDs -- /ui/candidates/<uuid>. Sending
        # that to another origin in a Referer is a small leak of exactly the
        # identifier the rest of the system is careful with.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )

        if request.url.path not in CACHEABLE:
            response.headers["Cache-Control"] = "no-store"

        # Only where there is TLS to insist on. Sending HSTS from a plain-HTTP
        # development server would pin a developer's browser to https for
        # localhost, which is a nuisance nobody expects.
        if get_settings().data_residency is not Residency.LOCAL_DEV:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        return response
