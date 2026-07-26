"""Controlled end-to-end application journey backed by a fake ATS.

The journey deliberately exercises the production application coordinator and
its one-use authorizations.  Only the external systems are synthetic: PDF
tailoring writes small real documents and submission is recorded in a durable
local fake ATS instead of contacting an employer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping, Protocol

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from application_domain import (
    AnswerDisclosure,
    AnswerVisibility,
    ArtifactFamily,
    FilledApplication,
    OfficialVacancy,
    OperationalStatus,
    PreparedArtifacts,
    PreSubmitManifest,
    ReviewEvidence,
    ReviewEvidencePage,
    SubmissionEvidence,
    SubmissionOutcome,
    SubmissionStatus,
    SubmissionVerificationKind,
    WorkflowAction,
)
from application_interventions import (
    InterventionRecord,
    SubmissionInspection,
    SubmissionInspectionSource,
    SubmissionInspectionStatus,
)
from application_storage import JsonApplicationStore, MarkdownApplicationReportWriter
from application_telegram import TelegramCommandHandler, TelegramPreSubmitSummary
from application_workflow import ApplicationWorkflowCoordinator


_APPLICATION_ID = "synthetic-e2e-application"
_ACTOR = "Synthetic test owner"
_DETAILS_CALLBACK = "synthetic:details"
_DISCARD_CALLBACK = "synthetic:discard"


@dataclass(frozen=True)
class SyntheticButton:
    label: str
    callback_data: str


@dataclass(frozen=True)
class SyntheticJourneyMessage:
    title: str
    text: str
    lifecycle_state: str
    buttons: tuple[SyntheticButton, ...] = ()
    documents: tuple[Path, ...] = ()
    confirmation_id: str | None = None
    report_path: str | None = None
    terminal: bool = False


class SyntheticTelegramApi(Protocol):
    def send_journey_message(self, message: SyntheticJourneyMessage) -> None: ...
    def send_document(self, path: Path, caption: str) -> None: ...
    def acknowledge_callback(
        self, callback_query_id: str, text: str
    ) -> None: ...


class SyntheticTelegramSession:
    """Present and advance a journey for one explicitly scoped Telegram owner."""

    def __init__(
        self,
        *,
        journey: "SyntheticApplicationJourney",
        telegram: SyntheticTelegramApi,
        actor_id: str,
        chat_id: str,
    ):
        self._journey = journey
        self._telegram = telegram
        self._actor_id = str(actor_id)
        self._chat_id = str(chat_id)

    def start(self) -> SyntheticJourneyMessage:
        message = self._journey.start()
        self._deliver(message)
        return message

    def handle_update(self, update: Mapping[str, Any]) -> bool:
        callback = update.get("callback_query")
        if not isinstance(callback, Mapping):
            return False
        callback_id = str(callback.get("id", ""))
        actor = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        actor_id = actor.get("id") if isinstance(actor, Mapping) else None
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        if str(actor_id) != self._actor_id or str(chat_id) != self._chat_id:
            if callback_id:
                self._telegram.acknowledge_callback(
                    callback_id, "Questo test è riservato al proprietario."
                )
            return False
        callback_data = callback.get("data")
        if not isinstance(callback_data, str):
            if callback_id:
                self._telegram.acknowledge_callback(
                    callback_id, "Callback non valida."
                )
            return False
        response = self._journey.handle(callback_data)
        if callback_id:
            self._telegram.acknowledge_callback(callback_id, "Ricevuto")
        self._deliver(response)
        return self._is_terminal(response)

    def _deliver(self, response: SyntheticJourneyMessage) -> None:
        for document in response.documents:
            self._telegram.send_document(
                document, f"{document.name} — TEST E2E sintetico"
            )
        self._telegram.send_journey_message(response)
        if self._is_terminal(response) and response.report_path:
            self._telegram.send_document(
                Path(response.report_path),
                "Report verificabile — candidatura sintetica",
            )

    @staticmethod
    def _is_terminal(response: SyntheticJourneyMessage) -> bool:
        return response.terminal


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _SyntheticOfficialVacancies:
    def __init__(self, vacancy: OfficialVacancy):
        self._vacancy = vacancy

    def retrieve(self, opportunity: Mapping[str, Any]) -> OfficialVacancy:
        return self._vacancy

    def revalidate(
        self, opportunity: Mapping[str, Any], previous: OfficialVacancy
    ) -> OfficialVacancy:
        return self._vacancy


class _SyntheticTailoringAdapter:
    def __init__(self, root: Path):
        self._root = root

    def prepare(
        self,
        application_id: str,
        intent_id: str,
        opportunity: Mapping[str, Any],
        official_vacancy: OfficialVacancy,
    ) -> PreparedArtifacts:
        destination = self._root / application_id
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        cv_path = destination / "curriculum-vitae.pdf"
        cover_letter_path = destination / "cover-letter.pdf"
        if not cv_path.exists():
            _write_pdf(
                cv_path,
                "Synthetic Candidate",
                (
                    "Synthetic CV for an end-to-end safety test",
                    f"Target: {opportunity['title']}",
                    "Synthetic experience in computer vision and ML systems",
                    "THIS DOCUMENT IS SYNTHETIC AND MUST NOT BE SENT TO AN EMPLOYER",
                ),
            )
        if not cover_letter_path.exists():
            _write_pdf(
                cover_letter_path,
                "Synthetic cover letter",
                (
                    f"Dear {opportunity['company']} test team,",
                    "This controlled application validates the human approval gates.",
                    "No real vacancy or employer is contacted by this test.",
                    "Synthetic Candidate",
                ),
            )
        hashes = (_sha256_file(cv_path), _sha256_file(cover_letter_path))
        version = "sha256:" + hashlib.sha256(
            f"{intent_id}:{hashes[0]}:{hashes[1]}".encode()
        ).hexdigest()
        return PreparedArtifacts(
            version=version,
            cv_path=str(cv_path),
            cover_letter_path=str(cover_letter_path),
            cv_hash=hashes[0],
            cover_letter_hash=hashes[1],
            evidence_source_version="synthetic-master-cv-v1",
            matrix_version="synthetic-requirements-v1",
            family=ArtifactFamily.RESEARCH,
        )

    def reload_master_cv(self) -> str:
        return "synthetic-master-cv-v1"

    def verify_artifacts(self, artifacts: PreparedArtifacts) -> bool:
        try:
            return (
                _sha256_file(Path(artifacts.cv_path)) == artifacts.cv_hash
                and _sha256_file(Path(artifacts.cover_letter_path))
                == artifacts.cover_letter_hash
            )
        except OSError:
            return False

    def preparation_resolution(
        self,
        application_id: str,
        intent_id: str,
        official_vacancy: OfficialVacancy,
    ) -> None:
        return None


class _SyntheticAtsAdapter:
    def __init__(self, path: Path, clock: _SystemClock):
        self._path = path
        self._clock = clock

    def fill(
        self, application_id: str, intent_id: str, artifacts: PreparedArtifacts
    ) -> FilledApplication:
        existing = self._read()
        if existing.get("fill_intent_id") not in {None, intent_id}:
            raise ValueError("Fake ATS already contains a different fill intent")
        answers = {
            "work_authorization": "Synthetic answer — not for real use",
            "references": "Synthetic answer — not for real use",
        }
        state = {
            **existing,
            "application_id": application_id,
            "state": "filled",
            "fill_intent_id": intent_id,
            "artifact_version": artifacts.version,
            "artifact_hashes": {
                "cv": artifacts.cv_hash,
                "cover_letter": artifacts.cover_letter_hash,
            },
            "answers": answers,
            "submit_count": int(existing.get("submit_count", 0)),
        }
        self._write(state)
        return FilledApplication(
            answers=answers,
            artifact_version=artifacts.version,
            review_evidence=ReviewEvidence(
                page=ReviewEvidencePage.REVIEW,
                form_snapshot=answers,
                attachment_hashes=dict(state["artifact_hashes"]),
            ),
            answer_disclosures=tuple(
                AnswerDisclosure(field, AnswerVisibility.PUBLIC_SUMMARY)
                for field in answers
            ),
        )

    def validate_submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> bool:
        state = self._read()
        evidence = manifest.review_evidence
        return bool(
            state.get("application_id") == application_id
            and state.get("state") in {"filled", "submitted"}
            and state.get("artifact_version") == manifest.artifact_version
            and state.get("artifact_hashes") == manifest.artifact_hashes
            and state.get("answers") == manifest.answers
            and evidence is not None
            and evidence.page == ReviewEvidencePage.REVIEW
            and evidence.form_snapshot == manifest.answers
            and evidence.attachment_hashes == manifest.artifact_hashes
        )

    def submit(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionOutcome:
        if not self.validate_submit(application_id, manifest):
            raise ValueError("Fake ATS review state does not match the manifest")
        state = self._read()
        previous_manifest = state.get("submitted_manifest_version")
        if previous_manifest not in {None, manifest.version}:
            raise ValueError("Fake ATS already submitted a different manifest")
        confirmation_id = state.get("confirmation_id")
        if confirmation_id is None:
            confirmation_id = (
                "FAKE-ATS-"
                + hashlib.sha256(manifest.version.encode()).hexdigest()[:12].upper()
            )
            state.update(
                {
                    "state": "submitted",
                    "submit_count": int(state.get("submit_count", 0)) + 1,
                    "submitted_manifest_version": manifest.version,
                    "confirmation_id": confirmation_id,
                    "submitted_at": self._clock.now().isoformat(),
                }
            )
            self._write(state)
        evidence = self._evidence(state)
        return SubmissionOutcome(
            status=SubmissionStatus.VERIFIED,
            confirmation_id=str(confirmation_id),
            evidence=evidence,
            recorded_at=str(state["submitted_at"]),
        )

    def intervention_is_resolved(
        self, application_id: str, intervention: InterventionRecord
    ) -> bool:
        return False

    def inspect_submission(
        self, application_id: str, manifest: PreSubmitManifest
    ) -> SubmissionInspection:
        state = self._read()
        if (
            state.get("application_id") == application_id
            and state.get("state") == "submitted"
            and state.get("submitted_manifest_version") == manifest.version
        ):
            return SubmissionInspection(
                status=SubmissionInspectionStatus.VERIFIED,
                checked_at=self._clock.now().isoformat(),
                sources_checked=(SubmissionInspectionSource.ATS,),
                evidence=self._evidence(state),
            )
        return SubmissionInspection(
            status=SubmissionInspectionStatus.INCOMPLETE,
            checked_at=self._clock.now().isoformat(),
            sources_checked=(SubmissionInspectionSource.ATS,),
            sources_unavailable=(SubmissionInspectionSource.CAREER_MAILBOX,),
        )

    def status(self) -> dict[str, Any]:
        return self._read()

    @staticmethod
    def _evidence(state: Mapping[str, Any]) -> SubmissionEvidence:
        confirmation_id = str(state["confirmation_id"])
        submitted_at = str(state["submitted_at"])
        return SubmissionEvidence(
            captured_at=submitted_at,
            verified_by=(
                SubmissionVerificationKind.CONFIRMATION_PAGE,
                SubmissionVerificationKind.CONFIRMATION_ID,
                SubmissionVerificationKind.ATS_SUBMITTED,
            ),
            confirmation_page=f"fake-ats://confirmation/{confirmation_id}",
            confirmation_id=confirmation_id,
            ats_application_id=f"fake-app-{confirmation_id.removeprefix('FAKE-ATS-')}",
            ats_status="submitted",
        )

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"state": "empty", "submit_count": 0}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, value: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._path.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self._path)
        os.chmod(self._path, 0o600)


class SyntheticApplicationJourney:
    """Drive one synthetic candidate application through the real coordinator."""

    def __init__(
        self,
        *,
        root: Path,
        coordinator: ApplicationWorkflowCoordinator,
        ats: _SyntheticAtsAdapter,
    ):
        self._root = root
        self._coordinator = coordinator
        self._ats = ats
        self._handler = TelegramCommandHandler(coordinator)

    @classmethod
    def create(cls, root: Path) -> "SyntheticApplicationJourney":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        clock = _SystemClock()
        vacancy = _load_or_create_vacancy(root, clock)
        ats = _SyntheticAtsAdapter(root / "fake-ats.json", clock)
        store = JsonApplicationStore(root / "state")
        coordinator = ApplicationWorkflowCoordinator(
            store=store,
            tailoring=_SyntheticTailoringAdapter(root / "artifacts"),
            ats=ats,
            report_writer=MarkdownApplicationReportWriter(root / "reports"),
            official_vacancies=_SyntheticOfficialVacancies(vacancy),
            clock=clock,
            token_factory=lambda: secrets.token_urlsafe(18),
        )
        _recover_filled_manifest_after_fixture_restart(
            store=store,
            ats=ats,
            vacancy=vacancy,
        )
        return cls(root=root, coordinator=coordinator, ats=ats)

    def start(self) -> SyntheticJourneyMessage:
        try:
            application = self._coordinator.get(_APPLICATION_ID)
        except KeyError:
            application = self._coordinator.propose(
                application_id=_APPLICATION_ID,
                opportunity={
                    "stable_id": "synthetic:zurich-ai-research-engineer",
                    "company": "Synthetic Helvetic AI Lab",
                    "title": "Synthetic AI Research Engineer",
                    "location": "Zurich, Switzerland (FAKE ATS)",
                    "official_url": (
                        "https://example.invalid/jobs/"
                        "synthetic-ai-research-engineer"
                    ),
                    "official_description": (
                        "Controlled fake role for application workflow testing."
                    ),
                },
                version="synthetic-opportunity-v1",
            )
        if application.next_action == WorkflowAction.FILL:
            fill = self._coordinator.issue_authorization(
                _APPLICATION_ID, WorkflowAction.FILL, actor=_ACTOR
            )
            assert application.artifacts is not None
            return self._prepared_message(application, fill)
        if application.next_action == WorkflowAction.SUBMIT:
            summary, submit = self._handler.present_submit(
                _APPLICATION_ID, actor=_ACTOR
            )
            return self._pre_submit_message(application, summary, submit)
        if application.next_action != WorkflowAction.PREPARE:
            return self._message_for_current_state()
        command = self._coordinator.issue_authorization(
            _APPLICATION_ID, WorkflowAction.PREPARE, actor=_ACTOR
        )
        return SyntheticJourneyMessage(
            title="Synthetic AI Research Engineer",
            text=(
                "🧪 TEST E2E — nessuna azienda reale\n\n"
                "Synthetic Helvetic AI Lab · Zurigo\n"
                "Rank: #1/1 (synthetic) · Score: 9.1/10\n"
                "Fit: AI research/engineering, computer vision, ML systems.\n"
                "Premi 👍 per generare CV e cover letter sintetici."
            ),
            lifecycle_state=application.lifecycle_state.value,
            buttons=(
                SyntheticButton(
                    "👍", TelegramCommandHandler.encode_callback(command)
                ),
                SyntheticButton("👎", _DISCARD_CALLBACK),
                SyntheticButton("Dimmi di più", _DETAILS_CALLBACK),
            ),
        )

    def handle(self, callback_data: str) -> SyntheticJourneyMessage:
        if callback_data == _DETAILS_CALLBACK:
            return SyntheticJourneyMessage(
                title="Dettagli — Synthetic AI Research Engineer",
                text=(
                    "Questa è una vacancy interamente finta. Il test esercita "
                    "autorizzazioni monouso, creazione PDF, compilazione, manifest, "
                    "invio idempotente, ricevuta e report."
                ),
                lifecycle_state=self._coordinator.get(
                    _APPLICATION_ID
                ).lifecycle_state.value,
                buttons=(),
            )
        if callback_data == _DISCARD_CALLBACK:
            return SyntheticJourneyMessage(
                title="Test E2E annullato",
                text="Nessuna candidatura è stata preparata o inviata.",
                lifecycle_state=self._coordinator.get(
                    _APPLICATION_ID
                ).lifecycle_state.value,
                terminal=True,
            )

        command = self._coordinator.command_for_token(
            callback_data.removeprefix("app:")
        )
        result = self._handler.handle_callback_data(callback_data)
        application = self._coordinator.get(_APPLICATION_ID)
        if result.status.value == "replayed":
            return SyntheticJourneyMessage(
                title=application.opportunity["title"],
                text="Questa azione è già elaborata; nessun effetto è stato ripetuto.",
                lifecycle_state=application.lifecycle_state.value,
                confirmation_id=(
                    None
                    if application.outcome is None
                    else application.outcome.confirmation_id
                ),
                report_path=self._report_path_if_present(),
                terminal=application.lifecycle_state.value == "inviata",
            )
        if result.status.value != "completed" or command is None:
            return SyntheticJourneyMessage(
                title=application.opportunity["title"],
                text=f"Azione non completata: {result.status.value}.",
                lifecycle_state=application.lifecycle_state.value,
            )

        if command.scope.action == WorkflowAction.PREPARE:
            fill = self._coordinator.issue_authorization(
                _APPLICATION_ID, WorkflowAction.FILL, actor=_ACTOR
            )
            assert application.artifacts is not None
            return self._prepared_message(application, fill)
        if command.scope.action == WorkflowAction.FILL:
            summary, submit = self._handler.present_submit(
                _APPLICATION_ID, actor=_ACTOR
            )
            return self._pre_submit_message(application, summary, submit)
        if command.scope.action == WorkflowAction.SUBMIT:
            outcome = application.outcome
            assert outcome is not None and outcome.confirmation_id is not None
            return SyntheticJourneyMessage(
                title=application.opportunity["title"],
                text=(
                    "✅ Candidatura sintetica inviata e verificata.\n"
                    f"Ricevuta: {outcome.confirmation_id}\n"
                    "Destinazione: fake ATS locale (nessuna azienda reale)."
                ),
                lifecycle_state=application.lifecycle_state.value,
                confirmation_id=outcome.confirmation_id,
                report_path=self._report_path_if_present(),
                terminal=True,
            )
        raise AssertionError(f"Unexpected synthetic action: {command.scope.action}")

    def fake_ats_status(self) -> dict[str, Any]:
        return self._ats.status()

    def _message_for_current_state(self) -> SyntheticJourneyMessage:
        application = self._coordinator.get(_APPLICATION_ID)
        return SyntheticJourneyMessage(
            title=str(application.opportunity["title"]),
            text=f"Test sintetico già nello stato: {application.lifecycle_state.value}.",
            lifecycle_state=application.lifecycle_state.value,
            confirmation_id=(
                None
                if application.outcome is None
                else application.outcome.confirmation_id
            ),
            report_path=self._report_path_if_present(),
            terminal=application.lifecycle_state.value == "inviata",
        )

    def _pre_submit_message(
        self, application, summary: TelegramPreSubmitSummary, submit
    ) -> SyntheticJourneyMessage:
        answers = "\n".join(
            f"• {key}: {value}" for key, value in summary.principal_answers
        )
        attachments = "\n".join(
            f"• {attachment.kind}: {Path(attachment.path).name} "
            f"({attachment.sha256[:20]}…)"
            for attachment in summary.attachments
        )
        return SyntheticJourneyMessage(
            title=str(application.opportunity["title"]),
            text=(
                "Candidatura pronta per inviare — REVIEW FAKE ATS\n\n"
                f"Azienda: {summary.company}\n"
                f"Ruolo: {summary.title}\n"
                f"Località: {summary.location}\n\n"
                f"Risposte:\n{answers}\n\n"
                f"Allegati:\n{attachments}\n\n"
                "Premendo Invia verrà scritta una sola ricevuta nel fake ATS."
            ),
            lifecycle_state=application.lifecycle_state.value,
            buttons=(
                SyntheticButton(
                    "Invia", TelegramCommandHandler.encode_callback(submit)
                ),
            ),
        )

    @staticmethod
    def _prepared_message(application, fill) -> SyntheticJourneyMessage:
        assert application.artifacts is not None
        return SyntheticJourneyMessage(
            title=application.opportunity["title"],
            text=(
                "CV posizione Synthetic AI Research Engineer completo.\n"
                "Allegati sintetici pronti. Premi Compila per popolare il fake ATS."
            ),
            lifecycle_state=application.lifecycle_state.value,
            buttons=(
                SyntheticButton(
                    "Compila", TelegramCommandHandler.encode_callback(fill)
                ),
            ),
            documents=(
                Path(application.artifacts.cv_path),
                Path(application.artifacts.cover_letter_path),
            ),
        )

    def _report_path_if_present(self) -> str | None:
        path = self._root / "reports" / f"{_APPLICATION_ID}.md"
        return str(path) if path.is_file() else None


def _write_pdf(path: Path, title: str, lines: tuple[str, ...]) -> None:
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle(title)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(54, 790, title)
    canvas.setFont("Helvetica", 10)
    y = 760
    for line in lines:
        canvas.drawString(54, y, line)
        y -= 20
    canvas.save()
    os.chmod(path, 0o600)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_or_create_vacancy(
    root: Path, clock: _SystemClock
) -> OfficialVacancy:
    vacancy_path = root / "synthetic-vacancy.json"
    if vacancy_path.is_file():
        return OfficialVacancy.from_dict(
            json.loads(vacancy_path.read_text(encoding="utf-8"))
        )
    state_path = root / "state" / f"{_APPLICATION_ID}.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        existing = state.get("official_vacancy")
        if isinstance(existing, Mapping):
            vacancy = OfficialVacancy.from_dict(existing)
            _write_private_json(vacancy_path, existing)
            return vacancy
    vacancy = OfficialVacancy(
        version="synthetic-vacancy-v1",
        fingerprint="sha256:"
        + hashlib.sha256(b"synthetic-ai-research-engineer").hexdigest(),
        freshness=clock.now().isoformat(),
        description=(
            "Synthetic vacancy used only to validate the gated application "
            "workflow. Build computer-vision and trustworthy AI systems."
        ),
    )
    _write_private_json(
        vacancy_path,
        {
            "version": vacancy.version,
            "fingerprint": vacancy.fingerprint,
            "freshness": vacancy.freshness,
            "description": vacancy.description,
            "available": vacancy.available,
            "verified": vacancy.verified,
        },
    )
    return vacancy


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _recover_filled_manifest_after_fixture_restart(
    *,
    store: JsonApplicationStore,
    ats: _SyntheticAtsAdapter,
    vacancy: OfficialVacancy,
) -> None:
    """Repair runs created before the synthetic vacancy became persistent."""

    try:
        application = store.load(_APPLICATION_ID)
    except KeyError:
        return
    if (
        application.operational_status != OperationalStatus.VACANCY_CHANGED
        or application.manifest is not None
        or application.artifacts is None
        or application.official_vacancy is None
        or application.outcome is not None
        or application.submission_intents
    ):
        return
    if (
        application.official_vacancy.version != vacancy.version
        or application.official_vacancy.fingerprint != vacancy.fingerprint
    ):
        return
    status = ats.status()
    answers = status.get("answers")
    hashes = status.get("artifact_hashes")
    artifacts = application.artifacts
    if (
        status.get("state") != "filled"
        or status.get("application_id") != application.application_id
        or status.get("artifact_version") != artifacts.version
        or hashes
        != {
            "cv": artifacts.cv_hash,
            "cover_letter": artifacts.cover_letter_hash,
        }
        or not isinstance(answers, Mapping)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in answers.items())
    ):
        return
    filled = FilledApplication(
        answers=dict(answers),
        artifact_version=artifacts.version,
        review_evidence=ReviewEvidence(
            page=ReviewEvidencePage.REVIEW,
            form_snapshot=dict(answers),
            attachment_hashes=dict(hashes),
        ),
        answer_disclosures=tuple(
            AnswerDisclosure(field, AnswerVisibility.PUBLIC_SUMMARY)
            for field in answers
        ),
    )
    manifest = PreSubmitManifest.build(
        application_id=application.application_id,
        opportunity_version=application.opportunity_version,
        official_vacancy=application.official_vacancy,
        artifacts=artifacts,
        filled=filled,
    )
    store.save(
        replace(
            application,
            authorization_version=manifest.version,
            manifest=manifest,
            operational_status=None,
            package_publication_pending=True,
        )
    )


__all__ = [
    "SyntheticApplicationJourney",
    "SyntheticButton",
    "SyntheticJourneyMessage",
    "SyntheticTelegramApi",
    "SyntheticTelegramSession",
]
