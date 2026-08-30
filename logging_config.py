"""Structured logging: one JSON object per line, on stdout.

Without this the root logger has no handler at all, so `logger.info` is dropped
outright and `logger.warning` escapes through `logging.lastResort` as a bare
message — no timestamp, no level, no module. The nightly job's only success
signal was invisible.

PRIVACY: the store is encrypted per user, so a log line must never carry an
amount, a merchant, a category or anything else decrypted. Identifiers,
counters and durations only — a blind index is truncated, never a clear value.
"""

import json
import logging
import logging.config
from contextvars import ContextVar

# Set by the request-id middleware, read by the filter below. A ContextVar so
# background tasks and threads spawned per request inherit it on their own.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


# LogRecord attributes that are structural, not payload: everything else an
# `extra=` passes in is merged into the JSON object.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "request_id", "taskName"}


class JsonFormatter(logging.Formatter):
    """One line, one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Route every logger, uvicorn's included, through one JSON handler.

    Runs after uvicorn has applied its own config (it configures logging before
    importing the app), so this one wins. The uvicorn loggers are re-declared
    with no handler of their own, otherwise each line would be emitted twice —
    once by them, once by root.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_id": {"()": RequestIdFilter}},
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json",
                    "filters": ["request_id"],
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                name: {"handlers": [], "propagate": True}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
            },
        }
    )
