"""Shared FileTypeMap reference library.

Workflow step 02 (file categorisation) is instructed to read a
``FileTypeMap.xlsx`` — a living lookup of filename fragments -> document
type. Rather than every project uploading its own copy, one shared copy
lives in a reserved ``_file_type_map/`` folder of the connected store (S3
bucket, or the local data dir in dev) — the same library pattern as the
shared labour rates.

Before a workflow run, ``resolve()`` appends the most recently modified
file from that folder to the documents handed to the model, unless the
project uploaded its own. The injected file keeps its own basename, so it
appears in the corpus DOCUMENT MANIFEST under the exact name the model
must use with read_document.
"""

from __future__ import annotations

import logging
import re

from . import core, repo as _repo

LOGGER = logging.getLogger(__name__)

# "FileTypeMap.xlsx", "file_type_map v2.xlsx", "File Type Map.xlsx", ...
_RX = re.compile(r"file[ _\-]*type[ _\-]*map", re.IGNORECASE)


def looks_like_file_type_map(filename: str) -> bool:
    return bool(_RX.search(filename or ""))


def resolve(rec: dict, documents: list[dict]) -> dict:
    """Ensure the run's documents include a FileTypeMap when one exists.

    Returns {"source": "project"|"library"|"none", "filename": str,
             "note": str} — mirroring labour_rates.resolve.
    """
    for d in core.documents_in(rec, core.SECTION_PROJECT):
        if looks_like_file_type_map(d["filename"]):
            name = d["filename"]
            return {"source": "project", "filename": name,
                    "note": f"FileTypeMap from project documents: {name}."}

    files = _repo.get_repo().list_file_type_map()
    files.sort(key=lambda f: f.get("modified") or 0, reverse=True)
    for meta in files:
        data = _repo.get_repo().read_file_type_map(meta["filename"])
        if data is not None:
            documents.append({"filename": meta["filename"], "bytes": data})
            name = meta["filename"]
            return {"source": "library", "filename": name,
                    "note": f"Using the shared FileTypeMap reference: {name}."}
        LOGGER.warning("FileTypeMap %s listed but unreadable.", meta["filename"])

    return {"source": "none", "filename": "",
            "note": ("No FileTypeMap found (neither uploaded to the project "
                     "nor in the shared _file_type_map/ folder) — step 02 "
                     "will categorise from filenames and content alone.")}
