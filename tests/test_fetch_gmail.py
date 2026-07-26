import pathlib
import sys

from google.auth.exceptions import RefreshError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from fetch_gmail import _format_refresh_failure


def test_format_refresh_failure_for_local_reauth(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    message = _format_refresh_failure(
        RefreshError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
        )
    )

    assert "expired or revoked" in message
    assert "python auth_gmail.py" in message
    assert "GMAIL_TOKEN_JSON" not in message


def test_format_refresh_failure_for_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    message = _format_refresh_failure(
        RefreshError(
            "('invalid_grant: Token has been expired or revoked.', "
            "{'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
        )
    )

    assert "GMAIL_TOKEN_JSON" in message
    assert "Production instead of Testing" in message
