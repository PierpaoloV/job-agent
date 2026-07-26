from types import SimpleNamespace

import fetch_gmail


def test_sender_filters_include_current_indeed_job_alert_sender():
    assert "donotreply@jobalert.indeed.com" in fetch_gmail.SENDER_FILTERS
    assert "indeed.com" in fetch_gmail.SENDER_FILTERS


def test_email_source_counts_classify_sender_without_exposing_addresses():
    assert fetch_gmail._email_source_counts(
        [
            {"from": "Indeed Job Alerts <campaign@alerts.indeed.com>"},
            {"from": "Glassdoor Jobs <jobs@glassdoor.com>"},
            {"from": "LinkedIn <jobs@linkedin.com>"},
            {"from": "Specialist board <alerts@example.org>"},
        ]
    ) == {
        "Indeed": 1,
        "Glassdoor": 1,
        "LinkedIn": 1,
        "Other": 1,
    }


def test_indeed_link_shape_audit_redacts_values_and_opaque_path_ids():
    counts = fetch_gmail._indeed_link_shape_counts(
        [
            {
                "from": "Indeed <alerts@indeed.com>",
                "body": (
                    '<a href="https://click.indeed.com/1234567890abcdef1234567890'
                    '?jk=secret-job-id&amp;campaign=private-value">Role</a>'
                ),
            }
        ]
    )

    rendered = " ".join(counts)
    assert "click.indeed.com/{id}?campaign,jk" in rendered
    assert "secret-job-id" not in rendered
    assert "private-value" not in rendered


class _Credentials:
    valid = True
    expired = False
    refresh_token = "refresh-token"
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    granted_scopes = None


def test_get_service_verifies_authenticated_mailbox(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    credentials = _Credentials()
    service = SimpleNamespace()
    calls = []

    monkeypatch.setattr(fetch_gmail, "resolve_token_input_path", lambda: token)
    monkeypatch.setattr(
        fetch_gmail.Credentials,
        "from_authorized_user_file",
        lambda *_args: credentials,
    )
    monkeypatch.setattr(fetch_gmail, "build", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(
        fetch_gmail,
        "verify_dedicated_mailbox",
        lambda candidate_service, candidate_credentials: calls.append(
            (candidate_service, candidate_credentials)
        ),
        raising=False,
    )

    assert fetch_gmail._get_service() is service
    assert calls == [(service, credentials)]
