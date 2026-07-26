from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "job_agent.py"


def write_config(tmp_path: Path, *, secret_marker: str = "") -> Path:
    credentials = tmp_path / "gmail-credentials.json"
    token = tmp_path / "gmail-token.json"
    profile = tmp_path / "grading-profile.json"
    evidence = tmp_path / "professional-evidence.yaml"
    preferences = tmp_path / "preferences.yaml"
    cv = tmp_path / "curriculum-vitae.pdf"
    for path in (credentials, token, profile, evidence, preferences, cv):
        path.write_text("synthetic", encoding="utf-8")

    config = {
        "version": 1,
        "deployment": {"name": "sample-job-agent"},
        "github": {
            "repository": "example/job-agent",
            "branch": "main",
            "workflow": "run.yml",
            "dispatch_token_env": "TEST_GITHUB_DISPATCH_TOKEN",
        },
        "gmail": {
            "account": "career.example@gmail.com",
            "credentials_file": str(credentials),
            "token_file": str(token),
        },
        "telegram": {
            "bot_token_env": "TEST_TELEGRAM_TOKEN",
            "chat_id": "12345",
            "actor_id": "12345",
        },
        "providers": {
            "openai": {"api_key_env": "TEST_OPENAI_KEY"},
            "anthropic": {
                "enabled": True,
                "api_key_env": "TEST_ANTHROPIC_KEY",
            },
        },
        "profile": {
            "candidate_name": "Example Candidate",
            "cv_file": str(cv),
            "canonical_cv_url": "https://example.test/curriculum-vitae.pdf",
            "grading_profile_file": str(profile),
            "evidence_file": str(evidence),
            "preferences_file": str(preferences),
            "artifact_handoff_key_env": "TEST_ARTIFACT_HANDOFF_KEY",
        },
        "cloudflare": {
            "worker_name": "sample-job-agent-gateway",
            "d1_database_name": "sample-job-agent",
            "worker_directory": "cloudflare/telegram-gateway",
            "api_token_env": "TEST_CLOUDFLARE_TOKEN",
            "callback_token_env": "TEST_CALLBACK_TOKEN",
            "webhook_secret_env": "TEST_WEBHOOK_SECRET",
            "worker_url": "https://sample-job-agent.example.workers.dev",
        },
    }
    path = tmp_path / "hosted.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False) + secret_marker, encoding="utf-8")
    return path


