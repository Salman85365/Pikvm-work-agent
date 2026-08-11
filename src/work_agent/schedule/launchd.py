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


class InterpreterProbe(Protocol):
    def can_import_work_agent(self, python: Path, working_directory: Path) -> bool: ...


class SubprocessInterpreterProbe:
    """Confirm a recorded interpreter can actually run the scheduled command."""

    def can_import_work_agent(self, python: Path, working_directory: Path) -> bool:
        if not python.is_file() or not working_directory.is_dir():
            return False
        try:
            completed = subprocess.run(
                (str(python), "-c", "import work_agent"),
                check=False,
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return completed.returncode == 0


@dataclass(frozen=True, slots=True)
class LaunchAgentStatus:
    label: str
    path: Path
    installed: bool
    loaded: bool


@dataclass(frozen=True, slots=True)
class ScheduleHealth:
    """Whether installed agents can actually run, not merely whether launchd loaded them."""

    interpreter: Path | None
    working_directory: Path | None
    interpreter_can_run: bool
    timezone_name: str | None
    timezone_ok: bool
    problems: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.problems


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
        interpreter_probe: InterpreterProbe | None = None,
    ) -> None:
        self._launch_agents_dir = launch_agents_dir or (Path.home() / "Library" / "LaunchAgents")
        self._log_dir = log_dir or (Path.home() / "Library" / "Logs" / "pikvm-work-agent")
        self._python = (python_executable or Path(sys.executable)).resolve()
        self._working_directory = (working_directory or Path.cwd()).resolve()
        self._uid = os.getuid() if uid is None else uid
        self._timezone_name = timezone_name or _system_timezone_name()
        self._runner = runner or SubprocessCommandRunner()
        self._probe = interpreter_probe or SubprocessInterpreterProbe()

    def install(self) -> tuple[LaunchAgentStatus, ...]:
        if self._timezone_name != _REQUIRED_TIMEZONE:
            raise ScheduleError(
                "The Mac system time zone must be Asia/Karachi before installing this schedule."
            )
        # launchd records an absolute interpreter path, so a wrong one fails silently every run.
        if not self._probe.can_import_work_agent(self._python, self._working_directory):
            raise ScheduleError(
                f"{self._python} cannot import work_agent from {self._working_directory}, so "
                "every scheduled run would fail. Install this schedule with the interpreter that "
                "has the project installed, for example "
                "`.venv/bin/python -m work_agent schedule slack-availability install`."
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

    def health(self) -> ScheduleHealth:
        """Probe what the installed agents would actually execute."""

        recorded = self._recorded_invocation()
        interpreter, working_directory = recorded if recorded is not None else (None, None)
        timezone_ok = self._timezone_name == _REQUIRED_TIMEZONE
        problems: list[str] = []
        if not timezone_ok:
            problems.append(
                f"The Mac system time zone is {self._timezone_name or 'unknown'}, but launchd "
                "calendar triggers need Asia/Karachi."
            )

        if interpreter is None or working_directory is None:
            return ScheduleHealth(
                interpreter=None,
                working_directory=None,
                interpreter_can_run=False,
                timezone_name=self._timezone_name,
                timezone_ok=timezone_ok,
                problems=tuple(problems),
            )

        can_run = self._probe.can_import_work_agent(interpreter, working_directory)
        if not can_run:
            problems.append(
                f"The installed agents run {interpreter}, which cannot import work_agent from "
                f"{working_directory}. Every scheduled run fails. Reinstall with the project "
                "interpreter."
            )
        return ScheduleHealth(
            interpreter=interpreter,
            working_directory=working_directory,
            interpreter_can_run=can_run,
            timezone_name=self._timezone_name,
            timezone_ok=timezone_ok,
            problems=tuple(problems),
        )

    def _recorded_invocation(self) -> tuple[Path, Path] | None:
        for spec in self._specs():
            if not spec.path.is_file():
                continue
            try:
                payload = plistlib.loads(spec.path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            arguments = payload.get("ProgramArguments")
            directory = payload.get("WorkingDirectory")
            if (
                isinstance(arguments, list)
                and arguments
                and isinstance(arguments[0], str)
                and isinstance(directory, str)
            ):
                return Path(arguments[0]), Path(directory)
        return None

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
