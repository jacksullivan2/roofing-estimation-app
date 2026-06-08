"""Runtime settings for the Roofing Estimator web app.

Auth is OPTIONAL here so the tool runs friction-free in a local Docker
container: if ADMIN_PASSWORD is unset, the login gate is bypassed. Set
ADMIN_PASSWORD in any shared/hosted deployment to turn the gate on.
"""

import os
import secrets


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# Optional. When empty/unset the app does not require a login (local dev mode).
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)

SESSION_COOKIE_NAME = "re_session"
SESSION_TTL_SECONDS = 60 * 60 * 8  # 8 hours

# Local http://localhost keeps this False; set COOKIE_SECURE=true behind HTTPS.
COOKIE_SECURE = _env_bool("COOKIE_SECURE", default=False)

# Per-request upload cap (sum of all files in one submit). Default 200 MB.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))

# Allowed document extensions for project uploads.
ALLOWED_DOC_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv",
    ".png", ".jpg", ".jpeg", ".heic", ".txt", ".rtf", ".dwg", ".zip",
}


def auth_enabled() -> bool:
    return bool((ADMIN_PASSWORD or "").strip())
