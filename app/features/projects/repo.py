"""Project storage repository — local disk or AWS S3.

A project is one *folder*:
  - the record  (metadata + answers + params + section text + document index)
  - its documents (raw uploaded bytes)

Two backends implement the same interface. ``get_repo()`` returns the S3
backend when the user has configured + enabled an S3 connection on the
homepage, otherwise the local-disk backend (so the app works out of the box
and in dev without AWS).

The project id is the (sanitised, unique) project name, so the S3 folder is
named after the project. S3 key layout for a project id like "5 Ebury Street":
    5 Ebury Street/project.json
    5 Ebury Street/documents/<filename>
Creating a project writes ``project.json``, which is what "creates the folder"
in S3 (S3 has no real directories — a key prefix is the folder).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.infra import local_store, s3_config

LOGGER = logging.getLogger(__name__)

# Reserved top-level folder holding the contractor's shared labour-rates
# documents (used as a fallback when a project has none uploaded). Kept out
# of the project namespace — create_project must never use this name.
LABOUR_LIBRARY_FOLDER = "_labour_rates"

# Reserved top-level folder holding the shared FileTypeMap reference
# spreadsheet (the filename-fragment -> document-type lookup that workflow
# step 02 reads first). Same library pattern as the labour rates above:
# one copy in the store serves every project.
FILE_TYPE_MAP_FOLDER = "_file_type_map"


def _safe_name(name: str) -> str:
    name = Path(name or "").name
    return re.sub(r"[^A-Za-z0-9._ \-()]+", "_", name).strip() or "file"


# --------------------------------------------------------------------------- #
# Local disk backend                                                          #
# --------------------------------------------------------------------------- #

class LocalRepo:
    mode = "local"

    def _record_name(self, pid: str) -> str:
        return f"project_{pid}.json"

    def read_record(self, pid: str) -> dict | None:
        return local_store.read_json(self._record_name(pid), default=None)

    def write_record(self, rec: dict) -> None:
        local_store.write_json(self._record_name(rec["id"]), rec)

    def list_records(self) -> list[dict]:
        out: list[dict] = []
        for p in local_store.base_dir().glob("project_*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def _doc_dir(self, pid: str) -> Path:
        d = local_store.uploads_dir() / pid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_document(self, pid: str, filename: str, data: bytes) -> None:
        (self._doc_dir(pid) / _safe_name(filename)).write_bytes(data)

    def read_document(self, pid: str, filename: str) -> bytes | None:
        p = local_store.uploads_dir() / pid / _safe_name(filename)
        return p.read_bytes() if p.exists() else None

    def delete_document(self, pid: str, filename: str) -> None:
        p = local_store.uploads_dir() / pid / _safe_name(filename)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    # labour-rates library ------------------------------------------------- #
    def _lab_dir(self) -> Path:
        d = local_store.base_dir() / LABOUR_LIBRARY_FOLDER
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_labour_rates(self) -> list[dict]:
        out = []
        for p in self._lab_dir().glob("*"):
            if p.is_file():
                out.append({"filename": p.name, "size": p.stat().st_size,
                            "modified": p.stat().st_mtime})
        return out

    def read_labour_rates(self, filename: str) -> bytes | None:
        p = self._lab_dir() / _safe_name(filename)
        return p.read_bytes() if p.exists() else None

    def write_labour_rates(self, filename: str, data: bytes) -> None:
        (self._lab_dir() / _safe_name(filename)).write_bytes(data)

    # file-type-map reference library -------------------------------------- #
    def _ftm_dir(self) -> Path:
        d = local_store.base_dir() / FILE_TYPE_MAP_FOLDER
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_file_type_map(self) -> list[dict]:
        out = []
        for p in self._ftm_dir().glob("*"):
            if p.is_file():
                out.append({"filename": p.name, "size": p.stat().st_size,
                            "modified": p.stat().st_mtime})
        return out

    def read_file_type_map(self, filename: str) -> bytes | None:
        p = self._ftm_dir() / _safe_name(filename)
        return p.read_bytes() if p.exists() else None

    def delete_project(self, pid: str) -> None:
        pdir = local_store.uploads_dir() / pid
        if pdir.exists():
            for f in pdir.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                pdir.rmdir()
            except OSError:
                pass
        try:
            (local_store.base_dir() / self._record_name(pid)).unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# AWS S3 backend                                                              #
# --------------------------------------------------------------------------- #

class S3Repo:
    mode = "s3"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bucket = cfg["bucket"]
        self._client = s3_config.client(cfg)

    # key helpers ---------------------------------------------------------- #
    def _folder(self, pid: str) -> str:
        # The project id is the folder name, e.g. "5 Ebury Street/".
        return f"{pid}/"

    def _record_key(self, pid: str) -> str:
        return f"{self._folder(pid)}project.json"

    def _doc_key(self, pid: str, filename: str) -> str:
        return f"{self._folder(pid)}documents/{_safe_name(filename)}"

    # records -------------------------------------------------------------- #
    def read_record(self, pid: str) -> dict | None:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=self._record_key(pid))
            return json.loads(obj["Body"].read().decode("utf-8"))
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 read_record(%s) failed: %s", pid, exc)
            return None

    def write_record(self, rec: dict) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._record_key(rec["id"]),
            Body=json.dumps(rec, indent=2, ensure_ascii=False, default=str).encode("utf-8"),
            ContentType="application/json",
        )

    def list_records(self) -> list[dict]:
        out: list[dict] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket)
            for page in pages:
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith("/project.json"):
                        try:
                            body = self._client.get_object(
                                Bucket=self.bucket, Key=obj["Key"])["Body"].read()
                            out.append(json.loads(body.decode("utf-8")))
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.warning("S3 read %s failed: %s", obj["Key"], exc)
        except Exception as exc:  # noqa: BLE001
            # Bucket unreachable / wrong creds / wrong region / missing bucket.
            # Degrade gracefully so the page still renders instead of 500-ing.
            LOGGER.warning(
                "S3 list_records failed for bucket %s (%s); returning what we have.",
                self.bucket, exc)
        return out

    # documents ------------------------------------------------------------ #
    def write_document(self, pid: str, filename: str, data: bytes) -> None:
        self._client.put_object(
            Bucket=self.bucket, Key=self._doc_key(pid, filename), Body=data)

    def read_document(self, pid: str, filename: str) -> bytes | None:
        try:
            return self._client.get_object(
                Bucket=self.bucket, Key=self._doc_key(pid, filename))["Body"].read()
        except Exception:  # noqa: BLE001
            return None

    def delete_document(self, pid: str, filename: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._doc_key(pid, filename))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 delete_document failed: %s", exc)

    # labour-rates library ------------------------------------------------- #
    def _lab_key(self, filename: str) -> str:
        return f"{LABOUR_LIBRARY_FOLDER}/{_safe_name(filename)}"

    def list_labour_rates(self) -> list[dict]:
        out: list[dict] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket,
                                           Prefix=f"{LABOUR_LIBRARY_FOLDER}/"):
                for obj in page.get("Contents", []):
                    name = obj["Key"].split("/", 1)[-1]
                    if not name:
                        continue
                    out.append({"filename": name, "size": obj.get("Size", 0),
                                "modified": obj["LastModified"].timestamp()})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 list_labour_rates failed: %s", exc)
        return out

    def read_labour_rates(self, filename: str) -> bytes | None:
        try:
            return self._client.get_object(
                Bucket=self.bucket, Key=self._lab_key(filename))["Body"].read()
        except Exception:  # noqa: BLE001
            return None

    def write_labour_rates(self, filename: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket,
                                Key=self._lab_key(filename), Body=data)

    # file-type-map reference library -------------------------------------- #
    def _ftm_key(self, filename: str) -> str:
        return f"{FILE_TYPE_MAP_FOLDER}/{_safe_name(filename)}"

    def list_file_type_map(self) -> list[dict]:
        out: list[dict] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket,
                                           Prefix=f"{FILE_TYPE_MAP_FOLDER}/"):
                for obj in page.get("Contents", []):
                    name = obj["Key"].split("/", 1)[-1]
                    if not name:
                        continue
                    out.append({"filename": name, "size": obj.get("Size", 0),
                                "modified": obj["LastModified"].timestamp()})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 list_file_type_map failed: %s", exc)
        return out

    def read_file_type_map(self, filename: str) -> bytes | None:
        try:
            return self._client.get_object(
                Bucket=self.bucket, Key=self._ftm_key(filename))["Body"].read()
        except Exception:  # noqa: BLE001
            return None

    def delete_project(self, pid: str) -> None:
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[dict] = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self._folder(pid)):
                for obj in page.get("Contents", []):
                    keys.append({"Key": obj["Key"]})
            for i in range(0, len(keys), 1000):
                self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": keys[i:i + 1000]})
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 delete_project(%s) failed: %s", pid, exc)


# --------------------------------------------------------------------------- #
# Selector                                                                    #
# --------------------------------------------------------------------------- #

def get_repo():
    """Return the active repository. S3 when configured + reachable-ish,
    otherwise local disk. Falls back to local on any S3 construction error."""
    cfg = s3_config.get()
    if s3_config.is_configured(cfg):
        try:
            return S3Repo(cfg)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("S3 repo unavailable (%s); using local storage.", exc)
    return LocalRepo()


def storage_mode() -> str:
    return get_repo().mode
