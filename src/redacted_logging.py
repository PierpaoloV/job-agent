"""Small JSON-lines logger that redacts before data reaches the formatter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import re
import threading
from typing import Any, Callable, TextIO


REDACTED = "[redacted]"
_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,95}")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_NAMED_SECRET = re.compile(
    r"(?i)\b[a-z0-9_-]*(?:token|password|secret|api[_-]?key|authorization|cookie)"
    r"\s*[:=]\s*[^\s,;]+"
)
_SENSITIVE_KEY_PARTS = {
    "answer",
    "answers",
    "attachment",
    "authorization",
    "browser",
    "cookie",
    "credential",
    "cv",
    "demographic",
    "diagnosis",
    "disability",
    "document",
    "health",
    "oauth",
    "password",
    "profile",
    "report",
    "secret",
    "token",
}
_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "cover_letter",
    "identity_document",
    "resume_path",
}


class RedactedStructuredLogger:
    def __init__(
        self,
        stream: TextIO,
        *,
        secrets: Sequence[str] = (),
        sensitive_values: Sequence[str] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stream = stream
        self._sensitive_values = tuple(
            sorted(
                {str(value) for value in (*secrets, *sensitive_values) if str(value)},
                key=len,
                reverse=True,
            )
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def info(self, event: str, **fields: Any) -> None:
        self._write("info", event, fields)

    def error(self, event: str, error: BaseException, **fields: Any) -> None:
        error_fields = dict(fields)
        error_fields.update(
            {
                "error_type": _safe_error_type(error),
                "error": str(error),
            }
        )
        self._write("error", event, error_fields)

    def _write(self, level: str, event: str, fields: Mapping[str, Any]) -> None:
        if not _EVENT.fullmatch(event):
            raise ValueError("Log event must be a safe identifier")
        entry = {
            "timestamp": self._now().isoformat(),
            "level": level,
            "event": event,
            "fields": self.redact(fields),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._stream.write(line)
            self._stream.flush()

    def redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    REDACTED if _is_sensitive_key(str(key)) else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._redact_text(str(value))

    def _redact_text(self, value: str) -> str:
        result = value
        for sensitive in self._sensitive_values:
            result = result.replace(sensitive, REDACTED)
        result = _BEARER.sub(REDACTED, result)
        result = _NAMED_SECRET.sub(REDACTED, result)
        return _EMAIL.sub("[email redacted]", result)


def _is_sensitive_key(key: str) -> bool:
    split_camel_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", split_camel_case.casefold()).strip("_")
    parts = {part for part in normalized.split("_") if part}
    singular_parts = {
        part[:-1] for part in parts if part.endswith("s") and len(part) > 1
    }
    return normalized in _SENSITIVE_EXACT_KEYS or bool(
        (parts | singular_parts) & _SENSITIVE_KEY_PARTS
    )


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"


__all__ = ["REDACTED", "RedactedStructuredLogger"]
