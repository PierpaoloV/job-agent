"""Typed equivalent identities for one employer vacancy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RoleIdentityKind(str, Enum):
    STABLE_ID = "stable_id"
    URL = "url"
    EMPLOYER_JOB_ID = "employer_job_id"


@dataclass(frozen=True)
class RoleIdentity:
    kind: RoleIdentityKind
    value: str


def role_identity_aliases(
    opportunity: Mapping[str, Any],
) -> frozenset[RoleIdentity]:
    """Return normalized aliases without conflating job IDs across employers."""

    identities: set[RoleIdentity] = set()
    stable_id = _normalized_value(opportunity.get("stable_id"))
    if stable_id:
        identities.add(RoleIdentity(RoleIdentityKind.STABLE_ID, stable_id))
    for field in ("canonical_url", "official_url", "url"):
        value = _normalized_value(opportunity.get(field)).rstrip("/")
        if value:
            identities.add(RoleIdentity(RoleIdentityKind.URL, value))
    official_job_id = _normalized_value(
        opportunity.get(
            "official_job_id",
            opportunity.get("official_id", opportunity.get("job_id")),
        )
    )
    company = _normalized_value(opportunity.get("company"))
    if official_job_id and company:
        identities.add(
            RoleIdentity(
                RoleIdentityKind.EMPLOYER_JOB_ID,
                f"{company}:{official_job_id}",
            )
        )
    return frozenset(identities)


def canonical_role_identity(
    opportunity: Mapping[str, Any],
) -> RoleIdentity | None:
    """Choose the strongest deterministic key for migration conflict checks."""

    aliases = role_identity_aliases(opportunity)
    for kind in (
        RoleIdentityKind.URL,
        RoleIdentityKind.EMPLOYER_JOB_ID,
        RoleIdentityKind.STABLE_ID,
    ):
        matching = sorted(
            (identity for identity in aliases if identity.kind == kind),
            key=lambda identity: identity.value,
        )
        if matching:
            return matching[0]
    return None


def _normalized_value(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).casefold()


__all__ = [
    "RoleIdentity",
    "RoleIdentityKind",
    "canonical_role_identity",
    "role_identity_aliases",
]
