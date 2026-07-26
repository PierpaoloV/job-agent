import base64
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from career_correspondence_adapters import GmailCareerMailboxReader  # noqa: E402
from career_correspondence_adapters import (  # noqa: E402
    TelegramCorrespondenceOutboxDispatcher,
    TelegramDeliveryError,
)
from career_correspondence_domain import TelegramClassificationRequest  # noqa: E402
from career_correspondence_store import JsonCareerCorrespondenceStore  # noqa: E402
from career_correspondence_composition import (  # noqa: E402
    build_career_correspondence_runtime,
    run_career_correspondence_cycle,
)


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


class GmailRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class ReadOnlyMessages:
    def __init__(self, messages):
        self.messages_by_id = {item["id"]: item for item in messages}
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return GmailRequest({"messages": [{"id": key} for key in self.messages_by_id]})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return GmailRequest(self.messages_by_id[kwargs["id"]])


class ReadOnlyUsers:
    def __init__(self, messages, *, email_address):
        self._messages = ReadOnlyMessages(messages)
        self._email_address = email_address

    def messages(self):
        return self._messages

    def getProfile(self, **kwargs):
        return GmailRequest({"emailAddress": self._email_address})


class ReadOnlyGmail:
    def __init__(
        self,
        messages,
        *,
        email_address="alex.jobs@gmail.com",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
    ):
        self._users = ReadOnlyUsers(messages, email_address=email_address)
        self._http = type(
            "AuthorizedHttp",
            (),
            {"credentials": type("Credentials", (), {"scopes": scopes})()},
        )()

    def users(self):
        return self._users


class CapturingTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, *, delivery_id, text):
        self.messages.append((delivery_id, text))


class RefusingTelegram:
    def __init__(self):
        self.calls = 0

    def send_message(self, *, delivery_id, text):
        self.calls += 1
        raise TelegramDeliveryError("connection refused", may_have_sent=False)


class TimingOutTelegram:
    def send_message(self, *, delivery_id, text):
        raise TelegramDeliveryError("response timed out", may_have_sent=True)


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_gmail_adapter_reads_only_dedicated_mailbox_and_derives_authentication():
    gmail = ReadOnlyGmail(
        [
            {
                "id": "gmail-1",
                "threadId": "thread-1",
                "internalDate": "1784202300000",
                "payload": {
                    "headers": [
                        {
                            "name": "From",
                            "value": "Example Labs Talent <careers@example-labs.example>",
                        },
                        {"name": "Subject", "value": "Interview — REQ-42"},
                        {
                            "name": "Authentication-Results",
                            "value": (
                                "mx.google.com; dkim=pass header.i=@example-labs.example; "
                                "spf=pass smtp.mailfrom=example-labs.example; "
                                "dmarc=pass header.from=example-labs.example"
                            ),
                        },
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("Schedule your interview for REQ-42")},
                },
            }
        ]
    )
    reader = GmailCareerMailboxReader(gmail)

    messages = reader.fetch(account_address="alex.jobs@gmail.com")

    assert len(messages) == 1
    assert messages[0].message_id == "gmail-1"
    assert messages[0].thread_id == "thread-1"
    assert messages[0].sender_address == "careers@example-labs.example"
    assert messages[0].authenticated_sender is True
    assert messages[0].authenticated_domain == "example-labs.example"
    assert messages[0].body_text == "Schedule your interview for REQ-42"
    assert (
        messages[0].received_at
        == datetime.fromtimestamp(1784202300, timezone.utc).isoformat()
    )
    assert [name for name, _ in gmail._users._messages.calls] == ["list", "get"]
    with pytest.raises(ValueError, match="dedicated career"):
        reader.fetch(account_address="personal@gmail.com")
    assert [name for name, _ in gmail._users._messages.calls] == ["list", "get"]


def test_duplicate_authentication_results_fail_closed():
    gmail = ReadOnlyGmail(
        [
            {
                "id": "gmail-forged-auth",
                "threadId": "thread-forged-auth",
                "internalDate": "1784202300000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Example Labs <careers@example-labs.example>"},
                        {
                            "name": "Authentication-Results",
                            "value": "mx.google.com; dmarc=fail header.from=example-labs.example",
                        },
                        {
                            "name": "Authentication-Results",
                            "value": (
                                "attacker.invalid; dkim=pass; spf=pass; "
                                "dmarc=pass header.from=example-labs.example"
                            ),
                        },
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("REQ-42 rejected")},
                },
            }
        ]
    )

    parsed = GmailCareerMailboxReader(gmail).fetch(
        account_address="alex.jobs@gmail.com"
    )[0]

    assert parsed.authenticated_sender is False
    assert parsed.authenticated_domain is None


