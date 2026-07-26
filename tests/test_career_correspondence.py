from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from application_domain import (  # noqa: E402
    ApplicationSnapshot,
    LifecycleEvent,
    LifecycleState,
)
from application_packages import LocalApplicationPackageWriter  # noqa: E402
from application_storage import JsonApplicationStore  # noqa: E402
from application_workflow import ApplicationWorkflowCoordinator  # noqa: E402
from career_correspondence import (  # noqa: E402
    CareerCorrespondenceMonitor,
    CareerMailboxConnection,
    CareerMessage,
    MailboxPollStatus,
    SenderKind,
)
from career_correspondence_store import JsonCareerCorrespondenceStore  # noqa: E402
from career_correspondence_domain import TrustedDomain  # noqa: E402


class FakeMailbox:
    def __init__(self, messages=()):
        self.messages = tuple(messages)
        self.fetch_calls = []

    def fetch(self, *, account_address):
        self.fetch_calls.append(account_address)
        return self.messages


class FailFirstMessageCompletion:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def complete_message(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated completion failure")
        return self.delegate.complete_message(*args, **kwargs)


class UnusedCapability:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected workflow capability: {name}")


@dataclass
class FixedClock:
    current: datetime = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current


def make_application(
    tmp_path,
    *,
    application_id="app-example-labs-42",
    state=LifecycleState.SUBMITTED,
    opportunity_extra=None,
):
    application_store = JsonApplicationStore(tmp_path / "application-state")
    report_writer = LocalApplicationPackageWriter(tmp_path / "private-applications")
    snapshot = ApplicationSnapshot(
        application_id=application_id,
        opportunity={
            "stable_id": "example-labs:ai-scientist:42",
            "official_job_id": "REQ-42",
            "company": "Example Labs",
            "title": "AI Scientist",
            "location": "Zurich",
            "trusted_correspondence_domains": ["example-labs.example"],
            **(opportunity_extra or {}),
        },
        opportunity_version="opportunity-v1",
        lifecycle_state=state,
        authorization_version="opportunity-v1",
        history=(LifecycleEvent(state, "2026-07-15T10:00:00+00:00"),),
    )
    application_store.save(snapshot)
    report_writer.write(snapshot)
    coordinator = ApplicationWorkflowCoordinator(
        store=application_store,
        tailoring=UnusedCapability(),
        ats=UnusedCapability(),
        report_writer=report_writer,
        official_vacancies=UnusedCapability(),
        clock=FixedClock(),
    )
    return coordinator, report_writer


def connect(store):
    return store.connect(
        CareerMailboxConnection(
            address="alex.jobs@gmail.com",
            connected_at="2026-07-16T11:30:00+00:00",
        )
    )


@pytest.mark.parametrize("value", ("com", "co.uk"))
def test_trusted_domain_rejects_public_suffix_like_values(value):
    with pytest.raises(ValueError, match="registrable domain"):
        TrustedDomain(value)


def message(
    *,
    message_id,
    subject,
    body,
    sender_kind=SenderKind.UNKNOWN,
    received_at="2026-07-16T11:45:00+00:00",
):
    return CareerMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender_address="careers@example-labs.example",
        sender_name="Example Labs Talent",
        sender_kind=sender_kind,
        authenticated_sender=True,
        subject=subject,
        body_text=body,
        received_at=received_at,
        authenticated_domain="example-labs.example",
    )


def make_monitor(tmp_path, mailbox, coordinator):
    store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    return (
        CareerCorrespondenceMonitor(
            mailbox=mailbox,
            store=store,
            applications=coordinator,
            clock=FixedClock(),
            candidate_name="Alex Example",
        ),
        store,
    )


def test_mailbox_is_explicitly_unconfigured_and_never_falls_back(tmp_path):
    coordinator, _ = make_application(tmp_path)
    mailbox = FakeMailbox()
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)

    result = monitor.poll()

    assert result.status == MailboxPollStatus.UNCONFIGURED
    assert result.processed == 0
    assert mailbox.fetch_calls == []
    assert store.connection() is None

    with pytest.raises(ValueError, match="dedicated career Gmail"):
        store.connect(
            CareerMailboxConnection(
                address="not-an-email",
                connected_at="2026-07-16T11:30:00+00:00",
            )
        )


