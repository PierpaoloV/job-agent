import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import gmail_auth
import auth_gmail as auth_command


class _ProfileRequest:
    def __init__(self, email_address):
        self._email_address = email_address

    def execute(self):
        return {"emailAddress": self._email_address}


class _Users:
    def __init__(self, email_address):
        self._email_address = email_address

    def getProfile(self, *, userId):
        assert userId == "me"
        return _ProfileRequest(self._email_address)


class _GmailService:
    def __init__(self, email_address):
        self._email_address = email_address

    def users(self):
        return _Users(self._email_address)


class _Credentials:
    def __init__(self, scopes):
        self.scopes = scopes
        self.granted_scopes = None


def test_resolve_credentials_path_prefers_env(monkeypatch, tmp_path):
    creds = tmp_path / "client.json"
    creds.write_text("{}")
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds))

    resolved = gmail_auth.resolve_credentials_path()

    assert resolved == creds


def test_resolve_token_output_path_prefers_env(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token))

    resolved = gmail_auth.resolve_token_output_path()

    assert resolved == token


def test_resolve_token_output_path_prefers_config_dir_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("GMAIL_TOKEN_PATH", raising=False)
    monkeypatch.setattr(gmail_auth, "CONFIG_DIR", tmp_path / ".config" / "google")
    monkeypatch.setattr(gmail_auth, "REPO_ROOT", tmp_path / "repo")
    gmail_auth.REPO_ROOT.mkdir()
    gmail_auth.CONFIG_DIR.mkdir(parents=True)

    resolved = gmail_auth.resolve_token_output_path()

    assert resolved == gmail_auth.CONFIG_DIR / "job-agent-token.json"


def test_resolve_token_output_path_reuses_existing_token(monkeypatch, tmp_path):
    monkeypatch.delenv("GMAIL_TOKEN_PATH", raising=False)
    monkeypatch.setattr(gmail_auth, "CONFIG_DIR", tmp_path / ".config" / "google")
    monkeypatch.setattr(gmail_auth, "REPO_ROOT", tmp_path / "repo")
    gmail_auth.REPO_ROOT.mkdir()
    existing = gmail_auth.REPO_ROOT / "token.json"
    existing.write_text("{}")

    resolved = gmail_auth.resolve_token_output_path()

    assert resolved == existing


def test_github_secret_set_command_accepts_token_on_stdin():
    command = gmail_auth.github_secret_set_command("alex-example/job-agent")

    assert command == [
        "gh",
        "secret",
        "set",
        "GMAIL_TOKEN_JSON",
        "--repo",
        "alex-example/job-agent",
    ]


def test_verify_dedicated_mailbox_accepts_configured_career_account():
    result = gmail_auth.verify_dedicated_mailbox(
        _GmailService("Alex.Jobs@gmail.com"),
        _Credentials(gmail_auth.SCOPES),
        expected_email="alex.jobs@gmail.com",
    )

    assert result == "alex.jobs@gmail.com"


def test_verify_dedicated_mailbox_rejects_a_different_account():
    try:
        gmail_auth.verify_dedicated_mailbox(
            _GmailService("alex.personal@gmail.com"),
            _Credentials(gmail_auth.SCOPES),
            expected_email="alex.jobs@gmail.com",
        )
    except ValueError as exc:
        assert "dedicated career mailbox" in str(exc)
    else:
        raise AssertionError("Personal Gmail account must be rejected")


def test_verify_dedicated_mailbox_rejects_mutating_gmail_scope():
    try:
        gmail_auth.verify_dedicated_mailbox(
            _GmailService("alex.jobs@gmail.com"),
            _Credentials(["https://www.googleapis.com/auth/gmail.modify"]),
            expected_email="alex.jobs@gmail.com",
        )
    except ValueError as exc:
        assert "read-only Gmail scope" in str(exc)
    else:
        raise AssertionError("Mutating Gmail scope must be rejected")


def test_auth_command_does_not_save_token_before_mailbox_verification(
    monkeypatch, tmp_path
):
    credentials_path = tmp_path / "client.json"
    credentials_path.write_text("{}")
    token_path = tmp_path / "token.json"
    credentials = object()

    class _Flow:
        def run_local_server(self, **_kwargs):
            return credentials

    monkeypatch.setattr(
        auth_command,
        "resolve_credentials_path",
        lambda _explicit=None: credentials_path,
    )
    monkeypatch.setattr(
        auth_command,
        "resolve_token_output_path",
        lambda _explicit=None: token_path,
    )
    monkeypatch.setattr(
        auth_command.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *_args, **_kwargs: _Flow(),
    )
    monkeypatch.setattr(auth_command, "build", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        auth_command,
        "verify_dedicated_mailbox",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(ValueError("wrong mailbox")),
    )

    try:
        auth_command.main()
    except ValueError as exc:
        assert str(exc) == "wrong mailbox"
    else:
        raise AssertionError("OAuth for the wrong mailbox must fail")

    assert not token_path.exists()


def test_verify_dedicated_mailbox_fails_without_configured_identity(monkeypatch):
    monkeypatch.delenv("JOB_AGENT_CAREER_GMAIL", raising=False)

    try:
        gmail_auth.verify_dedicated_mailbox(
            _GmailService("alex.jobs@gmail.com"),
            _Credentials(gmail_auth.SCOPES),
        )
    except ValueError as exc:
        assert "JOB_AGENT_CAREER_GMAIL" in str(exc)
    else:
        raise AssertionError("Missing mailbox identity must fail closed")
