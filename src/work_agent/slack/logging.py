from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.models import AvailabilityResult


class AvailabilityLogger(Protocol):
    def record(self, result: AvailabilityResult) -> None: ...


class JsonlAvailabilityLogger:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (
            Path.home() / "Library" / "Logs" / "pikvm-work-agent" / "slack-availability.jsonl"
        )

    @property
    def path(self) -> Path:
        return self._path

    def record(self, result: AvailabilityResult) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kvm": result.kvm,
            "desired_availability": result.desired.value if result.desired is not None else None,
            "observed_availability": (
                result.observed.value if result.observed is not None else "unknown"
            ),
            "changed": result.changed,
            "outcome": "success" if result.success else "failure",
            "stop_code": result.stop_code,
            "error": result.error,
        }
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            descriptor = os.open(
                self._path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n")
        except OSError:
            raise SlackAvailabilityError(
                "The local Slack availability log could not be written."
            ) from None
