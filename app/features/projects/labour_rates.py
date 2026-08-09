"""Shared labour-rates library + workflow fallback.

Contractors keep labour-rates documents (general rate cards, or rates reused
from other projects) in a reserved ``_labour_rates/`` folder of the connected
store (S3 bucket, or the local data dir in dev). When an estimate runs and the
project's uploaded documents contain no labour-rates document, the workflow
pulls the MOST RECENTLY UPLOADED document from that library and hands it to
the model alongside the project documents, clearly marked as shared.

Detection is filename-based: a document counts as labour rates when its name
matches the keyword patterns below (e.g. "Labour Rates 2026.xlsx",
"day-rate card.pdf", "wage rates.csv").
"""

from __future__ import annotations

import logging
import re

from . import core, repo as _repo

LOGGER = logging.getLogger(__name__)

SHARED_PREFIX = "[Shared labour rates] "

_PATTERNS = [
    r"labou?r[ _\-]*rates?",       # labour rates / labor rate
    r"labou?r[ _\-]*costs?",
    r"rate[ _\-]*card",
    r"day[ _\-]*rates?",
    r"wage[ _\-]*rates?",
    r"gang[ _\-]*rates?",
]
_RX = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def looks_like_labour_rates(filename: str) -> bool:
    return bool(_RX.search(filename or ""))


def project_has_labour_rates(rec: dict) -> bool:
    return any(looks_like_labour_rates(d["filename"])
               for d in core.documents_in(rec, core.SECTION_PROJECT))


# --------------------------------------------------------------------------- #
# Library access (backed by the active repo — S3 or local)                    #
# --------------------------------------------------------------------------- #

def list_library() -> list[dict]:
    files = _repo.get_repo().list_labour_rates()
    files.sort(key=lambda f: f.get("modified") or 0, reverse=True)
    return files


def latest_library_meta() -> dict | None:
    files = list_library()
    return files[0] if files else None


def read_library(filename: str) -> bytes | None:
    return _repo.get_repo().read_labour_rates(filename)


def upload_to_library(filename: str, data: bytes) -> None:
    _repo.get_repo().write_labour_rates(filename, data)


# --------------------------------------------------------------------------- #
# Workflow resolution ("after file categorisation")                           #
# --------------------------------------------------------------------------- #

def resolve(rec: dict, documents: list[dict]) -> dict:
    """Categorise the project documents for labour rates and, when none are
    present, append the most recent library document to ``documents``.

    Returns {"source": "project"|"library"|"none", "filename": str,
             "note": str} — the note is surfaced on job status and in outputs.
    """
    for d in core.documents_in(rec, core.SECTION_PROJECT):
        if looks_like_labour_rates(d["filename"]):
            return {"source": "project", "filename": d["filename"],
                    "note": f"Labour rates from project documents: {d['filename']}."}

    meta = latest_library_meta()
    if meta:
        data = read_library(meta["filename"])
        if data is not None:
            documents.append({
                "filename": f"{SHARED_PREFIX}{meta['filename']}",
                "bytes": data,
            })
            return {"source": "library", "filename": meta["filename"],
                    "note": ("No labour rates uploaded — using the most recent "
                             f"shared library document: {meta['filename']}.")}
        LOGGER.warning("Labour library doc %s listed but unreadable.",
                       meta["filename"])

    return {"source": "none", "filename": "",
            "note": ("No labour rates uploaded and the shared labour-rates "
                     "library is empty — labour will be estimated from "
                     "internal assumptions.")}
