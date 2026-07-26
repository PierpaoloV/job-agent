from datetime import date, datetime, timezone
import json
import pathlib
import stat
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ats_answer_service import (  # noqa: E402
    AnswerScope,
    AnswerServiceError,
    ApplicationFieldReference,
    AtsQuestion,
    CompensationEvidence,
    LocalAnswerProfile,
    LocalAtsAnswerService,
    LocalFormAnswer,
    ProtectedAnswerReportReader,
    QuestionMeaning,
    SalaryContext,
    TelegramAnswerQuestion,
    VerifiedSalaryRange,
)
from ats_answer_storage import LocalAnswerVault, VaultError  # noqa: E402


NOW = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)


def build_service(tmp_path, profile=None):
    vault = LocalAnswerVault.for_repository(tmp_path)
    request_numbers = iter(range(1, 100))
    return (
        LocalAtsAnswerService(
            vault=vault,
            profile=profile or LocalAnswerProfile(),
            now=lambda: NOW,
            request_id_factory=lambda: f"request-synthetic-{next(request_numbers):03d}",
        ),
        vault,
    )


def question(
    prompt="Are you legally authorized to work in this country?",
    *,
    field_id="work-authorization",
    meaning=QuestionMeaning.CUSTOM,
    standardized_voluntary=False,
    salary_context=None,
    mandatory=True,
):
    return AtsQuestion(
        field=ApplicationFieldReference("application-synthetic-001", field_id),
        prompt=prompt,
        mandatory=mandatory,
        meaning=meaning,
        standardized_voluntary=standardized_voluntary,
        salary_context=salary_context,
    )


def test_unknown_mandatory_question_yields_transport_neutral_telegram_question(
    tmp_path,
):
    service, _ = build_service(tmp_path)

    decision = service.resolve(question())

    assert isinstance(decision, TelegramAnswerQuestion)
    assert decision.field == ApplicationFieldReference(
        "application-synthetic-001", "work-authorization"
    )
    assert decision.prompt == "Are you legally authorized to work in this country?"
    assert decision.actions == ("Usa solo qui", "Salva come default")
    assert not hasattr(decision, "answer")


def test_reusable_default_matches_meaning_but_custom_questions_require_exact_normalization(
    tmp_path,
):
    service, _ = build_service(tmp_path)
    known = question(
        "Please select your veteran classification",
        field_id="veteran-1",
        meaning=QuestionMeaning.VETERAN_STATUS,
    )
    request = service.resolve(known)
    service.answer(request.request_id, "synthetic-not-a-veteran", AnswerScope.DEFAULT)
    restarted, _ = build_service(tmp_path)

    rephrased = question(
        "Protected veteran status",
        field_id="veteran-2",
        meaning=QuestionMeaning.VETERAN_STATUS,
    )
    assert restarted.resolve(rephrased) == LocalFormAnswer(
        field=ApplicationFieldReference("application-synthetic-001", "veteran-2"),
        value="synthetic-not-a-veteran",
        source="reusable_default",
    )

    custom = service.resolve(question("Portfolio URL?", field_id="portfolio-1"))
    service.answer(custom.request_id, "https://portfolio.example", AnswerScope.DEFAULT)
    normalized_equivalent = question("  PORTFOLIO   URL ? ", field_id="portfolio-2")
    assert isinstance(service.resolve(normalized_equivalent), LocalFormAnswer)

    superficially_similar = question("Personal portfolio URL?", field_id="portfolio-3")
    assert isinstance(service.resolve(superficially_similar), TelegramAnswerQuestion)


