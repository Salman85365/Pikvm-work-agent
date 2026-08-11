from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Availability(StrEnum):
    ACTIVE = "active"
    AWAY = "away"


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    kvm: str
    desired: Availability | None
    observed: Availability | None
    changed: bool | None
    success: bool
    error: str | None = None
    log_error: str | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityBatchResult:
    results: tuple[AvailabilityResult, ...]

    @property
    def success(self) -> bool:
        return bool(self.results) and all(result.success for result in self.results)
