from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import telegram_smoke


def test_smoke_test_exercises_realistic_alert_boundaries(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "example-org/job-agent")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(
        telegram_smoke,
        "send_message",
        lambda message: sent.append(message),
    )

    assert telegram_smoke.main() == 0

    assert len(sent) == 1
    assert "[DIAGNOSTICA] AI Scientist" in sent[0].text
    assert "Job Agent Smoke Test" in sent[0].text
    assert "example-org/job-agent/actions/runs/123" in sent[0].text