def test_salary_always_requires_fresh_answer_and_verified_dated_context(tmp_path):
    service, _ = build_service(tmp_path)
    salary_context = SalaryContext(
        published_range=VerifiedSalaryRange(
            minimum=140_000,
            maximum=180_000,
            currency="CHF",
            source="https://jobs.example/synthetic-001",
            verified_at=date(2026, 7, 15),
        ),
        benchmarks=(
            CompensationEvidence(
                label="Synthetic Zurich benchmark",
                minimum=150_000,
                maximum=190_000,
                currency="CHF",
                source="https://benchmark.example/zurich-ai",
                as_of=date(2026, 6, 30),
            ),
        ),
    )
    salary = question(
        "What are your salary expectations?",
        field_id="salary-expectation",
        meaning=QuestionMeaning.SALARY_EXPECTATION,
        salary_context=salary_context,
    )

    first = service.resolve(salary)
    assert isinstance(first, TelegramAnswerQuestion)
    assert first.actions == ("Usa solo qui",)
    assert "CHF 140000–180000" in first.context
    assert "verified 2026-07-15" in first.context
    assert "Synthetic Zurich benchmark: CHF 150000–190000" in first.context
    assert "as of 2026-06-30" in first.context
    with pytest.raises(AnswerServiceError) as error:
        service.answer(first.request_id, "CHF 170000", AnswerScope.DEFAULT)
    assert str(error.value) == "This answer cannot be saved as a default"

    service.answer(first.request_id, "CHF 170000", AnswerScope.ONE_USE)
    with pytest.raises(AnswerServiceError) as error:
        service.answer(first.request_id, "CHF 180000", AnswerScope.ONE_USE)
    assert str(error.value) == "Answer request is already resolved"
    assert service.resolve(salary) == LocalFormAnswer(
        field=ApplicationFieldReference(
            "application-synthetic-001", "salary-expectation"
        ),
        value="CHF 170000",
        source="one_use",
    )
    service.mark_used(
        ApplicationFieldReference("application-synthetic-001", "salary-expectation")
    )
    second = service.resolve(salary)
    assert isinstance(second, TelegramAnswerQuestion)
    assert second.request_id != first.request_id

    missing_context = question(
        "Expected salary",
        field_id="salary-without-evidence",
        meaning=QuestionMeaning.SALARY_EXPECTATION,
    )
    with pytest.raises(AnswerServiceError) as error:
        service.resolve(missing_context)
    assert str(error.value) == "Verified salary context is required"


def test_standardized_defaults_are_only_resolved_for_local_voluntary_form_fields(
    tmp_path,
):
    profile = LocalAnswerProfile(
        standardized_defaults={
            QuestionMeaning.GENDER: "SENSITIVE-GENDER-7b9",
            QuestionMeaning.RACE_ETHNICITY: "SENSITIVE-ETHNICITY-a21",
            QuestionMeaning.VETERAN_STATUS: "SENSITIVE-VETERAN-3d2",
            QuestionMeaning.DISABILITY_STATUS: "SENSITIVE-DISABILITY-f48",
        },
        protected_terms=("SENSITIVE-DIAGNOSIS-91c",),
    )
    service, vault = build_service(tmp_path, profile)
    profile.save(vault)
    assert LocalAnswerProfile.load(vault) == profile

    local_standardized = question(
        "Voluntary self-identification of disability",
        field_id="eeoc-disability",
        meaning=QuestionMeaning.DISABILITY_STATUS,
        standardized_voluntary=True,
        mandatory=False,
    )
    assert service.resolve(local_standardized) == LocalFormAnswer(
        field=ApplicationFieldReference(
            "application-synthetic-001", "eeoc-disability"
        ),
        value="SENSITIVE-DISABILITY-f48",
        source="local_profile",
    )

    free_text = question(
        "Please discuss any disability",
        field_id="free-text-disability",
        meaning=QuestionMeaning.DISABILITY_STATUS,
        standardized_voluntary=False,
    )
    assert isinstance(service.resolve(free_text), TelegramAnswerQuestion)


