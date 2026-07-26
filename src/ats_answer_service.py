"""Local form-answer policy and the transport-neutral human question seam."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import re
import unicodedata
from typing import Any, Callable, Mapping

from ats_answer_storage import (
    AnswerRequestRecord,
    LocalAnswerVault,
    LocalProfileRecord,
    VaultState,
)


class AnswerServiceError(RuntimeError):
    """A policy error whose message is safe for general notifications and logs."""


class AnswerScope(str, Enum):
    ONE_USE = "one_use"
    DEFAULT = "default"


class QuestionMeaning(str, Enum):
    CUSTOM = "custom"
    WORK_AUTHORIZATION = "eligibility.work_authorization"
    SPONSORSHIP = "eligibility.sponsorship"
    START_DATE = "availability.start_date"
    REFERENCES = "application.references"
    GENDER = "demographic.gender"
    RACE_ETHNICITY = "demographic.race_ethnicity"
    NATIONALITY = "demographic.nationality"
    VETERAN_STATUS = "demographic.veteran_status"
    DISABILITY_STATUS = "demographic.disability_status"
    SALARY_EXPECTATION = "compensation.salary_expectation"


_STANDARDIZED_PROFILE_MEANINGS = {
    QuestionMeaning.GENDER,
    QuestionMeaning.RACE_ETHNICITY,
    QuestionMeaning.NATIONALITY,
    QuestionMeaning.VETERAN_STATUS,
    QuestionMeaning.DISABILITY_STATUS,
}


@dataclass(frozen=True)
class VerifiedSalaryRange:
    minimum: int
    maximum: int
    currency: str
    source: str
    verified_at: date


@dataclass(frozen=True)
class CompensationEvidence:
    label: str
    minimum: int
    maximum: int
    currency: str
    source: str
    as_of: date


@dataclass(frozen=True)
class SalaryContext:
    published_range: VerifiedSalaryRange
    benchmarks: tuple[CompensationEvidence, ...]


@dataclass(frozen=True)
class ApplicationFieldReference:
    application_id: str
    field_id: str

    @property
    def vault_key(self) -> str:
        return f"{self.application_id}\u001f{self.field_id}"


@dataclass(frozen=True)
class AtsQuestion:
    field: ApplicationFieldReference
    prompt: str
    mandatory: bool
    meaning: QuestionMeaning = QuestionMeaning.CUSTOM
    standardized_voluntary: bool = False
    salary_context: SalaryContext | None = None


@dataclass(frozen=True)
class TelegramAnswerQuestion:
    request_id: str
    field: ApplicationFieldReference
    prompt: str
    context: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class LocalFormAnswer:
    """An exact answer that may be consumed only by the local form filler."""

    field: ApplicationFieldReference
    value: str
    source: str


@dataclass(frozen=True)
class LocalAnswerProfile:
    """Sensitive inputs supplied from protected local configuration, never Git."""

    standardized_defaults: Mapping[QuestionMeaning, str] = field(default_factory=dict)
    protected_terms: tuple[str, ...] = ()

    @classmethod
    def load(cls, vault: LocalAnswerVault) -> "LocalAnswerProfile":
        record = vault.snapshot()["profile"]
        try:
            defaults = {
                QuestionMeaning(key): str(value)
                for key, value in record["standardized_defaults"].items()
            }
            protected_terms = tuple(map(str, record["protected_terms"]))
        except (KeyError, TypeError, ValueError):
            raise AnswerServiceError("Local answer profile is unavailable") from None
        return cls(defaults, protected_terms)

    def save(self, vault: LocalAnswerVault) -> None:
        record: LocalProfileRecord = {
            "standardized_defaults": {
                meaning.value: str(value)
                for meaning, value in self.standardized_defaults.items()
            },
            "protected_terms": list(map(str, self.protected_terms)),
        }

        def store_profile(vault_state: VaultState) -> VaultState:
            vault_state["profile"] = record
            return vault_state

        vault.transact(store_profile)


@dataclass(frozen=True)
class ProtectedAnswerReport:
    application_id: str
    answers: dict[str, str]
    recorded_at: str


class ProtectedAnswerReportReader:
    """Explicit local-report capability; general services expose no exact-answer read."""

    def __init__(self, vault: LocalAnswerVault):
        self._vault = vault

    def read(self, application_id: str) -> ProtectedAnswerReport:
        record = self._vault.snapshot()["submitted"].get(application_id)
        if record is None:
            raise AnswerServiceError("Protected application answer report is unavailable")
        return ProtectedAnswerReport(
            application_id=application_id,
            answers={str(key): str(value) for key, value in record["answers"].items()},
            recorded_at=str(record["recorded_at"]),
        )


class LocalAtsAnswerService:
    """Resolve ATS questions without guessing or crossing public boundaries."""

    def __init__(
        self,
        *,
        vault: LocalAnswerVault,
        profile: LocalAnswerProfile,
        now: Callable[[], datetime],
        request_id_factory: Callable[[], str],
    ) -> None:
        self._vault = vault
        self._profile = profile
        self._now = now
        self._request_id_factory = request_id_factory

    def resolve(self, question: AtsQuestion) -> LocalFormAnswer | TelegramAnswerQuestion:
        semantic_key = _semantic_key(question)
        salary = question.meaning == QuestionMeaning.SALARY_EXPECTATION
        context = ""
        if salary:
            context = _salary_context_text(question.salary_context)

        vault_state = self._vault.snapshot()
        one_use_key = question.field.vault_key
        if one_use_key in vault_state["one_use"]:
            return self._local_form_answer(
                question.field,
                str(vault_state["one_use"][one_use_key]),
                "one_use",
            )

        if (
            question.standardized_voluntary
            and question.meaning in _STANDARDIZED_PROFILE_MEANINGS
        ):
            local_value = self._profile.standardized_defaults.get(question.meaning)
            if local_value:
                return self._local_form_answer(
                    question.field, str(local_value), "local_profile"
                )

        if not question.mandatory:
            raise AnswerServiceError("Only mandatory ATS questions are resolved")

        if not salary and semantic_key in vault_state["defaults"]:
            return self._local_form_answer(
                question.field,
                str(vault_state["defaults"][semantic_key]),
                "reusable_default",
            )

        request_id = str(self._request_id_factory())
        record: AnswerRequestRecord = {
            "application_id": question.field.application_id,
            "field_id": question.field.field_id,
            "semantic_key": semantic_key,
            "salary": salary,
            "created_at": self._now().isoformat(),
        }

        def add_request(vault_state: VaultState) -> VaultState:
            if request_id in vault_state["requests"]:
                raise AnswerServiceError("Answer request could not be created")
            vault_state["requests"][request_id] = record
            return vault_state

        self._vault.transact(add_request)
        actions = (
            ("Usa solo qui",)
            if salary
            else ("Usa solo qui", "Salva come default")
        )
        return TelegramAnswerQuestion(
            request_id=request_id,
            field=question.field,
            prompt=str(self.redact_for_public_boundaries(question.prompt)),
            context=context,
            actions=actions,
        )

    def answer(
        self, request_id: str, answer: str, scope: AnswerScope
    ) -> LocalFormAnswer:
        scope = AnswerScope(scope)
        if not isinstance(answer, str) or not answer.strip():
            raise AnswerServiceError("A non-empty answer is required")
        if self._contains_protected_term(answer):
            raise AnswerServiceError(
                "Sensitive health detail cannot be used as an ATS answer"
            )
        resolved: LocalFormAnswer | None = None

        def store_answer(vault_state: VaultState) -> VaultState:
            nonlocal resolved
            request = vault_state["requests"].get(request_id)
            if request is None:
                raise AnswerServiceError("Answer request is unavailable")
            if request.get("answered_at") is not None:
                raise AnswerServiceError("Answer request is already resolved")
            if request["salary"] and scope == AnswerScope.DEFAULT:
                raise AnswerServiceError("This answer cannot be saved as a default")
            if scope == AnswerScope.DEFAULT:
                vault_state["defaults"][request["semantic_key"]] = answer
                source = "reusable_default"
            else:
                reference = ApplicationFieldReference(
                    request["application_id"], request["field_id"]
                )
                vault_state["one_use"][reference.vault_key] = answer
                source = "one_use"
            request["answered_at"] = self._now().isoformat()
            request["scope"] = scope.value
            resolved = LocalFormAnswer(
                field=ApplicationFieldReference(
                    str(request["application_id"]), str(request["field_id"])
                ),
                value=answer,
                source=source,
            )
            return vault_state

        self._vault.transact(store_answer)
        assert resolved is not None
        return resolved

    def mark_used(self, field: ApplicationFieldReference) -> None:
        key = field.vault_key

        def remove(vault_state: VaultState) -> VaultState:
            vault_state["one_use"].pop(key, None)
            return vault_state

        self._vault.transact(remove)

    def record_submitted_answers(
        self, application_id: str, answers: Mapping[str, str]
    ) -> None:
        exact = {str(key): str(value) for key, value in answers.items()}
        if any(self._contains_protected_term(value) for value in exact.values()):
            raise AnswerServiceError(
                "Sensitive health detail cannot be used as an ATS answer"
            )

        def record(vault_state: VaultState) -> VaultState:
            vault_state["submitted"][application_id] = {
                "answers": exact,
                "recorded_at": self._now().isoformat(),
            }
            return vault_state

        self._vault.transact(record)

    def public_status(self, application_id: str) -> dict[str, str | int]:
        submitted = self._vault.snapshot()["submitted"].get(application_id)
        count = 0 if submitted is None else len(submitted["answers"])
        return {"application_id": application_id, "submitted_answer_count": count}

    def redact_for_public_boundaries(self, payload: Any) -> Any:
        sensitive = set(self._profile.standardized_defaults.values())
        sensitive.update(self._profile.protected_terms)
        vault_state = self._vault.snapshot()
        sensitive.update(map(str, vault_state["defaults"].values()))
        sensitive.update(map(str, vault_state["one_use"].values()))
        for record in vault_state["submitted"].values():
            sensitive.update(map(str, record["answers"].values()))
        return _redact(payload, tuple(value for value in sensitive if value))

    def _local_form_answer(
        self, field: ApplicationFieldReference, value: str, source: str
    ) -> LocalFormAnswer:
        if self._contains_protected_term(value):
            raise AnswerServiceError(
                "Sensitive health detail cannot be used as an ATS answer"
            )
        return LocalFormAnswer(field=field, value=value, source=source)

    def _contains_protected_term(self, value: str) -> bool:
        return any(
            re.search(re.escape(term), value, flags=re.IGNORECASE)
            for term in self._profile.protected_terms
            if term
        )


def _semantic_key(question: AtsQuestion) -> str:
    if question.meaning != QuestionMeaning.CUSTOM:
        return question.meaning.value
    normalized = unicodedata.normalize("NFKC", question.prompt).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized)
    normalized = " ".join(normalized.split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"custom:{digest}"


def _salary_context_text(context: SalaryContext | None) -> str:
    if context is None or not context.benchmarks:
        raise AnswerServiceError("Verified salary context is required")
    published = context.published_range
    if (
        published.minimum > published.maximum
        or not published.currency
        or not published.source
        or not isinstance(published.verified_at, date)
    ):
        raise AnswerServiceError("Verified salary context is required")
    lines = [
        f"Published range: {published.currency} {published.minimum}–{published.maximum} "
        f"(verified {published.verified_at.isoformat()}; source: {published.source})"
    ]
    for benchmark in context.benchmarks:
        if (
            benchmark.minimum > benchmark.maximum
            or not benchmark.currency
            or not benchmark.source
            or not benchmark.label
            or not isinstance(benchmark.as_of, date)
        ):
            raise AnswerServiceError("Verified salary context is required")
        lines.append(
            f"{benchmark.label}: {benchmark.currency} {benchmark.minimum}–{benchmark.maximum} "
            f"(as of {benchmark.as_of.isoformat()}; source: {benchmark.source})"
        )
    return "\n".join(lines)


def _redact(payload: Any, sensitive: tuple[str, ...]) -> Any:
    if is_dataclass(payload) and not isinstance(payload, type):
        return _redact(asdict(payload), sensitive)
    if isinstance(payload, str):
        for value in sorted(set(sensitive), key=len, reverse=True):
            payload = re.sub(re.escape(value), "[REDACTED]", payload, flags=re.IGNORECASE)
        return payload
    if isinstance(payload, Mapping):
        return {
            _redact(key, sensitive) if isinstance(key, str) else key: _redact(
                value, sensitive
            )
            for key, value in payload.items()
        }
    if isinstance(payload, tuple):
        return tuple(_redact(value, sensitive) for value in payload)
    if isinstance(payload, list):
        return [_redact(value, sensitive) for value in payload]
    return payload
