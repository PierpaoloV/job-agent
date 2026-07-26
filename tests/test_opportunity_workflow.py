from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from opportunity_sources import OpportunityLead  # noqa: E402
from opportunity_workflow import (  # noqa: E402
    EmailLeadRouter,
    DecisionAction,
    DecisionCommand,
    Evaluation,
    HostedFetchBlocked,
    JsonOpportunityStore,
    OfficialVacancyData,
    OfficialVacancyUnavailable,
    OpportunityWorkflow,
    OpportunityTelegramHandler,
    Runtime,
)


def test_supported_and_fallback_email_sources_emit_one_normalized_lead_contract():
    discovered_at = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)
    emails = [
        {
            "from": "LinkedIn Jobs <jobs-noreply@linkedin.com>",
            "date": "Thu, 16 Jul 2026 08:00:00 +0000",
            "publication_date": "2026-07-15",
            "body": '<a href="https://www.linkedin.com/jobs/view/101?trk=email">ML Engineer</a>',
        },
        {
            "from": "Indeed <alert@indeed.com>",
            "date": "Thu, 16 Jul 2026 08:01:00 +0000",
            "body": '<a href="https://ch.indeed.com/viewjob?jk=abc101&utm_source=email">AI Scientist</a>',
        },
        {
            "from": "Glassdoor <jobs@glassdoor.com>",
            "date": "Thu, 16 Jul 2026 08:02:00 +0000",
            "body": '<a href="https://www.glassdoor.com/job-listing/researcher-acme-JV.htm?jl=30101&utm_source=email">Researcher</a>',
        },
        {
            "from": "Welcome to the Jungle <alerts@welcometothejungle.com>",
            "date": "Thu, 16 Jul 2026 08:03:00 +0000",
            "body": '<a href="https://www.welcometothejungle.com/en/companies/acme/jobs/research-engineer_zurich?utm_source=email">Research Engineer</a>',
        },
        {
            "from": "Specialist Jobs <alerts@example.org>",
            "date": "Thu, 16 Jul 2026 08:04:00 +0000",
            "body": '<a href="https://careers.example.org/jobs/501?utm_source=email">Computer Vision Scientist</a>',
        },
    ]

    leads = EmailLeadRouter.default().normalize(emails, discovered_at=discovered_at)

    assert len(leads) == 5
    assert {lead.source for lead in leads} == {
        "LinkedIn",
        "Indeed",
        "Glassdoor",
        "Welcome to the Jungle",
        "Specialist Jobs <alerts@example.org>",
    }
    assert {lead.source_confidence for lead in leads[:-1]} == {"supported"}
    assert leads[-1].source_confidence == "fallback"
    assert len({lead.stable_id for lead in leads}) == 5
    assert all(lead.canonical_url.startswith("https://") for lead in leads)
    assert leads[0].canonical_url == "https://www.linkedin.com/jobs/view/101"
    assert leads[0].email_received_at == "Thu, 16 Jul 2026 08:00:00 +0000"
    assert leads[0].discovered_at == "2026-07-16T10:30:00+00:00"
    assert leads[0].published_at == "2026-07-15"
    assert leads[1].published_at is None


class FixedClock:
    def now(self):
        return datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


class FakeEvaluator:
    def __init__(self):
        self.calls = []

    def evaluate(self, vacancy):
        self.calls.append(vacancy)
        return Evaluation(
            fit_summary="Strong research and computer-vision overlap",
            gaps=("No drug-discovery experience",),
            compensation_status="CHF 160k-180k published",
            wealth_potential_confidence="high; base cash supports target savings",
            immigration="EU citizen: Swiss permit after employment",
            ownership="Ownership verified 2026-07-16",
            risks=("Matrixed organization",),
            rank_explanation="High research fit and strong savings potential",
            requirement_analysis=("Python: verified", "Drug discovery: gap"),
            sources=("https://careers.acme.example/jobs/42",),
        )


class RecoverableOfficialSource:
    def __init__(self):
        self.calls = []

    def retrieve(self, lead, runtime):
        self.calls.append((lead.stable_id, runtime))
        if runtime == Runtime.HOSTED:
            raise HostedFetchBlocked("official ATS blocked datacenter IP")
        return official_vacancy()


def normalized_lead():
    return OpportunityLead(
        stable_id="linkedin:42",
        source="LinkedIn",
        source_confidence="supported",
        canonical_url="https://www.linkedin.com/jobs/view/42",
        title="Research Scientist",
        company="Acme AI",
        location="Zurich",
        modality="hybrid",
        snippet="Email snippet must never be graded.",
        email_received_at="Thu, 16 Jul 2026 08:00:00 +0000",
        discovered_at="2026-07-16T10:30:00+00:00",
        published_at=None,
    )