def test_single_untrusted_authentication_results_service_fails_closed():
    gmail = ReadOnlyGmail(
        [
            {
                "id": "gmail-forged-auth-service",
                "threadId": "thread-forged-auth-service",
                "internalDate": "1784202300000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Example Labs <careers@example-labs.example>"},
                        {
                            "name": "Authentication-Results",
                            "value": (
                                "attacker.invalid; dkim=pass; spf=pass; "
                                "dmarc=pass header.from=example-labs.example"
                            ),
                        },
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _encoded("REQ-42 rejected")},
                },
            }
        ]
    )

    parsed = GmailCareerMailboxReader(gmail).fetch(
        account_address="alex.jobs@gmail.com"
    )[0]

    assert parsed.authenticated_sender is False
    assert parsed.authenticated_domain is None


def test_direct_fetch_revalidates_gmail_identity_and_readonly_scope():
    personal = ReadOnlyGmail(
        [],
        email_address="personal@gmail.com",
        scopes=("https://www.googleapis.com/auth/gmail.modify",),
    )
    reader = GmailCareerMailboxReader(personal)

    with pytest.raises(ValueError, match="read-only Gmail scope"):
        reader.fetch(account_address="career.user@gmail.com")

    assert personal._users._messages.calls == []


def test_telegram_dispatch_ack_is_durable_across_restart(tmp_path):
    store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    claim = store.claim_message(
        "gmail-ambiguous-1",
        claimed_at="2026-07-16T11:59:00+00:00",
    )
    request = TelegramClassificationRequest(
        request_id="classification-1",
        message_id="gmail-ambiguous-1",
        application_id="app-1",
        reason="message_not_deterministic",
        summary=(
            "Nuova corrispondenza nella casella carriera; "
            "apri il report locale per i dettagli."
        ),
        created_at="2026-07-16T11:59:00+00:00",
    )
    store.complete_message(
        "gmail-ambiguous-1",
        claim_token=claim.token,
        completed_at="2026-07-16T11:59:00+00:00",
        application_id="app-1",
        classification="ambiguous",
        request=request,
    )
    telegram = CapturingTelegram()

    first = TelegramCorrespondenceOutboxDispatcher(
        store=store, transport=telegram, clock=FixedClock()
    ).dispatch_pending()
    second = TelegramCorrespondenceOutboxDispatcher(
        store=JsonCareerCorrespondenceStore(tmp_path / "career-correspondence"),
        transport=telegram,
        clock=FixedClock(),
    ).dispatch_pending()

    assert first.delivered == 1
    assert second.delivered == 0
    assert telegram.messages == [
        (
            "classification-1",
            "Classificazione Telegram pronta. "
            "Apri il report locale; motivo: message_not_deterministic.",
        )
    ]


def _pending_classification(store, delivery_id="classification-retry"):
    message_id = f"gmail-{delivery_id}"
    claim = store.claim_message(message_id, claimed_at="2026-07-16T11:59:00+00:00")
    request = TelegramClassificationRequest(
        request_id=delivery_id,
        message_id=message_id,
        application_id="app-1",
        reason="message_not_deterministic",
        summary="Review locally.",
        created_at="2026-07-16T11:59:00+00:00",
    )
    store.complete_message(
        message_id,
        claim_token=claim.token,
        completed_at="2026-07-16T11:59:00+00:00",
        application_id="app-1",
        classification="ambiguous",
        request=request,
    )


def test_definitive_telegram_failure_retries_bounded_and_can_be_requeued(tmp_path):
    store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    _pending_classification(store)
    telegram = RefusingTelegram()

    result = TelegramCorrespondenceOutboxDispatcher(
        store=store, transport=telegram, clock=FixedClock()
    ).dispatch_pending()

    assert telegram.calls == 3
    assert result.retryable == 2
    assert result.failed == 1
    assert result.uncertain == 0
    assert store.telegram_delivery_status("classification-retry") == "failed"

    store.requeue_telegram_delivery(
        "classification-retry", requeued_at="2026-07-16T12:01:00+00:00"
    )
    assert store.telegram_delivery_status("classification-retry") == "pending"


