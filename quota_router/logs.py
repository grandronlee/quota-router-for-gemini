"""JSON-lines logging on stdout, scrubbed after formatting (DESIGN §17, §20.7).

Every record becomes one JSON object. The per-request line uses the logger
`quota_router.request` and passes its fields as `extra={"fields": {...}}`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys

from .scrub import Scrubber

REQUEST_LOGGER = "quota_router.request"


class JsonFormatter(logging.Formatter):
    def __init__(self, scrubber: Scrubber) -> None:
        super().__init__()
        self._scrub = scrubber

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        else:
            out["msg"] = record.getMessage()
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        # Scrub the values before encoding (JSON escapes could hide a secret from a text
        # match), then the encoded line as a last pass.
        out = self._scrub.obj(json.loads(json.dumps(out, ensure_ascii=False, default=str)))
        return self._scrub.text(json.dumps(out, ensure_ascii=False))


def configure_logging(scrubber: Scrubber, level: int = logging.INFO) -> None:
    """Route every logger (uvicorn's included) to one scrubbed JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(scrubber))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # the request line replaces it
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry full URLs
