from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from work_agent.diagnostics import log_exception
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.triage_models import AttentionLevel, TriageBatchResult, TriageReport

_LOGGER = logging.getLogger(__name__)


class TriageOperator(Protocol):
    def execute(self, kvm: str) -> TriageReport: ...


class TriageLogger(Protocol):
    def record(self, report: TriageReport) -> None: ...


class JsonlTriageLogger:
    """Record only counts.

    Conversation names, senders, and any message text stay out of the log: the long-term roadmap
    forbids persisting Slack content locally, and a channel list is itself sensitive.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (
            Path.home() / "Library" / "Logs" / "pikvm-work-agent" / "slack-triage.jsonl"
        )

    @property
    def path(self) -> Path:
        return self._path

    def record(self, report: TriageReport) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kvm": report.kvm,
            "outcome": "success" if report.success else "failure",
            "unread_conversations": len(report.items),
            "mentions": sum(
                1 for item in report.items if item.attention is AttentionLevel.MENTIONED
            ),
            "direct_messages": sum(
                1 for item in report.items if item.attention is AttentionLevel.DIRECT
            ),
            "sidebar_truncated": report.sidebar_truncated,
            "stop_code": report.stop_code,
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
            raise SlackAvailabilityError(
                "The local Slack triage log could not be written."
            ) from None


class SlackTriageService:
    def __init__(self, operator: TriageOperator, logger: TriageLogger) -> None:
        self._operator = operator
        self._logger = logger

    def run(self, kvms: tuple[str, ...]) -> TriageBatchResult:
        reports: list[TriageReport] = []
        for kvm in kvms:
            try:
                report = self._operator.execute(kvm)
            except SlackAvailabilityError as exc:
                report = TriageReport(kvm=kvm, success=False, error=str(exc))
            except Exception as exc:
                log_exception(_LOGGER, f"Slack triage for {kvm} crashed", exc)
                report = TriageReport(
                    kvm=kvm,
                    success=False,
                    error=(
                        f"An unexpected local error stopped this KVM triage ({type(exc).__name__})."
                    ),
                )
            try:
                self._logger.record(report)
            except SlackAvailabilityError as exc:
                report = replace(report, log_error=str(exc))
            except Exception as exc:
                log_exception(_LOGGER, "Slack triage logging failed", exc)
                report = replace(
                    report,
                    log_error="An unexpected local error prevented triage logging.",
                )
            reports.append(report)
        return TriageBatchResult(reports=tuple(reports))
