"""Fail-closed monitoring of the explicitly connected career mailbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import Protocol
from urllib.parse import urlparse

from application_domain import (
    ApplicationSnapshot,
    CorrespondenceClassification,
    CorrespondenceEvent,
    CorrespondenceTrustEvidence,
    LifecycleState,
)
from career_correspondence_domain import (
    CareerDraft,
    CareerMailboxConnection,
    CareerMessage,
    CareerMessageClaim,
    CareerProcessingPlan,
    MailboxPollResult,
    MailboxPollStatus,
    MessageClaimStatus,
    SenderKind,
    TelegramClassificationRequest,
    TelegramDraftReviewRequest,
    TrustedDomain,
)


class CareerMailboxReader(Protocol):
    """Read-only Gmail boundary; intentionally exposes no send operation."""

    def fetch(self, *, account_address: str) -> tuple[CareerMessage, ...]: ...


class CorrespondenceStore(Protocol):
    def connection(self) -> CareerMailboxConnection | None: ...
    def claim_message(
        self, message_id: str, *, claimed_at: str
    ) -> CareerMessageClaim: ...
    def complete_message(
        self,
        message_id: str,
        *,
        claim_token: str,
        completed_at: str,
        application_id: str | None,
        classification: str,
        request: TelegramClassificationRequest | None = None,
        draft: CareerDraft | None = None,
        draft_review: TelegramDraftReviewRequest | None = None,
    ) -> None: ...
    def fail_message(
        self, message_id: str, *, claim_token: str, failed_at: str
    ) -> None: ...
    def stage_message_plan(
        self,
        message_id: str,
        *,
        claim_token: str,
        plan: CareerProcessingPlan,
    ) -> None: ...


class CorrespondenceApplications(Protocol):
    def list_applications(self) -> tuple[ApplicationSnapshot, ...]: ...
    def record_correspondence(
        self, event: CorrespondenceEvent
    ) -> ApplicationSnapshot: ...


class Clock(Protocol):
    def now(self): ...


@dataclass(frozen=True)
class _Decision:
    classification: CorrespondenceClassification
    application: ApplicationSnapshot | None
    reason: str
    sender_trust_evidence: CorrespondenceTrustEvidence | None = None


_RECEIPT_PHRASES = (
    "we have received your application",
    "your application has been received",
    "thank you for applying to",
)
_REJECTION_PHRASES = (
    "we will not be moving forward with your application",
    "we have decided not to move forward with your application",
    "we regret to inform you that your application",
)
_INTERVIEW_PHRASES = (
    "we would like to invite you to interview",
    "we'd like to invite you to interview",
    "schedule your interview",
)


class CareerCorrespondenceMonitor:
    """Classify new mail, stage human requests, and persist local drafts."""

    def __init__(
        self,
        *,
        mailbox: CareerMailboxReader,
        store: CorrespondenceStore,
        applications: CorrespondenceApplications,
        clock: Clock,
        candidate_name: str,
    ) -> None:
        if not candidate_name.strip():
            raise ValueError("Candidate name is required for career correspondence")
        self._mailbox = mailbox
        self._store = store
        self._applications = applications
        self._clock = clock
        self._candidate_name = candidate_name.strip()

    def poll(self) -> MailboxPollResult:
        connection = self._store.connection()
        if connection is None:
            return MailboxPollResult(MailboxPollStatus.UNCONFIGURED)

        processed = 0
        already_processed = 0
        ambiguous = 0
        drafted = 0
        failed = 0
        messages = sorted(
            self._mailbox.fetch(account_address=connection.address),
            key=lambda item: (datetime.fromisoformat(item.received_at), item.message_id),
        )
        for message in messages:
            now = self._clock.now().isoformat()
            claim = self._store.claim_message(
                message.message_id,
                claimed_at=now,
            )
            if claim.status in {
                MessageClaimStatus.COMPLETED,
                MessageClaimStatus.BUSY,
            }:
                already_processed += 1
                continue
            assert claim.token is not None
            try:
                plan = claim.plan
                if plan is None:
                    plan = self._processing_plan(message, created_at=now)
                    self._store.stage_message_plan(
                        message.message_id,
                        claim_token=claim.token,
                        plan=plan,
                    )

                if plan.event is not None:
                    self._applications.record_correspondence(plan.event)

                self._store.complete_message(
                    message.message_id,
                    claim_token=claim.token,
                    completed_at=now,
                    application_id=plan.application_id,
                    classification=plan.classification.value,
                    request=plan.request,
                    draft=plan.draft,
                    draft_review=plan.draft_review,
                )
                processed += 1
                if plan.request is not None:
                    ambiguous += 1
                if plan.draft is not None:
                    drafted += 1
            except Exception:
                self._store.fail_message(
                    message.message_id,
                    claim_token=claim.token,
                    failed_at=now,
                )
                failed += 1

        return MailboxPollResult(
            MailboxPollStatus.COMPLETED,
            processed=processed,
            already_processed=already_processed,
            ambiguous=ambiguous,
            drafted=drafted,
            failed=failed,
        )

    def _processing_plan(
        self, message: CareerMessage, *, created_at: str
    ) -> CareerProcessingPlan:
        decision = self._classify(
            message, self._applications.list_applications()
        )
        application_id = (
            None
            if decision.application is None
            else decision.application.application_id
        )
        request = None
        draft = None
        draft_review = None
        if decision.classification == CorrespondenceClassification.AMBIGUOUS:
            request = self._classification_request(
                message,
                application_id=application_id,
                reason=decision.reason,
                created_at=created_at,
            )
        elif decision.classification in {
            CorrespondenceClassification.RECRUITER,
            CorrespondenceClassification.HIRING_MANAGER,
            CorrespondenceClassification.REFERRAL,
        }:
            assert decision.application is not None
            draft = self._draft(
                message,
                decision.application,
                decision.classification,
                created_at=created_at,
            )
            draft_review = self._draft_review_request(
                message,
                draft,
                created_at=created_at,
            )
        event = (
            None
            if decision.application is None
            else self._event(
                message,
                decision,
                recorded_at=created_at,
                request=request,
                draft=draft,
            )
        )
        return CareerProcessingPlan(
            application_id=application_id,
            classification=decision.classification,
            event=event,
            request=request,
            draft=draft,
            draft_review=draft_review,
        )

    @staticmethod
    def _classify(
        message: CareerMessage,
        applications: tuple[ApplicationSnapshot, ...],
    ) -> _Decision:
        application = _link_application(message, applications)
        text = _normalized_text(f"{message.subject}\n{message.body_text}")
        if not message.authenticated_sender:
            return _Decision(
                CorrespondenceClassification.AMBIGUOUS,
                application,
                "sender_not_authenticated",
            )
        signals = []
        if any(phrase in text for phrase in _RECEIPT_PHRASES):
            signals.append(CorrespondenceClassification.RECEIPT)
        if any(phrase in text for phrase in _REJECTION_PHRASES):
            signals.append(CorrespondenceClassification.REJECTION)
        if any(phrase in text for phrase in _INTERVIEW_PHRASES):
            signals.append(CorrespondenceClassification.INTERVIEW)

        if application is None:
            return _Decision(
                CorrespondenceClassification.AMBIGUOUS,
                None,
                "application_not_linked_unambiguously",
            )
        sender_trust_evidence = _sender_trust_evidence_for_application(
            message, application
        )
        if sender_trust_evidence is None:
            return _Decision(
                CorrespondenceClassification.AMBIGUOUS,
                application,
                "sender_not_trusted_for_application",
            )
        latest_lifecycle = max(
            datetime.fromisoformat(event.occurred_at)
            for event in application.history
        )
        if datetime.fromisoformat(message.received_at) < latest_lifecycle:
            return _Decision(
                CorrespondenceClassification.AMBIGUOUS,
                application,
                "message_predates_current_lifecycle",
            )
        if len(signals) > 1:
            return _Decision(
                CorrespondenceClassification.AMBIGUOUS,
                application,
                "conflicting_deterministic_signals",
            )
        if len(signals) == 1:
            signal = signals[0]
            if signal == CorrespondenceClassification.INTERVIEW and (
                application.lifecycle_state != LifecycleState.SUBMITTED
            ):
                return _Decision(
                    CorrespondenceClassification.AMBIGUOUS,
                    application,
                    "lifecycle_transition_not_safe",
                )
            if signal == CorrespondenceClassification.REJECTION and (
                application.lifecycle_state
                not in {LifecycleState.SUBMITTED, LifecycleState.INTERVIEW}
            ):
                return _Decision(
                    CorrespondenceClassification.AMBIGUOUS,
                    application,
                    "lifecycle_transition_not_safe",
                )
            return _Decision(
                signal,
                application,
                "deterministic_template",
                sender_trust_evidence,
            )

        people_kind = {
            SenderKind.RECRUITER: CorrespondenceClassification.RECRUITER,
            SenderKind.HIRING_MANAGER: CorrespondenceClassification.HIRING_MANAGER,
            SenderKind.REFERRAL: CorrespondenceClassification.REFERRAL,
        }.get(message.sender_kind)
        if people_kind is None:
            if "talent acquisition" in text or "i am a recruiter" in text:
                people_kind = CorrespondenceClassification.RECRUITER
            elif "hiring manager" in text:
                people_kind = CorrespondenceClassification.HIRING_MANAGER
            elif "referral" in text or "refer you" in text:
                people_kind = CorrespondenceClassification.REFERRAL
        if people_kind is not None:
            return _Decision(
                people_kind,
                application,
                "known_sender_kind",
                sender_trust_evidence,
            )
        return _Decision(
            CorrespondenceClassification.AMBIGUOUS,
            application,
            "message_not_deterministic",
        )

    @staticmethod
    def _classification_request(
        message: CareerMessage,
        *,
        application_id: str | None,
        reason: str,
        created_at: str,
    ) -> TelegramClassificationRequest:
        request_id = _stable_id("classification", message.message_id)
        return TelegramClassificationRequest(
            request_id=request_id,
            message_id=message.message_id,
            application_id=application_id,
            reason=reason,
            summary=_telegram_summary(message),
            created_at=created_at,
        )

    def _draft(
        self,
        message: CareerMessage,
        application: ApplicationSnapshot,
        classification: CorrespondenceClassification,
        *,
        created_at: str,
    ) -> CareerDraft:
        company = str(application.opportunity.get("company", "the company"))
        title = str(application.opportunity.get("title", "the role"))
        greeting = message.sender_name.strip() or "there"
        if classification == CorrespondenceClassification.REFERRAL:
            subject = f"Referral request — {title} at {company}"
            body = (
                f"Hi {greeting},\n\nI am interested in the {title} role at "
                f"{company}. Would you feel comfortable referring me? I can "
                "share the tailored CV and role link for your review.\n\n"
                f"Best,\n{self._candidate_name}"
            )
        else:
            subject = f"Re: {message.subject.strip()}"
            body = (
                f"Dear {greeting},\n\nThank you for reaching out about the "
                f"{title} opportunity at {company}. I would be happy to discuss "
                "the role and how my experience may help the team.\n\n"
                f"Best,\n{self._candidate_name}"
            )
        return CareerDraft(
            draft_id=_stable_id("draft", message.message_id, classification.value),
            message_id=message.message_id,
            application_id=application.application_id,
            kind=classification.value,
            summary=_local_summary(message),
            subject=subject,
            body=body,
            created_at=created_at,
        )

    @staticmethod
    def _draft_review_request(
        message: CareerMessage,
        draft: CareerDraft,
        *,
        created_at: str,
    ) -> TelegramDraftReviewRequest:
        return TelegramDraftReviewRequest(
            request_id=_stable_id("draft-review", draft.draft_id),
            draft_id=draft.draft_id,
            message_id=message.message_id,
            application_id=draft.application_id,
            summary=_telegram_summary(message),
            created_at=created_at,
        )

    @staticmethod
    def _event(
        message: CareerMessage,
        decision: _Decision,
        *,
        recorded_at: str,
        request: TelegramClassificationRequest | None,
        draft: CareerDraft | None,
    ) -> CorrespondenceEvent:
        assert decision.application is not None
        evidence_role = (
            "application_receipt_only"
            if decision.classification == CorrespondenceClassification.RECEIPT
            else None
        )
        return CorrespondenceEvent(
            event_id=_stable_id(
                "event",
                message.message_id,
                decision.application.application_id,
            ),
            application_id=decision.application.application_id,
            message_id=message.message_id,
            thread_id=message.thread_id,
            classification=decision.classification,
            sender=message.sender_address,
            subject=message.subject,
            received_at=message.received_at,
            recorded_at=recorded_at,
            summary=_local_summary(message),
            sender_trust_evidence=decision.sender_trust_evidence,
            evidence_role=evidence_role,
            draft_id=None if draft is None else draft.draft_id,
            classification_request_id=(
                None if request is None else request.request_id
            ),
        )


def _link_application(
    message: CareerMessage,
    applications: tuple[ApplicationSnapshot, ...],
) -> ApplicationSnapshot | None:
    text = _normalized_text(f"{message.subject}\n{message.body_text}")
    strong = []
    for application in applications:
        markers = _application_markers(application)
        if any(_normalized_text(marker) in text for marker in markers):
            strong.append(application)
    if len(strong) == 1:
        return strong[0]
    if strong:
        return None

    contextual = []
    for application in applications:
        company = _normalized_text(
            str(application.opportunity.get("company", ""))
        )
        title = _normalized_text(str(application.opportunity.get("title", "")))
        if company and title and company in text and title in text:
            contextual.append(application)
    return contextual[0] if len(contextual) == 1 else None


def _application_markers(application: ApplicationSnapshot) -> tuple[str, ...]:
    values = [
        application.application_id,
        str(application.opportunity.get("stable_id", "")),
        str(application.opportunity.get("official_job_id", "")),
    ]
    if application.outcome is not None:
        values.append(application.outcome.confirmation_id or "")
        if application.outcome.evidence is not None:
            values.append(application.outcome.evidence.ats_application_id or "")
    return tuple(value for value in values if len(value.strip()) >= 4)


def _sender_trust_evidence_for_application(
    message: CareerMessage, application: ApplicationSnapshot
) -> CorrespondenceTrustEvidence | None:
    authenticated_domain = TrustedDomain.try_from(message.authenticated_domain or "")
    sender_domain = _address_domain(message.sender_address)
    if authenticated_domain is None or not authenticated_domain.matches(sender_domain):
        return None

    trusted = {
        domain
        for key in ("trusted_correspondence_domains", "trusted_ats_domains")
        for value in application.opportunity.get(key, ())
        if (domain := TrustedDomain.try_from(value)) is not None
    }
    # Discovery aggregators are not authoritative lifecycle senders. Only the
    # employer's official site and the configured ATS may establish trust.
    for key in ("official_url", "ats_url"):
        hostname = (urlparse(str(application.opportunity.get(key, ""))).hostname or "")
        domain = TrustedDomain.try_from(hostname)
        if domain is not None:
            trusted.add(domain)
    if any(domain.matches(sender_domain) for domain in trusted):
        return CorrespondenceTrustEvidence.CONFIGURED_DOMAIN
    trusted_thread = any(
        event.thread_id == message.thread_id
        and event.sender_trust_evidence is not None
        and event.classification != CorrespondenceClassification.AMBIGUOUS
        and (domain := TrustedDomain.try_from(_address_domain(event.sender))) is not None
        and domain.matches(sender_domain)
        for event in application.correspondence
    )
    return (
        CorrespondenceTrustEvidence.TRUSTED_THREAD
        if trusted_thread
        else None
    )


def _address_domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().casefold().rstrip(".")


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _local_summary(message: CareerMessage) -> str:
    sender = message.sender_name.strip() or message.sender_address
    subject = re.sub(r"\s+", " ", message.subject).strip()
    body = re.sub(r"\s+", " ", message.body_text).strip()
    return (
        f"From {sender} <{message.sender_address}>: {subject} — {body[:240]}"
    )[:500]


def _telegram_summary(message: CareerMessage) -> str:
    del message
    return (
        "Nuova corrispondenza nella casella carriera; "
        "apri il report locale per i dettagli."
    )


def _stable_id(*parts: str) -> str:
    value = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "CareerCorrespondenceMonitor",
    "CareerMailboxConnection",
    "CareerMessage",
    "MailboxPollResult",
    "MailboxPollStatus",
    "SenderKind",
]
