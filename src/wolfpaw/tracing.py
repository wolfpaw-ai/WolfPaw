"""Structured JSON logging with per-request `trace_id` propagation.

`trace_id` lives in a ContextVar so any log line emitted inside a request
handler picks it up automatically — no manual plumbing through call sites.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

_trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_trace_id() -> str | None:
    return _trace_id_ctx.get()


def set_trace_id(trace_id: str | None) -> None:
    _trace_id_ctx.set(trace_id)


def new_trace_id() -> str:
    trace_id = uuid.uuid4().hex
    _trace_id_ctx.set(trace_id)
    return trace_id


def _add_trace_id(_logger: Any, _method: str, event_dict: dict) -> dict:
    tid = _trace_id_ctx.get()
    if tid is not None:
        event_dict["trace_id"] = tid
    return event_dict


# Integration API credentials (Document Organizer keys are df_live_… /
# df_test_…). Scrubbed from every log line so a key that leaks into an
# error message or exception text never reaches stdout.
_SECRET_PATTERNS = [re.compile(r"\bdf[a-z]?_(?:live|test)_[0-9A-Za-z]{8,}")]


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted-key]", text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _redact_processor(_logger: Any, _method: str, event_dict: dict) -> dict:
    return {k: _redact_value(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog → stdout JSON, one event per line. Idempotent."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Not dict_tracebacks: it serializes frame locals, leaking
            # API keys and session tokens into stdout.
            structlog.processors.format_exc_info,
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "wolfpaw") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
