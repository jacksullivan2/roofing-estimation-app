"""Runtime settings for the Roofing Estimator web app.

Auth is OPTIONAL here so the tool runs friction-free in a local Docker
container: if ADMIN_PASSWORD is unset, the login gate is bypassed. Set
ADMIN_PASSWORD in any shared/hosted deployment to turn the gate on.
"""

import os
import secrets
from pathlib import Path


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


# --------------------------------------------------------------------------- #
# Tender workflow — AWS S3 (agent prompts) + AI model                          #
#                                                                              #
# These are intentionally optional. The "Estimate tender" workflow degrades    #
# gracefully when they are unset: prompts fall back to the bundled local       #
# _agent_prompts folder, and the AI step produces clearly-labelled placeholder #
# outputs. Fill the values in via environment variables once available.        #
# --------------------------------------------------------------------------- #

# Standard AWS credentials are read by boto3 from the environment / instance
# role (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, etc.).
AWS_REGION = os.getenv("AWS_REGION", "")

# Bucket + key prefix holding the workflow-step markdown files (the
# "_agent_prompts" folder, mirrored to S3). e.g. prefix "_agent_prompts/".
AGENT_PROMPTS_S3_BUCKET = os.getenv("AGENT_PROMPTS_S3_BUCKET", "")
AGENT_PROMPTS_S3_PREFIX = os.getenv("AGENT_PROMPTS_S3_PREFIX", "_agent_prompts/")

# Local fallback directory of agent-prompt markdown files, bundled with the
# app so the workflow works before S3 is wired up.
AGENT_PROMPTS_LOCAL_DIR = os.getenv(
    "AGENT_PROMPTS_LOCAL_DIR",
    str(Path(__file__).resolve().parents[1] / "_agent_prompts"),
)

# AI model connection. AI_PROVIDER selects the integration branch in
# app/infra/ai_client.py: "bedrock" (AWS) | "http" (generic REST) | "" (off).
AI_PROVIDER = os.getenv("AI_PROVIDER", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_ENDPOINT = os.getenv("AI_ENDPOINT", "")        # REST endpoint (http provider)
AI_MODEL_ID = os.getenv("AI_MODEL_ID", "")        # e.g. a Bedrock model id
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "300"))

# ---- Bedrock step-runner + prompt caching (see app/infra/bedrock_runner.py).
# Prompt caching re-uses the byte-identical prefix of consecutive requests
# (documents corpus, step prompts) at ~10% of the normal input-token price.
# Leave enabled unless the chosen model/region rejects cachePoint blocks —
# the runner also auto-falls-back per call if the API refuses them.
AI_CACHE_ENABLED = _env_bool("AI_CACHE_ENABLED", default=True)

# Max tool-use round-trips one workflow step may take before erroring.
AI_MAX_TOOL_TURNS = int(os.getenv("AI_MAX_TOOL_TURNS", "12"))

# Max output tokens per model call.
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "8192"))

# Per-document character cap inside the cached corpus (full text stays
# available to the model via the read_document tool).
AI_CORPUS_DOC_CHAR_CAP = int(os.getenv("AI_CORPUS_DOC_CHAR_CAP", "60000"))

# Optional per-step model overrides as JSON, e.g. cheap model for the
# mechanical steps: {"01": "eu.anthropic.claude-haiku-...", "02": "..."}.
# Steps not listed use AI_MODEL_ID. NOTE: caches are per-model — mixing
# models means each model writes its own copy of the corpus cache, so group
# consecutive steps on the same model where possible.
def _step_models() -> dict:
    import json
    raw = os.getenv("AI_STEP_MODELS", "")
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except ValueError:
        return {}


AI_STEP_MODELS = _step_models()


def s3_configured() -> bool:
    return bool(AGENT_PROMPTS_S3_BUCKET.strip())


def ai_configured() -> bool:
    """True when enough is set to attempt a real model call. Bedrock needs a
    model id; the generic http provider needs an endpoint + key."""
    provider = (AI_PROVIDER or "").strip().lower()
    if provider == "bedrock":
        return bool(AI_MODEL_ID.strip())
    if provider == "http":
        return bool(AI_ENDPOINT.strip() and AI_API_KEY.strip())
    return False
