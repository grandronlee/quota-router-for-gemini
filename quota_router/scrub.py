"""Removing secrets from log lines, error bodies and captures (DESIGN §10, §20.7)."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

REDACTED = "<redacted>"
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
_PROJECT = re.compile(r"(projects/)\d+")
_CONSUMER = re.compile(r"(\"consumer\"\s*:\s*\")[^\"]+")


class Scrubber:
    def __init__(self, secrets: Iterable[str], extra: Iterable[str] = ()) -> None:
        # Longest first, so a secret that contains another is removed whole. `extra` values
        # are scrubbed whatever their length (restored signatures, see with_extra).
        kept = {s for s in secrets if s and len(s) >= 8} | {v for v in extra if v}
        self._secrets = sorted(kept, key=len, reverse=True)

    def with_extra(self, values: Iterable[str]) -> "Scrubber":
        """This scrubber plus `values`, for one upstream call: the thought signatures the
        proxy restored into its body (DESIGN §23.8 amendment 2), at any length (found in
        review: a short stored value went unscrubbed). Returns self when none."""
        extra = [v for v in values if v]
        return Scrubber(self._secrets, extra) if extra else self

    def text(self, s: str) -> str:
        for secret in self._secrets:
            s = s.replace(secret, REDACTED)
        return _GOOGLE_KEY.sub(REDACTED, s)

    def obj(self, o: Any, _text: Callable[[str], str] | None = None) -> Any:
        """Scrub every string (keys included) in decoded JSON-like data. Run this before
        encoding: once encoded, JSON escapes (`\"`, `\\`, `\u0041`) can hide a secret from
        a text match."""
        fn = _text or self.text
        if isinstance(o, str):
            return fn(o)
        if isinstance(o, dict):
            return {fn(k) if isinstance(k, str) else k: self.obj(v, fn) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v, fn) for v in o]
        return o

    def bytes(self, b: bytes) -> bytes:
        """Scrub a body that is probably JSON: decode it, scrub the values, re-encode; a
        text pass runs on the result either way (and is all a non-JSON body gets)."""
        text = b.decode("utf-8", "replace")
        try:
            text = json.dumps(self.obj(json.loads(text)), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
        return self.text(text).encode("utf-8")

    def capture(self, s: str) -> str:
        """For files that may be shared (429 captures): also drop project numbers."""
        s = self.text(s)
        s = _PROJECT.sub(r"\1" + REDACTED, s)
        return _CONSUMER.sub(r"\1" + REDACTED, s)

    def capture_body(self, b: bytes) -> str:
        """A whole body, scrubbed for a capture file (structured first, then as text)."""
        text = b.decode("utf-8", "replace")
        try:
            text = json.dumps(self.obj(json.loads(text), self.capture), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
        return self.capture(text)
