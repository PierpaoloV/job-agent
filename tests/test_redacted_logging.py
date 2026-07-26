from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from redacted_logging import RedactedStructuredLogger


def test_structured_logger_redacts_fields_and_exception_before_serializing():
    output = StringIO()
    logger = RedactedStructuredLogger(
        output,
        secrets=("telegram-token-123",),
        sensitive_values=("private-diagnosis-456",),
        now=lambda: datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )

    logger.info(
        "worker.cycle",
        capability="applications",
        pending=2,
        token="telegram-token-123",
        cover_letter="private-cover-letter",
        protected_report="private-report-path",
        resume_path="private-cv-path",
        candidate_profile={
            "email": "synthetic-owner@example.com",
            "health": "private-diagnosis-456",
        },
    )
    logger.error(
        "worker.capability_failed",
        RuntimeError(
            "Bearer telegram-token-123 rejected for synthetic-owner@example.com: "
            "private-diagnosis-456"
        ),
        capability="applications",
    )

    entries = [json.loads(line) for line in output.getvalue().splitlines()]

    assert entries[0] == {
        "timestamp": "2026-07-16T12:00:00+00:00",
        "level": "info",
        "event": "worker.cycle",
        "fields": {
            "capability": "applications",
            "pending": 2,
            "token": "[redacted]",
            "cover_letter": "[redacted]",
            "protected_report": "[redacted]",
            "resume_path": "[redacted]",
            "candidate_profile": "[redacted]",
        },
    }
    assert entries[1]["fields"]["error_type"] == "RuntimeError"
    serialized = output.getvalue()
    for sensitive in (
        "telegram-token-123",
        "synthetic-owner@example.com",
        "private-diagnosis-456",
    ):
        assert sensitive not in serialized


def test_structured_logger_splits_camel_case_sensitive_keys():
    logger = RedactedStructuredLogger(StringIO())

    redacted = logger.redact(
        {
            "accessToken": "token-value",
            "apiKey": "api-value",
            "clientSecret": "secret-value",
            "healthData": "health-value",
            "identityDocuments": ["passport-value"],
            "pendingCount": 3,
        }
    )

    assert redacted == {
        "accessToken": "[redacted]",
        "apiKey": "[redacted]",
        "clientSecret": "[redacted]",
        "healthData": "[redacted]",
        "identityDocuments": "[redacted]",
        "pendingCount": 3,
    }


def test_structured_logger_redacts_camel_case_secret_assignments_in_error_text():
    output = StringIO()
    logger = RedactedStructuredLogger(output)

    logger.error(
        "worker.capability_failed",
        RuntimeError("accessToken=TOPSECRET clientSecret:SECOND apiKey=THIRD"),
    )

    serialized = output.getvalue()
    for sensitive in ("TOPSECRET", "SECOND", "THIRD"):
        assert sensitive not in serialized
