from pathlib import Path
import sys
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hosted_opportunity_decision import execute_decision
from opportunity_decisions import FileOpportunityDecisionStore


def test_hosted_details_use_exact_verified_input_without_mutating_decisions(tmp_path):
    version = f"sha256:{'b' * 64}"
    messages = []
    decisions = FileOpportunityDecisionStore(tmp_path / "decisions.json")
    result = execute_decision(
        action="details",
        application_id="approved-1234567890abcdef",
        vacancy_version=version,
        inputs=SimpleNamespace(
            load=lambda application_id, vacancy_version: SimpleNamespace(
                official_vacancy=SimpleNamespace(
                    version=version,
                    description="Build medical imaging AI.",
                ),
                opportunity={
                    "requirements_evidence_matrix": {
                        "rows": [{"requirement": "Python", "status": "met"}]
                    }
                },
            )
        ),
        job_lookup=lambda application_id, vacancy_version: {
            "company": "Example Health",
            "title": "AI Scientist",
            "location": "Zurich",
        },
        decisions=decisions,
        send_status=messages.append,
    )

    assert result == "Dettagli inviati"
    assert "Build medical imaging AI." in messages[0]
    assert "Python — met" in messages[0]
    assert not (tmp_path / "decisions.json").exists()


def test_hosted_discard_persists_only_the_exact_material_role_version(tmp_path):
    version = f"sha256:{'c' * 64}"
    application_id = "approved-1234567890abcdef"
    decisions = FileOpportunityDecisionStore(tmp_path / "decisions.json")
    job = {
        "company": "Example Health",
        "title": "AI Scientist",
        "location": "Zurich",
    }

    result = execute_decision(
        action="discard",
        application_id=application_id,
        vacancy_version=version,
        reason=(
            "Il ruolo dice Lead AI Scientist. "
            "Io non ho i requisiti per Lead AI."
        ),
        inputs=SimpleNamespace(
            load=lambda requested_id, requested_version: SimpleNamespace(
                official_vacancy=SimpleNamespace(version=version)
            )
        ),
        job_lookup=lambda requested_id, requested_version: job,
        decisions=decisions,
        send_status=lambda message: None,
    )

    assert result == "Opportunità scartata"
    assert decisions.is_discarded(application_id, version)
    assert decisions.suppresses(job)
    assert not decisions.suppresses({**job, "location": "Basel"})
    state = (tmp_path / "decisions.json").read_text()
    assert "Io non ho i requisiti per Lead AI." in state