def test_receipt_is_evidence_and_never_promotes_application_to_submitted(tmp_path):
    coordinator, report_writer = make_application(
        tmp_path, state=LifecycleState.PROPOSED
    )
    mailbox = FakeMailbox(
        [
            message(
                message_id="gmail-receipt-1",
                subject="Application received — AI Scientist (REQ-42)",
                body=(
                    "We have received your application for AI Scientist at Example Labs. "
                    "Reference REQ-42."
                ),
            )
        ]
    )
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)
    connect(store)

    result = monitor.poll()
    application = coordinator.get("app-example-labs-42")

    assert result.status == MailboxPollStatus.COMPLETED
    assert application.lifecycle_state == LifecycleState.PROPOSED
    assert application.outcome is None
    assert len(application.correspondence) == 1
    event = application.correspondence[0]
    assert event.classification == "receipt"
    assert event.sender_trust_evidence == "configured_domain"
    assert event.evidence_role == "application_receipt_only"
    assert event.previous_state == "proposta"
    assert event.resulting_state == "proposta"
    assert store.classification_requests() == ()

    package = report_writer.package_path("app-example-labs-42")
    correspondence = json.loads(
        (package / "correspondence.json").read_text(encoding="utf-8")
    )
    assert correspondence[0]["message_id"] == "gmail-receipt-1"
    assert correspondence[0]["sender_trust_evidence"] == "configured_domain"
    report = (package / "report.md").read_text(encoding="utf-8")
    assert "application receipt only; not submission verification" in report


@pytest.mark.parametrize(
    ("message_id", "subject", "body", "expected"),
    [
        (
            "gmail-interview-1",
            "Interview invitation — AI Scientist REQ-42",
            "We would like to invite you to interview for AI Scientist at Example Labs.",
            LifecycleState.INTERVIEW,
        ),
        (
            "gmail-rejection-1",
            "Update on AI Scientist REQ-42",
            "We have decided not to move forward with your application to Example Labs.",
            LifecycleState.REJECTED,
        ),
    ],
)
def test_unambiguous_message_updates_lifecycle_and_local_report(
    tmp_path, message_id, subject, body, expected
):
    coordinator, report_writer = make_application(tmp_path)
    mailbox = FakeMailbox([message(message_id=message_id, subject=subject, body=body)])
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)
    connect(store)

    first = monitor.poll()
    restarted = CareerCorrespondenceMonitor(
        mailbox=mailbox,
        store=JsonCareerCorrespondenceStore(tmp_path / "career-correspondence"),
        applications=coordinator,
        clock=FixedClock(),
        candidate_name="Alex Example",
    )
    second = restarted.poll()
    application = coordinator.get("app-example-labs-42")

    assert first.processed == 1
    assert second.processed == 0
    assert second.already_processed == 1
    assert application.lifecycle_state == expected
    assert len(application.correspondence) == 1
    assert application.history[-1].state == expected

    package = report_writer.package_path("app-example-labs-42")
    report = (package / "report.md").read_text(encoding="utf-8")
    assert message_id in report
    assert expected.value in report


def test_ambiguous_message_creates_telegram_request_without_status_change(tmp_path):
    coordinator, _ = make_application(tmp_path)
    mailbox = FakeMailbox(
        [
            message(
                message_id="gmail-ambiguous-1",
                subject="Application update — AI Scientist REQ-42",
                body=(
                    "We would like to invite you to interview. However, we have "
                    "decided not to move forward with your application."
                ),
            )
        ]
    )
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)
    connect(store)

    result = monitor.poll()
    application = coordinator.get("app-example-labs-42")
    requests = store.classification_requests()

    assert result.ambiguous == 1
    assert application.lifecycle_state == LifecycleState.SUBMITTED
    assert len(requests) == 1
    assert requests[0].application_id == "app-example-labs-42"
    assert requests[0].message_id == "gmail-ambiguous-1"
    assert requests[0].delivery_channel == "telegram"
    assert requests[0].status == "pending"
    assert application.correspondence[0].classification == "ambiguous"
    assert application.correspondence[0].sender_trust_evidence is None
    assert application.correspondence[0].resulting_state == "inviata"