def official_vacancy(**changes):
    values = {
        "official_job_id": "acme-42",
        "canonical_url": "https://careers.acme.example/jobs/42",
        "company": "Acme AI",
        "role": "Research Scientist",
        "team": "Vision Research",
        "location": "Zurich",
        "modality": "hybrid",
        "seniority": "senior",
        "compensation": "CHF 160k-180k",
        "requirements": ("Python", "PyTorch", "PhD"),
        "ownership": "Acme Group, Switzerland",
        "sponsorship": "not required for EU citizen",
        "description": "Full official description for trustworthy vision research.",
        "published_at": "2026-07-14",
    }
    values.update(changes)
    return OfficialVacancyData(**values)


def test_verify_official_fetches_and_persists_snapshot_without_evaluation(tmp_path):
    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=RecoverableOfficialSource(),
        evaluator=evaluator,
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())

    verified = workflow.verify_official("linkedin:42", runtime=Runtime.LOCAL)

    assert verified.status == "verified"
    assert verified.snapshot.vacancy.description == official_vacancy().description
    assert verified.evaluation is None
    assert evaluator.calls == []


def test_hosted_fetch_block_never_grades_snippet_and_local_resume_uses_same_record(
    tmp_path,
):
    source = RecoverableOfficialSource()
    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=source,
        evaluator=evaluator,
        clock=FixedClock(),
    )
    lead = normalized_lead()
    workflow.record_lead(lead)

    blocked = workflow.verify_and_evaluate(lead.stable_id, runtime=Runtime.HOSTED)

    assert blocked.status == "needs_local_fetch"
    assert "Mac" in blocked.operator_request
    assert evaluator.calls == []

    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=source,
        evaluator=evaluator,
        clock=FixedClock(),
    )
    resumed = workflow.resume_local(lead.stable_id)

    assert resumed.status == "verified"
    assert resumed.stable_id == lead.stable_id
    assert resumed.snapshot.vacancy.description.startswith("Full official description")
    assert resumed.snapshot.retrieved_at == "2026-07-16T10:30:00+00:00"
    assert resumed.snapshot.vacancy.published_at == "2026-07-14"
    assert len(evaluator.calls) == 1
    assert evaluator.calls[0].vacancy.description == official_vacancy().description
    assert lead.snippet not in evaluator.calls[0].vacancy.description


def test_temporary_fetch_failure_preserves_cached_grading_for_same_recovered_version(
    tmp_path,
):
    source = RecoverableOfficialSource()
    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=source,
        evaluator=evaluator,
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())
    first = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)

    blocked = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.HOSTED)
    recovered = workflow.resume_local("linkedin:42")

    assert blocked.status == "needs_local_fetch"
    assert recovered.snapshot.version == first.snapshot.version
    assert len(evaluator.calls) == 1


def test_missing_or_ambiguous_official_vacancy_stops_with_clear_request(tmp_path):
    class MissingOfficialSource:
        def retrieve(self, lead, runtime):
            raise OfficialVacancyUnavailable(
                "Two employer roles match the aggregator link"
            )

    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=MissingOfficialSource(),
        evaluator=evaluator,
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())

    result = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)

    assert result.status == "needs_official_description"
    assert result.operator_request == (
        "Official vacancy required: Two employer roles match the aggregator link"
    )
    assert evaluator.calls == []
    prepare = workflow.decide(
        DecisionCommand(
            token="unverified-token",
            stable_id="linkedin:42",
            verified_version="unverified",
            action=DecisionAction.PREPARE,
        )
    )
    assert prepare.status == "not_verified"
    assert prepare.approved_application is None


def test_empty_official_description_is_not_verified_or_graded(tmp_path):
    class EmptyOfficialSource:
        def retrieve(self, lead, runtime):
            return official_vacancy(description="   ")

    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=EmptyOfficialSource(),
        evaluator=evaluator,
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())

    result = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)

    assert result.status == "needs_official_description"
    assert evaluator.calls == []


