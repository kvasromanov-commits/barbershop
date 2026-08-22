"""Off-site database backup to S3-compatible storage.

HIGH-1 FIX: the daily backup previously lived only on the same Railway volume
as the database, so a volume loss meant total data loss. This module uploads
the local backup produced by ``backup.py`` to any S3-compatible object store
(AWS S3, Cloudflare R2, Backblaze B2, MinIO, ...) using the standard S3 API.

Design guarantees:
* **Never raises.** Every public function swallows exceptions and returns a
  status/int. A transient S3 failure must NOT crash the scheduler job or
  delete/break the local on-disk backup.
* **No secrets in logs.** Access keys, secret keys, session tokens and full
  signed URLs are never logged. Only the bucket name and object key are.
* **Config via env only.** Credentials are read exclusively from environment
  variables; nothing is hardcoded. Missing config is handled gracefully (dev
  mode) — the upload is simply skipped.
* **Timestamped keys.** Object keys embed the backup timestamp, so previous
  backups are never overwritten.
* **Verified upload + retry/backoff.** Upload is verified with ``head_object``
  and retried with exponential backoff on transient errors.
* **Retention.** Old off-site backups beyond ``S3_BACKUP_RETENTION`` (default
  30) are pruned, keeping the newest. Fresh backups are never deleted.
"""

import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Retry/backoff for transient network/API errors during upload.
_MAX_UPLOAD_RETRIES = 3
_UPLOAD_RETRY_BACKOFF = 2.0  # seconds; exponential: 2, 4, 8, ...

_DEFAULT_RETENTION = 30
_BACKUP_EXTENSIONS = (".db.gz", ".sql.gz")


def _s3_env() -> dict:
    """Read S3 configuration from the environment.

    No hardcoded defaults for credentials. Empty strings mean "unset".
    """
    return {
        "endpoint_url": (os.getenv("S3_ENDPOINT_URL", "") or "").strip() or None,
        "region": (os.getenv("S3_REGION", "") or "").strip() or None,
        "bucket": (os.getenv("S3_BUCKET", "") or "").strip(),
        "prefix": (os.getenv("S3_PREFIX", "") or "").strip().strip("/"),
        "access_key": (os.getenv("S3_ACCESS_KEY_ID", "") or "").strip(),
        "secret_key": (os.getenv("S3_SECRET_ACCESS_KEY", "") or "").strip(),
    }


def is_s3_configured() -> bool:
    """Return True only when a bucket AND both credentials are present.

    In development (or when off-site backup is intentionally disabled) this
    returns False and the upload path is skipped silently.
    """
    cfg = _s3_env()
    return bool(cfg["bucket"] and cfg["access_key"] and cfg["secret_key"])


def get_s3_retention() -> int:
    """Number of newest off-site backups to keep. Default 30."""
    raw = (os.getenv("S3_BACKUP_RETENTION", "") or "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_RETENTION


def build_object_key(local_path: str, prefix: str = "") -> str:
    """Build a timestamped S3 object key from the local backup file name.

    The local filename already contains a ``YYYYMMDD_HHMMSS`` timestamp (see
    ``backup.py``), so the resulting key is unique per backup and never
    overwrites a previous one. An optional ``prefix`` (no leading/trailing
    slashes) namespaces the keys, e.g. ``prod/barbershop_20260822_031500.db.gz``.
    """
    base = os.path.basename(local_path)
    prefix = (prefix or "").strip().strip("/")
    if prefix:
        return f"{prefix}/{base}"
    return base


def _build_client(cfg: dict):
    """Lazily import boto3 and build an S3 client.

    ``endpoint_url`` enables Cloudflare R2 / Backblaze B2 / MinIO. When a custom
    endpoint is used we force path-style addressing, which those providers
    require. Credentials are passed from env only.
    """
    import boto3
    from botocore.config import Config

    client_kwargs: dict = {}
    if cfg["endpoint_url"]:
        client_kwargs["endpoint_url"] = cfg["endpoint_url"]
    if cfg["region"]:
        client_kwargs["region_name"] = cfg["region"]
    if cfg["access_key"]:
        client_kwargs["aws_access_key_id"] = cfg["access_key"]
        client_kwargs["aws_secret_access_key"] = cfg["secret_key"]
    if cfg["endpoint_url"]:
        client_kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **client_kwargs)


