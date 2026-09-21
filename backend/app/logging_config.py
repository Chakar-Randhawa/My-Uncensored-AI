"""
Structured (JSON) logging configuration.

Deliberately hand-rolled instead of pulling in `python-json-logger` — it's
about 30 lines of actual logic and one fewer dependency to keep the
free-tier container image small. Every log line becomes a single-line JSON
object, which is what every log aggregator (Render's own log viewer
included) wants for filtering/searching by field.
"""

from __future__ import annotations

import json
import logging
import sys
import time


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Any extra= fields passed to the logging call ride along verbatim
        # — e.g. logger.info("race finished", extra={"provider": "groq"}).
        reserved = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
        for key, value in record.__dict__.items():
            if key not in reserved and key not in payload:
                payload[key] = value

        return json.dumps(payload, default=str)


def configure_logging(*, environment: str) -> None:
    handler = logging.StreamHandler(sys.stdout)

    if environment == "development":
        # Human-readable in local dev — JSON is great for log aggregators,
        # miserable to eyeball in a terminal while iterating.
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet down noisy third-party loggers at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
