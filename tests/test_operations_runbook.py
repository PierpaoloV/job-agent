from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_operator_runbook_covers_required_release_operations():
    runbook = (ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    required = (
        "Install and verify",
        "Connect the dedicated Gmail account",
        "career.user@gmail.com",
        "gmail.readonly",
        "Dedicated browser profile",
        "Job Applications",
        "Keychain and local configuration",
        "Schedules and health",
        "Pause, recovery, and uncertain outcomes",
        "/pausa",
        "/riprendi",
        "/riconcilia",
        "Shutdown",
    )

    assert all(value in runbook for value in required)
    assert "TELEGRAM_BOT_TOKEN=" not in runbook
    assert "GMAIL_TOKEN_JSON=" not in runbook
