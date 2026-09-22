"""Small, shared observability helpers.

Keep logging, event payloads, and persisted error messages on the same redaction
path.  This module deliberately stays dependency-free so capture startup cannot
be blocked by an optional logging package.
"""

from __future__ import annotations

from logging.handlers import TimedRotatingFileHandler
import logging
from pathlib import Path
import re
from typing import Any, Mapping


_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"secret|password)\b\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+\-/\\=]{8,}")
_SK_TOKEN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._\-\\]{8,}")
_URL_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|key|secret)=)[^&#\s]+"
)


def redact_text(value: object, *, limit: int = 4_000) -> str:
    """Remove common credential shapes and bound persisted diagnostic text."""

    text = str(value)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _URL_SECRET.sub(r"\1[REDACTED]", text)
    text = _SK_TOKEN.sub("[REDACTED]", text)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)
    if len(text) > limit:
        return text[:limit] + "…[truncated]"
    return text


def redact_payload(value: Any) -> Any:
    """Recursively redact persisted event payloads without changing their shape."""

    if isinstance(value, Mapping):
        return {str(key): redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactingFormatter(logging.Formatter):
    """Apply redaction after exception tracebacks and arguments are rendered."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), limit=100_000)


class _PrivateTimedRotatingFileHandler(TimedRotatingFileHandler):
    def _secure(self) -> None:
        Path(self.baseFilename).chmod(0o600)

    def doRollover(self) -> None:
        super().doRollover()
        self._secure()


def configure_service_logging(
    log_path: str | Path,
    *,
    retention_days: int = 14,
) -> Path:
    """Configure one daily-rotated service log with a bounded retention window."""

    path = Path(log_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _PrivateTimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
    )
    handler._secure()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path