def test_official_snapshot_is_durable_before_evaluation_starts(tmp_path):
    class StaticOfficialSource:
        def retrieve(self, lead, runtime):
            return official_vacancy()

    class FailingEvaluator:
        def evaluate(self, vacancy):
            raise RuntimeError("synthetic grader outage")

    root = tmp_path / "opportunities"
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(root),
        official_source=StaticOfficialSource(),
        evaluator=FailingEvaluator(),
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())

    try:
        workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    except RuntimeError as error:
        assert str(error) == "synthetic grader outage"
    else:
        raise AssertionError("synthetic grader outage was swallowed")

    persisted = JsonOpportunityStore(root).load("linkedin:42")
    assert persisted.status == "verified"
    assert persisted.latest_snapshot.vacancy.description.startswith(
        "Full official description"
    )
    assert persisted.evaluation is None


def test_dated_snapshots_explain_every_material_change_and_survive_restart(tmp_path):
    class ChangingOfficialSource:
        def __init__(self):
            self.current = official_vacancy()

        def retrieve(self, lead, runtime):
            return self.current

    source = ChangingOfficialSource()
    root = tmp_path / "opportunities"
    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(root),
        official_source=source,
        evaluator=evaluator,
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())
    first = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    source.current = official_vacancy(
        official_job_id="acme-84",
        role="Principal Research Scientist",
        team="Foundation Models",
        location="Basel",
        modality="onsite",
        seniority="principal",
        compensation="CHF 190k-220k",
        requirements=("Python", "JAX", "large-scale training"),
        ownership="New Parent Group, United States",
        sponsorship="Swiss sponsorship available",
    )
    second = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)

    assert first.snapshot.material_fingerprint != second.snapshot.material_fingerprint
    assert {change.field for change in second.snapshot.changes} == {
        "official_job_id",
        "role",
        "team",
        "location",
        "modality",
        "seniority",
        "compensation",
        "requirements",
        "ownership",
        "sponsorship",
    }
    assert "compensation: 'CHF 160k-180k' -> 'CHF 190k-220k'" in (
        second.snapshot.change_explanation
    )

    restarted = JsonOpportunityStore(root).load("linkedin:42")
    assert len(restarted.snapshots) == 2
    assert restarted.lead.email_received_at == "Thu, 16 Jul 2026 08:00:00 +0000"
    assert restarted.lead.discovered_at == "2026-07-16T10:30:00+00:00"
    assert restarted.latest_snapshot.vacancy.published_at == "2026-07-14"


def test_retrieval_time_does_not_create_a_new_verified_content_version(tmp_path):
    class AdvancingClock:
        def __init__(self):
            self.current = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

        def now(self):
            return self.current

    class StaticOfficialSource:
        def retrieve(self, lead, runtime):
            return official_vacancy()

    clock = AdvancingClock()
    evaluator = FakeEvaluator()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=StaticOfficialSource(),
        evaluator=evaluator,
        clock=clock,
    )
    workflow.record_lead(normalized_lead())
    first = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    clock.current = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    second = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)

    assert first.snapshot.version == second.snapshot.version
    assert first.snapshot.material_fingerprint == second.snapshot.material_fingerprint
    assert second.snapshot.changes == ()
    assert len(evaluator.calls) == 1


def verified_workflow(tmp_path, source=None):
    class StaticOfficialSource:
        def retrieve(self, lead, runtime):
            return official_vacancy()

    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=source or StaticOfficialSource(),
        evaluator=FakeEvaluator(),
        clock=FixedClock(),
    )
    workflow.record_lead(normalized_lead())
    verified = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    return workflow, verified


def test_role_card_and_more_details_are_complete_and_read_only(tmp_path):
    workflow, verified = verified_workflow(tmp_path)

    card = workflow.role_card("linkedin:42")

    assert card.identity == "Acme AI — Research Scientist"
    assert card.location == "Zurich"
    assert card.modality == "hybrid"
    assert card.source == "LinkedIn"
    assert card.freshness == "published 2026-07-14; verified 2026-07-16T10:30:00+00:00"
    assert card.fit_summary == "Strong research and computer-vision overlap"
    assert card.gaps == ("No drug-discovery experience",)
    assert card.compensation_status == "CHF 160k-180k published"
    assert card.wealth_potential_confidence.startswith("high")
    assert "Swiss permit" in card.immigration
    assert "Ownership verified" in card.ownership
    assert card.risks == ("Matrixed organization",)
    assert card.rank_explanation.startswith("High research fit")
    assert card.actions == ("Dimmi di più", "Prepara candidatura", "Scarta")

    telegram = OpportunityTelegramHandler(workflow)
    details_callback = telegram.create_callback(
        "linkedin:42",
        verified.snapshot.version,
        "Dimmi di più",
        actor="Synthetic Owner",
    )
    details = telegram.handle_callback(details_callback)

    assert details.status == "details"
    assert details.details.description.startswith("Full official description")
    assert details.details.requirement_analysis == (
        "Python: verified",
        "Drug discovery: gap",
    )
    assert details.details.sources == (
        "https://www.linkedin.com/jobs/view/42",
        "https://careers.acme.example/jobs/42",
    )
    assert details.details.link == "https://careers.acme.example/jobs/42"
    assert details.details.risks == ("Matrixed organization",)
    assert workflow.get("linkedin:42").approved_applications == ()
    assert workflow.get("linkedin:42").discard is None
    assert telegram.handle_callback(details_callback).status == "replayed"


