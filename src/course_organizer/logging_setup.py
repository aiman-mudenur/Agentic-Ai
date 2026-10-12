"""Structured logging with a run id stamped on every record.

Lab 6 of the document requires an audit log where "every action is logged with
inputs/outputs". This module is the *observability* half of that (the durable
half is `runtime/audit.py`, built in Lab 6) — but it starts here so every lab
from Lab 1 onward already emits correlatable, machine-parsable log lines.

The doc's Lab 6 pitfall — "log inputs and outputs, not just messages" — is why
`log_event()` takes structured keyword fields rather than a formatted string.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"


class RunIdFilter(logging.Filter):
    """Injects a `run_id` attribute into every record.

    Falls back to "-" when no run is active, so handlers configured with this
    filter can never raise `KeyError` on a formatting error.
    """

    def __init__(self, run_id: str = "-") -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach the run id to the record; always returns True."""
        if not hasattr(record, "run_id"):
            record.run_id = self.run_id
        return True


class JsonFormatter(logging.Formatter):
    """Renders a record as a single JSON object.

    Used for the machine-readable run log so a reviewer can `jq` it, and so a
    future lab can replay a run without scraping human-formatted text.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise the record, including any structured `extra` fields."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        # Any keyword passed via `extra=` beyond the reserved LogRecord keys.
        reserved = {
            "args", "asctime", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "message", "module",
            "msecs", "msg", "name", "pathname", "process", "processName",
            "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in reserved and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(
    *,
    level: str = "INFO",
    json_logfile: Path | None = None,
    run_id: str | None = None,
) -> str:
    """Configure root logging once and return the active run id.

    Args:
        level: Standard logging level name.
        json_logfile: Optional path for the structured JSON run log.
        run_id: Reuse an existing run id instead of generating a fresh one.

    Returns:
        The run id that will be stamped on every subsequent log record.
    """
    active_run_id = run_id or uuid.uuid4().hex[:12]
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Replace handlers rather than append, so repeated calls (notebooks re-run
    # cells constantly) do not produce duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    run_filter = RunIdFilter(active_run_id)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    console.addFilter(run_filter)
    root.addHandler(console)

    if json_logfile is not None:
        json_logfile.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(json_logfile, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(run_filter)
        root.addHandler(file_handler)

    # These libraries are extremely chatty at DEBUG and drown out our own logs.
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return active_run_id


@contextmanager
def run_scope(run_id: str) -> Iterator[str]:
    """Re-stamp the root filter to a different run id for the block's duration.

    Lets one process host several runs (useful in tests and in the Lab 8
    parallel comparison) without cross-contaminating their log lines.
    """
    root = logging.getLogger()
    filters = [
        log_filter
        for handler in root.handlers
        for log_filter in handler.filters
        if isinstance(log_filter, RunIdFilter)
    ]
    saved = filters[0].run_id if filters else "-"
    for log_filter in filters:
        log_filter.run_id = run_id
    try:
        yield run_id
    finally:
        for log_filter in filters:
            log_filter.run_id = saved


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event.

    Args:
        logger: The logger to emit on.
        event: A stable, snake_case event name, e.g. `clarify_question_asked`.
        level: Logging level for this event.
        **fields: Structured payload. Avoid unserialisable objects; the JSON
            formatter falls back to `str()` for anything exotic.
    """
    logger.log(level, event, extra={"event": event, **fields})


__all__ = [
    "JsonFormatter",
    "RunIdFilter",
    "log_event",
    "run_scope",
    "setup_logging",
]
