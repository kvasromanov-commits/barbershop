"""Structured logging configuration with request-ID correlation.

Two output modes:
- Default (human): `2026-08-18 12:45:55,123 [INFO] bot: Bot started  rid=…`
- LOG_JSON=1 (machine): single-line JSON with timestamps and contextual fields.
  Suitable for fluentbit / Loki / CloudWatch ingestion.

A `request_id` ContextVar is exposed so middleware can attach a UUID to every
incoming update; downstream handlers and adapters inherit it for free via the
`extra={…}` mechanism wrapped by `bind_request_id()`.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from logging import LogRecord
from typing import Any, Optional

# Per-asyncio-task request id. Default is "-" so logs without an id still
# emit valid records.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    """Generate a fresh request id and bind it to the current context."""
    rid = uuid.uuid4().hex[:16]
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    """Return the request id bound to the current context (or '-')."""
    return _request_id.get()


def bind_request_id(rid: str) -> None:
    """Attach an existing request id to the current context (e.g., for retries)."""
    _request_id.set(rid)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line. Standard fields + extras flattened."""

    # Standard LogRecord attributes we don't want to copy as 'extra'
    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    def format(self, record: LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or _request_id.get(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        # Merge any `extra={…}` fields passed to logger calls (skip reserved attrs).
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _safe(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Two-line readable format with rid and short path."""

    def format(self, record: LogRecord) -> str:
        rid = getattr(record, "request_id", None) or _request_id.get()
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        short_name = record.name.split(".")[-1] if record.name else record.name
        base = f"{ts} [{record.levelname:<5}] {short_name}: {record.getMessage()}"
        if rid and rid != "-":
            base = f"{base}  rid={rid}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _safe(value: Any) -> Any:
    """Recursively ensure values are JSON-serializable (datetimes, sets, etc.)."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)


def configure_logging(level: Optional[str] = None) -> None:
    """Install our handlers; safe to call multiple times (idempotent)."""
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    json_mode = os.getenv("LOG_JSON", "").strip().lower() in {"1", "true", "yes"}

    root = logging.getLogger()
    # Replace any existing handlers (e.g., the default `logging.basicConfig`
    # added by libraries) with one of ours.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_mode else HumanFormatter())
    root.addHandler(handler)
    root.setLevel(log_level)

    # Tame noisy third-party loggers unless user opts in to verbose
    for noisy in ("aiogram.event", "aiogram.dispatcher", "aiohttp.access"):
        logging.getLogger(noisy).setLevel("WARNING")

    # Stamp every record with a host tag (useful when ingesting multiple pods)
    _hostname = socket.gethostname()


def add_host_tag(record: LogRecord) -> bool:
    """Attach `host` field to records. Returns True so it's usable as a filter."""
    record.host = socket.gethostname()
    return True