def test_prepare_is_scoped_to_verified_version_and_discard_is_conditional(tmp_path):
    class ChangingOfficialSource:
        def __init__(self):
            self.current = official_vacancy()

        def retrieve(self, lead, runtime):
            return self.current

    source = ChangingOfficialSource()
    workflow, verified = verified_workflow(tmp_path, source)

    telegram = OpportunityTelegramHandler(workflow)
    prepare_callback = telegram.create_callback(
        "linkedin:42",
        verified.snapshot.version,
        "Prepara candidatura",
        actor="Synthetic Owner",
    )
    source.current = official_vacancy(modality="onsite")
    stale = telegram.handle_callback(prepare_callback)
    assert stale.status == "stale"
    assert stale.approved_application is None
    assert workflow.get("linkedin:42").evaluation is None

    verified = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    approved = telegram.handle_callback(
        telegram.create_callback(
            "linkedin:42",
            verified.snapshot.version,
            "Prepara candidatura",
            actor="Synthetic Owner",
        )
    )
    assert approved.status == "approved"
    assert approved.approved_application.opportunity_version == (
        verified.snapshot.version
    )
    assert approved.approved_application.actor == "Synthetic Owner"
    assert approved.approved_application.action == "Prepara candidatura"
    assert approved.approved_application.expires_at.endswith("+00:00")
    assert workflow.get("linkedin:42").lifecycle == "approvata"
    cannot_discard = telegram.handle_callback(
        replace(
            telegram.create_callback(
                "linkedin:42",
                verified.snapshot.version,
                "Scarta",
                actor="Synthetic Owner",
            ),
            reason="Changed my mind",
        )
    )
    assert cannot_discard.status == "invalid_state"
    assert workflow.get("linkedin:42").discard is None

    discard_workflow, discard_verified = verified_workflow(
        tmp_path / "discard", source
    )
    discard_telegram = OpportunityTelegramHandler(discard_workflow)
    discard_callback = discard_telegram.create_callback(
        "linkedin:42",
        discard_verified.snapshot.version,
        "Scarta",
        actor="Synthetic Owner",
    )
    needs_reason = discard_telegram.handle_callback(discard_callback)
    assert needs_reason.status == "needs_reason"
    awaiting = discard_workflow.get("linkedin:42").decision_authorizations[-1]
    assert awaiting.awaiting_reason_at == "2026-07-16T10:30:00+00:00"
    discard_workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "discard" / "opportunities"),
        official_source=source,
        evaluator=FakeEvaluator(),
        clock=FixedClock(),
    )
    discard_telegram = OpportunityTelegramHandler(discard_workflow)
    discarded = discard_telegram.handle_callback_data(
        discard_telegram.encode_callback(discard_callback),
        reason="Too little remote flexibility",
    )
    assert discarded.status == "discarded"
    assert discard_telegram.handle_callback_data(
        discard_telegram.encode_callback(discard_callback),
        reason="Second attempt",
    ).status == "replayed"
    assert discard_workflow.get("linkedin:42").lifecycle == "scartata"
    assert discard_workflow.suppression("linkedin:42").suppressed is True

    similar = replace(
        normalized_lead(),
        stable_id="indeed:similar-42",
        source="Indeed",
        canonical_url="https://ch.indeed.com/viewjob?jk=similar42",
    )
    discard_workflow.record_lead(similar)
    discard_workflow.verify_and_evaluate(similar.stable_id, runtime=Runtime.LOCAL)
    similar_suppression = discard_workflow.suppression(similar.stable_id)
    assert similar_suppression.suppressed is True
    assert similar_suppression.reason == "Too little remote flexibility"

    source.current = official_vacancy(modality="remote")
    changed = discard_workflow.verify_and_evaluate(
        "linkedin:42", runtime=Runtime.LOCAL
    )
    suppression = discard_workflow.suppression("linkedin:42")

    assert changed.snapshot.change_explanation == (
        "modality: 'onsite' -> 'remote'",
    )
    assert suppression.suppressed is False
    assert suppression.material_changes == changed.snapshot.change_explanation


