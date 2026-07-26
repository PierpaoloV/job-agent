"""Canonical identities shared by opportunity and application workflows."""

from __future__ import annotations

import hashlib
import re


_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def approved_application_id(stable_id: str, vacancy_version: str) -> str:
    stable = str(stable_id)
    version = str(vacancy_version)
    if not stable or stable != stable.strip():
        raise ValueError("Application opportunity id must be canonical")
    if not _CANONICAL_SHA256.fullmatch(version):
        raise ValueError("Application vacancy version must be canonical sha256")
    digest = hashlib.sha256(f"{stable}:{version}".encode("utf-8")).hexdigest()[:16]
    return f"approved-{digest}"


__all__ = ["approved_application_id"]
