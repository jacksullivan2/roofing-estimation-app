"""User-supplied AWS S3 connection for PROJECT DATA storage.

The estimator enters these details on the homepage; they are persisted in the
local data dir (s3_config.json) so the app can read/write every project's
folder in the bucket. This is separate from the agent-prompts S3 location
(app/infra/s3_client.py), though the same bucket can be used.

Security note: credentials are stored on the app's own persistent volume in
plain JSON. That's acceptable for a single-tenant internal tool; for a shared
deployment prefer an instance role (leave the keys blank to use the default
AWS credential chain) or a secrets manager.
"""

from __future__ import annotations

import logging

from app.infra import local_store

LOGGER = logging.getLogger(__name__)

_CONFIG_FILE = "s3_config.json"

_DEFAULT = {
    "enabled": False,
    "bucket": "",
    "region": "",
    "access_key_id": "",
    "secret_access_key": "",
}


def get() -> dict:
    cfg = dict(_DEFAULT)
    stored = local_store.read_json(_CONFIG_FILE, default={})
    if isinstance(stored, dict):
        cfg.update({k: stored.get(k, cfg[k]) for k in cfg})
    return cfg


def save(*, bucket: str, region: str = "",
         access_key_id: str = "", secret_access_key: str = "",
         enabled: bool = True) -> dict:
    cfg = {
        "enabled": bool(enabled),
        "bucket": (bucket or "").strip(),
        "region": (region or "").strip(),
        "access_key_id": (access_key_id or "").strip(),
        "secret_access_key": (secret_access_key or "").strip(),
    }
    # Preserve an existing secret if the form left it blank (masked field).
    if not cfg["secret_access_key"]:
        cfg["secret_access_key"] = get().get("secret_access_key", "")
    local_store.write_json(_CONFIG_FILE, cfg)
    return cfg


def clear() -> None:
    local_store.write_json(_CONFIG_FILE, dict(_DEFAULT))


def boto3_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("boto3") is not None


def is_configured(cfg: dict | None = None) -> bool:
    """True when we should use S3 for project storage."""
    cfg = cfg or get()
    return bool(cfg.get("enabled") and cfg.get("bucket") and boto3_available())


def has_secret(cfg: dict | None = None) -> bool:
    return bool((cfg or get()).get("secret_access_key"))


def client(cfg: dict | None = None):
    """Build a boto3 S3 client from the stored config. Uses explicit keys when
    provided, otherwise falls back to the default AWS credential chain."""
    import boto3  # lazy — app runs without boto3 installed

    cfg = cfg or get()
    kwargs = {}
    if cfg.get("region"):
        kwargs["region_name"] = cfg["region"]
    if cfg.get("access_key_id") and cfg.get("secret_access_key"):
        kwargs["aws_access_key_id"] = cfg["access_key_id"]
        kwargs["aws_secret_access_key"] = cfg["secret_access_key"]
    return boto3.client("s3", **kwargs)


def test_connection(cfg: dict | None = None) -> tuple[bool, str]:
    """Verify the bucket is reachable with the given credentials."""
    cfg = cfg or get()
    if not cfg.get("bucket"):
        return False, "No bucket name provided."
    if not boto3_available():
        return False, "boto3 is not installed in this environment."
    try:
        client(cfg).head_bucket(Bucket=cfg["bucket"])
        return True, f"Connected to s3://{cfg['bucket']}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not connect: {exc}"
