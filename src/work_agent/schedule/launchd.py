from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from work_agent.schedule.errors import ScheduleError

_LABEL_PREFIX = "com.pikvm-work-agent.slack-availability"
_REQUIRED_TIMEZONE = "Asia/Karachi"


def _system_timezone_name() -> str | None:
    try:
        resolved = Path("/etc/localtime").resolve(strict=True).as_posix()
    except OSError:
        return None
    marker = "/zoneinfo/"
    return resolved.split(marker, 1)[1] if marker in resolved else None


class CommandRunner(Protocol):
    def run(self, arguments: tuple[str, ...]) -> int: ...


class SubprocessCommandRunner:
    def run(self, arguments: tuple[str, ...]) -> int:
        try:
            completed = subprocess.run(
                arguments,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise ScheduleError("launchctl could not be executed.") from None
        return completed.returncode


@dataclass(frozen=True, slots=True)
class LaunchAgentStatus:
    label: str
    path: Path
    installed: bool
    loaded: bool


@dataclass(frozen=True, slots=True)
class _LaunchAgentSpec:
    label: str
    path: Path
    payload: dict[str, object]


class SlackAvailabilityLaunchdManager:
    def __init__(
        self,
        *,
        launch_agents_dir: Path | None = None,
        log_dir: Path | None = None,
        python_executable: Path | None = None,
        working_directory: Path | None = None,
        uid: int | None = None,
        timezone_name: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._launch_agents_dir = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
        self._log_dir = log_dir or (Path.home() / "Library" / "Logs" / "pikvm-work-agent")
        self._python = (python_executable or Path(sys.executable)).resolve()
        self._working_directory = (working_directory or Path.cwd()).resolve()
        self._uid = os.getuid() if uid is None else uid
        self._timezone_name = timezone_name or _system_timezone_name()
        self._runner = runner or SubprocessCommandRunner()

    def install(self) -> tuple[LaunchAgentStatus, ...]:
        if self._timezone_name != _REQUIRED_TIMEZONE:
            raise ScheduleError(
                "The Mac system time zone must be Asia/Karachi before installing this schedule."
            )
        specs = self._specs()
        try:
            self._launch_agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise ScheduleError(
                "The user LaunchAgents or log directory could not be created."
            ) from None

        domain = f"gui/{self._uid}"
        failures: list[str] = []
        for spec in specs:
            self._write_plist(spec)
            self._runner.run(("launchctl", "bootout", domain, str(spec.path)))
            if self._runner.run(("launchctl", "bootstrap", domain, str(spec.path))) != 0:
                failures.append(spec.label)
        if failures:
            raise ScheduleError(
                "launchd could not load all Slack availability agents: " + ", ".join(failures)
            )
        return self.status()

    def uninstall(self) -> tuple[LaunchAgentStatus, ...]:
        domain = f"gui/{self._uid}"
        for spec in self._specs():
            self._runner.run(("launchctl", "bootout", domain, str(spec.path)))
            try:
                spec.path.unlink(missing_ok=True)
            except OSError:
                raise ScheduleError(
                    "A generated Slack availability LaunchAgent could not be removed."
                ) from None
        return self.status()

    def status(self) -> tuple[LaunchAgentStatus, ...]:
        domain = f"gui/{self._uid}"
        return tuple(
            LaunchAgentStatus(
                label=spec.label,
                path=spec.path,
                installed=spec.path.is_file(),
                loaded=self._runner.run(("launchctl", "print", f"{domain}/{spec.label}")) == 0,
            )
            for spec in self._specs()
        )

    def _specs(self) -> tuple[_LaunchAgentSpec, ...]:
        stdout_path = str(self._log_dir / "launchd.stdout.log")
        stderr_path = str(self._log_dir / "launchd.stderr.log")

        def base(label: str, command: list[str]) -> dict[str, object]:
            return {
                "Label": label,
                "ProgramArguments": [
                    str(self._python),
                    "-m",
                    "work_agent",
                    *command,
                ],
                "WorkingDirectory": str(self._working_directory),
                "EnvironmentVariables": {"TZ": "Asia/Karachi"},
                "ProcessType": "Background",
                "StandardOutPath": stdout_path,
                "StandardErrorPath": stderr_path,
            }

        active_label = f"{_LABEL_PREFIX}.active"
        active = base(
            active_label,
            [
                "schedule",
                "slack-availability",
                "run-now",
                "--availability",
                "active",
            ],
        )
        active["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": 18, "Minute": 0} for weekday in range(1, 6)
        ]

        away_label = f"{_LABEL_PREFIX}.away"
        away = base(
            away_label,
            [
                "schedule",
                "slack-availability",
                "run-now",
                "--availability",
                "away",
            ],
        )
        away["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": 2, "Minute": 0} for weekday in range(2, 7)
        ]

        reconcile_label = f"{_LABEL_PREFIX}.reconcile"
        reconcile = base(
            reconcile_label,
            ["schedule", "slack-availability", "reconcile", "--if-due"],
        )
        reconcile["RunAtLoad"] = True
        reconcile["StartInterval"] = 3600

        return tuple(
            _LaunchAgentSpec(
                label=label,
                path=self._launch_agents_dir / f"{label}.plist",
                payload=payload,
            )
            for label, payload in (
                (active_label, active),
                (away_label, away),
                (reconcile_label, reconcile),
            )
        )

    @staticmethod
    def _write_plist(spec: _LaunchAgentSpec) -> None:
        temporary = spec.path.with_name(f"{spec.path.name}.tmp")
        try:
            temporary.write_bytes(
                plistlib.dumps(spec.payload, fmt=plistlib.FMT_XML, sort_keys=True)
            )
            temporary.chmod(0o600)
            temporary.replace(spec.path)
        except OSError:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise ScheduleError("A Slack availability LaunchAgent could not be written.") from None
