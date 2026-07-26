"""Secret-free launchd configuration with an injected command boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import plistlib
import re
from typing import Protocol


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")


class CommandRunner(Protocol):
    def run(self, command: tuple[str, ...]) -> None: ...


@dataclass(frozen=True)
class LaunchAgentConfig:
    label: str
    python_executable: Path
    worker_module: str
    working_directory: Path
    plist_path: Path
    log_directory: Path
    uid: int

    def __post_init__(self) -> None:
        if not _LABEL.fullmatch(self.label):
            raise ValueError("LaunchAgent label is invalid")
        if not _MODULE.fullmatch(self.worker_module):
            raise ValueError("Worker module is invalid")
        if self.uid < 0:
            raise ValueError("LaunchAgent uid is invalid")
        for field in (
            "python_executable",
            "working_directory",
            "plist_path",
            "log_directory",
        ):
            path = Path(getattr(self, field))
            if not path.is_absolute():
                raise ValueError(f"{field} must be absolute")
            object.__setattr__(self, field, path)


class MacOSLaunchAgent:
    def __init__(self, *, config: LaunchAgentConfig, runner: CommandRunner) -> None:
        self._config = config
        self._runner = runner

    def render_plist(self) -> bytes:
        config = self._config
        payload = {
            "Label": config.label,
            "ProgramArguments": [
                str(config.python_executable),
                "-m",
                config.worker_module,
            ],
            "WorkingDirectory": str(config.working_directory),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "StandardOutPath": str(config.log_directory / "worker.stdout.log"),
            "StandardErrorPath": str(config.log_directory / "worker.stderr.log"),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def install(self) -> Path:
        config = self._config
        config.log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.log_directory.chmod(0o700)
        config.plist_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config.plist_path.with_suffix(config.plist_path.suffix + ".tmp")
        temporary.write_bytes(self.render_plist())
        temporary.chmod(0o644)
        temporary.replace(config.plist_path)
        self._runner.run(
            (
                "launchctl",
                "bootstrap",
                f"gui/{config.uid}",
                str(config.plist_path),
            )
        )
        return config.plist_path

    def start(self) -> None:
        config = self._config
        self._runner.run(
            (
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{config.uid}/{config.label}",
            )
        )

    def stop(self) -> None:
        config = self._config
        self._runner.run(
            ("launchctl", "bootout", f"gui/{config.uid}/{config.label}")
        )


__all__ = [
    "CommandRunner",
    "LaunchAgentConfig",
    "MacOSLaunchAgent",
]
