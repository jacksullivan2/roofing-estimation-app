"""In-memory session store.

Single uvicorn worker, single-tenant tool — holding session state in the
process is fine (same model as z_profiler). The browser only ever sees an
opaque random session id in an HttpOnly cookie.
"""

from __future__ import annotations

import secrets
import threading
import time

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def new_session() -> str:
    sid = secrets.token_urlsafe(24)
    with _sessions_lock:
        _sessions[sid] = {"created_at": time.time(), "authed": False}
    return sid


def get_session(sid: str | None) -> dict | None:
    if not sid:
        return None
    with _sessions_lock:
        return _sessions.get(sid)


def set_session_value(sid: str, key: str, value) -> None:
    with _sessions_lock:
        if sid in _sessions:
            _sessions[sid][key] = value


def drop_session(sid: str) -> None:
    with _sessions_lock:
        _sessions.pop(sid, None)
