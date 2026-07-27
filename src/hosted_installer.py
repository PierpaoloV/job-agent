"""Fail-closed, resumable provisioning for a single-user hosted deployment.

The module deliberately keeps secrets out of configuration and persisted state.
Secret material is resolved from environment variables only when a command is
about to cross an external boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Protocol, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import yaml


STATE_SCHEMA_VERSION = 1
INLINE_SECRET_KEYS = {
    "api_key",
    "bot_token",
    "callback_token",
    "client_secret",
    "password",
    "secret",
    "token",
    "webhook_secret",
}


class HostedInstallerError(Exception):
    """Safe-to-display installer error."""


class ConfigError(HostedInstallerError):
    """The hosted configuration is invalid."""


class ProvisioningError(HostedInstallerError):
    """An external command failed definitively."""


class UncertainProvisioningError(HostedInstallerError):
    """An external operation may have happened and must not be retried."""


@dataclass(frozen=True)
class Command:
    """One shell-free process invocation.

    ``display`` is always safe for logs. ``stdin`` and ``environment`` may hold
    secret material and must never be included in exceptions or persisted.
    """

    argv: tuple[str, ...]
    display: str
    stdin: str | None = field(default=None, repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: int = 120
    uncertain_on_failure: bool = False


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""


class CommandRunner(Protocol):
    def run(self, command: Command) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run commands without a shell and without exposing subprocess output."""

    def run(self, command: Command) -> CommandResult:
        environment = os.environ.copy()
        environment.update(command.environment)
        try:
            completed = subprocess.run(
                list(command.argv),
                input=command.stdin,
                text=True,
                capture_output=True,
                shell=False,
                timeout=command.timeout_seconds,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UncertainProvisioningError(
                f"{command.display}: outcome uncertain; inspect the provider "
                "before retrying"
            ) from exc
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
        )


@dataclass(frozen=True)
class HostedConfig:
    source: Path
    data: Mapping[str, Any]
    fingerprint: str

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.data[name]
        assert isinstance(value, Mapping)
        return value

    def path(self, section: str, key: str) -> Path:
        raw = Path(str(self.section(section)[key])).expanduser()
        return raw if raw.is_absolute() else (self.source.parent / raw).resolve()

    def secret_environment_names(self) -> tuple[str, ...]:
        names = [
            str(self.section("telegram")["bot_token_env"]),
            str(self.section("github")["dispatch_token_env"]),
            str(self.section("providers")["openai"]["api_key_env"]),
            str(self.section("cloudflare")["api_token_env"]),
            str(self.section("cloudflare")["callback_token_env"]),
            str(self.section("cloudflare")["webhook_secret_env"]),
            str(self.section("profile")["artifact_handoff_key_env"]),
        ]
        anthropic = self.section("providers")["anthropic"]
        if anthropic["enabled"]:
            names.append(str(anthropic["api_key_env"]))
        return tuple(names)


@dataclass(frozen=True)
class ProvisioningStep:
    identifier: str
    title: str
    commands: tuple[Command, ...]
    optional: bool = False


@dataclass(frozen=True)
class InstallResult:
    completed: bool
    completed_steps: tuple[str, ...]
    skipped_steps: tuple[str, ...]


@dataclass(frozen=True)
class DoctorCheck:
    label: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.ok for check in self.checks)

    def render(self) -> str:
        lines = [
            f"[{'OK' if check.ok else 'FAIL'}] {check.label}: {check.detail}"
            for check in self.checks
        ]
        return "\n".join(lines)


def _reject_inline_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in INLINE_SECRET_KEYS:
                dotted = ".".join((*path, str(key)))
                raise ConfigError(
                    f"{dotted}: inline secret values are forbidden; use an "
                    "environment-variable name ending in `_env`"
                )
            _reject_inline_secrets(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, (*path, str(index)))


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key}: required mapping is missing")
    return value