def test_prepare_revalidation_reports_missing_official_description_actionably(tmp_path):
    class StatusTransport:
        def __init__(self):
            self.statuses = []

        def send_role_card(self, card, buttons):
            pass

        def send_details(self, details):
            pass

        def send_status(self, message):
            self.statuses.append(message)

    class DisappearingOfficialSource:
        def __init__(self):
            self.available = True

        def retrieve(self, lead, runtime):
            if not self.available:
                raise OfficialVacancyUnavailable("the employer page is ambiguous")
            return official_vacancy()

    source = DisappearingOfficialSource()
    workflow, verified = verified_workflow(tmp_path, source)
    transport = StatusTransport()
    telegram = OpportunityTelegramHandler(workflow, transport)
    callback = telegram.create_callback(
        "linkedin:42",
        verified.snapshot.version,
        "Prepara candidatura",
        actor="Synthetic Owner",
    )
    source.available = False

    result = telegram.handle_callback(callback)

    assert result.status == "not_verified"
    record = workflow.get("linkedin:42")
    assert record.status == "needs_official_description"
    assert record.operator_request == (
        "Official vacancy required: the employer page is ambiguous"
    )
    assert transport.statuses[-1] == record.operator_request
    assert record.approved_applications == ()


def test_telegram_callback_expiry_and_scope_mismatch_have_no_decision_effect(tmp_path):
    class AdjustableClock:
        def __init__(self):
            self.current = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

        def now(self):
            return self.current

    class StaticOfficialSource:
        def retrieve(self, lead, runtime):
            return official_vacancy()

    clock = AdjustableClock()
    workflow = OpportunityWorkflow(
        store=JsonOpportunityStore(tmp_path / "opportunities"),
        official_source=StaticOfficialSource(),
        evaluator=FakeEvaluator(),
        clock=clock,
    )
    workflow.record_lead(normalized_lead())
    verified = workflow.verify_and_evaluate("linkedin:42", runtime=Runtime.LOCAL)
    telegram = OpportunityTelegramHandler(workflow)
    expired = telegram.create_callback(
        "linkedin:42",
        verified.snapshot.version,
        "Scarta",
        actor="Synthetic Owner",
        ttl=timedelta(seconds=1),
    )
    clock.current += timedelta(seconds=2)
    assert telegram.handle_callback(expired).status == "expired"

    valid = telegram.create_callback(
        "linkedin:42",
        verified.snapshot.version,
        "Scarta",
        actor="Synthetic Owner",
    )
    tampered = replace(valid, action=DecisionAction.PREPARE)
    assert telegram.handle_callback(tampered).status == "mismatched"
    record = workflow.get("linkedin:42")
    assert record.lifecycle == "proposta"
    assert record.discard is None
    assert record.approved_applications == ()


def test_telegram_adapter_presents_buttons_and_clear_callback_statuses(tmp_path):
    class FakeTelegramTransport:
        def __init__(self):
            self.cards = []
            self.details = []
            self.statuses = []

        def send_role_card(self, card, buttons):
            self.cards.append((card, buttons))

        def send_details(self, details):
            self.details.append(details)

        def send_status(self, message):
            self.statuses.append(message)

    workflow, _ = verified_workflow(tmp_path)
    transport = FakeTelegramTransport()
    telegram = OpportunityTelegramHandler(workflow, transport)

    card, buttons = telegram.present_role("linkedin:42", actor="Synthetic Owner")

    assert transport.cards == [(card, buttons)]
    assert tuple(button.label for button in buttons) == (
        "Dimmi di più",
        "Prepara candidatura",
        "Scarta",
    )
    assert all(button.callback_data.startswith("opp:") for button in buttons)
    assert all(len(button.callback_data) < 64 for button in buttons)

    details = telegram.handle_callback_data(buttons[0].callback_data)
    assert details.status == "details"
    assert len(transport.details) == 1
    assert transport.statuses[-1] == (
        "Dettagli ufficiali mostrati senza avviare la candidatura."
    )

    replayed = telegram.handle_callback_data(buttons[0].callback_data)
    assert replayed.status == "replayed"
    assert transport.statuses[-1] == "Questa decisione è già stata elaborata."

    mismatched = telegram.handle_callback_data("other:unknown")
    assert mismatched.status == "mismatched"
    assert transport.statuses[-1] == (
        "Il pulsante non corrisponde a questa opportunità."
    )
