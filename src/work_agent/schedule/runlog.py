"""Outcome streaks read from the sanitized Slack availability JSONL log.

Both the scheduler (for notifications) and the dashboard (for schedule health, the failure
banner, and per-KVM status) need the same question answered: how many consecutive runs has each
KVM just failed, and why. Keep that logic in one place so their answers never disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The endpoint lock was held by another workflow: the KVM itself neither succeeded nor failed,
# so such a record neither extends nor breaks a failure streak.
LOCK_BUSY_STOP_CODE = "lock_busy"

# Controller stop codes that mean the PiKVM itself could not be used, as opposed to a GUI step
# that could not be verified.
PIKVM_UNREACHABLE_STOP_CODE = "pikvm_unreachable"
PIKVM_AUTH_FAILED_STOP_CODE = "pikvm_auth_failed"
CONNECTIVITY_STOP_CODES = frozenset({PIKVM_UNREACHABLE_STOP_CODE, PIKVM_AUTH_FAILED_STOP_CODE})

# Human labels for the reasons a notification or banner may name. Deliberately terse: these
# strings reach macOS Notification Center and the browser, never any screen content.
STOP_CODE_CATEGORY_LABELS: dict[str, str] = {
    PIKVM_UNREACHABLE_STOP_CODE: "PiKVM unreachable",
    PIKVM_AUTH_FAILED_STOP_CODE: "PiKVM rejected credentials",
}


@dataclass(frozen=True, slots=True)
class RunOutcome:
    timestamp: datetime
    kvm: str
    success: bool
    stop_code: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class FailureStreak:
    """The run of consecutive failures ending at a KVM's most recent recorded outcome."""

    kvm: str
    count: int
    first_at: datetime | None
    last_at: datetime | None
    last_stop_code: str | None
    last_error: str | None
    # First record in the streak that stopped because the PiKVM was unreachable, if any.
    unreachable_since: datetime | None
    # Most recent recorded outcome, whether or not it failed.
    latest_at: datetime | None
    latest_success: bool | None

    @property
    def failing(self) -> bool:
        return self.count > 0


def _parse_outcome(payload: object) -> RunOutcome | None:
    if not isinstance(payload, dict):
        return None
    raw_timestamp = payload.get("timestamp")
    kvm = payload.get("kvm")
    outcome = payload.get("outcome")
    if not isinstance(raw_timestamp, str) or not isinstance(kvm, str):
        return None
    if outcome not in {"success", "failure"}:
        return None
    try:
        timestamp = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return None
    stop_code = payload.get("stop_code")
    error = payload.get("error")
    return RunOutcome(
        timestamp=timestamp,
        kvm=kvm,
        success=outcome == "success",
        stop_code=stop_code if isinstance(stop_code, str) else None,
        error=error if isinstance(error, str) else None,
    )


def read_outcomes(path: Path) -> list[RunOutcome]:
    """Read every well-formed outcome from the log, oldest first; a missing log is empty."""

    if not path.is_file():
        return []
    outcomes: list[RunOutcome] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                parsed = _parse_outcome(payload)
                if parsed is not None:
                    outcomes.append(parsed)
    except OSError:
        return []
    outcomes.sort(key=lambda outcome: outcome.timestamp)
    return outcomes


def failure_streaks(outcomes: list[RunOutcome]) -> dict[str, FailureStreak]:
    """Summarize each KVM's trailing failure streak from oldest-first outcomes."""

    by_kvm: dict[str, list[RunOutcome]] = {}
    for outcome in outcomes:
        by_kvm.setdefault(outcome.kvm, []).append(outcome)

    streaks: dict[str, FailureStreak] = {}
    for kvm, owned in by_kvm.items():
        counted = [outcome for outcome in owned if outcome.stop_code != LOCK_BUSY_STOP_CODE]
        trailing: list[RunOutcome] = []
        for outcome in reversed(counted):
            if outcome.success:
                break
            trailing.append(outcome)
        trailing.reverse()
        unreachable = next(
            (
                outcome.timestamp
                for outcome in trailing
                if outcome.stop_code == PIKVM_UNREACHABLE_STOP_CODE
            ),
            None,
        )
        latest = owned[-1]
        last_failure = trailing[-1] if trailing else None
        streaks[kvm] = FailureStreak(
            kvm=kvm,
            count=len(trailing),
            first_at=trailing[0].timestamp if trailing else None,
            last_at=last_failure.timestamp if last_failure is not None else None,
            last_stop_code=last_failure.stop_code if last_failure is not None else None,
            last_error=last_failure.error if last_failure is not None else None,
            unreachable_since=unreachable,
            latest_at=latest.timestamp,
            latest_success=latest.success,
        )
    return streaks


def read_failure_streaks(path: Path) -> dict[str, FailureStreak]:
    return failure_streaks(read_outcomes(path))
