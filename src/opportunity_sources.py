"""Source-specific email adapters for normalized opportunity leads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from parse_jobs import parse_emails


@dataclass(frozen=True)
class OpportunityLead:
    stable_id: str
    source: str
    source_confidence: str
    canonical_url: str
    title: str
    company: str
    location: str
    modality: str
    snippet: str
    email_received_at: str | None
    discovered_at: str
    published_at: str | None


class EmailSourceAdapter(Protocol):
    source_key: str
    source_confidence: str

    def matches(self, sender: str) -> bool: ...

    def parse(
        self, email: Mapping[str, object], *, discovered_at: datetime
    ) -> tuple[OpportunityLead, ...]: ...


class _ParserBackedAdapter:
    source_key = "fallback"
    source_confidence = "supported"
    sender_markers: tuple[str, ...] = ()

    def matches(self, sender: str) -> bool:
        normalized = sender.casefold()
        return any(marker in normalized for marker in self.sender_markers)

    def parse(
        self, email: Mapping[str, object], *, discovered_at: datetime
    ) -> tuple[OpportunityLead, ...]:
        parsed = parse_emails([dict(email)])
        return tuple(self._normalize(job, email, discovered_at) for job in parsed)

    def _normalize(
        self,
        job: Mapping[str, object],
        email: Mapping[str, object],
        discovered_at: datetime,
    ) -> OpportunityLead:
        external_identity = str(job.get("dedup_key") or job["url"])
        return OpportunityLead(
            stable_id=f"{self.source_key}:{external_identity}",
            source=str(job.get("source") or self.source_key),
            source_confidence=self.source_confidence,
            canonical_url=str(job["url"]),
            title=str(job.get("title", "")),
            company=str(job.get("company", "")),
            location=str(job.get("location", "")),
            modality=str(job.get("remote_policy", "")),
            snippet=str(job.get("snippet", "")),
            email_received_at=_optional_string(email.get("date")),
            discovered_at=discovered_at.isoformat(),
            published_at=_optional_string(email.get("publication_date")),
        )


class LinkedInEmailAdapter(_ParserBackedAdapter):
    source_key = "linkedin"
    sender_markers = ("linkedin",)


class IndeedEmailAdapter(_ParserBackedAdapter):
    source_key = "indeed"
    sender_markers = ("indeed",)


class GlassdoorEmailAdapter(_ParserBackedAdapter):
    source_key = "glassdoor"
    sender_markers = ("glassdoor",)


class WelcomeToTheJungleEmailAdapter(_ParserBackedAdapter):
    source_key = "wttj"
    sender_markers = ("welcometothejungle", "wttj")


class FallbackEmailAdapter(_ParserBackedAdapter):
    source_key = "fallback"
    source_confidence = "fallback"

    def matches(self, sender: str) -> bool:
        return True


class EmailLeadRouter:
    """Normalize alert emails without exposing parser-specific record shapes."""

    def __init__(
        self,
        adapters: Sequence[EmailSourceAdapter],
        fallback: EmailSourceAdapter,
    ) -> None:
        self._adapters = tuple(adapters)
        self._fallback = fallback

    @classmethod
    def default(cls) -> "EmailLeadRouter":
        return cls(
            adapters=(
                LinkedInEmailAdapter(),
                IndeedEmailAdapter(),
                GlassdoorEmailAdapter(),
                WelcomeToTheJungleEmailAdapter(),
            ),
            fallback=FallbackEmailAdapter(),
        )

    def normalize(
        self,
        emails: Sequence[Mapping[str, object]],
        *,
        discovered_at: datetime,
    ) -> tuple[OpportunityLead, ...]:
        leads = []
        for email in emails:
            sender = str(email.get("from", ""))
            adapter = next(
                (candidate for candidate in self._adapters if candidate.matches(sender)),
                self._fallback,
            )
            leads.extend(adapter.parse(email, discovered_at=discovered_at))
        return tuple(leads)


def _optional_string(value: object) -> str | None:
    return None if value is None or value == "" else str(value)
