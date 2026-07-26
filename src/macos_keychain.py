"""macOS Keychain and local ATS credential generation adapters."""

from __future__ import annotations

import secrets
import string
import subprocess
from typing import Callable, Sequence


class MacOSKeychainCredentialStore:
    """Store generated ATS credentials without repository persistence or a shell."""

    def __init__(
        self,
        command_runner: Callable[[Sequence[str]], str | None] | None = None,
    ) -> None:
        self._run = command_runner or _run_security

    def get(self, service: str, account: str) -> str | None:
        return self._run(
            (
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            )
        )

    def store(self, service: str, account: str, password: str) -> None:
        self._run(
            (
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                password,
            )
        )


def generate_ats_password() -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#%"
    return "".join(secrets.choice(alphabet) for _ in range(28))


def _run_security(arguments: Sequence[str]) -> str | None:
    result = subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 44 and "find-generic-password" in arguments:
        return None
    if result.returncode != 0:
        raise RuntimeError("macOS Keychain operation failed")
    return result.stdout.rstrip("\n")


__all__ = ["MacOSKeychainCredentialStore", "generate_ats_password"]
