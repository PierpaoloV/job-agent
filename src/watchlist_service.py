"""Policies and durable human gates for watchlist and job-alert changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit

from watchlist_domain import (
    CompanyCandidate,
    CompanyProposal,
    DecisionResult,
    EligibilityEvidence,
    JobAlertCandidate,
    JobAlertProposal,
    SubscriptionReport,
)
from watchlist_store import JsonWatchlistStore, company_key


MAX_COMPANY_PROPOSALS = 5
PROPOSAL_WINDOW = timedelta(days=14)
EVIDENCE_MAX_AGE = timedelta(days=90)
DEFAULT_CALLBACK_TTL = timedelta(minutes=30)
SUBSCRIPTION_ATTEMPT_LEASE = timedelta(minutes=5)
ALLOWED_SPONSORSHIP = {"not_required_eu", "sponsors", "not_stated", "yes", "no"}
UNVERIFIED_CLASSIFICATIONS = {
    "",
    "error",
    "failed",
    "unknown",
    "unverified",
    "verification_failed",
}
ISO_3166_ALPHA2_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
    """.split()
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SubscriptionExecutor(Protocol):
    def subscribe(
        self, alert: JobAlertCandidate, *, idempotency_key: str
    ) -> dict[str, Any]: ...


class SubscriptionDefinitiveError(RuntimeError):
    """The adapter knows no external subscription was created."""


@dataclass(frozen=True)
class CompanyEligibilityPolicy:
    """Private, operator-configured company exclusions."""

    excluded_ownership: frozenset[str] = frozenset()
    excluded_country_codes: frozenset[str] = frozenset()

    @classmethod
    def from_values(
        cls,
        *,
        excluded_ownership: Iterable[str] = (),
        excluded_country_codes: Iterable[str] = (),
    ) -> "CompanyEligibilityPolicy":
        return cls(
            excluded_ownership=frozenset(
                _normalized_classification(value)
                for value in excluded_ownership
                if str(value).strip()
            ),
            excluded_country_codes=frozenset(
                str(value).strip().upper()
                for value in excluded_country_codes
                if str(value).strip()
            ),
        )


@dataclass(frozen=True)
class _ExpiredSubscriptionAttempt:
    proposal_id: str


@dataclass(frozen=True)
class _PreparedSubscriptionAttempt:
    proposal_id: str
    alert: JobAlertCandidate
    idempotency_key: str
    owner: str
    token: str


