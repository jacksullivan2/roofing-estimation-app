"""Atomic JSON storage + an uploads directory on a persistent volume.

Same design as z_profiler's local_store: a small key/value JSON store that
doesn't justify a real database, with a per-file thread lock and
write-tmp-then-rename so a crash mid-write never corrupts the file.

DATA_DIR (default /home/data) should be a mounted Docker volume so projects,
answers and uploaded documents survive container restarts. Falls back to a
temp dir if the path isn't writable (e.g. running directly on macOS).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_DIR = Path(os.getenv("DATA_DIR", "/home/data"))

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_resolved_dir: Path | None = None


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def ensure_dir(path: Path = DEFAULT_DIR) -> Path:
    """Create + verify the data dir is writable. Caches the resolved path.
    Falls back to a temp dir (warned) so the app never crashes on startup."""
    global _resolved_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        _resolved_dir = path
    except (OSError, PermissionError) as exc:
        fallback = Path(tempfile.gettempdir()) / "roofing-estimator-data"
        fallback.mkdir(parents=True, exist_ok=True)
        LOGGER.warning(
            "DATA_DIR %s is not writable (%s); falling back to %s — data will "
            "NOT persist across container restarts.", path, exc, fallback,
        )
        _resolved_dir = fallback
    (_resolved_dir / "uploads").mkdir(parents=True, exist_ok=True)
    return _resolved_dir


def base_dir() -> Path:
    return _resolved_dir or ensure_dir()


def uploads_dir() -> Path:
    d = base_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(filename: str, default: Any) -> Any:
    path = base_dir() / filename
    lock = _lock_for(path)
    with lock:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read %s (%s); returning default.", path, exc)
            return default


def write_json(filename: str, data: Any) -> None:
    base = base_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    lock = _lock_for(path)
    with lock:
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=str(base),
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