def run_cli(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_hosted_init_dry_run_emits_deterministic_redacted_plan(tmp_path: Path):
    config_path = write_config(tmp_path)
    secret_values = {
        "TEST_TELEGRAM_TOKEN": "telegram-secret-value",
        "TEST_OPENAI_KEY": "openai-secret-value",
        "TEST_ANTHROPIC_KEY": "anthropic-secret-value",
        "TEST_CLOUDFLARE_TOKEN": "cloudflare-secret-value",
        "TEST_CALLBACK_TOKEN": "callback-secret-value",
        "TEST_WEBHOOK_SECRET": "webhook-secret-value",
        "TEST_GITHUB_DISPATCH_TOKEN": "github-dispatch-secret-value",
        "TEST_ARTIFACT_HANDOFF_KEY": "artifact-handoff-secret-value",
    }
    env = {**os.environ, **secret_values}

    first = run_cli("hosted", "init", "--config", str(config_path), "--dry-run", env=env)
    second = run_cli("hosted", "init", "--config", str(config_path), "--dry-run", env=env)

    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert "Hosted provisioning plan: sample-job-agent" in first.stdout
    assert "GitHub authentication and repository access" in first.stdout
    assert "gh api repos/example/job-agent/branches/main" in first.stdout
    assert "gh workflow view run.yml --repo example/job-agent --ref main" in first.stdout
    assert first.stdout.index("Cloudflare authentication") < first.stdout.index(
        "GitHub Actions variables"
    )
    assert "GitHub Actions variables" in first.stdout
    assert "Gmail OAuth credentials" in first.stdout
    assert "Telegram bot and owner scope" in first.stdout
    assert "OpenAI API access" in first.stdout
    assert "Anthropic API access" in first.stdout
    assert "Cloudflare D1 database" in first.stdout
    assert "Cloudflare Worker migrations, secrets, and deployment" in first.stdout
    assert "Telegram webhook" in first.stdout
    assert "Hosted smoke tests" in first.stdout
    assert "gh workflow run run.yml --repo example/job-agent --ref main" in first.stdout
    assert not (tmp_path / ".job-agent" / "hosted-state.json").exists()
    combined = first.stdout + first.stderr
    for secret in secret_values.values():
        assert secret not in combined


def test_non_dry_run_uses_injected_runner_and_resumes_from_non_secret_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = write_config(tmp_path)
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "telegram-secret-value")
    monkeypatch.setenv("TEST_OPENAI_KEY", "openai-secret-value")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "anthropic-secret-value")
    monkeypatch.setenv("TEST_CLOUDFLARE_TOKEN", "cloudflare-secret-value")
    monkeypatch.setenv("TEST_CALLBACK_TOKEN", "callback-secret-value")
    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "webhook-secret-value")
    monkeypatch.setenv("TEST_GITHUB_DISPATCH_TOKEN", "github-dispatch-secret-value")
    monkeypatch.setenv("TEST_ARTIFACT_HANDOFF_KEY", "artifact-handoff-secret-value")

    sys.path.insert(0, str(ROOT / "src"))
    from hosted_installer import CommandResult, HostedInstaller, load_hosted_config

    class RecordingRunner:
        def __init__(self) -> None:
            self.calls = []

        def run(self, command):
            self.calls.append(command)
            if command.argv[2:5] == ("d1", "list", "--json"):
                return CommandResult(
                    returncode=0,
                    stdout=(
                        '[{"name":"sample-job-agent",'
                        '"uuid":"00000000-0000-4000-8000-000000000001"}]'
                    ),
                )
            return CommandResult(returncode=0)

    state_path = tmp_path / "state.json"
    first_runner = RecordingRunner()
    installer = HostedInstaller(
        load_hosted_config(config_path),
        state_path=state_path,
        runner=first_runner,
    )
    first_result = installer.install()

    assert first_result.completed
    assert first_runner.calls
    state_text = state_path.read_text(encoding="utf-8")
    state = json.loads(state_text)
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert state["schema_version"] == 1
    assert state["deployment"] == "sample-job-agent"
    assert all(step["status"] == "completed" for step in state["steps"].values())
    generated_wrangler = (
        config_path.parent / ".job-agent" / "wrangler.generated.jsonc"
    )
    generated = json.loads(generated_wrangler.read_text(encoding="utf-8"))
    assert (
        generated["d1_databases"][0]["database_id"]
        == "00000000-0000-4000-8000-000000000001"
    )
    assert generated["vars"]["GITHUB_REPOSITORY"] == "example/job-agent"
    for secret in (
        "telegram-secret-value",
        "openai-secret-value",
        "anthropic-secret-value",
        "cloudflare-secret-value",
        "callback-secret-value",
        "webhook-secret-value",
        "github-dispatch-secret-value",
        "artifact-handoff-secret-value",
    ):
        assert secret not in state_text

    second_runner = RecordingRunner()
    resumed = HostedInstaller(
        load_hosted_config(config_path),
        state_path=state_path,
        runner=second_runner,
    ).install()

    assert resumed.completed
    assert second_runner.calls == []


def test_uncertain_command_outcome_is_durable_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = write_config(tmp_path)
    for name in (
        "TEST_TELEGRAM_TOKEN",
        "TEST_OPENAI_KEY",
        "TEST_ANTHROPIC_KEY",
        "TEST_CLOUDFLARE_TOKEN",
        "TEST_CALLBACK_TOKEN",
        "TEST_WEBHOOK_SECRET",
        "TEST_GITHUB_DISPATCH_TOKEN",
        "TEST_ARTIFACT_HANDOFF_KEY",
    ):
        monkeypatch.setenv(name, f"{name.lower()}-secret")

    sys.path.insert(0, str(ROOT / "src"))
    from hosted_installer import (
        HostedInstaller,
        UncertainProvisioningError,
        load_hosted_config,
    )

    class UncertainRunner:
        def __init__(self) -> None:
            self.call_count = 0

        def run(self, command):
            self.call_count += 1
            raise UncertainProvisioningError("outcome uncertain")

    state_path = tmp_path / "state.json"
    runner = UncertainRunner()
    installer = HostedInstaller(
        load_hosted_config(config_path),
        state_path=state_path,
        runner=runner,
    )

    with pytest.raises(UncertainProvisioningError):
        installer.install()
    with pytest.raises(UncertainProvisioningError, match="prior outcome is uncertain"):
        installer.install()

    assert runner.call_count == 1
    state_text = state_path.read_text(encoding="utf-8")
    assert '"status": "uncertain"' in state_text
    assert "-secret" not in state_text