def test_sensitive_values_cross_only_local_form_and_protected_report_boundaries(
    tmp_path,
):
    sentinels = {
        "SENSITIVE-GENDER-7b9",
        "SENSITIVE-DISABILITY-f48",
        "SENSITIVE-DIAGNOSIS-91c",
        "SENSITIVE-CUSTOM-ANSWER-31a",
    }
    profile = LocalAnswerProfile(
        standardized_defaults={
            QuestionMeaning.GENDER: "SENSITIVE-GENDER-7b9",
            QuestionMeaning.DISABILITY_STATUS: "SENSITIVE-DISABILITY-f48",
        },
        protected_terms=("SENSITIVE-DIAGNOSIS-91c",),
    )
    service, vault = build_service(tmp_path, profile)
    request = service.resolve(question("Synthetic mandatory question"))
    local_answer = service.answer(
        request.request_id, "SENSITIVE-CUSTOM-ANSWER-31a", AnswerScope.ONE_USE
    )
    assert local_answer.value == "SENSITIVE-CUSTOM-ANSWER-31a"
    redacted_question = service.resolve(
        question(
            "Explain SENSITIVE-DIAGNOSIS-91c in this mandatory field",
            field_id="protected-free-text",
        )
    )
    assert "SENSITIVE-DIAGNOSIS-91c" not in redacted_question.prompt
    with pytest.raises(AnswerServiceError) as error:
        service.answer(
            redacted_question.request_id,
            "sensitive-diagnosis-91C",
            AnswerScope.ONE_USE,
        )
    assert str(error.value) == (
        "Sensitive health detail cannot be used as an ATS answer"
    )

    service.record_submitted_answers(
        "application-synthetic-001",
        {
            "custom": "SENSITIVE-CUSTOM-ANSWER-31a",
            "gender": "SENSITIVE-GENDER-7b9",
            "disability": "SENSITIVE-DISABILITY-f48",
        },
    )
    payloads = {
        "model_request": {
            "candidate": "SENSITIVE-GENDER-7b9",
            "notes": "SENSITIVE-DIAGNOSIS-91c",
        },
        "general_log": "answer=SENSITIVE-CUSTOM-ANSWER-31a",
        "telegram_role_card": "health SENSITIVE-DISABILITY-f48",
        "public_artifact": ["SENSITIVE-DIAGNOSIS-91c"],
        "cv": "SENSITIVE-DISABILITY-f48",
        "cover_letter": "SENSITIVE-DIAGNOSIS-91c",
        "accidental_local_answer": local_answer,
    }
    public_payloads = service.redact_for_public_boundaries(payloads)
    serialized = json.dumps(public_payloads, sort_keys=True)
    assert all(sentinel not in serialized for sentinel in sentinels)
    assert serialized.count("[REDACTED]") == 8

    with pytest.raises(AnswerServiceError) as error:
        service.record_submitted_answers(
            "application-synthetic-002",
            {"free_text": "SENSITIVE-DIAGNOSIS-91c"},
        )
    assert str(error.value) == (
        "Sensitive health detail cannot be used as an ATS answer"
    )

    with pytest.raises(AnswerServiceError) as error:
        service.answer(
            "missing-request", "SENSITIVE-CUSTOM-ANSWER-31a", AnswerScope.ONE_USE
        )
    assert sentinels.isdisjoint({str(error.value), repr(error.value)})
    assert all(sentinel not in repr(error.value) for sentinel in sentinels)

    status = service.public_status("application-synthetic-001")
    assert status == {
        "application_id": "application-synthetic-001",
        "submitted_answer_count": 3,
    }
    assert all(sentinel not in json.dumps(status) for sentinel in sentinels)

    report = ProtectedAnswerReportReader(vault).read("application-synthetic-001")
    assert report.answers == {
        "custom": "SENSITIVE-CUSTOM-ANSWER-31a",
        "gender": "SENSITIVE-GENDER-7b9",
        "disability": "SENSITIVE-DISABILITY-f48",
    }
    assert report.recorded_at == NOW.isoformat()


def test_vault_is_gitignored_private_storage_with_sanitized_failures(tmp_path):
    path = tmp_path / "data" / "private" / "ats-answers.json"
    service, _ = build_service(tmp_path)
    request = service.resolve(question())
    service.answer(request.request_id, "SENSITIVE-LOCAL-VALUE", AnswerScope.DEFAULT)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "data/" in (REPO_ROOT / ".gitignore").read_text()

    path.write_text("SENSITIVE-CORRUPTION-CONTENT", encoding="utf-8")
    with pytest.raises(VaultError) as error:
        LocalAnswerVault(path).snapshot()
    assert str(error.value) == "Local answer vault is unavailable"
    assert "SENSITIVE" not in repr(error.value)
