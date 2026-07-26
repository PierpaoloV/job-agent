"""Small public coordinator seam for the human-gated application workflow."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
import secrets
from typing import Any, Callable, ContextManager, Mapping, Protocol

from application_domain import (
    ActionCommand,
    ApplicationSnapshot,
    ApprovalRecord,
    AuthorizationRecord,
    AuthorizationScope,
    CommandResult,
    CommandStatus,
    CorrespondenceClassification,
    CorrespondenceEvent,
    FilledApplication,
    LifecycleEvent,
    LifecycleState,
    OperationIntent,
    OperationalStatus,
    OfficialVacancy,
    PreSubmitManifest,
    PreparationCapacityException,
    PreparationReminder,
    PreparedArtifacts,
    PriorApplicationEvidence,
    SubmissionIntent,
    SubmissionOutcome,
    SubmissionStatus,
    WorkflowAction,
)
from application_storage import JsonApplicationStore, MarkdownApplicationReportWriter
from application_telegram import TelegramCommandHandler
from application_interventions import (
    BrowserInterventionRequired,
    InterventionContinuation,
    InterventionContinuationKind,
    InterventionRecord,
    SubmissionInspection,
    SubmissionInspectionSource,
    SubmissionInspectionStatus,
    UncertainSubmissionRecord,
)
from application_policies import (
    PreparationCapacityPolicy,
    PreparationFreshnessPolicy,
    ReopenedRolePolicy,
)
from hosted_tailoring import (
    HostedPreparationFailed,
    HostedPreparationPending,
    HostedPreparationResolution,
    HostedPreparationResolutionRequired,
)


class ApplicationStore(Protocol):
    def load(self, application_id: str) -> ApplicationSnapshot: ...
    def save(self, application: ApplicationSnapshot) -> None: ...
    def transact(
        self,
        application_id: str,
        operation: Callable[[ApplicationSnapshot], ApplicationSnapshot],
    ) -> ApplicationSnapshot: ...
    def list(self) -> tuple[ApplicationSnapshot, ...]: ...
    def capacity_lock(self) -> ContextManager[None]: ...


class TailoringAdapter(Protocol):
    """Prepare artifacts idempotently for a durable ``intent_id``."""

    def prepare(
        self,
        application_id: str,
        intent_id: str,
        opportunity: Mapping[str, Any],
        official_vacancy: OfficialVacancy,
    ) -> PreparedArtifacts: ...
    def reload_master_cv(self) -> str: ...
    def verify_artifacts(self, artifacts: PreparedArtifacts) -> bool: ...
    def preparation_resolution(
        self,
        application_id: str,
        intent_id: str,
        official_vacancy: OfficialVacancy,
    ) -> HostedPreparationResolution | None: ...


class OfficialVacancyAdapter(Protocol):
    def retrieve(self, opportunity: Mapping[str, Any]) -> OfficialVacancy: ...
    def revalidate(
        self, opportunity: Mapping[str, Any], previous: OfficialVacancy
    ) -> OfficialVacancy: ...


class AtsAdapter(Protocol):
    """Fill idempotently for an intent; submit at most once per manifest."""

    def fill(
        self, application_id: str, intent_id: str, artifacts: PreparedArtifacts
    ) -> FilledApplication: ...
    def submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionOutcome: ...
    def validate_submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> bool: ...
    def intervention_is_resolved(
        self, application_id: str, intervention: InterventionRecord
    ) -> bool: ...
    def inspect_submission(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionInspection: ...


class ReportWriter(Protocol):
    def write(self, application: ApplicationSnapshot) -> Path: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class _ActionPolicy:
    transition: LifecycleState | None
    intent_prefix: str
    handler_name: str


_ACTION_POLICIES = {
    WorkflowAction.PREPARE: _ActionPolicy(
        LifecycleState.APPROVED, "prepare", "_prepare"
    ),
    WorkflowAction.FILL: _ActionPolicy(LifecycleState.FILLING, "fill", "_fill"),
    WorkflowAction.SUBMIT: _ActionPolicy(None, "submit", "_submit"),
}


class ApplicationWorkflowCoordinator:
    def __init__(
        self,
        *,
        store: ApplicationStore,
        tailoring: TailoringAdapter,
        ats: AtsAdapter,
        report_writer: ReportWriter,
        official_vacancies: OfficialVacancyAdapter,
        clock: Clock,
        token_factory=None,
        active_preparation_limit: int = 5,
    ) -> None:
        self._store = store
        self._tailoring = tailoring
        self._ats = ats
        self._report_writer = report_writer
        self._official_vacancies = official_vacancies
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._capacity_policy = PreparationCapacityPolicy(active_preparation_limit)
        self._freshness_policy = PreparationFreshnessPolicy()
        self._reopened_role_policy = ReopenedRolePolicy()

    def propose(
        self,
        *,
        application_id: str,
        opportunity: Mapping[str, Any],
        version: str,
        capacity_exception: PreparationCapacityException | None = None,
    ) -> ApplicationSnapshot:
        try:
            self._store.load(application_id)
        except KeyError:
            pass
        else:
            raise ValueError(f"Application already exists: {application_id}")
        now = self._now_iso()
        if (
            capacity_exception is not None
            and not self._capacity_policy.exception_applies(
                capacity_exception, opportunity, self._clock.now()
            )
        ):
            raise ValueError(
                "Capacity exception does not match the top-tier/deadline policy"
            )
        prior_applications = self._reopened_role_policy.prior_evidence(
            opportunity, self._store.list()
        )
        if self._reopened_role_policy.blocks_unchanged_reopen(prior_applications):
            raise ValueError(
                "Reopened role has no material changes from the prior application"
            )
        application = ApplicationSnapshot(
            application_id=application_id,
            opportunity=dict(opportunity),
            opportunity_version=version,
            lifecycle_state=LifecycleState.PROPOSED,
            authorization_version=version,
            history=(LifecycleEvent(LifecycleState.PROPOSED, now),),
            capacity_exception=capacity_exception,
            prior_applications=prior_applications,
        )
        self._persist(application)
        return application

    def get(self, application_id: str) -> ApplicationSnapshot:
        self._recover_package_publication(application_id)

        def refresh(application: ApplicationSnapshot) -> ApplicationSnapshot:
            return self._refresh_prior_application_evidence(
                self._expire_preparation(application)
            )

        return self._transact_and_publish(application_id, refresh)

    def _refresh_prior_application_evidence(
        self, application: ApplicationSnapshot
    ) -> ApplicationSnapshot:
        refreshed = []
        for evidence in application.prior_applications:
            try:
                prior = self._store.load(evidence.application_id)
            except KeyError:
                refreshed.append(evidence)
                continue
            refreshed.append(
                replace(
                    evidence,
                    lifecycle_state=prior.lifecycle_state,
                    recorded_at=prior.history[-1].occurred_at,
                )
            )
        values = tuple(refreshed)
        if values == application.prior_applications:
            return application
        return replace(application, prior_applications=values)

    def list_applications(self) -> tuple[ApplicationSnapshot, ...]:
        """Return recovered snapshots for deterministic correspondence linking."""

        return tuple(self.get(item.application_id) for item in self._store.list())

    def emit_due_preparation_reminders(self) -> tuple[PreparationReminder, ...]:
        """Persist and return reminders that became due in this call."""

        emitted: list[PreparationReminder] = []
        for existing in self._store.list():
            pending = None

            def emit(application: ApplicationSnapshot) -> ApplicationSnapshot:
                nonlocal pending
                application = self._expire_preparation(application)
                application, pending = self._freshness_policy.due_reminder(
                    application, self._clock.now()
                )
                return application

            self._transact_and_publish(existing.application_id, emit)
            if pending is not None:
                emitted.append(pending)
        return tuple(emitted)

    def acknowledge_preparation_reminder(
        self, application_id: str, reminder_id: str
    ) -> PreparationReminder:
        acknowledged = None

        def acknowledge(application: ApplicationSnapshot) -> ApplicationSnapshot:
            nonlocal acknowledged
            reminder = next(
                (
                    item
                    for item in application.preparation_reminders
                    if item.reminder_id == reminder_id
                ),
                None,
            )
            if reminder is None:
                raise ValueError("Unknown preparation reminder")
            acknowledged = (
                reminder
                if reminder.delivered_at is not None
                else replace(reminder, delivered_at=self._now_iso())
            )
            return replace(
                application,
                preparation_reminders=tuple(
                    acknowledged if item.reminder_id == reminder_id else item
                    for item in application.preparation_reminders
                ),
            )

        self._transact_and_publish(application_id, acknowledge)
        assert acknowledged is not None
        return acknowledged

    def record_correspondence(self, event: CorrespondenceEvent) -> ApplicationSnapshot:
        """Append one idempotent mail event and apply its narrow lifecycle policy."""

        if event.previous_state is not None or event.resulting_state is not None:
            raise ValueError("Correspondence lifecycle is assigned by the coordinator")

        def apply(application: ApplicationSnapshot) -> ApplicationSnapshot:
            if application.application_id != event.application_id:
                raise ValueError("Correspondence application does not match")
            if any(
                item.event_id == event.event_id for item in application.correspondence
            ):
                return application

            previous = application.lifecycle_state
            resulting = previous
            if event.classification == CorrespondenceClassification.INTERVIEW:
                if previous != LifecycleState.SUBMITTED:
                    raise ValueError("Interview mail cannot advance this lifecycle")
                resulting = LifecycleState.INTERVIEW
            elif event.classification == CorrespondenceClassification.REJECTION:
                if previous not in {
                    LifecycleState.SUBMITTED,
                    LifecycleState.INTERVIEW,
                }:
                    raise ValueError("Rejection mail cannot advance this lifecycle")
                resulting = LifecycleState.REJECTED

            recorded = replace(
                event,
                previous_state=previous,
                resulting_state=resulting,
            )
            history = application.history
            if resulting != previous:
                history += (LifecycleEvent(resulting, event.received_at),)
            return replace(
                application,
                lifecycle_state=resulting,
                history=history,
                correspondence=application.correspondence + (recorded,),
            )

        return self._transact_and_publish(event.application_id, apply)

    def command_for_token(self, token: str) -> ActionCommand | None:
        for application in self._store.list():
            for authorization in application.authorizations:
                if authorization.token == token:
                    return ActionCommand(authorization.token, authorization.scope)
        return None

    def issue_authorization(
        self,
        application_id: str,
        action: WorkflowAction,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> ActionCommand:
        if action not in _ACTION_POLICIES:
            raise ValueError(
                "Human-control authorizations are issued by the coordinator"
            )
        if ttl <= timedelta(0):
            raise ValueError("Authorization TTL must be positive")
        now = self._clock.now()
        issued = None
        capacity_reached = False

        def add(application: ApplicationSnapshot) -> ApplicationSnapshot:
            nonlocal issued, capacity_reached
            application = self._expire_preparation(application)
            if (
                action == WorkflowAction.PREPARE
                and application.operational_status
                == OperationalStatus.PREPARATION_FAILED
            ):
                raise ValueError(
                    "Failed preparation requires the scoped retry resolution"
                )
            if application.next_action != action:
                raise ValueError(f"Next valid action is {application.next_action}")
            if action == WorkflowAction.SUBMIT:
                active_prior = self._known_active_prior(application)
                if active_prior is not None:
                    raise ValueError(
                        "Known active ATS application "
                        f"{active_prior.application_id} "
                        f"({active_prior.lifecycle_state.value}); "
                        "duplicate submission authorization refused"
                    )
            if action == WorkflowAction.PREPARE:
                application = self._capacity_policy.revoke_invalid_exception(
                    application, now
                )
                if not self._preparation_capacity_available(application):
                    capacity_reached = True
                    return application
            scope = AuthorizationScope(
                application_id, action, application.authorization_version
            )
            issued = AuthorizationRecord(
                token=str(self._token_factory()),
                scope=scope,
                actor=actor,
                issued_at=now.isoformat(),
                expires_at=(now + ttl).isoformat(),
            )
            return replace(
                application,
                authorizations=application.authorizations + (issued,),
            )

        self._transact_and_publish(application_id, add)
        if capacity_reached:
            raise ValueError(
                "Active preparation capacity of "
                f"{self._capacity_policy.active_limit} reached"
            )
        assert issued is not None
        return ActionCommand(issued.token, issued.scope)

    def preparation_resolution_ids(self) -> tuple[str, ...]:
        """List failed preparations that still have their original intent."""

        return tuple(
            application.application_id
            for application in self._store.list()
            if application.operational_status
            == OperationalStatus.PREPARATION_FAILED
            and self._pending_preparation_intent(application) is not None
        )

    def preparation_resolution(
        self, application_id: str
    ) -> HostedPreparationResolution | None:
        """Return fresh hosted evidence for the exact failed preparation."""

        application = self.get(application_id)
        return self._inspect_preparation_resolution(application)

    def _inspect_preparation_resolution(
        self, application: ApplicationSnapshot
    ) -> HostedPreparationResolution | None:
        intent = self._pending_preparation_intent(application)
        vacancy = application.official_vacancy
        if (
            application.operational_status
            != OperationalStatus.PREPARATION_FAILED
            or intent is None
            or vacancy is None
        ):
            return None
        inspect = getattr(self._tailoring, "preparation_resolution", None)
        if not callable(inspect):
            return None
        try:
            return inspect(
                application.application_id,
                intent.intent_id,
                vacancy,
            )
        except Exception:
            return None

    def issue_preparation_retry_authorization(
        self,
        application_id: str,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> ActionCommand:
        """Offer a one-use retry only after a fresh, exact GitHub check."""

        if ttl <= timedelta(0):
            raise ValueError("Authorization TTL must be positive")
        resolution = self.preparation_resolution(application_id)
        if resolution is None or not resolution.retry_safe:
            raise ValueError("Hosted preparation retry is not proven safe")
        now = self._clock.now()
        issued = None

        def add(application: ApplicationSnapshot) -> ApplicationSnapshot:
            nonlocal issued
            intent = self._pending_preparation_intent(application)
            if (
                application.operational_status
                != OperationalStatus.PREPARATION_FAILED
                or intent is None
                or intent.intent_id != resolution.intent_id
            ):
                raise ValueError("Failed preparation changed before retry offer")
            scope = AuthorizationScope(
                application_id,
                WorkflowAction.RETRY_PREPARATION,
                intent.intent_id,
            )
            issued = AuthorizationRecord(
                token=str(self._token_factory()),
                scope=scope,
                actor=actor,
                issued_at=now.isoformat(),
                expires_at=(now + ttl).isoformat(),
            )
            return replace(
                application,
                authorizations=application.authorizations + (issued,),
            )

        self._transact_and_publish(application_id, add)
        assert issued is not None
        return ActionCommand(issued.token, issued.scope)

    def _preparation_capacity_available(self, candidate: ApplicationSnapshot) -> bool:
        return self._capacity_policy.can_admit(
            candidate, self._store.list(), self._clock.now()
        )

    def _known_active_prior(
        self, application: ApplicationSnapshot
    ) -> PriorApplicationEvidence | None:
        for evidence in application.prior_applications:
            try:
                prior = self._store.load(evidence.application_id)
            except KeyError:
                if evidence.is_active:
                    return evidence
                continue
            if prior.lifecycle_state in {
                LifecycleState.SUBMITTED,
                LifecycleState.INTERVIEW,
            }:
                return replace(
                    evidence,
                    lifecycle_state=prior.lifecycle_state,
                    recorded_at=prior.history[-1].occurred_at,
                )
        return None

    def handle(self, command: ActionCommand) -> CommandResult:
        self._recover_package_publication(command.scope.application_id)
        claim_status = CommandStatus.ACCEPTED
        accepted = None
        continuation = None

        def claim(application: ApplicationSnapshot) -> ApplicationSnapshot:
            nonlocal claim_status, accepted, continuation
            application = self._expire_preparation(application)
            authorization = next(
                (
                    item
                    for item in application.authorizations
                    if item.token == command.token
                ),
                None,
            )
            if authorization is None:
                claim_status = CommandStatus.MISMATCHED
                return application
            if authorization.consumed_at is not None:
                claim_status = CommandStatus.REPLAYED
                return application
            if authorization.invalidated_at is not None:
                claim_status = CommandStatus.STALE
                return application
            if authorization.scope != command.scope:
                claim_status = CommandStatus.MISMATCHED
                return application
            if self._clock.now() >= datetime.fromisoformat(authorization.expires_at):
                intervention = application.intervention
                if (
                    authorization.scope.action == WorkflowAction.RESUME
                    and intervention is not None
                    and authorization.token == intervention.resume_token
                    and authorization.scope.version == intervention.intervention_id
                ):
                    application = self._reissue_resume(application, authorization)
                    claim_status = CommandStatus.INTERVENTION_REQUIRED
                    return application
                uncertain = application.uncertain_submission
                if (
                    authorization.scope.action == WorkflowAction.RESOLVE_NOT_SUBMITTED
                    and uncertain is not None
                    and authorization.token == uncertain.resolution_token
                    and authorization.scope.version == uncertain.version
                ):
                    application = self._reissue_uncertain_resolution(
                        application, authorization
                    )
                    claim_status = CommandStatus.UNCERTAIN
                    return application
                claim_status = CommandStatus.EXPIRED
                return application
            if authorization.scope.action == WorkflowAction.RESUME:
                intervention = application.intervention
                if (
                    intervention is None
                    or authorization.scope.version != intervention.intervention_id
                    or authorization.token != intervention.resume_token
                ):
                    claim_status = CommandStatus.STALE
                    return application
                if not self._intervention_is_resolved(application, intervention):
                    claim_status = CommandStatus.INTERVENTION_REQUIRED
                    return application
                accepted = authorization
                continuation = intervention
                return self._accept_control_authorization(application, authorization)
            if authorization.scope.action == WorkflowAction.RESOLVE_NOT_SUBMITTED:
                uncertain = application.uncertain_submission
                if (
                    uncertain is None
                    or authorization.scope.version != uncertain.version
                    or authorization.token != uncertain.resolution_token
                ):
                    claim_status = CommandStatus.STALE
                    return application
                accepted = authorization
                return self._resolve_not_submitted(application, authorization)
            if (
                application.operational_status == OperationalStatus.EXPIRED_PREPARATION
                and authorization.scope.action != WorkflowAction.PREPARE
            ):
                claim_status = CommandStatus.EXPIRED
                return application
            if authorization.scope.action == WorkflowAction.RETRY_PREPARATION:
                intent = self._pending_preparation_intent(application)
                resolution = self._inspect_preparation_resolution(application)
                if (
                    application.operational_status
                    != OperationalStatus.PREPARATION_FAILED
                    or intent is None
                    or authorization.scope.version != intent.intent_id
                    or resolution is None
                    or resolution.intent_id != intent.intent_id
                    or not resolution.retry_safe
                ):
                    claim_status = CommandStatus.RECONCILIATION_REQUIRED
                    return application
                accepted = authorization
                return self._accept_preparation_retry(application, authorization)
            if authorization.scope.action == WorkflowAction.PREPARE:
                if (
                    application.operational_status
                    == OperationalStatus.PREPARATION_FAILED
                ):
                    claim_status = CommandStatus.RECONCILIATION_REQUIRED
                    return application
                application = self._capacity_policy.revoke_invalid_exception(
                    application, self._clock.now()
                )
                if not self._preparation_capacity_available(application):
                    claim_status = CommandStatus.CAPACITY_REACHED
                    return application
            if (
                authorization.scope.action == WorkflowAction.SUBMIT
                and self._known_active_prior(application) is not None
            ):
                claim_status = CommandStatus.RECONCILIATION_REQUIRED
                return application
            if (
                authorization.scope.action
                in {WorkflowAction.FILL, WorkflowAction.SUBMIT}
                and application.artifacts is not None
                and not self._artifacts_are_intact(application.artifacts)
            ):
                application = self._invalidate_modified_artifacts(application)
                claim_status = CommandStatus.STALE
                return application
            resumed_authorization = self._is_resumed_authorization(
                application, authorization
            )
            if not resumed_authorization and (
                application.next_action != authorization.scope.action
                or application.authorization_version != authorization.scope.version
            ):
                claim_status = CommandStatus.STALE
                return application
            if authorization.scope.action == WorkflowAction.SUBMIT:
                try:
                    application, claim_status = self._revalidate_for_submit(application)
                except BrowserInterventionRequired as blocked:
                    application = self._with_intervention(
                        application,
                        blocked,
                        action=WorkflowAction.SUBMIT,
                        actor=authorization.actor,
                        continuation=InterventionContinuation(
                            kind=InterventionContinuationKind.PENDING_AUTHORIZATION,
                            reference=authorization.token,
                        ),
                    )
                    claim_status = CommandStatus.INTERVENTION_REQUIRED
                if claim_status != CommandStatus.ACCEPTED:
                    return application
            accepted = authorization
            return self._accept_authorization(application, authorization)

        capacity_lock = getattr(self._store, "capacity_lock", None)
        lock = (
            capacity_lock()
            if command.scope.action == WorkflowAction.PREPARE
            and callable(capacity_lock)
            else nullcontext()
        )
        with lock:
            try:
                application = self._transact_and_publish(
                    command.scope.application_id, claim
                )
            except KeyError:
                return CommandResult(CommandStatus.MISMATCHED, None, None)
        if claim_status != CommandStatus.ACCEPTED:
            return self._result(claim_status, application)
        assert accepted is not None
        if accepted.scope.action == WorkflowAction.RESUME:
            assert continuation is not None
            return self._continue_intervention(application, continuation)
        if accepted.scope.action == WorkflowAction.RESOLVE_NOT_SUBMITTED:
            return self._result(CommandStatus.RESOLVED, application)
        if accepted.scope.action == WorkflowAction.RETRY_PREPARATION:
            intent_id = f"prepare:{accepted.token}"
            return self._prepare(application, intent_id)
        policy = _ACTION_POLICIES[accepted.scope.action]
        intent_id = f"{policy.intent_prefix}:{accepted.token}"
        return getattr(self, policy.handler_name)(application, intent_id)

    def resume_pending(self, application_id: str) -> CommandResult:
        def recover_control(application: ApplicationSnapshot) -> ApplicationSnapshot:
            application = self._expire_preparation(application)
            uncertain = application.uncertain_submission
            if uncertain is None or uncertain.resolution_token is None:
                return application
            authorization = next(
                (
                    item
                    for item in application.authorizations
                    if item.token == uncertain.resolution_token
                ),
                None,
            )
            if (
                authorization is not None
                and self._clock.now()
                >= datetime.fromisoformat(authorization.expires_at)
            ):
                return self._reissue_uncertain_resolution(application, authorization)
            return application

        application = self._transact_and_publish(application_id, recover_control)
        if application.intervention is not None:
            if self._resume_was_consumed(application):
                return self._continue_intervention(
                    application, application.intervention
                )
            raise ValueError("Intervention can continue only through Riprendi")
        if application.uncertain_submission is not None:
            return self._result(CommandStatus.UNCERTAIN, application)
        pending = next(
            (intent for intent in application.operation_intents if intent.is_pending),
            None,
        )
        if pending is None:
            if application.operational_status == OperationalStatus.EXPIRED_PREPARATION:
                return self._result(CommandStatus.EXPIRED, application)
            if application.submission_intents and application.outcome is None:
                return self.reconcile_submission(
                    application_id,
                    SubmissionOutcome(
                        SubmissionStatus.UNCERTAIN,
                        recorded_at=self._now_iso(),
                    ),
                )
            raise ValueError("No pending operation to resume")
        policy = _ACTION_POLICIES[pending.action]
        return getattr(self, policy.handler_name)(application, pending.intent_id)

    def pending_preparation_ids(self) -> tuple[str, ...]:
        """List durable preparation intents without performing external work."""

        return tuple(
            application.application_id
            for application in self._store.list()
            if application.operational_status
            != OperationalStatus.PREPARATION_FAILED
            and any(
                intent.is_pending and intent.action == WorkflowAction.PREPARE
                for intent in application.operation_intents
            )
        )

    def reconcile_pending_preparations(
        self, *, limit: int = 5
    ) -> tuple[CommandResult, ...]:
        """Advance each hosted preparation once; never poll inside this cycle."""

        if limit <= 0:
            raise ValueError("Preparation reconciliation limit must be positive")
        results = []
        for application_id in self.pending_preparation_ids()[:limit]:
            result = self.resume_pending(application_id)
            results.append(result)
        return tuple(results)

    def preparation_completion_ids(self) -> tuple[str, ...]:
        """List exact completed preparations whose installed files remain valid."""

        completed = []
        for application in self._store.list():
            if (
                application.lifecycle_state != LifecycleState.CV_READY
                or application.artifacts is None
                or application.official_vacancy is None
                or application.next_action != WorkflowAction.FILL
                or not any(
                    intent.action == WorkflowAction.PREPARE
                    and intent.completed_at is not None
                    for intent in application.operation_intents
                )
            ):
                continue
            try:
                verified = self._tailoring.verify_artifacts(application.artifacts)
            except Exception:
                verified = False
            if verified:
                completed.append(application.application_id)
        return tuple(completed)

    @staticmethod
    def _pending_preparation_intent(
        application: ApplicationSnapshot,
    ) -> OperationIntent | None:
        matches = tuple(
            intent
            for intent in application.operation_intents
            if intent.is_pending and intent.action == WorkflowAction.PREPARE
        )
        return matches[0] if len(matches) == 1 else None

    def reconcile_submission(
        self, application_id: str, outcome: SubmissionOutcome
    ) -> CommandResult:
        current = self.get(application_id)
        resolved_outcome, inspection = self._inspect_uncertain_outcome(current, outcome)

        def reconcile(application: ApplicationSnapshot) -> ApplicationSnapshot:
            if (
                not application.submission_intents
                or application.outcome is not None
                and application.outcome.status == SubmissionStatus.VERIFIED
            ):
                raise ValueError("No pending submission intent to reconcile")
            if resolved_outcome.status == SubmissionStatus.VERIFIED:
                return self._with_submission_outcome(application, resolved_outcome)
            assert inspection is not None
            return self._with_uncertain_submission(
                application, resolved_outcome, inspection
            )

        reconciled = self._transact_and_publish(application_id, reconcile)
        status = (
            CommandStatus.COMPLETED
            if resolved_outcome.status == SubmissionStatus.VERIFIED
            else CommandStatus.UNCERTAIN
        )
        return self._result(status, reconciled)

    def reload_master_cv(self) -> str:
        """Reload candidate-owned sources and stale derived, unsubmitted bundles."""

        source_version = self._tailoring.reload_master_cv()
        for existing in self._store.list():
            if not self._should_invalidate_artifacts(existing, source_version):
                continue

            def invalidate(application: ApplicationSnapshot) -> ApplicationSnapshot:
                if not self._should_invalidate_artifacts(application, source_version):
                    return application
                now = self._now_iso()
                return replace(
                    application,
                    authorization_version=application.opportunity_version,
                    artifacts=None,
                    artifacts_expires_at=None,
                    manifest=None,
                    approvals=self._invalidate_downstream_approvals(
                        application.approvals,
                        invalidated_at=now,
                        reason="master CV or evidence bank reloaded",
                    ),
                    operation_intents=tuple(
                        intent.cancel(now)
                        if intent.action == WorkflowAction.FILL and intent.is_pending
                        else intent
                        for intent in application.operation_intents
                    ),
                    operational_status=OperationalStatus.MASTER_CV_RELOADED,
                )

            self._transact_and_publish(existing.application_id, invalidate)
        return source_version

    @staticmethod
    def _should_invalidate_artifacts(
        application: ApplicationSnapshot, source_version: str
    ) -> bool:
        return (
            application.artifacts is not None
            and application.artifacts.evidence_source_version != source_version
            and application.outcome is None
            and not application.submission_intents
        )

    def _accept_authorization(
        self,
        application: ApplicationSnapshot,
        authorization: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        now = self._now_iso()
        consumed = replace(authorization, consumed_at=now)
        policy = _ACTION_POLICIES[authorization.scope.action]
        approval = ApprovalRecord(
            token=authorization.token,
            scope=authorization.scope,
            actor=authorization.actor,
            authorized_at=now,
            expires_at=authorization.expires_at,
        )
        history = application.history
        operation_intents = application.operation_intents
        submission_intents = application.submission_intents
        operational_status = application.operational_status
        lifecycle_state = application.lifecycle_state
        if policy.transition is not None:
            lifecycle_state = policy.transition
            history += (LifecycleEvent(policy.transition, now),)
            operation_intents += (
                OperationIntent(
                    intent_id=f"{policy.intent_prefix}:{authorization.token}",
                    action=authorization.scope.action,
                    version=authorization.scope.version,
                    created_at=now,
                ),
            )
        else:
            submission_intents += (
                SubmissionIntent(
                    intent_id=f"submit:{authorization.token}",
                    manifest_version=authorization.scope.version,
                    created_at=now,
                ),
            )
            operational_status = OperationalStatus.SUBMISSION_STARTED
        return replace(
            application,
            lifecycle_state=lifecycle_state,
            history=history,
            authorizations=tuple(
                consumed if item.token == authorization.token else item
                for item in application.authorizations
            ),
            approvals=application.approvals + (approval,),
            operation_intents=operation_intents,
            submission_intents=submission_intents,
            operational_status=operational_status,
            intervention=None,
        )

    def _prepare(
        self, application: ApplicationSnapshot, intent_id: str
    ) -> CommandResult:
        try:
            official = self._official_vacancies.retrieve(application.opportunity)
            if not official.available:
                raise ValueError("official vacancy unavailable")
            artifacts = self._tailoring.prepare(
                application.application_id,
                intent_id,
                application.opportunity,
                official,
            )
        except HostedPreparationPending:
            pending = replace(application, official_vacancy=official)
            self._persist(pending)
            return self._result(CommandStatus.ACCEPTED, pending)
        except HostedPreparationResolutionRequired:
            blocked = replace(
                application,
                official_vacancy=official,
                operational_status=OperationalStatus.PREPARATION_FAILED,
            )
            self._persist(blocked)
            return self._result(CommandStatus.RECONCILIATION_REQUIRED, blocked)
        except HostedPreparationFailed:
            failed = replace(
                application,
                official_vacancy=official,
                operational_status=OperationalStatus.PREPARATION_FAILED,
            )
            self._persist(failed)
            return self._result(CommandStatus.FAILED, failed)
        except Exception:
            failed = replace(
                application, operational_status=OperationalStatus.PREPARATION_FAILED
            )
            self._persist(failed)
            return self._result(CommandStatus.FAILED, failed)
        now = self._now_iso()
        approvals = application.approvals
        if (
            application.artifacts is not None
            and application.artifacts.version != artifacts.version
        ):
            approvals = self._invalidate_downstream_approvals(
                approvals,
                invalidated_at=now,
                reason="application artifact bundle changed",
            )
        prepared = replace(
            application,
            lifecycle_state=LifecycleState.CV_READY,
            authorization_version=artifacts.version,
            history=application.history
            + (LifecycleEvent(LifecycleState.CV_READY, now),),
            official_vacancy=official,
            artifacts=artifacts,
            artifacts_expires_at=(self._clock.now() + timedelta(hours=72)).isoformat(),
            manifest=None,
            approvals=approvals,
            operation_intents=self._complete_intent(application, intent_id, now),
            operational_status=None,
        )
        self._persist(prepared)
        return self._result(CommandStatus.COMPLETED, prepared)

    def _fill(self, application: ApplicationSnapshot, intent_id: str) -> CommandResult:
        if application.artifacts is None or application.official_vacancy is None:
            return self._result(CommandStatus.STALE, application)
        if not self._artifacts_are_intact(application.artifacts):
            invalidated = self._invalidate_modified_artifacts(application)
            self._persist(invalidated)
            return self._result(CommandStatus.STALE, invalidated)
        try:
            filled = self._ats.fill(
                application.application_id, intent_id, application.artifacts
            )
        except BrowserInterventionRequired as blocked:
            paused = self._with_intervention(
                application,
                blocked,
                action=WorkflowAction.FILL,
                actor=self._actor_for_intent(application, intent_id),
                continuation=InterventionContinuation(
                    kind=InterventionContinuationKind.OPERATION_INTENT,
                    reference=intent_id,
                ),
            )
            self._persist(paused)
            return self._result(CommandStatus.INTERVENTION_REQUIRED, paused)
        except Exception:
            failed = replace(
                application,
                operational_status=OperationalStatus.FILL_FAILED,
                intervention=None,
            )
            self._persist(failed)
            return self._result(CommandStatus.FAILED, failed)
        if filled.artifact_version != application.artifacts.version:
            failed = replace(
                application, operational_status=OperationalStatus.ARTIFACT_MISMATCH
            )
            self._persist(failed)
            return self._result(CommandStatus.MISMATCHED, failed)
        manifest = PreSubmitManifest.build(
            application_id=application.application_id,
            opportunity_version=application.opportunity_version,
            official_vacancy=application.official_vacancy,
            artifacts=application.artifacts,
            filled=filled,
        )
        now = self._now_iso()
        ready = replace(
            application,
            lifecycle_state=LifecycleState.READY_TO_SUBMIT,
            authorization_version=manifest.version,
            history=application.history
            + (LifecycleEvent(LifecycleState.READY_TO_SUBMIT, now),),
            manifest=manifest,
            operation_intents=self._complete_intent(application, intent_id, now),
            operational_status=None,
            intervention=None,
        )
        self._persist(ready)
        return self._result(CommandStatus.COMPLETED, ready)

    def _submit(
        self, application: ApplicationSnapshot, intent_id: str
    ) -> CommandResult:
        if application.manifest is None:
            return self._result(CommandStatus.STALE, application)
        if application.artifacts is None or not self._artifacts_are_intact(
            application.artifacts
        ):
            invalidated = self._invalidate_modified_artifacts(
                application, submission_intent_id=intent_id
            )
            self._persist(invalidated)
            return self._result(CommandStatus.STALE, invalidated)
        try:
            outcome = self._ats.submit(application.application_id, application.manifest)
        except BrowserInterventionRequired as blocked:
            paused = self._with_intervention(
                application,
                blocked,
                action=WorkflowAction.SUBMIT,
                actor=self._actor_for_intent(application, intent_id),
                continuation=InterventionContinuation(
                    kind=InterventionContinuationKind.SUBMISSION_INTENT,
                    reference=intent_id,
                ),
            )
            self._persist(paused)
            return self._result(CommandStatus.INTERVENTION_REQUIRED, paused)
        except Exception:
            outcome = SubmissionOutcome(
                SubmissionStatus.UNCERTAIN, recorded_at=self._now_iso()
            )
        outcome, inspection = self._inspect_uncertain_outcome(application, outcome)
        if outcome.status == SubmissionStatus.VERIFIED:
            submitted = self._with_submission_outcome(application, outcome)
        else:
            assert inspection is not None
            submitted = self._with_uncertain_submission(
                application, outcome, inspection
            )
        self._persist(submitted)
        status = (
            CommandStatus.COMPLETED
            if outcome.status == SubmissionStatus.VERIFIED
            else CommandStatus.UNCERTAIN
        )
        return self._result(status, submitted)

    def _revalidate_for_submit(
        self, application: ApplicationSnapshot
    ) -> tuple[ApplicationSnapshot, CommandStatus]:
        if application.official_vacancy is None or application.manifest is None:
            return application, CommandStatus.STALE
        current = self._official_vacancies.revalidate(
            application.opportunity, application.official_vacancy
        )
        unchanged = (
            current.available
            and current.verified
            and current.version == application.official_vacancy.version
            and current.fingerprint == application.official_vacancy.fingerprint
            and current.freshness == application.manifest.vacancy_freshness
        )
        if not unchanged:
            return (
                replace(
                    application,
                    authorization_version=application.opportunity_version,
                    manifest=None,
                    operational_status=OperationalStatus.VACANCY_CHANGED,
                ),
                CommandStatus.STALE,
            )
        try:
            validation = self._ats.validate_submit(
                application.application_id, application.manifest
            )
        except BrowserInterventionRequired:
            raise
        except Exception:
            return self._require_ats_review_reprepare(application), CommandStatus.STALE
        if validation is not True:
            return self._require_ats_review_reprepare(application), CommandStatus.STALE
        return replace(application, official_vacancy=current), CommandStatus.ACCEPTED

    def _require_ats_review_reprepare(
        self, application: ApplicationSnapshot
    ) -> ApplicationSnapshot:
        """Invalidate a stale submit scope and return to an explicit re-fill gate."""

        now = self._now_iso()
        authorization_version = (
            application.opportunity_version
            if application.artifacts is None
            else application.artifacts.version
        )
        return replace(
            application,
            authorization_version=authorization_version,
            manifest=None,
            authorizations=tuple(
                replace(
                    authorization,
                    invalidated_at=now,
                    invalidation_reason="trusted ATS review session must be prepared again",
                )
                if authorization.consumed_at is None
                and authorization.invalidated_at is None
                and authorization.scope.action == WorkflowAction.SUBMIT
                else authorization
                for authorization in application.authorizations
            ),
            approvals=self._invalidate_downstream_approvals(
                application.approvals,
                invalidated_at=now,
                reason="trusted ATS review session must be prepared again",
            ),
            operational_status=OperationalStatus.ATS_REVIEW_REPREPARE_REQUIRED,
        )

    def _with_submission_outcome(
        self, application: ApplicationSnapshot, outcome: SubmissionOutcome
    ) -> ApplicationSnapshot:
        if outcome.status != SubmissionStatus.VERIFIED:
            recorded_at = outcome.recorded_at or self._now_iso()
            return replace(
                application,
                outcome=replace(outcome, recorded_at=recorded_at),
                operational_status=OperationalStatus.SUBMISSION_UNCERTAIN,
                intervention=None,
            )
        now = self._now_iso()
        return replace(
            application,
            lifecycle_state=LifecycleState.SUBMITTED,
            history=application.history
            + (LifecycleEvent(LifecycleState.SUBMITTED, now),),
            outcome=outcome,
            operational_status=None,
            intervention=None,
            uncertain_submission=None,
        )

    def _with_intervention(
        self,
        application: ApplicationSnapshot,
        blocked: BrowserInterventionRequired,
        *,
        action: WorkflowAction,
        actor: str,
        continuation: InterventionContinuation,
    ) -> ApplicationSnapshot:
        now = self._clock.now()
        resume_token = str(self._token_factory())
        intervention_id = f"intervention:{resume_token}"
        record = InterventionRecord(
            intervention_id=intervention_id,
            kind=blocked.kind,
            action=action.value,
            explanation=blocked.explanation,
            detected_at=now.isoformat(),
            browser_ready=blocked.browser_ready,
            resume_token=resume_token,
            actor=actor,
            continuation=continuation,
        )
        authorization = AuthorizationRecord(
            token=resume_token,
            scope=AuthorizationScope(
                application.application_id,
                WorkflowAction.RESUME,
                intervention_id,
            ),
            actor=actor,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
        return replace(
            application,
            intervention=record,
            uncertain_submission=None,
            operational_status=OperationalStatus.INTERVENTION_REQUIRED,
            authorizations=application.authorizations + (authorization,),
        )

    def _intervention_is_resolved(
        self, application: ApplicationSnapshot, intervention: InterventionRecord
    ) -> bool:
        probe = getattr(self._ats, "intervention_is_resolved", None)
        if not callable(probe):
            return False
        try:
            return probe(application.application_id, intervention) is True
        except Exception:
            return False

    def _reissue_resume(
        self,
        application: ApplicationSnapshot,
        expired: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        intervention = application.intervention
        if intervention is None:
            return application
        now = self._clock.now()
        token = str(self._token_factory())
        replacement = AuthorizationRecord(
            token=token,
            scope=AuthorizationScope(
                application.application_id,
                WorkflowAction.RESUME,
                intervention.intervention_id,
            ),
            actor=intervention.actor,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
        invalidated = replace(
            expired,
            invalidated_at=now.isoformat(),
            invalidation_reason="expired Riprendi replaced while intervention is pending",
        )
        return replace(
            application,
            intervention=replace(intervention, resume_token=token),
            authorizations=tuple(
                invalidated if item.token == expired.token else item
                for item in application.authorizations
            )
            + (replacement,),
        )

    def _reissue_uncertain_resolution(
        self,
        application: ApplicationSnapshot,
        expired: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        uncertain = application.uncertain_submission
        if (
            uncertain is None
            or uncertain.resolution_token != expired.token
            or not uncertain.inspection.permits_human_resolution
        ):
            return application
        now = self._clock.now()
        token = str(self._token_factory())
        replacement = AuthorizationRecord(
            token=token,
            scope=AuthorizationScope(
                application.application_id,
                WorkflowAction.RESOLVE_NOT_SUBMITTED,
                uncertain.version,
            ),
            actor=uncertain.actor,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
        invalidated = replace(
            expired,
            invalidated_at=now.isoformat(),
            invalidation_reason=(
                "expired uncertain-outcome resolution replaced while pending"
            ),
        )
        return replace(
            application,
            uncertain_submission=replace(uncertain, resolution_token=token),
            authorizations=tuple(
                invalidated if item.token == expired.token else item
                for item in application.authorizations
            )
            + (replacement,),
        )

    @staticmethod
    def _is_resumed_authorization(
        application: ApplicationSnapshot, authorization: AuthorizationRecord
    ) -> bool:
        intervention = application.intervention
        if (
            intervention is None
            or intervention.continuation.kind
            != InterventionContinuationKind.PENDING_AUTHORIZATION
            or intervention.continuation.reference != authorization.token
        ):
            return False
        return any(
            item.token == intervention.resume_token and item.consumed_at is not None
            for item in application.authorizations
        )

    @staticmethod
    def _resume_was_consumed(application: ApplicationSnapshot) -> bool:
        intervention = application.intervention
        if intervention is None:
            return False
        return any(
            item.token == intervention.resume_token and item.consumed_at is not None
            for item in application.authorizations
        )

    def _continue_intervention(
        self,
        application: ApplicationSnapshot,
        intervention: InterventionRecord,
    ) -> CommandResult:
        continuation = intervention.continuation
        if continuation.kind == InterventionContinuationKind.PENDING_AUTHORIZATION:
            command = self.command_for_token(continuation.reference)
            if command is None:
                return self._result(CommandStatus.STALE, application)
            return self.handle(command)
        if continuation.kind == InterventionContinuationKind.OPERATION_INTENT:
            action = WorkflowAction(intervention.action)
            policy = _ACTION_POLICIES[action]
            return getattr(self, policy.handler_name)(
                application, continuation.reference
            )
        claimed = False

        def begin_submission_continuation(
            current: ApplicationSnapshot,
        ) -> ApplicationSnapshot:
            nonlocal claimed
            if current.intervention != intervention:
                return current
            claimed = True
            return replace(
                current,
                intervention=None,
                operational_status=OperationalStatus.SUBMISSION_STARTED,
            )

        application = self._transact_and_publish(
            application.application_id, begin_submission_continuation
        )
        if not claimed:
            return self._result(CommandStatus.STALE, application)
        return self._submit(application, continuation.reference)

    def _accept_control_authorization(
        self,
        application: ApplicationSnapshot,
        authorization: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        now = self._now_iso()
        consumed = replace(authorization, consumed_at=now)
        approval = ApprovalRecord(
            token=authorization.token,
            scope=authorization.scope,
            actor=authorization.actor,
            authorized_at=now,
            expires_at=authorization.expires_at,
        )
        return replace(
            application,
            authorizations=tuple(
                consumed if item.token == authorization.token else item
                for item in application.authorizations
            ),
            approvals=application.approvals + (approval,),
            intervention=application.intervention,
            operational_status=application.operational_status,
        )

    def _accept_preparation_retry(
        self,
        application: ApplicationSnapshot,
        authorization: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        """Resolve the old intent and persist a distinct intent before dispatch."""

        accepted = self._accept_control_authorization(application, authorization)
        now = self._now_iso()
        old_intent_id = authorization.scope.version
        new_intent = OperationIntent(
            intent_id=f"prepare:{authorization.token}",
            action=WorkflowAction.PREPARE,
            version=application.authorization_version,
            created_at=now,
        )
        return replace(
            accepted,
            operation_intents=tuple(
                intent.cancel(now)
                if intent.intent_id == old_intent_id and intent.is_pending
                else intent
                for intent in accepted.operation_intents
            )
            + (new_intent,),
            operational_status=None,
        )

    def _resolve_not_submitted(
        self,
        application: ApplicationSnapshot,
        authorization: AuthorizationRecord,
    ) -> ApplicationSnapshot:
        accepted = self._accept_control_authorization(application, authorization)
        now = self._now_iso()
        return replace(
            accepted,
            authorizations=tuple(
                replace(
                    item,
                    invalidated_at=now,
                    invalidation_reason="human resolved uncertain outcome as not submitted",
                )
                if item.invalidated_at is None
                and item.consumed_at is None
                and item.scope.action == WorkflowAction.SUBMIT
                else item
                for item in accepted.authorizations
            ),
            approvals=tuple(
                replace(
                    approval,
                    invalidated_at=now,
                    invalidation_reason="human resolved uncertain outcome as not submitted",
                )
                if approval.is_valid and approval.scope.action == WorkflowAction.SUBMIT
                else approval
                for approval in accepted.approvals
            ),
            submission_intents=(),
            outcome=None,
            uncertain_submission=None,
            operational_status=None,
        )

    def _inspect_uncertain_outcome(
        self,
        application: ApplicationSnapshot,
        outcome: SubmissionOutcome,
    ) -> tuple[SubmissionOutcome, SubmissionInspection | None]:
        if outcome.status == SubmissionStatus.VERIFIED:
            return outcome, None
        inspector = getattr(self._ats, "inspect_submission", None)
        try:
            if not callable(inspector) or application.manifest is None:
                raise RuntimeError("submission inspection unavailable")
            inspection = inspector(application.application_id, application.manifest)
            if not isinstance(inspection, SubmissionInspection):
                raise RuntimeError("invalid submission inspection")
        except Exception:
            inspection = SubmissionInspection(
                status=SubmissionInspectionStatus.INCOMPLETE,
                checked_at=self._now_iso(),
                sources_checked=(),
                sources_unavailable=(
                    SubmissionInspectionSource.ATS,
                    SubmissionInspectionSource.CAREER_MAILBOX,
                ),
            )
        if inspection.status == SubmissionInspectionStatus.VERIFIED:
            assert inspection.evidence is not None
            return (
                SubmissionOutcome(
                    status=SubmissionStatus.VERIFIED,
                    evidence=inspection.evidence,
                    recorded_at=inspection.checked_at,
                ),
                inspection,
            )
        if outcome.recorded_at is None:
            outcome = replace(outcome, recorded_at=self._now_iso())
        return outcome, inspection

    def _with_uncertain_submission(
        self,
        application: ApplicationSnapshot,
        outcome: SubmissionOutcome,
        inspection: SubmissionInspection,
    ) -> ApplicationSnapshot:
        if not application.submission_intents or application.manifest is None:
            raise RuntimeError("Uncertain submission has no durable submit intent")
        intent = application.submission_intents[-1]
        version = f"uncertain:{intent.intent_id}:{inspection.checked_at}"
        actor = self._actor_for_intent(application, intent.intent_id)
        token = (
            str(self._token_factory()) if inspection.permits_human_resolution else None
        )
        authorizations = tuple(
            replace(
                item,
                invalidated_at=self._now_iso(),
                invalidation_reason="newer uncertain-submission inspection recorded",
            )
            if item.invalidated_at is None
            and item.consumed_at is None
            and item.scope.action == WorkflowAction.RESOLVE_NOT_SUBMITTED
            else item
            for item in application.authorizations
        )
        if token is not None:
            now = self._clock.now()
            authorizations += (
                AuthorizationRecord(
                    token=token,
                    scope=AuthorizationScope(
                        application.application_id,
                        WorkflowAction.RESOLVE_NOT_SUBMITTED,
                        version,
                    ),
                    actor=actor,
                    issued_at=now.isoformat(),
                    expires_at=(now + timedelta(minutes=30)).isoformat(),
                ),
            )
        uncertain = UncertainSubmissionRecord(
            version=version,
            manifest_version=application.manifest.version,
            submission_intent_id=intent.intent_id,
            inspection=inspection,
            resolution_token=token,
            actor=actor,
        )
        return replace(
            self._with_submission_outcome(application, outcome),
            uncertain_submission=uncertain,
            authorizations=authorizations,
        )

    @staticmethod
    def _actor_for_intent(application: ApplicationSnapshot, intent_id: str) -> str:
        token = intent_id.partition(":")[2]
        for approval in reversed(application.approvals):
            if approval.token == token:
                return approval.actor
        for authorization in reversed(application.authorizations):
            if authorization.token == token:
                return authorization.actor
        return "unknown human actor"

    def _expire_preparation(
        self, application: ApplicationSnapshot
    ) -> ApplicationSnapshot:
        return self._freshness_policy.expire(application, self._clock.now())

    def _artifacts_are_intact(self, artifacts: PreparedArtifacts) -> bool:
        try:
            return self._tailoring.verify_artifacts(artifacts)
        except Exception:
            return False

    def _invalidate_modified_artifacts(
        self,
        application: ApplicationSnapshot,
        *,
        submission_intent_id: str | None = None,
    ) -> ApplicationSnapshot:
        now = self._now_iso()
        return replace(
            application,
            authorization_version=application.opportunity_version,
            artifacts=None,
            artifacts_expires_at=None,
            manifest=None,
            approvals=self._invalidate_downstream_approvals(
                application.approvals,
                invalidated_at=now,
                reason="published application artifact bytes changed",
            ),
            operation_intents=tuple(
                intent.cancel(now)
                if intent.action == WorkflowAction.FILL and intent.is_pending
                else intent
                for intent in application.operation_intents
            ),
            submission_intents=tuple(
                intent
                for intent in application.submission_intents
                if intent.intent_id != submission_intent_id
            ),
            operational_status=OperationalStatus.ARTIFACT_MISMATCH,
        )

    @staticmethod
    def _complete_intent(
        application: ApplicationSnapshot, intent_id: str, completed_at: str
    ) -> tuple[OperationIntent, ...]:
        return tuple(
            replace(intent, completed_at=completed_at)
            if intent.intent_id == intent_id
            else intent
            for intent in application.operation_intents
        )

    @staticmethod
    def _invalidate_downstream_approvals(
        approvals: tuple[ApprovalRecord, ...],
        *,
        invalidated_at: str,
        reason: str,
    ) -> tuple[ApprovalRecord, ...]:
        return tuple(
            replace(
                approval,
                invalidated_at=invalidated_at,
                invalidation_reason=reason,
            )
            if approval.is_valid
            and approval.scope.action in {WorkflowAction.FILL, WorkflowAction.SUBMIT}
            else approval
            for approval in approvals
        )

    def _persist(self, application: ApplicationSnapshot) -> None:
        pending = replace(application, package_publication_pending=True)
        self._store.save(pending)
        self._publish_pending_application(pending.application_id)

    def _transact_and_publish(
        self,
        application_id: str,
        operation: Callable[[ApplicationSnapshot], ApplicationSnapshot],
    ) -> ApplicationSnapshot:
        """Atomically stage every state mutation in the package outbox."""

        def stage(current: ApplicationSnapshot) -> ApplicationSnapshot:
            updated = operation(current)
            if updated == current:
                return current
            return replace(updated, package_publication_pending=True)

        staged = self._store.transact(application_id, stage)
        if not staged.package_publication_pending:
            return staged
        return self._publish_pending_application(application_id)

    def _publish_pending_application(self, application_id: str) -> ApplicationSnapshot:
        """Drain the durable outbox without overwriting a concurrent mutation."""

        while True:
            pending = self._store.load(application_id)
            if not pending.package_publication_pending:
                return pending
            published = replace(pending, package_publication_pending=False)
            self._report_writer.write(published)

            def acknowledge(current: ApplicationSnapshot) -> ApplicationSnapshot:
                if current == pending:
                    return published
                return current

            current = self._store.transact(application_id, acknowledge)
            if not current.package_publication_pending:
                return current

    def _recover_package_publication(self, application_id: str) -> ApplicationSnapshot:
        application = self._store.load(application_id)
        if not application.package_publication_pending:
            return application
        return self._publish_pending_application(application_id)

    def _now_iso(self) -> str:
        return self._clock.now().isoformat()

    @staticmethod
    def _result(
        status: CommandStatus, application: ApplicationSnapshot
    ) -> CommandResult:
        return CommandResult(
            status, application.lifecycle_state, application.next_action
        )


# Re-export the stable public test/integration surface from this deep module.
__all__ = [
    "ActionCommand",
    "ApplicationWorkflowCoordinator",
    "CommandResult",
    "FilledApplication",
    "JsonApplicationStore",
    "MarkdownApplicationReportWriter",
    "OfficialVacancy",
    "PreparedArtifacts",
    "PreSubmitManifest",
    "SubmissionOutcome",
    "TelegramCommandHandler",
    "WorkflowAction",
]