@pytest.mark.parametrize(
    ("sender_kind", "expected_kind", "draft_fragment"),
    [
        (SenderKind.RECRUITER, "recruiter", "Thank you for reaching out"),
        (
            SenderKind.HIRING_MANAGER,
            "hiring_manager",
            "Thank you for reaching out",
        ),
        (SenderKind.REFERRAL, "referral", "Would you feel comfortable referring"),
    ],
)
def test_people_messages_create_local_summary_and_draft_but_cannot_send(
    tmp_path, sender_kind, expected_kind, draft_fragment
):
    coordinator, _ = make_application(tmp_path)
    mailbox = FakeMailbox(
        [
            message(
                message_id=f"gmail-{expected_kind}-1",
                subject="AI Scientist opportunity at Example Labs (REQ-42)",
                body="I would be glad to discuss the AI Scientist role at Example Labs.",
                sender_kind=sender_kind,
            )
        ]
    )
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)
    connect(store)

    monitor.poll()

    drafts = store.drafts()
    review_requests = store.draft_review_requests()
    assert len(drafts) == 1
    assert len(review_requests) == 1
    assert drafts[0].kind == expected_kind
    assert drafts[0].application_id == "app-example-labs-42"
    assert draft_fragment in drafts[0].body
    assert drafts[0].body.endswith("Best,\nAlex Example")
    assert drafts[0].status == "local_draft"
    assert review_requests[0].draft_id == drafts[0].draft_id
    assert review_requests[0].delivery_channel == "telegram"
    assert review_requests[0].status == "pending"
    assert not hasattr(monitor, "send")
    assert not hasattr(mailbox, "send")
    application = coordinator.get("app-example-labs-42")
    assert application.lifecycle_state == LifecycleState.SUBMITTED
    assert application.correspondence[0].draft_id == drafts[0].draft_id


def test_unlinked_message_is_ambiguous_and_does_not_mutate_any_application(tmp_path):
    coordinator, _ = make_application(tmp_path)
    mailbox = FakeMailbox(
        [
            message(
                message_id="gmail-unlinked-1",
                subject="Your application update",
                body="We have decided not to move forward with your application.",
            )
        ]
    )
    monitor, store = make_monitor(tmp_path, mailbox, coordinator)
    connect(store)

    result = monitor.poll()

    assert result.ambiguous == 1
    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    request = store.classification_requests()[0]
    assert request.application_id is None
    assert request.reason == "application_not_linked_unambiguously"


