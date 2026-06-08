"""Login + auth dependency (optional gate).

When ADMIN_PASSWORD is unset the gate is OFF: `require_login` returns an
"anon" principal and every page is reachable — ideal for a local container.
When ADMIN_PASSWORD is set, the same constant-time password check and
session-cookie flow as z_profiler applies.
"""

from __future__ import annotations

import hmac

from fastapi import Cookie, Request

from . import settings, sessions


class RedirectException(Exception):
    """Raised by `require_login` to bounce a request to a path. The app-level
    handler turns this into a real RedirectResponse (not JSON)."""

    def __init__(self, location: str):
        self.location = location


def password_matches(submitted: str) -> bool:
    expected = (settings.ADMIN_PASSWORD or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(
        submitted.strip().encode("utf-8"),
        expected.encode("utf-8"),
    )


def require_login(request: Request, re_session: str | None = Cookie(default=None)):
    """Ensure the request is authed. Returns the session id (or 'anon' when
    auth is disabled)."""
    if not settings.auth_enabled():
        return "anon"
    sess = sessions.get_session(re_session)
    if not sess or not sess.get("authed"):
        if request.headers.get("hx-request"):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Login required",
                                headers={"HX-Redirect": "/login"})
        raise RedirectException("/login")
    return re_session
