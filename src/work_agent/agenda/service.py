from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from work_agent.agenda.errors import AgendaError
from work_agent.agenda.models import AgendaBatchResult, AgendaReport, MeetingStatus
from work_agent.diagnostics import log_exception

_LOGGER = logging.getLogger(__name__)


class AgendaOperatorProtocol(Protocol):
    def execute(self, kvm: str) -> AgendaReport: ...


class AgendaLogger(Protocol):
    def record(self, report: AgendaReport) -> None: ...


class JsonlAgendaLogger:
    """Record only counts.

    Meeting titles, organizers, locations, and join links stay out of the log for the same reason
    Slack conversation names do: a calendar is a record of who the user meets and when, and the
    roadmap forbids persisting that locally. Counts are enough to see whether the workflow works.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (
            Path.home() / "Library" / "Logs" / "pikvm-work-agent" / "calendar-agenda.jsonl"
        )

    @property
    def path(self) -> Path:
        return self._path

    def record(self, report: AgendaReport) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kvm": report.kvm,
            "outcome": "success" if report.success else "failure",
            "surface": report.surface.value,
            "meetings": len(report.items),
            "upcoming": len(report.upcoming),
            "in_progress": sum(
                1 for item in report.items if item.status is MeetingStatus.IN_PROGRESS
            ),
            "clock_read": report.clock_read,
            "scrolled": report.scrolled,
            "later_truncated": report.later_truncated,
            "stop_code": report.stop_code,
            "warnings": list(report.warnings),
            "error": report.error,
        }
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")
        except OSError:
            raise AgendaError("The local calendar log could not be written.") from None


class AgendaService:
    def __init__(self, operator: AgendaOperatorProtocol, logger: AgendaLogger) -> None:
        self._operator = operator
        self._logger = logger

    def run(self, kvms: tuple[str, ...]) -> AgendaBatchResult:
        reports: list[AgendaReport] = []
        for kvm in kvms:
            try:
                report = self._operator.execute(kvm)
            except AgendaError as exc:
                report = AgendaReport(kvm=kvm, success=False, error=str(exc))
            except Exception as exc:
                log_exception(_LOGGER, f"Calendar read for {kvm} crashed", exc)
                report = AgendaReport(
                    kvm=kvm,
                    success=False,
                    error=(
                        f"An unexpected local error stopped this calendar read "
                        f"({type(exc).__name__})."
                    ),
                )
            try:
                self._logger.record(report)
            except AgendaError as exc:
                report = replace(report, log_error=str(exc))
            except Exception as exc:
                log_exception(_LOGGER, "Calendar logging failed", exc)
                report = replace(
                    report,
                    log_error="An unexpected local error prevented calendar logging.",
                )
            reports.append(report)
        return AgendaBatchResult(reports=tuple(reports))
