"""Tests for s3_backup.py (HIGH-1 off-site backup module).

These tests are fully hermetic: boto3 is never imported (we patch
``s3_backup._build_client`` to return a MagicMock), and S3 env vars are supplied
via a ``os.getenv`` side_effect so they never touch the real environment.
"""

import datetime
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import s3_backup


@contextmanager
def _s3_env_patch(env: dict):
    """Patch s3_backup's os.getenv with a dict-backed side_effect."""

    def _get(key, default=None):
        return env.get(key, "" if default is None else default)

    with patch.object(s3_backup.os, "getenv", side_effect=_get):
        yield


def _client_with_paginator(contents):
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": contents}]
    client.get_paginator.return_value = paginator
    return client


# ---------------------------------------------------------------------------
# is_s3_configured
# ---------------------------------------------------------------------------


def test_is_s3_configured_false_when_missing():
    with _s3_env_patch({}):
        assert s3_backup.is_s3_configured() is False


def test_is_s3_configured_true_when_present():
    with _s3_env_patch({"S3_BUCKET": "b", "S3_ACCESS_KEY_ID": "a", "S3_SECRET_ACCESS_KEY": "s"}):
        assert s3_backup.is_s3_configured() is True


def test_is_s3_configured_requires_bucket_and_credentials():
    # bucket present but no credentials -> not configured
    with _s3_env_patch({"S3_BUCKET": "b"}):
        assert s3_backup.is_s3_configured() is False
    # credentials but no bucket -> not configured
    with _s3_env_patch({"S3_ACCESS_KEY_ID": "a", "S3_SECRET_ACCESS_KEY": "s"}):
        assert s3_backup.is_s3_configured() is False


def test_get_s3_retention_default_and_env():
    with _s3_env_patch({}):
        assert s3_backup.get_s3_retention() == 30
    with _s3_env_patch({"S3_BACKUP_RETENTION": "12"}):
        assert s3_backup.get_s3_retention() == 12
    with _s3_env_patch({"S3_BACKUP_RETENTION": "not-a-number"}):
        assert s3_backup.get_s3_retention() == 30


# ---------------------------------------------------------------------------
# build_object_key
# ---------------------------------------------------------------------------


def test_build_object_key_no_prefix():
    assert s3_backup.build_object_key("/a/barbershop_20260822_031500.db.gz") == "barbershop_20260822_031500.db.gz"


def test_build_object_key_with_prefix():
    assert (
        s3_backup.build_object_key("/a/barbershop_20260822_031500.sql.gz", "prod")
        == "prod/barbershop_20260822_031500.sql.gz"
    )


def test_build_object_key_strips_slashes():
    assert s3_backup.build_object_key("/a/x.db.gz", "/prod/") == "prod/x.db.gz"


# ---------------------------------------------------------------------------
# upload_backup_to_s3
# ---------------------------------------------------------------------------


def test_upload_success(tmp_path):
    env = {
        "S3_BUCKET": "bkt",
        "S3_ACCESS_KEY_ID": "ak",
        "S3_SECRET_ACCESS_KEY": "sk",
        "S3_PREFIX": "prod",
    }
    backup = tmp_path / "barbershop_20260822_031500.db.gz"
    backup.write_bytes(b"data")
    client = MagicMock()
    with _s3_env_patch(env), patch.object(s3_backup, "_build_client", return_value=client):
        result = s3_backup.upload_backup_to_s3(str(backup))
    assert result is True
    client.upload_file.assert_called_once()
    client.head_object.assert_called_once()
    # key carries the timestamped filename under the prefix
    _, _, key = client.upload_file.call_args.args
    assert key == "prod/barbershop_20260822_031500.db.gz"


def test_upload_missing_file_returns_false():
    result = s3_backup.upload_backup_to_s3("/no/such/file.db.gz")
    assert result is False


def test_upload_not_configured_returns_false_without_client(tmp_path):
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    with _s3_env_patch({}):
        result = s3_backup.upload_backup_to_s3(str(backup))
    assert result is False


def test_upload_error_retries_then_fails(tmp_path):
    env = {"S3_BUCKET": "bkt", "S3_ACCESS_KEY_ID": "ak", "S3_SECRET_ACCESS_KEY": "sk"}
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    client = MagicMock()
    client.upload_file.side_effect = Exception("boom")
    with (
        _s3_env_patch(env),
        patch.object(s3_backup, "_build_client", return_value=client),
        patch.object(s3_backup.time, "sleep"),  # keep test fast
    ):
        result = s3_backup.upload_backup_to_s3(str(backup))
    assert result is False
    assert client.upload_file.call_count == s3_backup._MAX_UPLOAD_RETRIES


def test_upload_verify_failure_is_retried(tmp_path):
    """upload_file succeeds but head_object fails -> treated as failure."""
    env = {"S3_BUCKET": "bkt", "S3_ACCESS_KEY_ID": "ak", "S3_SECRET_ACCESS_KEY": "sk"}
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    client = MagicMock()
    client.head_object.side_effect = Exception("gone")
    with (
        _s3_env_patch(env),
        patch.object(s3_backup, "_build_client", return_value=client),
        patch.object(s3_backup.time, "sleep"),
    ):
        result = s3_backup.upload_backup_to_s3(str(backup))
    assert result is False
    assert client.upload_file.call_count == s3_backup._MAX_UPLOAD_RETRIES