def _require_text(data: Mapping[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ConfigError(f"{path}.{key}: required value is missing")
    return str(value).strip()


def _validate_config(data: Mapping[str, Any]) -> None:
    if data.get("version") != 1:
        raise ConfigError("version: expected 1")
    _reject_inline_secrets(data)

    deployment = _require_mapping(data, "deployment")
    github = _require_mapping(data, "github")
    gmail = _require_mapping(data, "gmail")
    telegram = _require_mapping(data, "telegram")
    providers = _require_mapping(data, "providers")
    profile = _require_mapping(data, "profile")
    cloudflare = _require_mapping(data, "cloudflare")
    openai = _require_mapping(providers, "openai")
    anthropic = _require_mapping(providers, "anthropic")

    required = (
        (deployment, "name", "deployment"),
        (github, "repository", "github"),
        (github, "branch", "github"),
        (github, "workflow", "github"),
        (github, "dispatch_token_env", "github"),
        (gmail, "account", "gmail"),
        (gmail, "credentials_file", "gmail"),
        (gmail, "token_file", "gmail"),
        (telegram, "bot_token_env", "telegram"),
        (telegram, "chat_id", "telegram"),
        (telegram, "actor_id", "telegram"),
        (openai, "api_key_env", "providers.openai"),
        (profile, "candidate_name", "profile"),
        (profile, "cv_file", "profile"),
        (profile, "canonical_cv_url", "profile"),
        (profile, "grading_profile_file", "profile"),
        (profile, "evidence_file", "profile"),
        (profile, "preferences_file", "profile"),
        (profile, "artifact_handoff_key_env", "profile"),
        (cloudflare, "worker_name", "cloudflare"),
        (cloudflare, "d1_database_name", "cloudflare"),
        (cloudflare, "worker_directory", "cloudflare"),
        (cloudflare, "api_token_env", "cloudflare"),
        (cloudflare, "callback_token_env", "cloudflare"),
        (cloudflare, "webhook_secret_env", "cloudflare"),
        (cloudflare, "worker_url", "cloudflare"),
    )
    for mapping, key, path in required:
        _require_text(mapping, key, path)

    if not isinstance(anthropic.get("enabled"), bool):
        raise ConfigError("providers.anthropic.enabled: expected true or false")
    if anthropic["enabled"] is not True:
        raise ConfigError(
            "providers.anthropic.enabled must be true in hosted v1 because "
            "Telegram approval prepares CV and cover-letter artifacts"
        )
    _require_text(anthropic, "api_key_env", "providers.anthropic")

    repository = str(github["repository"])
    if repository.count("/") != 1 or any(not part for part in repository.split("/")):
        raise ConfigError("github.repository: expected OWNER/REPOSITORY")
    gmail_account = str(gmail["account"]).casefold()
    if (
        gmail_account.count("@") != 1
        or not gmail_account.endswith("@gmail.com")
        or any(character.isspace() for character in gmail_account)
    ):
        raise ConfigError("gmail.account: expected the dedicated Gmail address")
    for key in ("chat_id", "actor_id"):
        value = str(telegram[key])
        if re.fullmatch(r"-?[0-9]+", value) is None:
            raise ConfigError(f"telegram.{key}: expected a numeric Telegram ID")
    for section, key in (
        (profile, "canonical_cv_url"),
        (cloudflare, "worker_url"),
    ):
        parsed = urllib_parse.urlparse(str(section[key]))
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError(f"{key}: expected an absolute HTTPS URL")
    for section, key in (
        (telegram, "bot_token_env"),
        (openai, "api_key_env"),
        (github, "dispatch_token_env"),
        (cloudflare, "api_token_env"),
        (cloudflare, "callback_token_env"),
        (cloudflare, "webhook_secret_env"),
        (profile, "artifact_handoff_key_env"),
    ):
        name = str(section[key])
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) is None:
            raise ConfigError(f"{key}: expected an environment-variable name")


def load_hosted_config(path: Path | str) -> HostedConfig:
    source = Path(path).expanduser().resolve()
    try:
        parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"config file is not readable: {source}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError("config file is not valid YAML") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError("config root must be a mapping")
    _validate_config(parsed)
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return HostedConfig(
        source=source,
        data=parsed,
        fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _secret_command(
    *,
    argv: Sequence[str],
    display: str,
    value: str,
    environment: Mapping[str, str] | None = None,
) -> Command:
    return Command(
        argv=tuple(argv),
        display=display,
        stdin=value,
        environment=environment or {},
    )


def _env_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment {name}: required secret is not set")
    return value


def build_provisioning_plan(
    config: HostedConfig,
    *,
    resolve_secret_inputs: bool = True,
) -> tuple[ProvisioningStep, ...]:
    github = config.section("github")
    gmail = config.section("gmail")
    telegram = config.section("telegram")
    providers = config.section("providers")
    profile = config.section("profile")
    cloudflare = config.section("cloudflare")
    repository = str(github["repository"])
    branch = str(github["branch"])
    workflow = str(github["workflow"])
    worker_directory = config.path("cloudflare", "worker_directory")
    wrangler_config = config.source.parent / ".job-agent" / "wrangler.generated.jsonc"
    cli_script = Path(__file__).resolve().parents[1] / "scripts" / "job_agent.py"
    def secret(name: str) -> str:
        return _env_secret(name) if resolve_secret_inputs else "[REDACTED]"

    def private_file(section: str, key: str) -> str:
        if not resolve_secret_inputs:
            return "[REDACTED]"
        try:
            return config.path(section, key).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{section}.{key}: configured file is not readable") from exc

    cf_environment = (
        {
            "CLOUDFLARE_API_TOKEN": secret(
                str(cloudflare["api_token_env"])
            )
        }
        if resolve_secret_inputs
        else {}
    )

    def gh_secret(name: str, value: str) -> Command:
        return _secret_command(
            argv=("gh", "secret", "set", name, "--repo", repository),
            display=f"gh secret set {name} --repo {repository} < [REDACTED]",
            value=value,
        )

    def gh_variable(name: str, value: str) -> Command:
        return Command(
            argv=(
                "gh",
                "variable",
                "set",
                name,
                "--repo",
                repository,
                "--body",
                value,
            ),
            display=f"gh variable set {name} --repo {repository}",
        )

    steps = [
        ProvisioningStep(
            "github-access",
            "GitHub authentication and repository access",
            (
                Command(("gh", "auth", "status"), "gh auth status"),
                Command(
                    ("gh", "repo", "view", repository),
                    f"gh repo view {repository}",
                ),
                Command(
                    ("gh", "api", f"repos/{repository}/branches/{branch}"),
                    f"gh api repos/{repository}/branches/{branch}",
                ),
                Command(
                    (
                        "gh",
                        "workflow",
                        "view",
                        workflow,
                        "--repo",
                        repository,
                        "--ref",
                        branch,
                    ),
                    (
                        f"gh workflow view {workflow} --repo {repository} "
                        f"--ref {branch}"
                    ),
                ),
            ),
        ),
        ProvisioningStep(
            "cloudflare-access",
            "Cloudflare authentication",
            (
                Command(
                    ("npx", "wrangler", "whoami"),
                    "npx wrangler whoami",
                    environment=cf_environment,
                ),
            ),
        ),
        ProvisioningStep(
            "github-actions-variables",
            "GitHub Actions variables",
            (
                gh_variable("JOB_AGENT_CANDIDATE_NAME", str(profile["candidate_name"])),
                gh_variable("JOB_AGENT_CAREER_GMAIL", str(gmail["account"])),
                gh_variable(
                    "JOB_AGENT_CANONICAL_CV_URL",
                    str(profile["canonical_cv_url"]),
                ),
                gh_variable(
                    "JOB_AGENT_CALLBACK_GATEWAY_URL",
                    str(cloudflare["worker_url"]).rstrip("/"),
                ),
            ),
        ),
        ProvisioningStep(
            "gmail-oauth",
            "Gmail OAuth credentials",
            (
                gh_secret(
                    "GMAIL_CREDENTIALS_JSON",
                    private_file("gmail", "credentials_file"),
                ),
                gh_secret(
                    "GMAIL_TOKEN_JSON",
                    private_file("gmail", "token_file"),
                ),
            ),
        ),
        ProvisioningStep(
            "telegram-scope",
            "Telegram bot and owner scope",
            (
                gh_secret(
                    "TELEGRAM_BOT_TOKEN",
                    secret(str(telegram["bot_token_env"])),
                ),
                gh_secret("TELEGRAM_CHAT_ID", str(telegram["chat_id"])),
                gh_secret("TELEGRAM_ACTOR_ID", str(telegram["actor_id"])),
                gh_secret(
                    "JOB_AGENT_CALLBACK_GATEWAY_TOKEN",
                    secret(str(cloudflare["callback_token_env"])),
                ),
            ),
        ),
        ProvisioningStep(
            "openai",
            "OpenAI API access",
            (
                gh_secret(
                    "OPENAI_API_KEY",
                    secret(str(providers["openai"]["api_key_env"])),
                ),
            ),
        ),
        ProvisioningStep(
            "anthropic",
            "Anthropic API access",
            (
                gh_secret(
                    "ANTHROPIC_API_KEY",
                    secret(str(providers["anthropic"]["api_key_env"])),
                ),
            ),
        ),
        ProvisioningStep(
            "private-profile-inputs",
            "Private grading, evidence, and preference inputs",
            (
                gh_secret(
                    "JOB_AGENT_GRADING_PROFILE_JSON",
                    private_file("profile", "grading_profile_file"),
                ),
                gh_secret(
                    "JOB_AGENT_EVIDENCE_YAML",
                    private_file("profile", "evidence_file"),
                ),
                gh_secret(
                    "JOB_AGENT_PREFERENCES_YAML",
                    private_file("profile", "preferences_file"),
                ),
                gh_secret(
                    "JOB_AGENT_ARTIFACT_HANDOFF_KEY",
                    secret(str(profile["artifact_handoff_key_env"])),
                ),
            ),
        ),
        ProvisioningStep(
            "cloudflare-d1",
            "Cloudflare D1 database",
            (
                Command(
                    (
                        "npx",
                        "wrangler",
                        "d1",
                        "list",
                        "--json",
                    ),
                    "ensure D1 database [configured-name] exists and capture its ID",
                    environment=cf_environment,
                ),
            ),
        ),
        ProvisioningStep(
            "cloudflare-worker",
            "Cloudflare Worker migrations, secrets, and deployment",
            (
                Command(
                    (
                        "npx",
                        "wrangler",
                        "d1",
                        "migrations",
                        "apply",
                        str(cloudflare["d1_database_name"]),
                        "--remote",
                        "--config",
                        str(wrangler_config),
                    ),
                    "npx wrangler d1 migrations apply [configured-name] --remote",
                    environment=cf_environment,
                ),
                _secret_command(
                    argv=(
                        "npx",
                        "wrangler",
                        "secret",
                        "put",
                        "INTERNAL_API_TOKEN",
                        "--config",
                        str(wrangler_config),
                    ),
                    display="npx wrangler secret put INTERNAL_API_TOKEN < [REDACTED]",
                    value=secret(str(cloudflare["callback_token_env"])),
                    environment=cf_environment,
                ),
                _secret_command(
                    argv=(
                        "npx",
                        "wrangler",
                        "secret",
                        "put",
                        "TELEGRAM_WEBHOOK_SECRET",
                        "--config",
                        str(wrangler_config),
                    ),
                    display="npx wrangler secret put TELEGRAM_WEBHOOK_SECRET < [REDACTED]",
                    value=secret(str(cloudflare["webhook_secret_env"])),
                    environment=cf_environment,
                ),
                _secret_command(
                    argv=(
                        "npx",
                        "wrangler",
                        "secret",
                        "put",
                        "TELEGRAM_BOT_TOKEN",
                        "--config",
                        str(wrangler_config),
                    ),
                    display="npx wrangler secret put TELEGRAM_BOT_TOKEN < [REDACTED]",
                    value=secret(str(telegram["bot_token_env"])),
                    environment=cf_environment,
                ),
                _secret_command(
                    argv=(
                        "npx",
                        "wrangler",
                        "secret",
                        "put",
                        "GITHUB_DISPATCH_TOKEN",
                        "--config",
                        str(wrangler_config),
                    ),
                    display="npx wrangler secret put GITHUB_DISPATCH_TOKEN < [REDACTED]",
                    value=secret(str(github["dispatch_token_env"])),
                    environment=cf_environment,
                ),
                Command(
                    (
                        "npx",
                        "wrangler",
                        "deploy",
                        "--config",
                        str(wrangler_config),
                    ),
                    "npx wrangler deploy --config [generated-config]",
                    environment=cf_environment,
                ),
            ),
        ),
        ProvisioningStep(
            "telegram-webhook",
            "Telegram webhook",
            (
                Command(
                    (
                        sys.executable,
                        str(cli_script),
                        "hosted",
                        "_set-webhook",
                        "--config",
                        str(config.source),
                    ),
                    "call Telegram setWebhook with [REDACTED] credentials",
                    uncertain_on_failure=True,
                ),
            ),
        ),
        ProvisioningStep(
            "smoke-tests",
            "Hosted smoke tests",
            (
                Command(
                    (
                        sys.executable,
                        str(cli_script),
                        "hosted",
                        "_smoke",
                        "--config",
                        str(config.source),
                    ),
                    "verify Worker health and Telegram webhook",
                ),
                Command(
                    (
                        "gh",
                        "workflow",
                        "run",
                        str(github["workflow"]),
                        "--repo",
                        repository,
                        "--ref",
                        branch,
                    ),
                    (
                        f"gh workflow run {workflow} --repo {repository} "
                        f"--ref {branch}"
                    ),
                ),
            ),
        ),
    ]
    return tuple(steps)


def render_plan(config: HostedConfig, steps: Sequence[ProvisioningStep]) -> str:
    lines = [f"Hosted provisioning plan: {config.section('deployment')['name']}"]
    for index, step in enumerate(steps, start=1):
        suffix = " [optional]" if step.optional else ""
        lines.append(f"{index:02d}. {step.title}{suffix}")
        if not step.commands:
            lines.append("    skipped by configuration")
        for command in step.commands:
            lines.append(f"    {command.display}")
    lines.append("Dry run only: no files or external services were changed.")
    return "\n".join(lines)


def _initial_state(config: HostedConfig) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "deployment": str(config.section("deployment")["name"]),
        "config_fingerprint": config.fingerprint,
        "steps": {},
    }


def _read_state(path: Path, config: HostedConfig) -> dict[str, Any]:
    if not path.exists():
        return _initial_state(config)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"state file is unreadable: {path}") from exc
    if (
        state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("deployment") != config.section("deployment")["name"]
        or state.get("config_fingerprint") != config.fingerprint
    ):
        raise ConfigError(
            "state does not match this deployment/config; choose another "
            "--state path or reconcile it explicitly"
        )
    return state


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _database_id_from_payload(
    payload: Any,
    database_name: str,
    *,
    allow_unnamed: bool,
) -> str | None:
    candidates: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            candidates.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    for candidate in candidates:
        name = candidate.get("name") or candidate.get("database_name")
        identifier = candidate.get("uuid") or candidate.get("database_id")
        if name == database_name and isinstance(identifier, str):
            return identifier
    if allow_unnamed:
        for candidate in candidates:
            identifier = candidate.get("uuid") or candidate.get("database_id")
            if isinstance(identifier, str):
                return identifier
    return None


