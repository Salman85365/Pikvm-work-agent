from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Availability(StrEnum):
    ACTIVE = "active"
    AWAY = "away"


@dataclass(frozen=True, slots=True)
class WorkflowTelemetry:
    """Operation metadata for one KVM's workflow, summed over its controller sessions."""

    sessions: int = 0
    steps: int = 0
    hid_actions: int = 0
    vision_calls: int = 0
    planner_calls: int = 0
    total_tokens: int = 0
    runtime_seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "sessions": self.sessions,
            "steps": self.steps,
            "hid_actions": self.hid_actions,
            "vision_calls": self.vision_calls,
            "planner_calls": self.planner_calls,
            "total_tokens": self.total_tokens,
            "runtime_seconds": round(self.runtime_seconds, 1),
        }


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    kvm: str
    desired: Availability | None
    observed: Availability | None
    changed: bool | None
    success: bool
    error: str | None = None
    log_error: str | None = None
    # Machine-readable controller stop cause; classify from this, never from `error`.
    stop_code: str | None = None
    telemetry: WorkflowTelemetry | None = None


@dataclass(frozen=True, slots=True)
class AvailabilityBatchResult:
    results: tuple[AvailabilityResult, ...]

    @property
    def success(self) -> bool:
        return bool(self.results) and all(result.success for result in self.results)
