"""Shared Gmail OAuth path and secret-sync helpers."""

from __future__ import annotations

import os
import pathlib
import subprocess

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_GMAIL_SCOPE_PREFIX = "https://www.googleapis.com/auth/gmail."

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_DIR = pathlib.Path.home() / ".config" / "google"


def verify_dedicated_mailbox(
    service,
    credentials,
    *,
    expected_email: str | None = None,
) -> str:
    """Fail closed unless OAuth belongs only to the dedicated career mailbox."""
    expected = (
        expected_email
        if expected_email is not None
        else os.environ.get("JOB_AGENT_CAREER_GMAIL", "")
    )
    expected = str(expected).strip().casefold()
    if not expected:
        raise ValueError(
            "JOB_AGENT_CAREER_GMAIL is required for Gmail authorization"
        )
    granted = getattr(credentials, "granted_scopes", None)
    scopes = granted or getattr(credentials, "scopes", None) or ()
    gmail_scopes = {
        str(scope)
        for scope in scopes
        if str(scope).startswith(_GMAIL_SCOPE_PREFIX)
    }
    if gmail_scopes != set(SCOPES):
        raise ValueError(
            "Career monitoring requires only the read-only Gmail scope"
        )

    profile = service.users().getProfile(userId="me").execute()
    authenticated = str(profile.get("emailAddress", "")).strip().casefold()
    if authenticated != expected:
        raise ValueError(
            "The authenticated Gmail account is not the dedicated career mailbox"
        )
    return authenticated


def _candidate_paths(
    explicit: str | pathlib.Path | None,
    env_var: str,
    defaults: list[pathlib.Path],
) -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()

    raw_values = []
    if explicit:
        raw_values.append(explicit)
    env_value = os.environ.get(env_var)
    if env_value:
        raw_values.append(env_value)
    raw_values.extend(defaults)

    for raw in raw_values:
        path = pathlib.Path(raw).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        if path not in seen:
            seen.add(path)
            candidates.append(path)
    return candidates


def credential_candidates(explicit: str | pathlib.Path | None = None) -> list[pathlib.Path]:
    return _candidate_paths(
        explicit=explicit,
        env_var="GMAIL_CREDENTIALS_PATH",
        defaults=[
            CONFIG_DIR / "client_secret.json",
            CONFIG_DIR / "credentials.json",
            REPO_ROOT / "credentials.json",
        ],
    )


def token_candidates(explicit: str | pathlib.Path | None = None) -> list[pathlib.Path]:
    return _candidate_paths(
        explicit=explicit,
        env_var="GMAIL_TOKEN_PATH",
        defaults=[
            CONFIG_DIR / "job-agent-token.json",
            REPO_ROOT / "token.json",
            CONFIG_DIR / "token.json",
        ],
    )


def resolve_existing_path(candidates: list[pathlib.Path]) -> pathlib.Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


def resolve_credentials_path(explicit: str | pathlib.Path | None = None) -> pathlib.Path:
    path = resolve_existing_path(credential_candidates(explicit))
    if path is None:
        searched = ", ".join(str(candidate) for candidate in credential_candidates(explicit))
        raise FileNotFoundError(
            "credentials.json not found. Searched: "
            f"{searched}. Place the Google OAuth desktop client file in one of those locations "
            "or set GMAIL_CREDENTIALS_PATH."
        )
    return path


def resolve_token_input_path(explicit: str | pathlib.Path | None = None) -> pathlib.Path | None:
    return resolve_existing_path(token_candidates(explicit))


def resolve_token_output_path(explicit: str | pathlib.Path | None = None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()

    env_value = os.environ.get("GMAIL_TOKEN_PATH")
    if env_value:
        return pathlib.Path(env_value).expanduser().resolve()

    existing = resolve_existing_path(token_candidates())
    if existing is not None:
        return existing

    preferred = CONFIG_DIR / "job-agent-token.json"
    if preferred.parent.exists():
        return preferred
    return REPO_ROOT / "token.json"


def github_secret_set_command(repo: str) -> list[str]:
    """Build the GitHub CLI command that accepts the token JSON on stdin."""
    return ["gh", "secret", "set", "GMAIL_TOKEN_JSON", "--repo", repo]


def update_github_token_secret(repo: str, token_path: pathlib.Path) -> None:
    subprocess.run(
        github_secret_set_command(repo),
        input=token_path.read_text(),
        text=True,
        check=True,
    )
