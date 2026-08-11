from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from work_agent.schedule.errors import ScheduleError
from work_agent.slack.models import Availability, AvailabilityBatchResult


class ReconciliationStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (
            Path.home()
            / "Library"
            / "Application Support"
            / "pikvm-work-agent"
            / "slack-availability-state.json"
        )

    @property
    def path(self) -> Path:
        return self._path

    def profiles_requiring_reconciliation(
        self,
        profiles: tuple[str, ...],
        desired: Availability,
    ) -> tuple[str, ...]:
        state = self._read()
        applied = state.get("applied", {})
        if not isinstance(applied, dict):
            return profiles
        return tuple(profile for profile in profiles if applied.get(profile) != desired.value)

    def record_successes(self, result: AvailabilityBatchResult) -> None:
        state = self._read()
        applied = state.get("applied", {})
        if not isinstance(applied, dict):
            applied = {}
        normalized = {
            str(profile): str(availability)
            for profile, availability in applied.items()
            if isinstance(profile, str) and isinstance(availability, str)
        }
        for item in result.results:
            if item.success and item.desired is not None and item.observed is item.desired:
                normalized[item.kvm] = item.desired.value
        payload: dict[str, object] = {
            "updated_at": datetime.now(UTC).isoformat(),
            "applied": normalized,
        }
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, payload: dict[str, object]) -> None:
        temporary = self._path.with_name(f"{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
            temporary.replace(self._path)
        except OSError:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise ScheduleError("The local reconciliation state could not be written.") from None