def test_unauthenticated_matching_mail_never_changes_lifecycle(tmp_path):
    coordinator, _ = make_application(tmp_path)
    suspicious = message(
        message_id="gmail-spoofed-1",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
    )
    suspicious = CareerMessage(
        **{
            **suspicious.__dict__,
            "authenticated_sender": False,
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([suspicious]), coordinator)
    connect(store)

    result = monitor.poll()

    assert result.ambiguous == 1
    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    assert store.classification_requests()[0].reason == "sender_not_authenticated"


def test_authenticated_unrelated_sender_cannot_change_lifecycle(tmp_path):
    coordinator, _ = make_application(tmp_path)
    suspicious = message(
        message_id="gmail-unrelated-1",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
    )
    suspicious = CareerMessage(
        **{
            **suspicious.__dict__,
            "sender_address": "attacker@unrelated.example",
            "authenticated_domain": "unrelated.example",
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([suspicious]), coordinator)
    connect(store)

    result = monitor.poll()

    assert result.ambiguous == 1
    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    assert (
        store.classification_requests()[0].reason
        == "sender_not_trusted_for_application"
    )


@pytest.mark.parametrize("public_suffix", ("com", "co.kr", "github.io", "appspot.com"))
def test_public_suffix_like_config_cannot_trust_an_attacker_domain(
    tmp_path, public_suffix
):
    coordinator, _ = make_application(
        tmp_path,
        opportunity_extra={"trusted_correspondence_domains": [public_suffix]},
    )
    suspicious = message(
        message_id="gmail-public-suffix-attacker",
        subject="Interview invitation — AI Scientist REQ-42",
        body="We would like to invite you to interview for AI Scientist at Example Labs.",
    )
    suspicious = CareerMessage(
        **{
            **suspicious.__dict__,
            "sender_address": f"careers@attacker.{public_suffix}",
            "authenticated_domain": f"attacker.{public_suffix}",
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([suspicious]), coordinator)
    connect(store)

    result = monitor.poll()

    assert result.ambiguous == 1
    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    assert (
        store.classification_requests()[0].reason
        == "sender_not_trusted_for_application"
    )


@pytest.mark.parametrize(
    ("opportunity_extra", "sender_domain"),
    (
        (
            {"trusted_correspondence_domains": ["Employer.Co.UK."]},
            "jobs.employer.co.uk",
        ),
        (
            {"trusted_ats_domains": ["Greenhouse.IO."]},
            "eu.greenhouse.io",
        ),
        (
            {"official_url": "https://CAREERS.EMPLOYER.CO.UK./jobs/REQ-42"},
            "mail.careers.employer.co.uk",
        ),
    ),
)
def test_legitimate_employer_and_ats_subdomains_remain_trusted(
    tmp_path, opportunity_extra, sender_domain
):
    coordinator, _ = make_application(
        tmp_path,
        opportunity_extra=opportunity_extra,
    )
    legitimate = message(
        message_id=f"gmail-legitimate-{sender_domain}",
        subject="Interview invitation — AI Scientist REQ-42",
        body="We would like to invite you to interview for AI Scientist at Example Labs.",
    )
    legitimate = CareerMessage(
        **{
            **legitimate.__dict__,
            "sender_address": f"careers@{sender_domain}",
            "authenticated_domain": sender_domain.upper() + ".",
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([legitimate]), coordinator)
    connect(store)

    result = monitor.poll()

    assert result.processed == 1
    assert result.ambiguous == 0
    application = coordinator.get("app-example-labs-42")
    assert application.lifecycle_state == LifecycleState.INTERVIEW
    assert application.correspondence[0].sender_trust_evidence == "configured_domain"
    assert store.classification_requests() == ()


def test_discovery_source_domain_is_not_trusted_for_lifecycle(tmp_path):
    coordinator, _ = make_application(
        tmp_path,
        opportunity_extra={"source_url": "https://linkedin.com/jobs/view/REQ-42"},
    )
    assert (
        coordinator.get("app-example-labs-42")
        .opportunity["source_url"]
        .startswith("https://linkedin.com/")
    )
    suspicious = message(
        message_id="gmail-linkedin-rejection",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
    )
    suspicious = CareerMessage(
        **{
            **suspicious.__dict__,
            "sender_address": "jobs-noreply@linkedin.com",
            "authenticated_domain": "linkedin.com",
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([suspicious]), coordinator)
    connect(store)

    result = monitor.poll()

    assert result.ambiguous == 1
    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    assert (
        store.classification_requests()[0].reason
        == "sender_not_trusted_for_application"
    )


def test_ambiguous_discovery_sender_cannot_bootstrap_thread_trust(tmp_path):
    coordinator, _ = make_application(
        tmp_path,
        opportunity_extra={"source_url": "https://linkedin.com/jobs/view/REQ-42"},
    )
    ambiguous = message(
        message_id="gmail-linkedin-thread-seed",
        subject="Question about REQ-42",
        body="Please review this application update.",
        received_at="2026-07-16T11:40:00+00:00",
    )
    rejection = message(
        message_id="gmail-linkedin-thread-rejection",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
        received_at="2026-07-16T11:50:00+00:00",
    )
    shared = []
    for item in (ambiguous, rejection):
        shared.append(
            CareerMessage(
                **{
                    **item.__dict__,
                    "thread_id": "linkedin-discovery-thread",
                    "sender_address": "jobs-noreply@linkedin.com",
                    "authenticated_domain": "linkedin.com",
                }
            )
        )
    monitor, store = make_monitor(tmp_path, FakeMailbox(shared), coordinator)
    connect(store)

    result = monitor.poll()
    application = coordinator.get("app-example-labs-42")

    assert result.ambiguous == 2
    assert application.lifecycle_state == LifecycleState.SUBMITTED
    assert [event.classification for event in application.correspondence] == [
        "ambiguous",
        "ambiguous",
    ]
    assert [request.reason for request in store.classification_requests()] == [
        "sender_not_trusted_for_application",
        "sender_not_trusted_for_application",
    ]


def test_message_that_predates_current_lifecycle_requires_human_classification(
    tmp_path,
):
    coordinator, _ = make_application(tmp_path)
    old_message = message(
        message_id="gmail-old-rejection-1",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
        received_at="2026-07-14T11:45:00+00:00",
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([old_message]), coordinator)
    connect(store)

    monitor.poll()

    assert coordinator.get("app-example-labs-42").lifecycle_state == LifecycleState.SUBMITTED
    assert (
        store.classification_requests()[0].reason
        == "message_predates_current_lifecycle"
    )


def test_messages_are_applied_in_received_order_not_provider_page_order(tmp_path):
    coordinator, _ = make_application(tmp_path)
    interview = message(
        message_id="gmail-interview-first",
        subject="Interview invitation — AI Scientist REQ-42",
        body="We would like to invite you to interview for AI Scientist at Example Labs.",
        received_at="2026-07-16T11:40:00+00:00",
    )
    rejection = message(
        message_id="gmail-rejection-second",
        subject="Update on AI Scientist REQ-42",
        body="We have decided not to move forward with your application to Example Labs.",
        received_at="2026-07-16T11:50:00+00:00",
    )
    monitor, store = make_monitor(
        tmp_path,
        FakeMailbox([rejection, interview]),
        coordinator,
    )
    connect(store)

    result = monitor.poll()
    application = coordinator.get("app-example-labs-42")

    assert result.processed == 2
    assert application.lifecycle_state == LifecycleState.REJECTED
    assert [event.classification for event in application.correspondence] == [
        "interview",
        "rejection",
    ]


def test_restart_reuses_staged_classification_after_application_transition(tmp_path):
    coordinator, _ = make_application(tmp_path)
    mailbox = FakeMailbox(
        [
            message(
                message_id="gmail-interview-recovery-1",
                subject="Interview invitation — AI Scientist REQ-42",
                body="We would like to invite you to interview for AI Scientist at Example Labs.",
            )
        ]
    )
    durable_store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    connect(durable_store)
    failing_store = FailFirstMessageCompletion(durable_store)
    first_monitor = CareerCorrespondenceMonitor(
        mailbox=mailbox,
        store=failing_store,
        applications=coordinator,
        clock=FixedClock(),
        candidate_name="Alex Example",
    )

    first = first_monitor.poll()
    restarted = CareerCorrespondenceMonitor(
        mailbox=mailbox,
        store=JsonCareerCorrespondenceStore(tmp_path / "career-correspondence"),
        applications=coordinator,
        clock=FixedClock(),
        candidate_name="Alex Example",
    )
    second = restarted.poll()
    application = coordinator.get("app-example-labs-42")

    assert first.failed == 1
    assert second.processed == 1
    assert second.ambiguous == 0
    assert application.lifecycle_state == LifecycleState.INTERVIEW
    assert [event.classification for event in application.correspondence] == [
        "interview"
    ]
    assert durable_store.classification_requests() == ()


def test_expired_message_claim_cannot_overwrite_new_owner(tmp_path):
    store = JsonCareerCorrespondenceStore(tmp_path / "career-correspondence")
    first = store.claim_message(
        "gmail-claim-1",
        claimed_at="2026-07-16T12:00:00+00:00",
    )
    busy = store.claim_message(
        "gmail-claim-1",
        claimed_at="2026-07-16T12:05:00+00:00",
    )
    replacement = store.claim_message(
        "gmail-claim-1",
        claimed_at="2026-07-16T12:11:00+00:00",
    )

    assert first.status == "claimed"
    assert busy.status == "busy"
    assert replacement.status == "claimed"
    assert replacement.token != first.token
    with pytest.raises(RuntimeError, match="no longer current"):
        store.complete_message(
            "gmail-claim-1",
            claim_token=first.token,
            completed_at="2026-07-16T12:12:00+00:00",
            application_id="app-example-labs-42",
            classification="receipt",
        )


def test_telegram_outbox_never_contains_mail_body_or_one_time_codes(tmp_path):
    coordinator, _ = make_application(tmp_path)
    sensitive = message(
        message_id="gmail-mfa-1",
        subject="Your verification code is 123456",
        body="Use 654321 to sign in. This code expires in ten minutes.",
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([sensitive]), coordinator)
    connect(store)

    monitor.poll()

    summary = store.classification_requests()[0].summary
    assert "123456" not in summary
    assert "654321" not in summary
    assert "expires in ten minutes" not in summary
    assert summary == (
        "Nuova corrispondenza nella casella carriera; "
        "apri il report locale per i dettagli."
    )


def test_telegram_outbox_uses_privacy_minimized_allowlisted_summary(tmp_path):
    coordinator, _ = make_application(tmp_path)
    sensitive = message(
        message_id="gmail-sensitive-subject-1",
        subject="SENSITIVE_HEALTH_MARKER salary CHF 175k token abc-def-secret REQ-42",
        body="Private body value",
    )
    sensitive = CareerMessage(
        **{
            **sensitive.__dict__,
            "sender_name": "Private Person",
            "sender_address": "private.person@example-labs.example",
        }
    )
    monitor, store = make_monitor(tmp_path, FakeMailbox([sensitive]), coordinator)
    connect(store)

    monitor.poll()

    summary = store.classification_requests()[0].summary
    assert summary == (
        "Nuova corrispondenza nella casella carriera; "
        "apri il report locale per i dettagli."
    )
    for private_value in (
        "SENSITIVE_HEALTH_MARKER",
        "CHF 175k",
        "abc-def-secret",
        "REQ-42",
        "Private Person",
        "private.person@example-labs.example",
        "Private body value",
    ):
        assert private_value not in summary
