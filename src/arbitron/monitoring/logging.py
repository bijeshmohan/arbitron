"""Structured, redacted logging (plan §19.1).

- JSON lines to stderr (``log_format = "json"``), or readable text.
- Every record carries timestamp (UTC), level, component (logger name),
  event (message), and any ``extra=`` fields such as ``correlation_id``,
  ``symbol``, ``order_id``.
- **Redaction:** registered secret values (API keys, database password,
  Telegram token) are replaced wherever they appear: message, extra fields,
  or exception text. Pattern-based scrubbing also catches URL credentials and
  Telegram bot tokens, as a backstop for secrets that were never registered.
"""

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar, Final, override

REDACTED: Final = "[REDACTED]"
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@", re.I
)
# Telegram bot tokens look like 123456789:AA... and appear in API URLs right
# after "bot" (".../bot123456789:AA.../sendMessage"), where there is no word
# boundary, so explicit lookarounds are used instead of \b.
_TELEGRAM_TOKEN = re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")
# Standard LogRecord attributes; everything else on a record is an extra field.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI-coloured duplicate of the message
}


class Redactor:
    """Scrubs registered secrets and secret-shaped patterns from text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        self.register(secrets)

    def register(self, secrets: Iterable[str]) -> None:
        # Very short values would redact innocent text; real secrets are long.
        self._secrets.update(s for s in secrets if s and len(s) >= 6)

    def __call__(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        text = _URL_CREDENTIALS.sub(rf"\g<scheme>\g<user>:{REDACTED}@", text)
        return _TELEGRAM_TOKEN.sub(REDACTED, text)


class _RedactingFormatter(logging.Formatter):
    redactor: ClassVar[Redactor] = Redactor()

    def _clean(self, value: object) -> object:
        if isinstance(value, str):
            return self.redactor(value)
        if isinstance(value, int | float | bool) or value is None:
            return value
        return self.redactor(str(value))

    def fields(self, record: logging.LogRecord) -> dict[str, object]:
        fields: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "component": record.name,
            "event": self.redactor(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                fields[key] = self._clean(value)
        if record.exc_info:
            fields["error"] = self.redactor(self.formatException(record.exc_info))
        return fields


class JsonFormatter(_RedactingFormatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.fields(record), default=str, separators=(",", ":"))


class TextFormatter(_RedactingFormatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        fields = self.fields(record)
        head = (
            f"{fields.pop('ts')} {str(fields.pop('level')).upper():7} {fields.pop('component')}: "
        )
        head += str(fields.pop("event"))
        error = fields.pop("error", None)
        extras = " ".join(f"{k}={v}" for k, v in fields.items())
        return head + (f" [{extras}]" if extras else "") + (f"\n{error}" if error else "")


def configure_logging(*, log_format: str, level: str, secrets: Iterable[str] = ()) -> Redactor:
    """Configure the root logger for the process. Returns the shared redactor."""
    redactor = Redactor(secrets)
    _RedactingFormatter.redactor = redactor
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library chatter (httpx request lines include URLs) stays at WARNING.
    for noisy in ("httpx", "httpcore", "uvicorn.access", "ccxt", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return redactor


def log_fields(**fields: Any) -> dict[str, Any]:
    """``extra=`` helper that drops ``None`` values."""
    return {k: v for k, v in fields.items() if v is not None}
