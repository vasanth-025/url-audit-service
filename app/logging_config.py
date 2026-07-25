"""Structured (JSON) logging with request-ID propagation.

Every log line is a single JSON object so it can be shipped to a log
aggregator and queried by field (request_id, url, status, latency_ms, ...)
without any string parsing.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

# Holds the current request's ID so any log call anywhere in the request's
# execution path can include it, without threading it through every function.
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        # Allow callers to attach structured extra fields via `extra={...}`.
        for key, value in record.__dict__.get("extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy third-party loggers down to warnings only.
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_extra(**fields: Any) -> dict[str, dict[str, Any]]:
    """Helper for attaching structured fields: logger.info("msg", extra=log_extra(url=url))"""
    return {"extra_fields": fields}