def _database_id_from_output(
    output: str,
    database_name: str,
    *,
    allow_unnamed: bool = False,
) -> str | None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = None
    identifier = _database_id_from_payload(
        payload,
        database_name,
        allow_unnamed=allow_unnamed,
    )
    if identifier:
        return identifier
    if not allow_unnamed:
        return None
    match = re.search(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
        output,
    )
    return match.group(0) if match else None


def _generated_wrangler_path(config: HostedConfig) -> Path:
    return config.source.parent / ".job-agent" / "wrangler.generated.jsonc"


def _write_generated_wrangler(config: HostedConfig, database_id: str) -> Path:
    github = config.section("github")
    telegram = config.section("telegram")
    cloudflare = config.section("cloudflare")
    worker_directory = config.path("cloudflare", "worker_directory")
    payload = {
        "name": str(cloudflare["worker_name"]),
        "main": str((worker_directory / "src" / "worker.mjs").resolve()),
        "compatibility_date": "2026-07-26",
        "workers_dev": True,
        "observability": {"enabled": True},
        "d1_databases": [
            {
                "binding": "DB",
                "database_name": str(cloudflare["d1_database_name"]),
                "database_id": database_id,
                "migrations_dir": str((worker_directory / "migrations").resolve()),
            }
        ],
        "vars": {
            "GITHUB_REPOSITORY": str(github["repository"]),
            "TELEGRAM_ACTOR_ID": str(telegram["actor_id"]),
            "TELEGRAM_CHAT_ID": str(telegram["chat_id"]),
        },
    }
    path = _generated_wrangler_path(config)
    _write_state(path, payload)
    return path