def test_install_does_not_bind_an_unrelated_existing_d1_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = write_config(tmp_path)
    for name in (
        "TEST_TELEGRAM_TOKEN",
        "TEST_OPENAI_KEY",
        "TEST_ANTHROPIC_KEY",
        "TEST_CLOUDFLARE_TOKEN",
        "TEST_CALLBACK_TOKEN",
        "TEST_WEBHOOK_SECRET",
        "TEST_GITHUB_DISPATCH_TOKEN",
        "TEST_ARTIFACT_HANDOFF_KEY",
    ):
        monkeypatch.setenv(name, f"{name.lower()}-secret")

    sys.path.insert(0, str(ROOT / "src"))
    from hosted_installer import CommandResult, HostedInstaller, load_hosted_config

    target_id = "22222222-2222-4222-8222-222222222222"

    class D1Runner:
        def __init__(self) -> None:
            self.calls = []

        def run(self, command):
            self.calls.append(command.argv)
            if command.argv[2:5] == ("d1", "list", "--json"):
                return CommandResult(
                    0,
                    '[{"name":"someone-elses-db",'
                    '"uuid":"11111111-1111-4111-8111-111111111111"}]',
                )
            if command.argv[2:5] == ("d1", "create", "sample-job-agent"):
                return CommandResult(
                    0,
                    '{"name":"sample-job-agent","database_id":'
                    f'"{target_id}"}}',
                )
            return CommandResult(0)

    runner = D1Runner()
    HostedInstaller(
        load_hosted_config(config_path),
        state_path=tmp_path / "state.json",
        runner=runner,
    ).install()

    assert any(call[2:5] == ("d1", "create", "sample-job-agent") for call in runner.calls)
    generated = json.loads(
        (config_path.parent / ".job-agent" / "wrangler.generated.jsonc").read_text()
    )
    assert generated["d1_databases"][0]["database_id"] == target_id


def test_hosted_doctor_reports_actionable_missing_prerequisites_and_config(
    tmp_path: Path,
):
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["gmail"]["credentials_file"] = str(tmp_path / "missing-credentials.json")
    config["profile"]["cv_file"] = str(tmp_path / "missing-cv.pdf")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    env = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path),
    }

    result = run_cli("hosted", "doctor", "--config", str(config_path), env=env)

    assert result.returncode == 1
    assert "[FAIL] executable gh: not found on PATH" in result.stdout
    assert "[FAIL] executable npx: not found on PATH" in result.stdout
    assert "[FAIL] Gmail OAuth credentials:" in result.stdout
    assert "missing-credentials.json does not exist" in result.stdout
    assert "[FAIL] CV source:" in result.stdout
    assert "missing-cv.pdf does not exist" in result.stdout
    assert "[FAIL] environment TEST_OPENAI_KEY: not set" in result.stdout
    assert "[FAIL] environment TEST_TELEGRAM_TOKEN: not set" in result.stdout
    assert "Run `job-agent hosted doctor --config" in result.stdout


def test_non_dry_init_runs_preflight_before_any_provisioning(tmp_path: Path):
    config_path = write_config(tmp_path)
    env = {
        "PATH": str(tmp_path / "empty-path"),
        "HOME": str(tmp_path),
        "TEST_TELEGRAM_TOKEN": "telegram-secret-value",
        "TEST_OPENAI_KEY": "openai-secret-value",
        "TEST_ANTHROPIC_KEY": "anthropic-secret-value",
        "TEST_CLOUDFLARE_TOKEN": "cloudflare-secret-value",
        "TEST_CALLBACK_TOKEN": "callback-secret-value",
        "TEST_WEBHOOK_SECRET": "webhook-secret-value",
        "TEST_GITHUB_DISPATCH_TOKEN": "github-dispatch-secret-value",
        "TEST_ARTIFACT_HANDOFF_KEY": "artifact-handoff-secret-value",
    }

    result = run_cli("hosted", "init", "--config", str(config_path), env=env)

    assert result.returncode == 1
    assert "Hosted provisioning did not start because preflight failed" in result.stdout
    assert not (tmp_path / ".job-agent" / "hosted-state.json").exists()


def test_config_rejects_inline_secrets_without_echoing_them(tmp_path: Path):
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    inline_secret = "do-not-print-this-secret"
    config["providers"]["openai"]["api_key"] = inline_secret
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = run_cli("hosted", "init", "--config", str(config_path), "--dry-run")

    assert result.returncode == 2
    assert "inline secret values are forbidden" in result.stderr
    assert inline_secret not in result.stdout + result.stderr


def test_hosted_v1_rejects_disabling_required_artifact_provider(tmp_path: Path):
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["providers"]["anthropic"]["enabled"] = False
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = run_cli("hosted", "init", "--config", str(config_path), "--dry-run")

    assert result.returncode == 2
    assert "providers.anthropic.enabled must be true" in result.stderr


def test_dry_run_does_not_require_or_read_secret_inputs(tmp_path: Path):
    config_path = write_config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["gmail"]["credentials_file"] = str(tmp_path / "not-created.json")
    config["profile"]["evidence_file"] = str(tmp_path / "not-created.yaml")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}

    result = run_cli("hosted", "init", "--config", str(config_path), "--dry-run", env=env)

    assert result.returncode == 0, result.stderr
    assert "[REDACTED]" in result.stdout
    assert "not set" not in result.stdout