class WatchlistService:
    def __init__(
        self,
        *,
        store: JsonWatchlistStore,
        clock: Clock,
        subscription_executor: SubscriptionExecutor,
        eligibility_policy: CompanyEligibilityPolicy = CompanyEligibilityPolicy(),
    ):
        self._store = store
        self._clock = clock
        self._subscription_executor = subscription_executor
        self._eligibility_policy = eligibility_policy
        self._subscription_owner = "subscription-worker:" + secrets.token_urlsafe(18)

    def active_company_names(self) -> tuple[str, ...]:
        now = _aware_utc(self._clock.now())

        def operation(state: dict[str, Any]) -> tuple[str, ...]:
            _expire_active_company_evidence(
                state, now, self._eligibility_policy
            )
            return tuple(
                item["name"]
                for _, item in sorted(state["active_companies"].items())
                if item.get("status", "active") == "active"
            )

        return self._store.transact(operation)

    def company_monitoring_status(self, name: str) -> str | None:
        now = _aware_utc(self._clock.now())

        def operation(state: dict[str, Any]) -> str | None:
            _expire_active_company_evidence(
                state, now, self._eligibility_policy
            )
            item = state["active_companies"].get(company_key(name))
            return None if item is None else str(item.get("status", "active"))

        return self._store.transact(operation)

    def propose_companies(
        self, candidates: Iterable[CompanyCandidate]
    ) -> tuple[CompanyProposal, ...]:
        now = _aware_utc(self._clock.now())
        candidate_values = tuple(candidates)

        def operation(state: dict[str, Any]) -> tuple[CompanyProposal, ...]:
            current_candidates = tuple(
                current
                for candidate in candidate_values
                if (
                    current := _record_company_evidence(
                        state,
                        candidate,
                        now,
                        self._eligibility_policy,
                    )
                )
                is not None
            )
            recent = sum(
                _parse_time(item["proposed_at"]) > now - PROPOSAL_WINDOW
                for item in state["company_proposals"].values()
            )
            available = max(0, MAX_COMPANY_PROPOSALS - recent)
            proposals: list[CompanyProposal] = []
            if available == 0:
                return ()
            proposed_versions = {
                item["evidence_version"]
                for item in state["company_proposals"].values()
            }
            for candidate in current_candidates:
                if len(proposals) >= available:
                    break
                latest = state["company_evidence"].get(company_key(candidate.name))
                if (
                    latest is None
                    or latest.get("conflict")
                    or latest.get("evidence_version") != candidate.evidence_version
                ):
                    continue
                if not _candidate_is_currently_eligible(
                    candidate, now, self._eligibility_policy
                ):
                    continue
                active = state["active_companies"].get(company_key(candidate.name))
                if active is not None and active.get("status", "active") == "active":
                    continue
                if candidate.evidence_version in proposed_versions:
                    continue
                proposal_id = "company-" + candidate.evidence_version[:20]
                proposed_at = now.isoformat()
                state["company_proposals"][proposal_id] = {
                    "candidate": candidate.to_dict(),
                    "evidence_version": candidate.evidence_version,
                    "proposed_at": proposed_at,
                    "status": "pending",
                }
                proposed_versions.add(candidate.evidence_version)
                proposals.append(
                    CompanyProposal(
                        proposal_id=proposal_id,
                        candidate=candidate,
                        evidence_version=candidate.evidence_version,
                        proposed_at=proposed_at,
                    )
                )
            return tuple(proposals)

        return self._store.transact(operation)

    def issue_company_authorization(
        self,
        proposal: CompanyProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
        ttl: timedelta = DEFAULT_CALLBACK_TTL,
    ) -> str:
        return self._issue_authorization(
            kind="company",
            proposal_id=proposal.proposal_id,
            version=proposal.evidence_version,
            intended_actor=intended_actor,
            intended_chat_id=intended_chat_id,
            ttl=ttl,
        )

    def approve_company_authorization(
        self, token: str, *, actor: str, chat_id: str
    ) -> DecisionResult:
        def operation(state: dict[str, Any]) -> DecisionResult:
            authorization = _matching_authorization(
                state, token, kind="company", actor=actor, chat_id=chat_id
            )
            if authorization is None:
                return DecisionResult("mismatched")
            proposal_id = str(authorization["proposal_id"])
            if authorization.get("consumed_at") is not None:
                if authorization.get("outcome") == "superseded":
                    return DecisionResult("superseded", proposal_id)
                return DecisionResult("replayed", proposal_id)
            now = _aware_utc(self._clock.now())
            if now >= _parse_time(str(authorization["expires_at"])):
                _consume_authorization(authorization, now, "expired")
                return DecisionResult("expired", proposal_id)
            record = state["company_proposals"].get(proposal_id)
            if (
                record is None
                or record["evidence_version"] != authorization["version"]
            ):
                _consume_authorization(authorization, now, "mismatched")
                return DecisionResult("mismatched", proposal_id)
            if record["status"] == "approved":
                _consume_authorization(authorization, now, "replayed")
                return DecisionResult("replayed", proposal_id)
            if record["status"] != "pending":
                _consume_authorization(authorization, now, "mismatched")
                return DecisionResult("mismatched", proposal_id)
            candidate = CompanyCandidate.from_dict(record["candidate"])
            latest = state.setdefault("company_evidence", {}).get(
                company_key(candidate.name)
            )
            if (
                latest is not None
                and latest.get("evidence_version") != record["evidence_version"]
            ):
                _consume_authorization(authorization, now, "superseded")
                record["status"] = "superseded"
                return DecisionResult("superseded", proposal_id)
            if not _candidate_is_currently_eligible(
                candidate, now, self._eligibility_policy
            ):
                _consume_authorization(authorization, now, "stale")
                return DecisionResult("stale", proposal_id)
            _consume_authorization(authorization, now, "monitoring_activated")
            record.update(
                {
                    "status": "approved",
                    "approved_by": actor,
                    "approved_at": now.isoformat(),
                }
            )
            state["active_companies"][company_key(candidate.name)] = {
                "name": candidate.name,
                "source": "approved_proposal",
                "proposal_id": proposal_id,
                "evidence_version": authorization["version"],
                "status": "active",
            }
            return DecisionResult("monitoring_activated", proposal_id)

        return self._store.transact(operation)

    def propose_job_alert(self, alert: JobAlertCandidate) -> JobAlertProposal:
        _validate_alert(alert)
        now = _aware_utc(self._clock.now())
        base_proposal_id = "alert-" + alert.version[:20]

        def operation(state: dict[str, Any]) -> JobAlertProposal:
            matching = [
                (identifier, record)
                for identifier, record in state["alert_proposals"].items()
                if record["version"] == alert.version
            ]
            if matching:
                proposal_id, record = max(
                    matching, key=lambda item: int(item[1].get("revision", 0))
                )
            else:
                proposal_id, record = base_proposal_id, None
            if record is not None and record["status"] == "failed":
                revision = int(record.get("revision", 0)) + 1
                proposal_id = f"{base_proposal_id}-r{revision}"
                record = None
            record = state["alert_proposals"].get(proposal_id)
            if record is None:
                record = {
                    "alert": alert.to_dict(),
                    "version": alert.version,
                    "proposed_at": now.isoformat(),
                    "status": "pending",
                    "revision": (
                        0
                        if proposal_id == base_proposal_id
                        else int(proposal_id.rsplit("-r", 1)[1])
                    ),
                }
                state["alert_proposals"][proposal_id] = record
            return JobAlertProposal(
                proposal_id=proposal_id,
                alert=JobAlertCandidate.from_dict(record["alert"]),
                version=str(record["version"]),
                proposed_at=str(record["proposed_at"]),
            )

        return self._store.transact(operation)

    def issue_subscription_authorization(
        self,
        proposal: JobAlertProposal,
        *,
        intended_actor: str,
        intended_chat_id: str,
        ttl: timedelta = DEFAULT_CALLBACK_TTL,
    ) -> str:
        return self._issue_authorization(
            kind="alert",
            proposal_id=proposal.proposal_id,
            version=proposal.version,
            intended_actor=intended_actor,
            intended_chat_id=intended_chat_id,
            ttl=ttl,
        )

    def confirm_subscription_authorization(
        self, token: str, *, actor: str, chat_id: str
    ) -> DecisionResult | SubscriptionReport:
        prepared = self._prepare_subscription_authorization(
            token, actor=actor, chat_id=chat_id
        )
        if isinstance(prepared, _ExpiredSubscriptionAttempt):
            return self._recover_expired_subscription_attempt(prepared.proposal_id)
        if isinstance(prepared, (DecisionResult, SubscriptionReport)):
            return prepared
        proposal_id = prepared.proposal_id
        with self._store.subscription_attempt_lock(
            proposal_id, blocking=True
        ) as acquired:
            if not acquired:
                return DecisionResult("in_progress", proposal_id)
            existing = self._existing_subscription_outcome(
                prepared
            )
            if existing is not None:
                return existing
            try:
                outcome = self._subscription_executor.subscribe(
                    prepared.alert, idempotency_key=prepared.idempotency_key
                )
                status = str(outcome.get("status", "")).strip().casefold()
                if status not in {"subscribed", "failed", "uncertain"}:
                    report = SubscriptionReport(
                        status="uncertain",
                        proposal_id=proposal_id,
                        idempotency_key=prepared.idempotency_key,
                        source=prepared.alert.source,
                        expected_coverage=prepared.alert.expected_coverage,
                        error_type="InvalidSubscriptionOutcome",
                    )
                else:
                    report = SubscriptionReport(
                        status=status,
                        proposal_id=proposal_id,
                        idempotency_key=prepared.idempotency_key,
                        source=prepared.alert.source,
                        expected_coverage=prepared.alert.expected_coverage,
                        external_reference=(
                            None
                            if outcome.get("external_reference") is None
                            else str(outcome["external_reference"])
                        ),
                    )
            except SubscriptionDefinitiveError as exc:
                report = SubscriptionReport(
                    status="failed",
                    proposal_id=proposal_id,
                    idempotency_key=prepared.idempotency_key,
                    source=prepared.alert.source,
                    expected_coverage=prepared.alert.expected_coverage,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                report = SubscriptionReport(
                    status="uncertain",
                    proposal_id=proposal_id,
                    idempotency_key=prepared.idempotency_key,
                    source=prepared.alert.source,
                    expected_coverage=prepared.alert.expected_coverage,
                    error_type=type(exc).__name__,
                )
            return self._record_subscription_report(
                report,
                attempt=prepared,
            )

    def reconcile_subscription(
        self,
        proposal_id: str,
        *,
        subscribed: bool,
        external_reference: str | None = None,
    ) -> SubscriptionReport:
        with self._store.subscription_attempt_lock(
            proposal_id, blocking=False
        ) as acquired:
            if not acquired:
                raise ValueError("An active subscription attempt cannot be reconciled")

            def operation(state: dict[str, Any]) -> SubscriptionReport:
                record = state["alert_proposals"].get(proposal_id)
                now = _aware_utc(self._clock.now())
                if record is not None and record.get("status") == "attempting":
                    if _subscription_lease_is_active(record, now):
                        raise ValueError(
                            "An active subscription attempt cannot be reconciled"
                        )
                    _persist_recovered_uncertain(record, proposal_id, now)
                if (
                    record is None
                    or record.get("report", {}).get("status") != "uncertain"
                ):
                    raise ValueError("Only an uncertain subscription can be reconciled")
                previous = SubscriptionReport.from_dict(record["report"])
                report = SubscriptionReport(
                    status="subscribed" if subscribed else "failed",
                    proposal_id=proposal_id,
                    idempotency_key=previous.idempotency_key,
                    source=previous.source,
                    expected_coverage=previous.expected_coverage,
                    external_reference=external_reference if subscribed else None,
                    error_type=None if subscribed else "ReconciledAbsent",
                )
                record["status"] = report.status
                record["report"] = report.to_dict()
                return report

            return self._store.transact(operation)

    def _prepare_subscription_authorization(
        self, token: str, *, actor: str, chat_id: str
    ) -> (
        _ExpiredSubscriptionAttempt
        | DecisionResult
        | SubscriptionReport
        | _PreparedSubscriptionAttempt
    ):
        def operation(state: dict[str, Any]):
            authorization = _matching_authorization(
                state, token, kind="alert", actor=actor, chat_id=chat_id
            )
            if authorization is None:
                return DecisionResult("mismatched")
            proposal_id = str(authorization["proposal_id"])
            record = state["alert_proposals"].get(proposal_id)
            now = _aware_utc(self._clock.now())
            if authorization.get("consumed_at") is not None:
                if record is not None and record.get("report") is not None:
                    return SubscriptionReport.from_dict(record["report"])
                if record is not None and record["status"] == "attempting":
                    return _replay_subscription_attempt(record, proposal_id, now)
                return DecisionResult("replayed", proposal_id)
            if now >= _parse_time(str(authorization["expires_at"])):
                _consume_authorization(authorization, now, "expired")
                return DecisionResult("expired", proposal_id)
            if record is None or record["version"] != authorization["version"]:
                _consume_authorization(authorization, now, "mismatched")
                return DecisionResult("mismatched", proposal_id)
            if record.get("report") is not None:
                _consume_authorization(authorization, now, "replayed")
                return SubscriptionReport.from_dict(record["report"])
            if record["status"] == "attempting":
                replay = _replay_subscription_attempt(record, proposal_id, now)
                _consume_authorization(
                    authorization,
                    now,
                    (
                        "attempt_expired"
                        if isinstance(replay, _ExpiredSubscriptionAttempt)
                        else replay.status
                    ),
                )
                return replay
            if record["status"] != "pending":
                _consume_authorization(authorization, now, "mismatched")
                return DecisionResult("mismatched", proposal_id)
            key = _subscription_key(str(authorization["version"]))
            attempt_token = secrets.token_urlsafe(24)
            _consume_authorization(authorization, now, "attempting")
            record.update(
                {
                    "status": "attempting",
                    "confirmed_by": actor,
                    "confirmed_at": now.isoformat(),
                    "idempotency_key": key,
                    "attempt_owner": self._subscription_owner,
                    "attempt_token": attempt_token,
                    "attempt_lease_expires_at": (
                        now + SUBSCRIPTION_ATTEMPT_LEASE
                    ).isoformat(),
                }
            )
            return _PreparedSubscriptionAttempt(
                proposal_id=proposal_id,
                alert=JobAlertCandidate.from_dict(record["alert"]),
                idempotency_key=key,
                owner=self._subscription_owner,
                token=attempt_token,
            )

        return self._store.transact(operation)

    def _issue_authorization(
        self,
        *,
        kind: str,
        proposal_id: str,
        version: str,
        intended_actor: str,
        intended_chat_id: str,
        ttl: timedelta,
    ) -> str:
        if ttl <= timedelta(0) or ttl > DEFAULT_CALLBACK_TTL:
            raise ValueError("Callback authorization TTL must be within 30 minutes")
        if not intended_actor.strip() or not intended_chat_id.strip():
            raise ValueError("Callback authorization requires actor and chat")
        now = _aware_utc(self._clock.now())

        def operation(state: dict[str, Any]) -> str:
            if kind == "company":
                record = state["company_proposals"].get(proposal_id)
                record_version = None if record is None else record["evidence_version"]
            else:
                record = state["alert_proposals"].get(proposal_id)
                record_version = None if record is None else record["version"]
            if record is None or record_version != version:
                raise ValueError("Cannot authorize a missing or changed proposal")
            authorizations = state.setdefault("callback_authorizations", {})
            token = secrets.token_urlsafe(18)
            while token in authorizations:
                token = secrets.token_urlsafe(18)
            authorizations[token] = {
                "kind": kind,
                "proposal_id": proposal_id,
                "version": version,
                "intended_actor": intended_actor,
                "intended_chat_id": intended_chat_id,
                "issued_at": now.isoformat(),
                "expires_at": (now + ttl).isoformat(),
                "consumed_at": None,
                "outcome": None,
            }
            return token

        return self._store.transact(operation)

    def _record_subscription_report(
        self,
        report: SubscriptionReport,
        *,
        attempt: _PreparedSubscriptionAttempt,
    ) -> SubscriptionReport:
        def operation(state: dict[str, Any]) -> SubscriptionReport:
            record = state["alert_proposals"].get(report.proposal_id)
            if (
                record is None
                or record.get("idempotency_key") != report.idempotency_key
            ):
                raise ValueError("Subscription intent changed before outcome recording")
            if record.get("report") is not None:
                return SubscriptionReport.from_dict(record["report"])
            if (
                record.get("status") != "attempting"
                or not secrets.compare_digest(
                    str(record.get("attempt_owner", "")), attempt.owner
                )
                or not secrets.compare_digest(
                    str(record.get("attempt_token", "")), attempt.token
                )
            ):
                raise ValueError("Subscription attempt lease is no longer owned")
            now = _aware_utc(self._clock.now())
            record["status"] = report.status
            record["report"] = report.to_dict()
            record["attempt_finalized_at"] = now.isoformat()
            return report

        return self._store.transact(operation)

    def _existing_subscription_outcome(
        self,
        attempt: _PreparedSubscriptionAttempt,
    ) -> DecisionResult | SubscriptionReport | None:
        def operation(state: dict[str, Any]):
            record = state["alert_proposals"].get(attempt.proposal_id)
            if (
                record is None
                or record.get("idempotency_key") != attempt.idempotency_key
            ):
                return DecisionResult("mismatched", attempt.proposal_id)
            if record.get("report") is not None:
                return SubscriptionReport.from_dict(record["report"])
            if (
                record.get("status") != "attempting"
                or not secrets.compare_digest(
                    str(record.get("attempt_owner", "")), attempt.owner
                )
                or not secrets.compare_digest(
                    str(record.get("attempt_token", "")), attempt.token
                )
            ):
                return DecisionResult("in_progress", attempt.proposal_id)
            return None

        return self._store.transact(operation)

    def _recover_expired_subscription_attempt(
        self, proposal_id: str
    ) -> DecisionResult | SubscriptionReport:
        with self._store.subscription_attempt_lock(
            proposal_id, blocking=False
        ) as acquired:
            if not acquired:
                return DecisionResult("in_progress", proposal_id)

            def operation(state: dict[str, Any]):
                record = state["alert_proposals"].get(proposal_id)
                if record is None:
                    return DecisionResult("mismatched", proposal_id)
                if record.get("report") is not None:
                    return SubscriptionReport.from_dict(record["report"])
                now = _aware_utc(self._clock.now())
                if record.get("status") != "attempting":
                    return DecisionResult("replayed", proposal_id)
                if _subscription_lease_is_active(record, now):
                    return DecisionResult("in_progress", proposal_id)
                return _persist_recovered_uncertain(record, proposal_id, now)

            return self._store.transact(operation)


def _candidate_is_currently_eligible(
    candidate: CompanyCandidate,
    now: datetime,
    policy: CompanyEligibilityPolicy = CompanyEligibilityPolicy(),
) -> bool:
    required_text = (
        candidate.name,
        candidate.careers_url,
        candidate.jurisdiction,
        candidate.discovery_source,
    )
    if any(not item.strip() for item in required_text):
        return False
    if not _valid_https_source(candidate.careers_url) or not _valid_https_source(
        candidate.discovery_source
    ):
        return False
    if not _valid_country_code(candidate.jurisdiction_country_code):
        return False
    if (
        str(candidate.jurisdiction_country_code).strip().upper()
        in policy.excluded_country_codes
    ):
        return False
    ownership = _normalized_classification(candidate.ownership.classification)
    if (
        ownership in UNVERIFIED_CLASSIFICATIONS
        or ownership in policy.excluded_ownership
    ):
        return False
    sponsorship = _normalized_classification(candidate.sponsorship.classification)
    if sponsorship not in ALLOWED_SPONSORSHIP:
        return False
    return _evidence_is_current(candidate.ownership, now) and _evidence_is_current(
        candidate.sponsorship, now
    )


def _record_company_evidence(
    state: dict[str, Any],
    candidate: CompanyCandidate,
    now: datetime,
    policy: CompanyEligibilityPolicy = CompanyEligibilityPolicy(),
) -> CompanyCandidate | None:
    key = company_key(candidate.name)
    evidence = state.setdefault("company_evidence", {})
    previous = evidence.get(key)

    incoming_facts = {
        "ownership": candidate.ownership,
        "sponsorship": candidate.sponsorship,
    }
    incoming_times = {
        name: _usable_evidence_time(value, now)
        for name, value in incoming_facts.items()
    }
    if previous is None:
        if any(value is None for value in incoming_times.values()):
            return None
        merged_facts = incoming_facts
        conflicts = {"ownership": False, "sponsorship": False}
    else:
        old = CompanyCandidate.from_dict(previous["candidate"])
        old_facts = {"ownership": old.ownership, "sponsorship": old.sponsorship}
        old_times = {
            name: _usable_evidence_time(value, now)
            for name, value in old_facts.items()
        }
        legacy_conflict = bool(previous.get("conflict", False))
        stored_conflicts = previous.get("conflicts") or {
            "ownership": legacy_conflict,
            "sponsorship": legacy_conflict,
        }
        conflicts = {
            "ownership": bool(stored_conflicts.get("ownership", False)),
            "sponsorship": bool(stored_conflicts.get("sponsorship", False)),
        }
        merged_facts: dict[str, EligibilityEvidence] = {}
        for name in ("ownership", "sponsorship"):
            incoming_time = incoming_times[name]
            old_time = old_times[name]
            if incoming_time is None:
                merged_facts[name] = old_facts[name]
                continue
            if old_time is None or incoming_time > old_time:
                merged_facts[name] = incoming_facts[name]
                conflicts[name] = False
            elif incoming_time == old_time and incoming_facts[name] != old_facts[name]:
                merged_facts[name] = old_facts[name]
                conflicts[name] = True
            else:
                merged_facts[name] = old_facts[name]
        candidate = CompanyCandidate(
            name=candidate.name,
            careers_url=candidate.careers_url,
            jurisdiction=candidate.jurisdiction,
            ownership=merged_facts["ownership"],
            sponsorship=merged_facts["sponsorship"],
            discovery_source=candidate.discovery_source,
            jurisdiction_country_code=candidate.jurisdiction_country_code,
        )
    eligible = _candidate_is_currently_eligible(candidate, now, policy)
    has_conflict = any(conflicts.values())
    evidence[key] = {
        "candidate": candidate.to_dict(),
        "evidence_version": candidate.evidence_version,
        "observed_at": now.isoformat(),
        "ownership_time": candidate.ownership.verified_at,
        "sponsorship_time": candidate.sponsorship.verified_at,
        "eligible": eligible,
        "conflict": has_conflict,
        "conflicts": conflicts,
    }
    if has_conflict:
        _supersede_company_state(state, key, now, candidate.evidence_version)
    for proposal_id, proposal in state["company_proposals"].items():
        proposal_candidate = CompanyCandidate.from_dict(proposal["candidate"])
        if company_key(proposal_candidate.name) != key:
            continue
        if (
            proposal["status"] == "pending"
            and proposal["evidence_version"] != candidate.evidence_version
        ):
            proposal["status"] = "superseded"
            for authorization in state.setdefault(
                "callback_authorizations", {}
            ).values():
                if (
                    authorization.get("kind") == "company"
                    and authorization.get("proposal_id") == proposal_id
                    and authorization.get("consumed_at") is None
                ):
                    _consume_authorization(authorization, now, "superseded")
    active = state["active_companies"].get(key)
    if (
        active is not None
        and active.get("status", "active") == "active"
        and (
            active.get("evidence_version") != candidate.evidence_version
            or not eligible
        )
    ):
        active.update(
            {
                "status": "review_required",
                "review_reason": (
                    "ineligible_current_evidence"
                    if not eligible
                    else "material_evidence_change"
                ),
                "latest_evidence_version": candidate.evidence_version,
            }
        )
    return None if has_conflict else candidate


def _supersede_company_state(
    state: dict[str, Any], key: str, now: datetime, version: str
) -> None:
    for proposal_id, proposal in state["company_proposals"].items():
        candidate = CompanyCandidate.from_dict(proposal["candidate"])
        if company_key(candidate.name) != key or proposal["status"] != "pending":
            continue
        proposal["status"] = "superseded"
        for authorization in state.setdefault("callback_authorizations", {}).values():
            if (
                authorization.get("proposal_id") == proposal_id
                and authorization.get("consumed_at") is None
            ):
                _consume_authorization(authorization, now, "superseded")
    active = state["active_companies"].get(key)
    if active is not None and active.get("status", "active") == "active":
        active.update({
            "status": "review_required",
            "review_reason": "conflicting_equal_timestamp_evidence",
            "latest_evidence_version": version,
        })


def _expire_active_company_evidence(
    state: dict[str, Any],
    now: datetime,
    policy: CompanyEligibilityPolicy = CompanyEligibilityPolicy(),
) -> None:
    evidence_by_company = state.setdefault("company_evidence", {})
    for key, active in state["active_companies"].items():
        if active.get("status", "active") != "active":
            continue
        evidence = evidence_by_company.get(key)
        if evidence is None:
            active.update(
                status="review_required",
                review_reason="missing_current_evidence",
            )
            continue
        try:
            candidate = CompanyCandidate.from_dict(evidence["candidate"])
        except (KeyError, TypeError, ValueError):
            active.update(
                status="review_required",
                review_reason="invalid_current_evidence",
            )
            continue
        ownership_current = _evidence_is_current(candidate.ownership, now)
        sponsorship_current = _evidence_is_current(candidate.sponsorship, now)
        jurisdiction_verified = _valid_country_code(
            candidate.jurisdiction_country_code
        )
        version = str(evidence.get("evidence_version", ""))
        if (
            ownership_current
            and sponsorship_current
            and jurisdiction_verified
            and not evidence.get("conflict")
            and active.get("evidence_version") == version
            and _candidate_is_currently_eligible(candidate, now, policy)
        ):
            continue
        if not jurisdiction_verified:
            reason = "unverified_jurisdiction"
        elif not ownership_current or not sponsorship_current:
            reason = "expired_evidence"
        elif evidence.get("conflict"):
            reason = "conflicting_current_evidence"
        elif active.get("evidence_version") != version:
            reason = "material_evidence_change"
        else:
            reason = "ineligible_current_evidence"
        active.update(
            status="review_required",
            review_reason=reason,
            latest_evidence_version=version or None,
            review_required_at=now.isoformat(),
        )


def _normalized_classification(value: str) -> str:
    return "_".join(value.strip().casefold().replace("-", " ").split())


def _valid_country_code(value: str | None) -> bool:
    if value is None or not str(value).strip():
        return False
    return str(value).strip().upper() in ISO_3166_ALPHA2_CODES


def _usable_evidence_time(
    evidence: EligibilityEvidence, now: datetime
) -> datetime | None:
    try:
        value = _parse_time(evidence.verified_at)
    except (TypeError, ValueError):
        return None
    return value if value <= now else None


def _evidence_is_current(evidence: EligibilityEvidence, now: datetime) -> bool:
    if (
        evidence.classification.strip().casefold() in UNVERIFIED_CLASSIFICATIONS
        or not _valid_https_source(evidence.source_url)
    ):
        return False
    try:
        verified_at = _parse_time(evidence.verified_at)
    except (TypeError, ValueError):
        return False
    return timedelta(0) <= now - verified_at <= EVIDENCE_MAX_AGE


def _valid_https_source(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
        hostname = str(parsed.hostname or "").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return False
    valid_host = (
        len(hostname) <= 253
        and "." in hostname
        and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is not None
            for label in hostname.split(".")
        )
        and any(character.isalpha() for character in hostname.rsplit(".", 1)[-1])
    )
    return (
        parsed.scheme.casefold() == "https"
        and valid_host
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _validate_alert(alert: JobAlertCandidate) -> None:
    if any(
        not value.strip()
        for value in (
            alert.source,
            alert.source_url,
            alert.expected_coverage,
            alert.query,
            alert.location,
        )
    ):
        raise ValueError("Job-alert source, coverage, query and location are required")


def _parse_time(value: str) -> datetime:
    return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _subscription_key(version: str) -> str:
    material = version.encode("utf-8")
    return "job-alert:" + hashlib.sha256(material).hexdigest()


def _subscription_lease_is_active(
    record: dict[str, Any], now: datetime
) -> bool:
    try:
        lease_expires = _parse_time(str(record["attempt_lease_expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return now < lease_expires


def _replay_subscription_attempt(
    record: dict[str, Any], proposal_id: str, now: datetime
) -> DecisionResult | _ExpiredSubscriptionAttempt:
    if _subscription_lease_is_active(record, now):
        return DecisionResult("in_progress", proposal_id)
    return _ExpiredSubscriptionAttempt(proposal_id)


def _persist_recovered_uncertain(
    record: dict[str, Any], proposal_id: str, now: datetime
) -> SubscriptionReport:
    alert = JobAlertCandidate.from_dict(record["alert"])
    report = SubscriptionReport(
        status="uncertain",
        proposal_id=proposal_id,
        idempotency_key=str(record["idempotency_key"]),
        source=alert.source,
        expected_coverage=alert.expected_coverage,
        error_type="RecoveredExpiredAttemptingLease",
    )
    record["status"] = report.status
    record["report"] = report.to_dict()
    record["attempt_recovered_at"] = now.isoformat()
    return report


def _matching_authorization(
    state: dict[str, Any],
    token: str,
    *,
    kind: str,
    actor: str,
    chat_id: str,
) -> dict[str, Any] | None:
    authorization = state.setdefault("callback_authorizations", {}).get(token)
    if authorization is None or authorization.get("kind") != kind:
        return None
    if not secrets.compare_digest(str(authorization["intended_actor"]), actor):
        return None
    if not secrets.compare_digest(str(authorization["intended_chat_id"]), chat_id):
        return None
    return authorization


def _consume_authorization(
    authorization: dict[str, Any], now: datetime, outcome: str
) -> None:
    authorization["consumed_at"] = now.isoformat()
    authorization["outcome"] = outcome
