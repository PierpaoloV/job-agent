from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_worker_telegram import TelegramUpdateStore
from opportunity_decisions import (
    FileOpportunityDecisionStore,
    OpportunityButtonFactory,
    OpportunityDecisionService,
    build_opportunity_callback_route,
)


def test_role_buttons_are_short_lived_worker_scoped_and_route_real_actions(tmp_path):
    now = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    updates = TelegramUpdateStore(tmp_path / "updates.sqlite", now=lambda: now)
    worker = SimpleNamespace(
        status=lambda: {"state": "resume", "resume_generation": 3}
    )
    buttons = OpportunityButtonFactory(
        store=updates,
        worker=worker,
        actor_id="42",
        chat_id="99",
        now=lambda: now,
    )(
        {
            "stable_id": "indeed:role-42",
            "official_vacancy_version": "sha256:" + "a" * 64,
        }
    )

    assert [button["text"] for button in buttons] == [
        "👍",
        "👎",
        "Dimmi di più",
    ]
    assert all(len(button["callback_data"].encode("utf-8")) <= 64 for button in buttons)

    accepted = updates.consume_callback_authorization(
        token=buttons[0]["callback_data"].removeprefix("worker:cb:v1:"),
        actor_id="42",
        chat_id="99",
        resume_generation=3,
    )
    handled = []
    events = []

    def handle(action, application_id, vacancy_version, before_external_action):
        before_external_action()
        handled.append((action, application_id, vacancy_version))
        return "Preparazione CV avviata"

    route = build_opportunity_callback_route(
        handle,
        state_sync=lambda: events.append("sync") or True,
    )

    assert accepted.authorization is not None
    result = route.handler(
        SimpleNamespace(checkpoint=lambda: events.append("gate")),
        SimpleNamespace(payload=accepted.authorization.payload),
    )
    assert result == "Preparazione CV avviata"
    assert handled == [
        (
            "prepare",
            "approved-6ae285f2b8c2f07b",
            "sha256:" + "a" * 64,
        )
    ]
    assert events == ["gate", "sync", "gate"]


def test_decisions_show_verified_details_prepare_once_and_persist_discard(tmp_path):
    version = "sha256:" + "b" * 64
    application_id = "approved-1234567890abcdef"
    vacancy = SimpleNamespace(
        version=version,
        description="Build trustworthy medical imaging systems.",
    )
    inputs = SimpleNamespace(
        load=lambda requested_id, requested_version: SimpleNamespace(
            official_vacancy=vacancy,
            opportunity={
                "requirements_evidence_matrix": {
                    "rows": [
                        {
                            "requirement": "Python",
                            "status": "met",
                        }
                    ]
                }
            },
        )
    )
    job = {
        "company": "Example Health",
        "title": "AI Scientist",
        "location": "Zurich",
        "url": "https://example.test/jobs/42",
        "portfolio_evaluation": {
            "risks": ["Salary not published"],
            "sources": ["https://example.test/jobs/42"],
        },
    }
    calls = []

    class Coordinator:
        def get(self, requested_id):
            raise KeyError(requested_id)

        def propose(self, **kwargs):
            calls.append(("propose", kwargs))

        def issue_authorization(self, requested_id, action, *, actor):
            calls.append(("authorize", requested_id, action.value, actor))
            return "command"

        def handle(self, command):
            calls.append(("handle", command))
            return SimpleNamespace(status=SimpleNamespace(value="accepted"))

    messages = []
    decisions = FileOpportunityDecisionStore(tmp_path / "decisions.json")
    service = OpportunityDecisionService(
        inputs=inputs,
        coordinator=Coordinator(),
        job_lookup=lambda requested_id, requested_version: job,
        decisions=decisions,
        actor="Synthetic Owner",
        send_status=messages.append,
    )

    assert service("details", application_id, version) == "Dettagli inviati"
    assert "Build trustworthy medical imaging systems." in messages[-1]
    assert "Python — met" in messages[-1]

    assert service("prepare", application_id, version) == "Preparazione CV avviata"
    assert [call[0] for call in calls] == ["propose", "authorize", "handle"]

    assert service("discard", application_id, version) == "Opportunità scartata"
    assert decisions.is_discarded(application_id, version)
    assert decisions.suppresses(job)
    assert not decisions.suppresses({**job, "location": "Basel"})


def test_details_are_chunked_and_every_external_action_is_gated(tmp_path):
    version = "sha256:" + "d" * 64
    messages = []
    gates = []
    service = OpportunityDecisionService(
        inputs=SimpleNamespace(
            load=lambda application_id, vacancy_version: SimpleNamespace(
                official_vacancy=SimpleNamespace(
                    version=version,
                    description="Long description. " * 600,
                ),
                opportunity={"requirements_evidence_matrix": {"rows": []}},
            )
        ),
        coordinator=SimpleNamespace(),
        job_lookup=lambda application_id, vacancy_version: {
            "company": "Example",
            "title": "AI Scientist",
        },
        decisions=FileOpportunityDecisionStore(tmp_path / "decisions.json"),
        actor="Synthetic Owner",
        send_status=messages.append,
    )

    assert (
        service(
            "details",
            "approved-1234567890abcdef",
            version,
            lambda: gates.append("gate"),
        )
        == "Dettagli inviati"
    )
    assert len(messages) > 1
    assert len(gates) == len(messages)
    assert all(len(message) <= 3800 for message in messages)


def test_expired_button_reissues_the_exact_role_card():
    events = []
    payload = (
        'opportunity:["details","approved-1234567890abcdef",'
        '"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]'
    )
    route = build_opportunity_callback_route(
        lambda *args: "unused",
        state_sync=lambda: events.append("sync") or True,
        refresh_handler=lambda application_id, version, token, gate: (
            gate()
            or events.append((application_id, version, token))
            or "Scheda aggiornata"
        ),
    )

    assert route.stale_handler is not None
    assert route.recover_stale_replay is True
    result = route.stale_handler(
        SimpleNamespace(checkpoint=lambda: events.append("gate")),
        SimpleNamespace(
            payload=payload,
            authorization=SimpleNamespace(token="expired-token"),
        ),
    )

    assert result == "Scheda aggiornata"
    assert events == [
        "gate",
        "sync",
        "gate",
        (
            "approved-1234567890abcdef",
            "sha256:" + "a" * 64,
            "expired-token",
        ),
    ]
