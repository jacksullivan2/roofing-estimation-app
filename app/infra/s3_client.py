"""Retrieve the workflow-step ("agent prompt") markdown files.

Primary source is an AWS S3 folder; if S3 is not configured (or boto3 isn't
available, or the fetch fails) it falls back to the bundled local
``_agent_prompts`` directory so the tender workflow still runs.

Returns a list of ``{"name": str, "text": str, "source": "s3"|"local"}``
ordered by filename — order matters because the steps run in sequence.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import settings

LOGGER = logging.getLogger(__name__)


def _is_step_file(name: str) -> bool:
    """Workflow-step markdown only — ignore README/index files."""
    base = name.rsplit("/", 1)[-1].lower()
    return base.endswith(".md") and not base.startswith("readme")


def _from_local() -> list[dict]:
    d = Path(settings.AGENT_PROMPTS_LOCAL_DIR)
    if not d.is_dir():
        LOGGER.warning("Local agent-prompts dir not found: %s", d)
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.md")):
        if not _is_step_file(p.name):
            continue
        try:
            out.append({"name": p.name, "text": p.read_text(encoding="utf-8"),
                        "source": "local"})
        except OSError as exc:
            LOGGER.warning("Could not read prompt %s: %s", p, exc)
    return out


def _from_s3() -> list[dict]:
    """Download every .md object under the configured bucket/prefix.

    Raises on any error so the caller can decide to fall back. boto3 reads
    AWS credentials from the standard environment / instance role.
    """
    import boto3  # imported lazily so the app runs without boto3 installed

    kwargs = {}
    if settings.AWS_REGION:
        kwargs["region_name"] = settings.AWS_REGION
    s3 = boto3.client("s3", **kwargs)

    bucket = settings.AGENT_PROMPTS_S3_BUCKET
    prefix = settings.AGENT_PROMPTS_S3_PREFIX or ""

    out: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not _is_step_file(key):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            out.append({
                "name": key.rsplit("/", 1)[-1],
                "text": body.decode("utf-8", errors="replace"),
                "source": "s3",
            })
    out.sort(key=lambda d: d["name"])
    return out


def fetch_agent_prompts() -> list[dict]:
    """Best-effort fetch: try S3 when configured, else local. Never raises —
    returns an empty list only if neither source yields anything."""
    if settings.s3_configured():
        try:
            prompts = _from_s3()
            if prompts:
                LOGGER.info("Fetched %d agent prompt(s) from S3.", len(prompts))
                return prompts
            LOGGER.warning("S3 prompts location empty; falling back to local.")
        except Exception as exc:  # boto3 missing, auth, network, etc.
            LOGGER.warning("S3 prompt fetch failed (%s); falling back to local.", exc)
    return _from_local()
