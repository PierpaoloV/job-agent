"""Resolve shortlisted alert leads to exact official vacancies before grading."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Protocol

from opportunity_domain import (
    OfficialVacancySnapshot,
    Runtime,
    VerificationStatus,
)
from opportunity_sources import OpportunityLead
from opportunity_workflow import (
    HostedFetchBlocked,
    OfficialSource,
    OfficialVacancyUnavailable,
)
from workflow import NormalizedOpportunity, ShortlistArtifact


class Clock(Protocol):
    def now(self): ...


def verify_shortlist(
    artifact: ShortlistArtifact,
    *,
    source: OfficialSource,
    clock: Clock,
) -> ShortlistArtifact:
    """Enrich only deterministic shortlist winners with official vacancy facts."""

    artifact.validate()
    opportunities = tuple(
        _verify(item, source=source, clock=clock)
        if item.shortlisted
        else item
        for item in artifact.opportunities
    )
    verified = ShortlistArtifact(
        version=artifact.version,
        created_at=artifact.created_at,
        opportunities=opportunities,
    )
    verified.validate()
    return verified


def _verify(
    opportunity: NormalizedOpportunity,
    *,
    source: OfficialSource,
    clock: Clock,
) -> NormalizedOpportunity:
    lead = _lead(opportunity)
    try:
        vacancy = source.retrieve(lead, Runtime.HOSTED)
        if not vacancy.description.strip():
            raise OfficialVacancyUnavailable(
                "the official description is empty"
            )
    except (HostedFetchBlocked, OfficialVacancyUnavailable):
        return replace(
            opportunity,
            job={
                **opportunity.job,
                "verification_status": VerificationStatus.NEEDS_LOCAL_FETCH.value,
                "official_description": "",
            },
        )

    retrieved_at = clock.now().isoformat()
    snapshot = OfficialVacancySnapshot.capture(
        vacancy,
        retrieved_at=retrieved_at,
    )
    compensation = (
        {
            "status": "published",
            "facts": [{"value": vacancy.compensation}],
        }
        if vacancy.compensation.strip()
        else {"status": "unknown", "facts": []}
    )
    job = {
        **opportunity.job,
        "title": vacancy.role,
        "role": vacancy.role,
        "company": vacancy.company,
        "team": vacancy.team,
        "location": vacancy.location,
        "modality": vacancy.modality,
        "remote_policy": vacancy.modality,
        "seniority": vacancy.seniority,
        "official_description": vacancy.description,
        "official_description_url": vacancy.canonical_url,
        "official_url": vacancy.canonical_url,
        "canonical_url": vacancy.canonical_url,
        "official_vacancy_version": snapshot.version,
        "verification_status": VerificationStatus.VERIFIED.value,
        "retrieved_at": retrieved_at,
        "published_at": vacancy.published_at,
        "requirements": list(vacancy.requirements),
        "compensation": compensation,
        "sponsorship": {
            "status": vacancy.sponsorship or "not_stated",
            "source": vacancy.canonical_url,
            "verified_at": retrieved_at[:10],
        },
        "ownership": {
            "classification": vacancy.ownership or "unknown",
            "source": vacancy.canonical_url,
            "verified_at": retrieved_at[:10],
        },
        "process_language": _process_language(vacancy.description),
    }
    return replace(opportunity, job=job)


def _lead(opportunity: NormalizedOpportunity) -> OpportunityLead:
    job = opportunity.job
    return OpportunityLead(
        stable_id=opportunity.stable_id,
        source=str(job.get("source", "")),
        source_confidence=opportunity.source_confidence,
        canonical_url=str(
            job.get("canonical_url")
            or job.get("official_url")
            or job.get("url")
            or ""
        ),
        title=str(job.get("title") or job.get("role") or ""),
        company=str(job.get("company", "")),
        location=str(job.get("location", "")),
        modality=str(job.get("modality") or job.get("remote_policy") or ""),
        snippet=str(job.get("snippet", "")),
        email_received_at=(
            str(job["email_date"]) if job.get("email_date") else None
        ),
        discovered_at=opportunity.discovered_at,
        published_at=(
            str(job["published_at"]) if job.get("published_at") else None
        ),
    )


def _process_language(description: str) -> str:
    words = set(re.findall(r"[a-zà-ÿ]+", description.casefold()))
    vocabularies = {
        "english": {"the", "and", "with", "you", "your", "we", "our"},
        "german": {"der", "die", "das", "und", "mit", "sie", "wir"},
        "french": {"le", "la", "les", "et", "avec", "vous", "nous"},
        "italian": {"il", "la", "gli", "e", "con", "tu", "noi"},
    }
    language, score = max(
        (
            (name, len(words & vocabulary))
            for name, vocabulary in vocabularies.items()
        ),
        key=lambda item: item[1],
    )
    return language if score >= 2 else "unknown"


__all__ = ["verify_shortlist"]
