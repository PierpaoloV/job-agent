"""Owner-local Telegram decisions for exact verified opportunity versions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from application_domain import WorkflowAction
from application_identity import approved_application_id
from local_worker_store import WorkerCommand
from local_worker_telegram import (
    CallbackContext,
    CallbackRoute,
    TelegramUpdateStore,
)


_PAYLOAD_PREFIX = "opportunity:"
_ACTIONS = (
    ("👍", "prepare"),
    ("👎", "discard"),
    ("Dimmi di più", "details"),
)
_DECISION_STATE_VERSION = "job-agent.opportunity-decisions.v2"


class FileOpportunityDecisionStore:
    """Persist exact-version discards locally without creating a broad block."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def discard(
        self,
        application_id: str,
        vacancy_version: str,
        *,
        job: Mapping[str, Any],
        reason: str = "Scartata dall'utente via Telegram",
    ) -> None:
        state = self._load()
        key = f"{application_id}@{vacancy_version}"
        material = _material_values(job)
        state["discards"][key] = {
            "application_id": application_id,
            "vacancy_version": vacancy_version,
            "reason": reason,
            "role_similarity_key": _role_similarity_key(job),
            "material_fingerprint": _fingerprint(material),
            "material_values": material,
        }
        self._write(state)

    def is_discarded(self, application_id: str, vacancy_version: str) -> bool:
        return (
            f"{application_id}@{vacancy_version}"
            in self._load()["discards"]
        )

    def suppresses(self, job: Mapping[str, Any]) -> bool:
        role_key = _role_similarity_key(job)
        fingerprint = _fingerprint(_material_values(job))
        return any(
            isinstance(discard, Mapping)
            and discard.get("role_similarity_key") == role_key
            and discard.get("material_fingerprint") == fingerprint
            for discard in self._load()["discards"].values()
        )

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {
                "version": _DECISION_STATE_VERSION,
                "discards": {},
            }
        value = json.loads(self._path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != _DECISION_STATE_VERSION
            or not isinstance(value.get("discards"), dict)
        ):
            raise ValueError("Opportunity decision state is invalid")
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path.parent.chmod(0o700)
        temporary = self._path.with_suffix(".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._path)
        self._path.chmod(0o600)


class OpportunityDecisionService:
    """Execute locally authorized role decisions against exact hosted inputs."""

    def __init__(
        self,
        *,
        inputs,
        coordinator,
        job_lookup: Callable[[str, str], Mapping[str, Any]],
        decisions: FileOpportunityDecisionStore,
        actor: str,
        send_status: Callable[[str], None],
    ) -> None:
        self._inputs = inputs
        self._coordinator = coordinator
        self._job_lookup = job_lookup
        self._decisions = decisions
        self._actor = str(actor)
        self._send_status = send_status

    def __call__(
        self,
        action: str,
        application_id: str,
        vacancy_version: str,
        before_external_action: Callable[[], Any] | None = None,
    ) -> str:
        gate = before_external_action or (lambda: None)
        prepared_input = self._inputs.load(application_id, vacancy_version)
        official = prepared_input.official_vacancy
        if official.version != vacancy_version:
            raise ValueError("Opportunity decision vacancy version mismatch")
        job = dict(self._job_lookup(application_id, vacancy_version))
        if action == "details":
            for message in opportunity_details_messages(job, prepared_input):
                gate()
                self._send_status(message)
            return "Dettagli inviati"
        if action == "discard":
            self._decisions.discard(
                application_id,
                vacancy_version,
                job=job,
            )
            return "Opportunità scartata"
        if action != "prepare":
            raise ValueError("Unsupported opportunity decision")
        try:
            application = self._coordinator.get(application_id)
        except KeyError:
            application = self._coordinator.propose(
                application_id=application_id,
                opportunity={
                    **job,
                    "application_id": application_id,
                    "official_vacancy_version": vacancy_version,
                },
                version=vacancy_version,
            )
        else:
            if application.opportunity_version != vacancy_version:
                raise ValueError("Application vacancy version mismatch")
        command = self._coordinator.issue_authorization(
            application_id,
            WorkflowAction.PREPARE,
            actor=self._actor,
        )
        gate()
        result = self._coordinator.handle(command)
        status = str(result.status.value)
        if status in {"accepted", "completed"}:
            return "Preparazione CV avviata"
        return f"Preparazione non avviata: {status}"


class SuppressingDiscoveryNotifier:
    """Apply local conditional discards before crossing Telegram."""

    def __init__(self, notifier, decisions: FileOpportunityDecisionStore) -> None:
        self._notifier = notifier
        self._decisions = decisions

    def send_alert(self, job, **kwargs) -> None:
        if self._decisions.suppresses(job):
            return
        self._notifier.send_alert(job, **kwargs)

    def send_digest(self, jobs, **kwargs) -> None:
        visible = [
            job for job in jobs if not self._decisions.suppresses(job)
        ]
        if not visible:
            return
        self._notifier.send_digest(visible, **kwargs)


class ScheduleJobLookup:
    """Resolve one application identity from synchronized public schedule state."""

    def __init__(self, store) -> None:
        self._store = store

    def __call__(
        self, application_id: str, vacancy_version: str
    ) -> Mapping[str, Any]:
        state = self._store.load()
        roles = state.get("roles", {})
        if not isinstance(roles, Mapping):
            raise ValueError("Discovery schedule roles are invalid")
        matches = []
        for record in roles.values():
            if not isinstance(record, Mapping) or not isinstance(
                record.get("job"), Mapping
            ):
                continue
            job = record["job"]
            stable_id = str(job.get("stable_id", "")).strip()
            version = str(
                job.get("official_vacancy_version", "")
            ).strip()
            if (
                version == vacancy_version
                and approved_application_id(stable_id, version)
                == application_id
            ):
                matches.append(dict(job))
        if not matches:
            raise KeyError((application_id, vacancy_version))
        if len(matches) != 1:
            raise RuntimeError("Opportunity schedule identity is ambiguous")
        return matches[0]


class OpportunityButtonFactory:
    """Issue compact one-use buttons whose full scope stays on the Mac."""

    def __init__(
        self,
        *,
        store: TelegramUpdateStore,
        worker,
        actor_id: str,
        chat_id: str,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Opportunity callback TTL must be positive")
        self._store = store
        self._worker = worker
        self._actor_id = str(actor_id)
        self._chat_id = str(chat_id)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl

    def __call__(self, job: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
        stable_id = str(job.get("stable_id", "")).strip()
        vacancy_version = str(
            job.get("official_vacancy_version", "")
        ).strip()
        application_id = approved_application_id(stable_id, vacancy_version)
        status = self._worker.status()
        if str(status.get("state")) != WorkerCommand.RESUME.value:
            raise RuntimeError("Worker must be resumed before issuing role buttons")
        try:
            resume_generation = int(status["resume_generation"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Worker resume generation is unavailable") from None
        buttons = []
        for label, action in _ACTIONS:
            payload = _encode_payload(
                action=action,
                application_id=application_id,
                vacancy_version=vacancy_version,
            )
            authorization = self._store.issue_callback_authorization(
                actor_id=self._actor_id,
                chat_id=self._chat_id,
                route="opportunities",
                capability="opportunity_decisions",
                payload=payload,
                resume_generation=resume_generation,
                expires_at=self._now() + self._ttl,
            )
            buttons.append(
                {
                    "text": label,
                    "callback_data": authorization.callback_data,
                }
            )
        return tuple(buttons)


def build_opportunity_callback_route(
    handler: Callable[[str, str, str, Callable[[], Any]], str],
    *,
    state_sync: Callable[[], Any] | None = None,
    refresh_handler: (
        Callable[[str, str, str, Callable[[], Any]], str] | None
    ) = None,
) -> CallbackRoute:
    """Route a verified local authorization to one opportunity decision."""

    if not callable(handler):
        raise TypeError("Opportunity decision handler must be callable")

    def synchronize(execution) -> None:
        if state_sync is not None:
            execution.checkpoint()
            synchronized = state_sync()
            if synchronized is False:
                raise RuntimeError(
                    "No authoritative Actions state is available"
                )

    def handle(execution, context: CallbackContext) -> str:
        action, application_id, vacancy_version = _decode_payload(context.payload)
        synchronize(execution)
        return str(
            handler(
                action,
                application_id,
                vacancy_version,
                execution.checkpoint,
            )
        )

    def refresh(execution, context: CallbackContext) -> str:
        _, application_id, vacancy_version = _decode_payload(context.payload)
        synchronize(execution)
        assert refresh_handler is not None
        return str(
            refresh_handler(
                application_id,
                vacancy_version,
                context.authorization.token,
                execution.checkpoint,
            )
        )

    return CallbackRoute(
        route="opportunities",
        prefixes=(_PAYLOAD_PREFIX,),
        capability="opportunity_decisions",
        handler=handle,
        stale_handler=(refresh if refresh_handler is not None else None),
        recover_stale_replay=refresh_handler is not None,
    )


def _encode_payload(
    *, action: str, application_id: str, vacancy_version: str
) -> str:
    if action not in {value for _, value in _ACTIONS}:
        raise ValueError("Unsupported opportunity decision")
    return _PAYLOAD_PREFIX + json.dumps(
        [action, application_id, vacancy_version],
        separators=(",", ":"),
    )


def _decode_payload(payload: str) -> tuple[str, str, str]:
    if not payload.startswith(_PAYLOAD_PREFIX):
        raise ValueError("Unsupported opportunity callback")
    try:
        value = json.loads(payload.removeprefix(_PAYLOAD_PREFIX))
    except json.JSONDecodeError:
        raise ValueError("Opportunity callback payload is invalid") from None
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError("Opportunity callback payload is invalid")
    action, application_id, vacancy_version = value
    if action not in {item for _, item in _ACTIONS}:
        raise ValueError("Unsupported opportunity decision")
    return action, application_id, vacancy_version


__all__ = [
    "FileOpportunityDecisionStore",
    "OpportunityButtonFactory",
    "OpportunityDecisionService",
    "ScheduleJobLookup",
    "SuppressingDiscoveryNotifier",
    "build_opportunity_callback_route",
    "opportunity_details_messages",
]


def opportunity_details_messages(
    job: Mapping[str, Any],
    prepared_input,
) -> tuple[str, ...]:
    matrix = prepared_input.opportunity.get(
        "requirements_evidence_matrix", {}
    )
    rows = matrix.get("rows", []) if isinstance(matrix, Mapping) else []
    requirements = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        requirement = str(
            row.get("requirement")
            or row.get("requirement_text")
            or row.get("text")
            or "Requisito"
        )
        status = str(row.get("status", "unknown"))
        requirements.append(f"• {requirement} — {status}")
    evaluation = job.get("portfolio_evaluation", {})
    risks = (
        evaluation.get("risks", [])
        if isinstance(evaluation, Mapping)
        else []
    )
    sources = (
        evaluation.get("sources", [])
        if isinstance(evaluation, Mapping)
        else []
    )
    lines = [
        f"{job.get('company', 'N/A')} — {job.get('title', 'N/A')}",
        f"Sede: {job.get('location', 'N/A')}",
        "",
        str(prepared_input.official_vacancy.description),
    ]
    if requirements:
        lines.extend(("", "Requisiti", *requirements))
    if risks:
        lines.extend(("", "Rischi", *(f"• {item}" for item in risks)))
    links = tuple(
        dict.fromkeys(
            [
                str(job.get("url", "")).strip(),
                *(str(item).strip() for item in sources),
            ]
        )
    )
    links = tuple(link for link in links if link)
    if links:
        lines.extend(("", "Fonti", *(f"• {item}" for item in links)))
    return _telegram_chunks("\n".join(lines))


def _telegram_chunks(value: str, *, limit: int = 3800) -> tuple[str, ...]:
    remaining = str(value)
    chunks = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:].lstrip("\n")
    return tuple(chunks or ("",))


_MATERIAL_FIELDS = (
    "company",
    "title",
    "team",
    "location",
    "modality",
    "seniority",
    "compensation",
    "requirements",
    "ownership",
    "sponsorship",
    "official_job_id",
)


def _role_similarity_key(job: Mapping[str, Any]) -> str:
    return ":".join(
        re.sub(r"[^a-z0-9]+", "-", str(job.get(field, "")).casefold()).strip("-")
        for field in ("company", "title")
    )


def _material_values(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: _json_safe(job.get(field))
        for field in _MATERIAL_FIELDS
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
