"""In-memory session store.

Single uvicorn worker, single-tenant tool — holding session state in the
process is fine (same model as z_profiler). The browser only ever sees an
opaque random session id in an HttpOnly cookie.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

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


# --------------------------------------------------------------------------- #
# Tender jobs — background "Estimate tender" workflow                          #
# --------------------------------------------------------------------------- #

@dataclass
class TenderJob:
    """One run of the tender-estimation workflow for a project.

    Lives in memory (single worker). Outputs (pricing sheet + tender doc) are
    held as bytes on the job until downloaded or the process restarts.
    """
    job_id: str
    project_id: str

    status: str = "queued"          # queued | running | done | error
    step: str = ""                  # human-readable current step
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    # Provenance / notes surfaced on the status panel.
    context_filename: str = ""
    prompt_source: str = ""         # s3 | local | none
    prompts_count: int = 0
    ai_used: bool = False
    notes: str = ""

    # Outputs.
    pricing_bytes: Optional[bytes] = None
    pricing_filename: str = ""
    pricing_media_type: str = ""
    tender_bytes: Optional[bytes] = None
    tender_filename: str = ""
    tender_media_type: str = ""


_tender_jobs: dict[str, TenderJob] = {}
_tender_jobs_lock = threading.Lock()


def create_tender_job(project_id: str) -> TenderJob:
    job = TenderJob(job_id=uuid.uuid4().hex[:12], project_id=project_id)
    with _tender_jobs_lock:
        _tender_jobs[job.job_id] = job
    return job


def get_tender_job(job_id: str) -> Optional[TenderJob]:
    with _tender_jobs_lock:
        return _tender_jobs.get(job_id)


def latest_tender_job(project_id: str) -> Optional[TenderJob]:
    with _tender_jobs_lock:
        jobs = [j for j in _tender_jobs.values() if j.project_id == project_id]
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs[0] if jobs else None
