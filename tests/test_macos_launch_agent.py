from pathlib import Path
import plistlib
import stat
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ROOT = Path(__file__).resolve().parents[1]

from macos_launch_agent import LaunchAgentConfig, MacOSLaunchAgent  # noqa: E402


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...]) -> None:
        self.commands.append(command)


def test_launch_agent_is_secret_free_and_all_lifecycle_commands_are_injected(tmp_path):
    label = "com.example.job-agent"
    plist_path = tmp_path / "LaunchAgents" / f"{label}.plist"
    log_directory = tmp_path / "logs"
    log_directory.mkdir(mode=0o755)
    log_directory.chmod(0o755)
    runner = RecordingRunner()
    agent = MacOSLaunchAgent(
        config=LaunchAgentConfig(
            label=label,
            python_executable=Path("/usr/bin/python3"),
            worker_module="local_worker_main",
            working_directory=ROOT / "src",
            plist_path=plist_path,
            log_directory=log_directory,
            uid=501,
        ),
        runner=runner,
    )

    installed = agent.install()
    agent.start()
    agent.stop()

    payload = plistlib.loads(plist_path.read_bytes())
    assert installed == plist_path
    assert payload["Label"] == label
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3",
        "-m",
        "local_worker_main",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert "EnvironmentVariables" not in payload
    assert "token" not in plist_path.read_text(encoding="utf-8").casefold()
    assert (ROOT / "src" / "local_worker_main.py").is_file()
    assert stat.S_IMODE(log_directory.stat().st_mode) == 0o700
    assert runner.commands == [
        ("launchctl", "bootstrap", "gui/501", str(plist_path)),
        ("launchctl", "kickstart", "-k", f"gui/501/{label}"),
        ("launchctl", "bootout", f"gui/501/{label}"),
    ]