def test_no_secrets_in_logs(caplog, tmp_path):
    import logging

    env = {
        "S3_BUCKET": "bkt",
        "S3_ACCESS_KEY_ID": "AKIA-LEAK-XYZ",
        "S3_SECRET_ACCESS_KEY": "super-secret-123",
    }
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    client = MagicMock()
    client.upload_file.side_effect = Exception("boom")
    with (
        _s3_env_patch(env),
        patch.object(s3_backup, "_build_client", return_value=client),
        patch.object(s3_backup.time, "sleep"),
        caplog.at_level(logging.DEBUG),
    ):
        s3_backup.upload_backup_to_s3(str(backup))
    combined = caplog.text
    assert "AKIA-LEAK-XYZ" not in combined
    assert "super-secret-123" not in combined


# ---------------------------------------------------------------------------
# cleanup_old_s3_backups
# ---------------------------------------------------------------------------


def test_cleanup_retention_removes_oldest():
    env = {
        "S3_BUCKET": "bkt",
        "S3_ACCESS_KEY_ID": "ak",
        "S3_SECRET_ACCESS_KEY": "sk",
        "S3_PREFIX": "prod",
    }
    contents = [
        {"Key": "prod/barbershop_20260101_000000.db.gz", "LastModified": datetime.datetime(2026, 1, 1)},
        {"Key": "prod/barbershop_20260102_000000.db.gz", "LastModified": datetime.datetime(2026, 1, 2)},
        {"Key": "prod/barbershop_20260103_000000.db.gz", "LastModified": datetime.datetime(2026, 1, 3)},
        {"Key": "prod/notes.txt", "LastModified": datetime.datetime(2026, 1, 4)},  # not a backup
    ]
    client = _client_with_paginator(contents)
    with _s3_env_patch(env), patch.object(s3_backup, "_build_client", return_value=client):
        removed = s3_backup.cleanup_old_s3_backups(retention=2)
    assert removed == 1
    client.delete_object.assert_called_once()
    deleted_key = client.delete_object.call_args.kwargs["Key"]
    assert deleted_key == "prod/barbershop_20260101_000000.db.gz"


def test_cleanup_unconfigured_returns_zero():
    with _s3_env_patch({}):
        assert s3_backup.cleanup_old_s3_backups(retention=5) == 0


def test_cleanup_error_does_not_raise():
    env = {"S3_BUCKET": "bkt", "S3_ACCESS_KEY_ID": "ak", "S3_SECRET_ACCESS_KEY": "sk"}
    client = MagicMock()
    client.get_paginator.side_effect = Exception("list failed")
    with _s3_env_patch(env), patch.object(s3_backup, "_build_client", return_value=client):
        removed = s3_backup.cleanup_old_s3_backups(retention=2)
    assert removed == 0


def test_cleanup_under_limit_removes_nothing():
    env = {"S3_BUCKET": "bkt", "S3_ACCESS_KEY_ID": "ak", "S3_SECRET_ACCESS_KEY": "sk", "S3_PREFIX": "prod"}
    contents = [
        {"Key": "prod/a.db.gz", "LastModified": datetime.datetime(2026, 1, 1)},
        {"Key": "prod/b.db.gz", "LastModified": datetime.datetime(2026, 1, 2)},
    ]
    client = _client_with_paginator(contents)
    with _s3_env_patch(env), patch.object(s3_backup, "_build_client", return_value=client):
        removed = s3_backup.cleanup_old_s3_backups(retention=30)
    assert removed == 0
    client.delete_object.assert_not_called()


# ---------------------------------------------------------------------------
# offsite_backup orchestrator
# ---------------------------------------------------------------------------


def test_offsite_backup_runs_retention_even_if_upload_fails(tmp_path):
    env = {
        "S3_BUCKET": "bkt",
        "S3_ACCESS_KEY_ID": "ak",
        "S3_SECRET_ACCESS_KEY": "sk",
        "S3_PREFIX": "prod",
        "S3_BACKUP_RETENTION": "2",
    }
    contents = [
        {"Key": "prod/a.db.gz", "LastModified": datetime.datetime(2026, 1, 1)},
        {"Key": "prod/b.db.gz", "LastModified": datetime.datetime(2026, 1, 2)},
        {"Key": "prod/c.db.gz", "LastModified": datetime.datetime(2026, 1, 3)},
    ]
    client = _client_with_paginator(contents)
    client.upload_file.side_effect = Exception("up fail")
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    with (
        _s3_env_patch(env),
        patch.object(s3_backup, "_build_client", return_value=client),
        patch.object(s3_backup.time, "sleep"),
    ):
        result = s3_backup.offsite_backup(str(backup))
    assert result is False  # upload failed
    # retention still pruned 1 of 3 with retention=2
    assert client.delete_object.call_count == 1


def test_offsite_backup_returns_true_on_success(tmp_path):
    env = {"S3_BUCKET": "bkt", "S3_ACCESS_KEY_ID": "ak", "S3_SECRET_ACCESS_KEY": "sk"}
    client = MagicMock()
    backup = tmp_path / "x.db.gz"
    backup.write_bytes(b"data")
    with (
        _s3_env_patch(env),
        patch.object(s3_backup, "_build_client", return_value=client),
    ):
        result = s3_backup.offsite_backup(str(backup))
    assert result is True
