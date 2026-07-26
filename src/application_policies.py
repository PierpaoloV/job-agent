"""Cohesive freshness, capacity, and reopening policies for applications."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from application_domain import (
    ApplicationSnapshot,
    LifecycleState,
    MaterialRoleFingerprint,
    OperationalStatus,
    PreparationCapacityException,
    PreparationCapacityExceptionKind,
    PreparationReminder,
    PreparationReminderPriority,
    PriorApplicationEvidence,
    WorkflowAction,
)
from role_identity import role_identity_aliases


def application_deadline(opportunity: Mapping[str, Any]) -> str | None:
    value = opportunity.get("application_deadline", opportunity.get("deadline"))
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if len(raw) != 10:
            return None
        parsed = parsed.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    return parsed.isoformat()


@dataclass(frozen=True)
class DeadlineCapacityPolicy:
    """Admit only a matching deadline in the current 72-hour window."""

    horizon: timedelta = timedelta(hours=72)

    def applies(
        self,
        exception: PreparationCapacityException,
        opportunity: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        deadline_at = application_deadline(opportunity)
        if deadline_at is None or exception.deadline_at is None:
            return False
        deadline = datetime.fromisoformat(deadline_at)
        configured = datetime.fromisoformat(exception.deadline_at)
        return configured == deadline and now <= deadline <= now + self.horizon


@dataclass(frozen=True)
class PreparationCapacityPolicy:
    """Own capacity admission and current exception qualification."""

    active_limit: int = 5
    deadline_policy: DeadlineCapacityPolicy = DeadlineCapacityPolicy()

    def __post_init__(self) -> None:
        if not 4 <= self.active_limit <= 6:
            raise ValueError("Active preparation limit must be between 4 and 6")

    def exception_applies(
        self,
        exception: PreparationCapacityException,
        opportunity: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        if exception.kind == PreparationCapacityExceptionKind.TOP_TIER:
            value = opportunity.get("top_tier", False)
            if isinstance(value, Mapping):
                return value.get("value") is True
            return value is True
        return self.deadline_policy.applies(exception, opportunity, now)

    def revoke_invalid_exception(
        self, application: ApplicationSnapshot, now: datetime
    ) -> ApplicationSnapshot:
        exception = application.capacity_exception
        if exception is None or self.exception_applies(
            exception, application.opportunity, now
        ):
            return application
        return replace(application, capacity_exception=None)

    def can_admit(
        self,
        candidate: ApplicationSnapshot,
        applications: tuple[ApplicationSnapshot, ...],
        now: datetime,
    ) -> bool:
        exception = candidate.capacity_exception
        if exception is not None and self.exception_applies(
            exception, candidate.opportunity, now
        ):
            return True
        active = 0
        for stored in applications:
            application = (
                candidate
                if stored.application_id == candidate.application_id
                else stored
            )
            if application.outcome is not None:
                continue
            has_prepared_artifacts = (
                application.artifacts is not None
                and application.operational_status
                != OperationalStatus.EXPIRED_PREPARATION
            )
            has_preparation_intent = any(
                intent.action == WorkflowAction.PREPARE and intent.is_pending
                for intent in application.operation_intents
            )
            if has_prepared_artifacts or has_preparation_intent:
                active += 1
        return active < self.active_limit


@dataclass(frozen=True)
class ReopenedRolePolicy:
    """Detect prior applications and explain normalized material changes."""

    def prior_evidence(
        self,
        opportunity: Mapping[str, Any],
        applications: tuple[ApplicationSnapshot, ...],
    ) -> tuple[PriorApplicationEvidence, ...]:
        identities = role_identity_aliases(opportunity)
        if not identities:
            return ()
        current_fingerprint = MaterialRoleFingerprint.from_opportunity(opportunity)
        evidence = []
        for prior in applications:
            if not identities.intersection(role_identity_aliases(prior.opportunity)):
                continue
            changes = current_fingerprint.changes_from(
                MaterialRoleFingerprint.from_opportunity(prior.opportunity)
            )
            evidence.append(
                PriorApplicationEvidence(
                    application_id=prior.application_id,
                    lifecycle_state=prior.lifecycle_state,
                    opportunity_version=prior.opportunity_version,
                    recorded_at=prior.history[-1].occurred_at,
                    material_changes=changes,
                )
            )
        return tuple(sorted(evidence, key=lambda item: item.recorded_at, reverse=True))

    @staticmethod
    def blocks_unchanged_reopen(evidence: tuple[PriorApplicationEvidence, ...]) -> bool:
        return bool(
            evidence
            and evidence[0].lifecycle_state
            in {LifecycleState.REJECTED, LifecycleState.DISCARDED}
            and not evidence[0].material_changes
        )


@dataclass(frozen=True)
class PreparationFreshnessPolicy:
    """Own preparation expiry and durable 48-hour reminder creation."""

    preparation_ttl: timedelta = timedelta(hours=72)
    reminder_after: timedelta = timedelta(hours=48)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.reminder_after < self.preparation_ttl:
            raise ValueError("Preparation reminder must precede preparation expiry")

    def expire(
        self, application: ApplicationSnapshot, now: datetime
    ) -> ApplicationSnapshot:
        if application.artifacts_expires_at is None:
            return application
        if now < datetime.fromisoformat(application.artifacts_expires_at):
            return application
        if application.lifecycle_state == LifecycleState.SUBMITTED:
            return application
        return replace(
            application,
            authorization_version=application.opportunity_version,
            manifest=None,
            operation_intents=tuple(
                intent.cancel(now.isoformat())
                if intent.action == WorkflowAction.FILL and intent.is_pending
                else intent
                for intent in application.operation_intents
            ),
            operational_status=OperationalStatus.EXPIRED_PREPARATION,
        )

    def due_reminder(
        self, application: ApplicationSnapshot, now: datetime
    ) -> tuple[ApplicationSnapshot, PreparationReminder | None]:
        expires_at = application.artifacts_expires_at
        if expires_at is None or (
            application.operational_status == OperationalStatus.EXPIRED_PREPARATION
        ):
            return application, None
        expiry = datetime.fromisoformat(expires_at)
        reminder_at = expiry - (self.preparation_ttl - self.reminder_after)
        if now < reminder_at:
            return application, None
        reminder_id = f"preparation-reminder:{application.application_id}:{expires_at}"
        existing = next(
            (
                item
                for item in application.preparation_reminders
                if item.reminder_id == reminder_id
            ),
            None,
        )
        if existing is not None:
            return application, existing if existing.delivered_at is None else None
        deadline_at = application_deadline(application.opportunity)
        deadline = None if deadline_at is None else datetime.fromisoformat(deadline_at)
        reminder = PreparationReminder(
            reminder_id=reminder_id,
            application_id=application.application_id,
            emitted_at=now.isoformat(),
            preparation_expires_at=expires_at,
            priority=(
                PreparationReminderPriority.DEADLINE
                if deadline is not None and deadline <= expiry
                else PreparationReminderPriority.NORMAL
            ),
            deadline_at=deadline_at,
        )
        return (
            replace(
                application,
                preparation_reminders=application.preparation_reminders + (reminder,),
            ),
            reminder,
        )