def test_possible_telegram_io_is_uncertain_until_explicit_reconciliation(tmp_path):
    store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    _pending_classification(store, "classification-uncertain")

    result = TelegramCorrespondenceOutboxDispatcher(
        store=store, transport=TimingOutTelegram(), clock=FixedClock()
    ).dispatch_pending()

    assert result.uncertain == 1
    assert store.telegram_delivery_status("classification-uncertain") == "uncertain"
    with pytest.raises(RuntimeError, match="definitively failed"):
        store.requeue_telegram_delivery(
            "classification-uncertain",
            requeued_at="2026-07-16T12:01:00+00:00",
        )
    store.reconcile_telegram_delivery(
        "classification-uncertain",
        delivered=False,
        reconciled_at="2026-07-16T12:02:00+00:00",
    )
    assert store.telegram_delivery_status("classification-uncertain") == "pending"


def test_production_composition_is_unconfigured_until_explicit_connection(tmp_path):
    gmail = ReadOnlyGmail([])
    runtime = build_career_correspondence_runtime(
        repository_root=tmp_path / "repository",
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=gmail,
        applications=object(),
        telegram_transport=CapturingTelegram(),
        clock=FixedClock(),
    )

    before = runtime.monitor.poll()
    runtime.connect_dedicated_mailbox()
    after = runtime.monitor.poll()

    assert before.status == "unconfigured"
    assert after.status == "completed"
    assert [name for name, _ in gmail._users._messages.calls] == ["list"]
    state_path = (
        tmp_path
        / "repository"
        / "data"
        / "private"
        / "career-correspondence"
        / "state.json"
    )
    assert state_path.is_file()


def test_production_composition_rejects_a_personal_authenticated_mailbox(tmp_path):
    gmail = ReadOnlyGmail([], email_address="personal@gmail.com")
    runtime = build_career_correspondence_runtime(
        repository_root=tmp_path / "repository",
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=gmail,
        applications=object(),
        telegram_transport=CapturingTelegram(),
        clock=FixedClock(),
    )

    with pytest.raises(ValueError, match="authenticated Gmail account"):
        runtime.connect_dedicated_mailbox()

    assert runtime.monitor.poll().status == "unconfigured"
    assert gmail._users._messages.calls == []


def test_production_composition_rejects_mutating_gmail_scopes(tmp_path):
    gmail = ReadOnlyGmail(
        [],
        scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ),
    )
    runtime = build_career_correspondence_runtime(
        repository_root=tmp_path / "repository",
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=gmail,
        applications=object(),
        telegram_transport=CapturingTelegram(),
        clock=FixedClock(),
    )

    with pytest.raises(ValueError, match="read-only Gmail scope"):
        runtime.connect_dedicated_mailbox()

    assert runtime.monitor.poll().status == "unconfigured"


def test_production_cycle_is_safe_while_mailbox_is_unconfigured(tmp_path):
    gmail = ReadOnlyGmail([])
    telegram = CapturingTelegram()
    runtime = build_career_correspondence_runtime(
        repository_root=tmp_path / "repository",
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=gmail,
        applications=object(),
        telegram_transport=telegram,
        clock=FixedClock(),
    )

    result = run_career_correspondence_cycle(runtime)

    assert result.mailbox.status == "unconfigured"
    assert result.telegram.delivered == 0
    assert gmail._users._messages.calls == []
    assert telegram.messages == []


def test_production_cycle_revalidates_persisted_mailbox_after_restart(tmp_path):
    repository = tmp_path / "repository"
    initial = build_career_correspondence_runtime(
        repository_root=repository,
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=ReadOnlyGmail([]),
        applications=object(),
        telegram_transport=CapturingTelegram(),
        clock=FixedClock(),
    )
    initial.connect_dedicated_mailbox()
    personal_gmail = ReadOnlyGmail([], email_address="personal@gmail.com")
    restarted = build_career_correspondence_runtime(
        repository_root=repository,
        candidate_name="Alex Example",
        career_gmail_address="alex.jobs@gmail.com",
        gmail_service=personal_gmail,
        applications=object(),
        telegram_transport=CapturingTelegram(),
        clock=FixedClock(),
    )

    with pytest.raises(ValueError, match="authenticated Gmail account"):
        run_career_correspondence_cycle(restarted)

    assert personal_gmail._users._messages.calls == []
