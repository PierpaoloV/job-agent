"""Public opportunity verification and decision workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import re
import secrets
from typing import Protocol

from application_identity import approved_application_id
from opportunity_domain import (
    ApprovedApplication,
    ConditionalDiscard,
    DecisionAction,
    DecisionAuthorization,
    DecisionCommand,
    DecisionResult,
    DecisionStatus,
    Evaluation,
    MATERIAL_FIELDS,
    OfficialVacancyData,
    OfficialVacancySnapshot,
    OpportunityDetails,
    OpportunityLifecycle,
    OpportunityRecord,
    RoleCard,
    Runtime,
    SuppressionResult,
    VerificationResult,
    VerificationStatus,
)
from opportunity_sources import EmailLeadRouter, OpportunityLead
from opportunity_storage import JsonOpportunityStore
from opportunity_telegram import OpportunityTelegramHandler


class HostedFetchBlocked(RuntimeError):
    """The hosted runner cannot reliably retrieve the official page."""


class OfficialVacancyUnavailable(RuntimeError):
    """No single official employer vacancy can be established."""


class OfficialSource(Protocol):
    def retrieve(
        self, lead: OpportunityLead, runtime: Runtime
    ) -> OfficialVacancyData: ...


class Evaluator(Protocol):
    def evaluate(self, vacancy: OfficialVacancySnapshot) -> Evaluation: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class OpportunityStore(Protocol):
    def save(self, record: OpportunityRecord) -> None: ...

    def load(self, stable_id: str) -> OpportunityRecord: ...

    def list(self) -> tuple[OpportunityRecord, ...]: ...


class OpportunityWorkflow:
    """Verify leads and expose only verified descriptions to evaluation."""

    def __init__(
        self,
        *,
        store: OpportunityStore,
        official_source: OfficialSource,
        evaluator: Evaluator,
        clock: Clock,
        token_factory=None,
    ) -> None:
        self._store = store
        self._official_source = official_source
        self._evaluator = evaluator
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))

    def record_lead(self, lead: OpportunityLead) -> OpportunityRecord:
        try:
            existing = self._store.load(lead.stable_id)
        except KeyError:
            record = OpportunityRecord(lead=lead)
        else:
            record = replace(existing, lead=lead)
        self._store.save(record)
        return record

    def verify_and_evaluate(
        self, stable_id: str, *, runtime: Runtime
    ) -> VerificationResult:
        verification = self.verify_official(stable_id, runtime=runtime)
        if (
            verification.status != VerificationStatus.VERIFIED
            or verification.snapshot is None
            or verification.evaluation is not None
        ):
            return verification
        record = self._store.load(stable_id)
        evaluation = self._evaluator.evaluate(verification.snapshot)
        updated = replace(
            record,
            evaluation=evaluation,
            evaluation_version=verification.snapshot.version,
        )
        self._store.save(updated)
        return self._result(updated)

    def verify_official(
        self, stable_id: str, *, runtime: Runtime
    ) -> VerificationResult:
        """Retrieve and persist an official snapshot without evaluating it."""

        record = self._store.load(stable_id)
        try:
            vacancy = self._official_source.retrieve(record.lead, Runtime(runtime))
            if not vacancy.description.strip():
                raise OfficialVacancyUnavailable(
                    "the official description is empty"
                )
        except HostedFetchBlocked:
            request = (
                "Official vacancy fetch is blocked on the hosted runner; "
                "resume this record on the Mac."
            )
            updated = replace(
                record,
                status=VerificationStatus.NEEDS_LOCAL_FETCH,
                operator_request=request,
            )
            self._store.save(updated)
            return self._result(updated)
        except OfficialVacancyUnavailable as exc:
            request = f"Official vacancy required: {exc}"
            updated = replace(
                record,
                status=VerificationStatus.NEEDS_OFFICIAL_DESCRIPTION,
                operator_request=request,
            )
            self._store.save(updated)
            return self._result(updated)

        snapshot = OfficialVacancySnapshot.capture(
            vacancy,
            retrieved_at=self._clock.now().isoformat(),
            previous=record.latest_snapshot,
        )
        evaluation_is_current = (
            record.evaluation is not None
            and record.evaluation_version == snapshot.version
        )
        verified = replace(
            record,
            status=VerificationStatus.VERIFIED,
            snapshots=record.snapshots + (snapshot,),
            evaluation=record.evaluation if evaluation_is_current else None,
            evaluation_version=(
                record.evaluation_version if evaluation_is_current else None
            ),
            operator_request=None,
            lifecycle=(
                record.lifecycle
                if evaluation_is_current
                else OpportunityLifecycle.PROPOSED
            ),
        )
        self._store.save(verified)
        return self._result(verified)

    def resume_local(self, stable_id: str) -> VerificationResult:
        record = self._store.load(stable_id)
        if record.status != VerificationStatus.NEEDS_LOCAL_FETCH:
            raise ValueError("Opportunity is not waiting for local retrieval")
        return self.verify_and_evaluate(stable_id, runtime=Runtime.LOCAL)

    def get(self, stable_id: str) -> OpportunityRecord:
        return self._store.load(stable_id)

    def role_card(self, stable_id: str) -> RoleCard:
        record = self._verified_record(stable_id)
        snapshot = record.latest_snapshot
        evaluation = record.evaluation
        assert snapshot is not None and evaluation is not None
        freshness = f"verified {snapshot.retrieved_at}"
        if snapshot.vacancy.published_at:
            freshness = f"published {snapshot.vacancy.published_at}; {freshness}"
        return RoleCard(
            identity=f"{snapshot.vacancy.company} — {snapshot.vacancy.role}",
            location=snapshot.vacancy.location,
            modality=snapshot.vacancy.modality,
            source=record.lead.source,
            freshness=freshness,
            fit_summary=evaluation.fit_summary,
            gaps=evaluation.gaps,
            compensation_status=evaluation.compensation_status,
            wealth_potential_confidence=evaluation.wealth_potential_confidence,
            immigration=evaluation.immigration,
            ownership=evaluation.ownership,
            risks=evaluation.risks,
            rank_explanation=evaluation.rank_explanation,
            actions=tuple(action.value for action in DecisionAction),
        )

    def issue_decision_authorization(
        self,
        stable_id: str,
        verified_version: str,
        action: DecisionAction,
        *,
        actor: str,
        ttl: timedelta = timedelta(minutes=30),
    ) -> DecisionCommand:
        if ttl <= timedelta(0):
            raise ValueError("Decision authorization TTL must be positive")
        record = self._verified_record(stable_id)
        snapshot = record.latest_snapshot
        assert snapshot is not None
        if snapshot.version != verified_version:
            raise ValueError("Cannot authorize a stale opportunity version")
        now = self._clock.now()
        authorization = DecisionAuthorization(
            token=str(self._token_factory()),
            stable_id=stable_id,
            verified_version=verified_version,
            action=DecisionAction(action),
            actor=actor,
            issued_at=now.isoformat(),
            expires_at=(now + ttl).isoformat(),
        )
        self._store.save(
            replace(
                record,
                decision_authorizations=(
                    record.decision_authorizations + (authorization,)
                ),
            )
        )
        return DecisionCommand(
            token=authorization.token,
            stable_id=stable_id,
            verified_version=verified_version,
            action=authorization.action,
        )

    def decide(self, command: DecisionCommand) -> DecisionResult:
        record = self._store.load(command.stable_id)
        snapshot = record.latest_snapshot
        if (
            record.status != VerificationStatus.VERIFIED
            or snapshot is None
            or record.evaluation is None
        ):
            return DecisionResult(DecisionStatus.NOT_VERIFIED)
        authorization = next(
            (
                item
                for item in record.decision_authorizations
                if item.token == command.token
            ),
            None,
        )
        if authorization is None or (
            authorization.stable_id != command.stable_id
            or authorization.verified_version != command.verified_version
            or authorization.action != command.action
        ):
            return DecisionResult(DecisionStatus.MISMATCHED)
        if authorization.consumed_at is not None:
            return DecisionResult(DecisionStatus.REPLAYED)
        if self._clock.now() >= datetime.fromisoformat(authorization.expires_at):
            return DecisionResult(DecisionStatus.EXPIRED)
        if command.verified_version != snapshot.version:
            return DecisionResult(DecisionStatus.STALE)
        action = DecisionAction(command.action)
        if action == DecisionAction.DISCARD and not (command.reason or "").strip():
            awaiting_reason = replace(
                authorization,
                awaiting_reason_at=(
                    authorization.awaiting_reason_at
                    or self._clock.now().isoformat()
                ),
            )
            self._store.save(
                replace(
                    record,
                    decision_authorizations=tuple(
                        awaiting_reason if item.token == command.token else item
                        for item in record.decision_authorizations
                    ),
                )
            )
            return DecisionResult(DecisionStatus.NEEDS_REASON)
        consumed = replace(authorization, consumed_at=self._clock.now().isoformat())
        record = replace(
            record,
            decision_authorizations=tuple(
                consumed if item.token == command.token else item
                for item in record.decision_authorizations
            ),
        )
        self._store.save(record)
        if action == DecisionAction.MORE_DETAILS:
            return DecisionResult(
                DecisionStatus.DETAILS,
                details=OpportunityDetails(
                    description=snapshot.vacancy.description,
                    requirement_analysis=record.evaluation.requirement_analysis,
                    sources=tuple(
                        dict.fromkeys(
                            (
                                record.lead.canonical_url,
                                snapshot.vacancy.canonical_url,
                                *record.evaluation.sources,
                            )
                        )
                    ),
                    link=snapshot.vacancy.canonical_url,
                    risks=record.evaluation.risks,
                ),
            )
        if action == DecisionAction.PREPARE:
            if record.lifecycle == OpportunityLifecycle.DISCARDED:
                return DecisionResult(DecisionStatus.INVALID_STATE)
            revalidation_failure = self._revalidate_for_prepare(record, snapshot)
            if revalidation_failure is not None:
                updated = self._store.load(record.lead.stable_id)
                return DecisionResult(
                    revalidation_failure,
                    message=updated.operator_request,
                )
            approved = next(
                (
                    item
                    for item in record.approved_applications
                    if item.opportunity_version == snapshot.version
                ),
                None,
            )
            if approved is None:
                approved = ApprovedApplication(
                    application_id=approved_application_id(
                        record.lead.stable_id,
                        snapshot.version,
                    ),
                    opportunity_id=record.lead.stable_id,
                    opportunity_version=snapshot.version,
                    actor=authorization.actor,
                    approved_at=self._clock.now().isoformat(),
                    action=DecisionAction.PREPARE,
                    expires_at=authorization.expires_at,
                )
                record = replace(
                    record,
                    approved_applications=record.approved_applications + (approved,),
                    lifecycle=OpportunityLifecycle.APPROVED,
                )
                self._store.save(record)
            return DecisionResult(
                DecisionStatus.APPROVED, approved_application=approved
            )
        reason = (command.reason or "").strip()
        if record.lifecycle == OpportunityLifecycle.APPROVED:
            return DecisionResult(DecisionStatus.INVALID_STATE)
        discard = ConditionalDiscard(
            opportunity_id=record.lead.stable_id,
            opportunity_version=snapshot.version,
            role_similarity_key=_role_similarity_key(
                snapshot.vacancy.company, snapshot.vacancy.role
            ),
            material_fingerprint=snapshot.material_fingerprint,
            material_values=_material_values(snapshot),
            reason=reason,
            actor=authorization.actor,
            discarded_at=self._clock.now().isoformat(),
        )
        self._store.save(
            replace(
                record,
                discard=discard,
                lifecycle=OpportunityLifecycle.DISCARDED,
            )
        )
        return DecisionResult(DecisionStatus.DISCARDED)

    def command_for_token(
        self, token: str, *, reason: str | None = None
    ) -> DecisionCommand | None:
        authorization = next(
            (
                item
                for record in self._store.list()
                for item in record.decision_authorizations
                if item.token == token
            ),
            None,
        )
        if authorization is None:
            return None
        return DecisionCommand(
            token=authorization.token,
            stable_id=authorization.stable_id,
            verified_version=authorization.verified_version,
            action=authorization.action,
            reason=reason,
        )

    def _revalidate_for_prepare(
        self,
        record: OpportunityRecord,
        previous: OfficialVacancySnapshot,
    ) -> DecisionStatus | None:
        try:
            vacancy = self._official_source.retrieve(record.lead, Runtime.LOCAL)
        except HostedFetchBlocked:
            self._store.save(
                replace(
                    record,
                    status=VerificationStatus.NEEDS_LOCAL_FETCH,
                    operator_request=(
                        "Official vacancy fetch is blocked; resume this record "
                        "on the Mac."
                    ),
                )
            )
            return DecisionStatus.NOT_VERIFIED
        except OfficialVacancyUnavailable as exc:
            self._store.save(
                replace(
                    record,
                    status=VerificationStatus.NEEDS_OFFICIAL_DESCRIPTION,
                    operator_request=f"Official vacancy required: {exc}",
                )
            )
            return DecisionStatus.NOT_VERIFIED
        if not vacancy.description.strip():
            self._store.save(
                replace(
                    record,
                    status=VerificationStatus.NEEDS_OFFICIAL_DESCRIPTION,
                    operator_request=(
                        "Official vacancy required: the official description is empty"
                    ),
                )
            )
            return DecisionStatus.NOT_VERIFIED
        current = OfficialVacancySnapshot.capture(
            vacancy,
            retrieved_at=self._clock.now().isoformat(),
            previous=previous,
        )
        if current.version == previous.version:
            return None
        self._store.save(
            replace(
                record,
                lifecycle=OpportunityLifecycle.PROPOSED,
                snapshots=record.snapshots + (current,),
                evaluation=None,
                evaluation_version=None,
            )
        )
        return DecisionStatus.STALE

    def suppression(self, stable_id: str) -> SuppressionResult:
        record = self._verified_record(stable_id)
        snapshot = record.latest_snapshot
        assert snapshot is not None
        role_key = _role_similarity_key(
            snapshot.vacancy.company, snapshot.vacancy.role
        )
        matching = tuple(
            candidate.discard
            for candidate in self._store.list()
            if candidate.discard is not None
            and candidate.discard.role_similarity_key == role_key
        )
        if not matching:
            return SuppressionResult(False)
        discard = matching[-1]
        if discard.material_fingerprint == snapshot.material_fingerprint:
            return SuppressionResult(True, reason=discard.reason)
        current = _material_values(snapshot)
        changes = tuple(
            f"{field}: {discard.material_values.get(field)!r} -> {current[field]!r}"
            for field in MATERIAL_FIELDS
            if discard.material_values.get(field) != current[field]
        )
        return SuppressionResult(False, reason=discard.reason, material_changes=changes)

    def _verified_record(self, stable_id: str) -> OpportunityRecord:
        record = self._store.load(stable_id)
        if (
            record.status != VerificationStatus.VERIFIED
            or record.latest_snapshot is None
            or record.evaluation is None
        ):
            raise ValueError("Opportunity has no verified official vacancy")
        return record

    @staticmethod
    def _result(record: OpportunityRecord) -> VerificationResult:
        return VerificationResult(
            stable_id=record.lead.stable_id,
            status=record.status,
            snapshot=record.latest_snapshot,
            evaluation=record.evaluation,
            operator_request=record.operator_request,
        )


__all__ = [
    "DecisionAction",
    "DecisionCommand",
    "EmailLeadRouter",
    "Evaluation",
    "HostedFetchBlocked",
    "JsonOpportunityStore",
    "OfficialVacancyData",
    "OfficialVacancyUnavailable",
    "OpportunityLead",
    "OpportunityWorkflow",
    "OpportunityTelegramHandler",
    "Runtime",
]


def _role_similarity_key(company: str, role: str) -> str:
    return ":".join(
        re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        for value in (company, role)
    )


def _material_values(snapshot: OfficialVacancySnapshot) -> dict:
    return {
        field: list(getattr(snapshot.vacancy, field))
        if isinstance(getattr(snapshot.vacancy, field), tuple)
        else getattr(snapshot.vacancy, field)
        for field in MATERIAL_FIELDS
    }