def upload_backup_to_s3(local_path: str) -> bool:
    """Upload a local backup file to S3-compatible storage.

    Steps: build a timestamped key, ``upload_file``, then verify with
    ``head_object``. Retries up to ``_MAX_UPLOAD_RETRIES`` times with
    exponential backoff on transient errors. Returns ``True`` on success,
    ``False`` on any failure or when S3 is not configured. Never raises.
    """
    if not local_path or not os.path.exists(local_path):
        logger.error("Off-site upload skipped: local backup file missing: %s", local_path)
        return False

    if not is_s3_configured():
        # Graceful in dev: no S3 config -> silent skip (local backup kept).
        logger.debug(
            "S3 not configured (S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY) — off-site upload skipped"
        )
        return False

    cfg = _s3_env()
    bucket = cfg["bucket"]
    key = build_object_key(local_path, cfg["prefix"])

    last_exc_type = None
    for attempt in range(1, _MAX_UPLOAD_RETRIES + 1):
        try:
            client = _build_client(cfg)
            client.upload_file(local_path, bucket, key)
            # Verify the object is actually present before declaring success.
            client.head_object(Bucket=bucket, Key=key)
            logger.info("Off-site backup uploaded and verified: s3://%s/%s", bucket, key)
            return True
        except Exception as exc:
            last_exc_type = type(exc).__name__
            logger.warning(
                "S3 upload attempt %d/%d failed for s3://%s/%s: %s",
                attempt,
                _MAX_UPLOAD_RETRIES,
                bucket,
                key,
                last_exc_type,
            )
            if attempt < _MAX_UPLOAD_RETRIES:
                time.sleep(_UPLOAD_RETRY_BACKOFF**attempt)

    logger.error(
        "Off-site backup upload FAILED after %d retries: s3://%s/%s (last error: %s)",
        _MAX_UPLOAD_RETRIES,
        bucket,
        key,
        last_exc_type,
    )
    return False


def cleanup_old_s3_backups(retention: int | None = None) -> int:
    """Prune old off-site backups, keeping the newest ``retention``.

    Uses S3 ListObjectsV2 pagination (handles >1000 objects). Fresh backups are
    never deleted. Returns the number removed. Never raises — any error is
    logged and 0 is returned.
    """
    try:
        if not is_s3_configured():
            return 0
        if retention is None:
            retention = get_s3_retention()
        if retention < 1:
            retention = _DEFAULT_RETENTION

        cfg = _s3_env()
        bucket = cfg["bucket"]
        prefix = cfg["prefix"]
        client = _build_client(cfg)

        objects: list[tuple[str, datetime]] = []
        paginator = client.get_paginator("list_objects_v2")
        list_kwargs: dict = {"Bucket": bucket}
        if prefix:
            list_kwargs["Prefix"] = prefix + "/"
        for page in paginator.paginate(**list_kwargs):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if key.endswith(_BACKUP_EXTENSIONS):
                    objects.append((key, obj.get("LastModified")))

        # Oldest first; drop the extension check already done above.
        objects.sort(key=lambda item: item[1] or datetime.min)
        to_delete = objects[:-retention] if len(objects) > retention else []

        removed = 0
        for key, _ in to_delete:
            try:
                client.delete_object(Bucket=bucket, Key=key)
                logger.info("Removed old off-site backup: s3://%s/%s", bucket, key)
                removed += 1
            except Exception as del_exc:
                logger.error(
                    "Failed to delete off-site backup s3://%s/%s: %s",
                    bucket,
                    key,
                    type(del_exc).__name__,
                )
        return removed
    except Exception as exc:
        logger.error("S3 retention cleanup failed: %s", type(exc).__name__)
        return 0


def offsite_backup(local_path: str) -> bool:
    """Best-effort orchestrator: upload then apply retention. Never raises.

    Returns ``True`` if the upload succeeded. Retention cleanup always runs
    (independent of upload result) but its errors are contained.
    """
    uploaded = False
    try:
        uploaded = upload_backup_to_s3(local_path)
    except Exception as exc:
        logger.error("Off-site backup step raised unexpectedly: %s", type(exc).__name__)
        uploaded = False

    try:
        cleanup_old_s3_backups()
    except Exception as exc:
        logger.error("Off-site retention cleanup raised unexpectedly: %s", type(exc).__name__)

    return uploaded