class HostedInstaller:
    """Execute a hosted plan once, retaining only non-secret progress."""

    def __init__(
        self,
        config: HostedConfig,
        *,
        state_path: Path,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.runner = runner or SubprocessCommandRunner()

    def _ensure_cloudflare_d1(self, step: ProvisioningStep) -> str:
        cloudflare = self.config.section("cloudflare")
        database_name = str(cloudflare["d1_database_name"])
        listed = self.runner.run(step.commands[0])
        if listed.returncode != 0:
            raise ProvisioningError(
                "Cloudflare D1 database: could not inspect existing databases; "
                "run hosted doctor and retry"
            )
        identifier = _database_id_from_output(listed.stdout, database_name)
        if identifier is None:
            environment = step.commands[0].environment
            create = Command(
                (
                    "npx",
                    "wrangler",
                    "d1",
                    "create",
                    database_name,
                    "--json",
                ),
                "npx wrangler d1 create [configured-name] --json",
                environment=environment,
                uncertain_on_failure=True,
            )
            created = self.runner.run(create)
            if created.returncode != 0:
                raise UncertainProvisioningError(
                    "Cloudflare D1 database: create outcome is uncertain; "
                    "inspect Cloudflare before retrying"
                )
            identifier = _database_id_from_output(
                created.stdout,
                database_name,
                allow_unnamed=True,
            )
            if identifier is None:
                raise UncertainProvisioningError(
                    "Cloudflare D1 database: creation returned no usable "
                    "database ID; inspect Cloudflare before retrying"
                )
        _write_generated_wrangler(self.config, identifier)
        return identifier

    def install(self) -> InstallResult:
        steps = build_provisioning_plan(self.config)
        state = _read_state(self.state_path, self.config)
        completed: list[str] = []
        skipped: list[str] = []
        for step in steps:
            prior = state["steps"].get(step.identifier, {})
            if prior.get("status") == "completed":
                if step.identifier == "cloudflare-d1":
                    identifier = prior.get("database_id")
                    if not isinstance(identifier, str) or not identifier:
                        raise ConfigError(
                            "Cloudflare D1 state has no database ID; reconcile "
                            "the state file before continuing"
                        )
                    if not _generated_wrangler_path(self.config).exists():
                        _write_generated_wrangler(self.config, identifier)
                skipped.append(step.identifier)
                continue
            if prior.get("status") == "uncertain":
                raise UncertainProvisioningError(
                    f"{step.title}: prior outcome is uncertain; inspect the "
                    "provider and reconcile state before retrying"
                )
            if not step.commands:
                state["steps"][step.identifier] = {"status": "completed"}
                _write_state(self.state_path, state)
                completed.append(step.identifier)
                continue
            if step.identifier == "cloudflare-d1":
                try:
                    identifier = self._ensure_cloudflare_d1(step)
                except UncertainProvisioningError:
                    state["steps"][step.identifier] = {"status": "uncertain"}
                    _write_state(self.state_path, state)
                    raise
                except ProvisioningError:
                    state["steps"][step.identifier] = {"status": "failed"}
                    _write_state(self.state_path, state)
                    raise
                state["steps"][step.identifier] = {
                    "status": "completed",
                    "database_id": identifier,
                }
                _write_state(self.state_path, state)
                completed.append(step.identifier)
                continue
            commands_succeeded = 0
            try:
                for command in step.commands:
                    result = self.runner.run(command)
                    if result.returncode != 0:
                        if command.uncertain_on_failure:
                            raise UncertainProvisioningError(
                                f"{step.title}: outcome uncertain; inspect the "
                                "provider before retrying"
                            )
                        if commands_succeeded:
                            raise UncertainProvisioningError(
                                f"{step.title}: partially completed; inspect "
                                "the provider before retrying"
                            )
                        raise ProvisioningError(
                            f"{step.title}: command failed; run hosted doctor "
                            "and retry after correcting the reported problem"
                        )
                    commands_succeeded += 1
            except UncertainProvisioningError:
                state["steps"][step.identifier] = {"status": "uncertain"}
                _write_state(self.state_path, state)
                raise
            except ProvisioningError:
                state["steps"][step.identifier] = {"status": "failed"}
                _write_state(self.state_path, state)
                raise
            state["steps"][step.identifier] = {"status": "completed"}
            _write_state(self.state_path, state)
            completed.append(step.identifier)
        return InstallResult(
            completed=True,
            completed_steps=tuple(completed),
            skipped_steps=tuple(skipped),
        )


def run_doctor(
    config: HostedConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> DoctorReport:
    environment = environment if environment is not None else os.environ
    checks: list[DoctorCheck] = []
    for executable in ("gh", "npx"):
        found = shutil.which(executable, path=environment.get("PATH"))
        checks.append(
            DoctorCheck(
                f"executable {executable}",
                bool(found),
                str(found) if found else "not found on PATH",
            )
        )
    files = (
        ("Gmail OAuth credentials", config.path("gmail", "credentials_file")),
        ("Gmail OAuth token", config.path("gmail", "token_file")),
        ("CV source", config.path("profile", "cv_file")),
        (
            "grading profile",
            config.path("profile", "grading_profile_file"),
        ),
        ("professional evidence", config.path("profile", "evidence_file")),
        ("job preferences", config.path("profile", "preferences_file")),
        (
            "Cloudflare Worker directory",
            config.path("cloudflare", "worker_directory"),
        ),
    )
    for label, path in files:
        exists = path.exists()
        checks.append(
            DoctorCheck(
                label,
                exists,
                str(path) if exists else f"{path} does not exist",
            )
        )
    grading_profile = config.path("profile", "grading_profile_file")
    professional_evidence = config.path("profile", "evidence_file")
    if grading_profile.exists() and professional_evidence.exists():
        aligned, detail = validate_professional_evidence_alignment(
            grading_profile,
            professional_evidence,
        )
        checks.append(
            DoctorCheck(
                "grading/artifact evidence alignment",
                aligned,
                detail,
            )
        )
    for name in config.secret_environment_names():
        present = bool(environment.get(name))
        checks.append(
            DoctorCheck(
                f"environment {name}",
                present,
                "set (value redacted)" if present else "not set",
            )
        )
    return DoctorReport(tuple(checks))


def validate_professional_evidence_alignment(
    grading_profile_path: Path,
    evidence_path: Path,
) -> tuple[bool, str]:
    """Ensure grading and tailoring authorize the same exact claim bank."""

    try:
        profile = json.loads(Path(grading_profile_path).read_text(encoding="utf-8"))
        evidence = yaml.safe_load(Path(evidence_path).read_text(encoding="utf-8"))
        grading_claims = _grading_claims(profile)
        artifact_claims = _artifact_claims(evidence)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, yaml.YAMLError):
        return False, "professional evidence inputs are malformed"
    if grading_claims != artifact_claims:
        return (
            False,
            "grading and tailoring must use the same approved professional "
            "evidence IDs and claims",
        )
    if not grading_claims:
        return False, "professional evidence inputs must not be empty"
    return True, f"{len(grading_claims)} approved claims aligned"


def _grading_claims(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("grading profile must be an object")
    rows = value.get("professional_evidence")
    if not isinstance(rows, list):
        raise ValueError("grading profile evidence must be a list")
    return _claim_map(rows)


def _artifact_claims(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("artifact evidence must be an object")
    rows: list[Any] = []
    for section in ("highlights", "skill_evidence"):
        section_rows = value.get(section, [])
        if not isinstance(section_rows, list):
            raise ValueError("artifact evidence sections must be lists")
        rows.extend(
            row
            for row in section_rows
            if isinstance(row, Mapping) and row.get("approved", True) is True
        )
    return _claim_map(rows)


def _claim_map(rows: Sequence[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("professional evidence entries must be objects")
        identifier = str(row.get("id", "")).strip()
        claim = str(row.get("claim", "")).strip()
        if not identifier or not claim or identifier in result:
            raise ValueError("professional evidence IDs and claims must be canonical")
        result[identifier] = claim
    return result


def default_state_path(config: HostedConfig) -> Path:
    return config.source.parent / ".job-agent" / "hosted-state.json"


def _read_json_response(request: urllib_request.Request) -> Mapping[str, Any]:
    with urllib_request.urlopen(request, timeout=20) as response:
        body = response.read(256_000)
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("expected an object")
    return decoded


def set_telegram_webhook(config: HostedConfig) -> None:
    """Register the exact Worker webhook without exposing bot credentials."""

    telegram = config.section("telegram")
    cloudflare = config.section("cloudflare")
    bot_token = _env_secret(str(telegram["bot_token_env"]))
    webhook_secret = _env_secret(str(cloudflare["webhook_secret_env"]))
    worker_url = str(cloudflare["worker_url"]).rstrip("/")
    endpoint = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    body = urllib_parse.urlencode(
        {
            "url": f"{worker_url}/telegram",
            "secret_token": webhook_secret,
            "allowed_updates": json.dumps(["callback_query", "message"]),
            "drop_pending_updates": "false",
        }
    ).encode("utf-8")
    request = urllib_request.Request(endpoint, data=body, method="POST")
    try:
        response = _read_json_response(request)
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        urllib_error.URLError,
    ) as exc:
        raise UncertainProvisioningError(
            "Telegram webhook: outcome uncertain; inspect getWebhookInfo "
            "before retrying"
        ) from exc
    if response.get("ok") is not True:
        raise ProvisioningError(
            "Telegram webhook: Telegram rejected the request; credentials or "
            "scope need correction"
        )


def smoke_test_hosted(config: HostedConfig) -> None:
    """Verify the deployed health endpoint and the exact Telegram webhook."""

    telegram = config.section("telegram")
    cloudflare = config.section("cloudflare")
    bot_token = _env_secret(str(telegram["bot_token_env"]))
    worker_url = str(cloudflare["worker_url"]).rstrip("/")
    try:
        health = _read_json_response(
            urllib_request.Request(f"{worker_url}/health", method="GET")
        )
        webhook = _read_json_response(
            urllib_request.Request(
                f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",
                method="GET",
            )
        )
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        urllib_error.URLError,
    ) as exc:
        raise ProvisioningError(
            "Hosted smoke tests: health or Telegram verification failed"
        ) from exc
    webhook_result = webhook.get("result")
    registered_url = (
        webhook_result.get("url")
        if isinstance(webhook_result, Mapping)
        else None
    )
    if health.get("ok") is not True:
        raise ProvisioningError(
            "Hosted smoke tests: Worker health endpoint is not healthy"
        )
    if webhook.get("ok") is not True or registered_url != f"{worker_url}/telegram":
        raise ProvisioningError(
            "Hosted smoke tests: Telegram webhook does not match the Worker URL"
        )
