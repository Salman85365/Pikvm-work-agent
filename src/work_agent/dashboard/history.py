from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from work_agent.dashboard.models import (
    FailureCategory,
    FailureReason,
    HistoryResponse,
    HistorySummary,
    KvmOutcome,
    RunRecord,
)

_MAX_REASONS = 8

# Preferred classification: the controller's own StopCode, recorded per run. Keep the keys as
# plain strings so an old log written by a newer/older build still renders.
_STOP_CODE_LABELS: dict[str, str] = {
    "verification_failed": "Could not verify the last action",
    "verification_missing": "Observation returned no verification",
    "completion_unverified": "Finished without visible proof",
    "policy_denied": "Local policy denied the action",
    "approval_denied": "Needed interactive approval",
    "step_cancelled": "Step cancelled before execution",
    "user_assistance_requested": "Planner asked for help",
    "screen_unsafe": "Screen unsafe for unattended use",
    "screen_low_confidence": "Screen below the confidence threshold",
    "planner_low_confidence": "Next action below the confidence threshold",
    "transport_failed": "PiKVM rejected the HID action",
    "stuck_repeated_action": "Stopped instead of repeating an action",
    "stuck_no_screen_change": "Action produced no screen change",
    "runtime_limit": "Hit the runtime limit",
    "step_limit": "Hit the step limit",
    "interrupted": "Interrupted",
    "internal_error": "Local error stopped the controller",
}

# Fallback for records written before stop_code existed. Ordered most-specific first; the first
# matching fragment wins. Categories deliberately reuse the StopCode vocabulary above so the
# chart stays continuous across the transition. New code paths must not rely on this.
_FAILURE_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "verification_failed",
        _STOP_CODE_LABELS["verification_failed"],
        ("could not be visually verified", "was not verified", "verification did not complete"),
    ),
    (
        "completion_unverified",
        _STOP_CODE_LABELS["completion_unverified"],
        ("no manual availability evidence", "not requested"),
    ),
    (
        "approval_denied",
        _STOP_CODE_LABELS["approval_denied"],
        ("interactive approval",),
    ),
    (
        "user_assistance_requested",
        _STOP_CODE_LABELS["user_assistance_requested"],
        ("requested user assistance",),
    ),
    (
        "screen_unsafe",
        _STOP_CODE_LABELS["screen_unsafe"],
        ("unsafe for unattended",),
    ),
    (
        "screen_low_confidence",
        _STOP_CODE_LABELS["screen_low_confidence"],
        ("confidence",),
    ),
    (
        "stuck_repeated_action",
        _STOP_CODE_LABELS["stuck_repeated_action"],
        ("unchanged screen", "stuck"),
    ),
    (
        "stuck_no_screen_change",
        _STOP_CODE_LABELS["stuck_no_screen_change"],
        ("no visible screen change", "did not change"),
    ),
    (
        "runtime_limit",
        _STOP_CODE_LABELS["runtime_limit"],
        ("runtime limit",),
    ),
    (
        "step_limit",
        _STOP_CODE_LABELS["step_limit"],
        ("maximum verified step", "step count"),
    ),
    (
        "legacy_unclassified",
        "Recorded before stop codes existed",
        ("stopped with status failed", "stopped with status paused", "paused before slack"),
    ),
)


def _categorize(record: RunRecord) -> tuple[str, str]:
    """Classify a stop by its recorded code, falling back to text only for old records."""

    if record.stop_code is not None:
        label = _STOP_CODE_LABELS.get(record.stop_code)
        if label is not None:
            return record.stop_code, label
        return record.stop_code, record.stop_code.replace("_", " ").capitalize()

    lowered = (record.error or "").casefold()
    for category, label, fragments in _FAILURE_CATEGORIES:
        if any(fragment in lowered for fragment in fragments):
            return category, label
    return "other", "Stopped for another reason"


def _parse_record(payload: object) -> RunRecord | None:
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
    desired = payload.get("desired_availability")
    observed = payload.get("observed_availability")
    changed = payload.get("changed")
    error = payload.get("error")
    stop_code = payload.get("stop_code")
    return RunRecord(
        timestamp=timestamp,
        kvm=kvm,
        desired=desired if isinstance(desired, str) else None,
        observed=observed if isinstance(observed, str) else "unknown",
        changed=changed if isinstance(changed, bool) else None,
        outcome=outcome,
        stop_code=stop_code if isinstance(stop_code, str) else None,
        error=error if isinstance(error, str) else None,
    )


def _read_lines(path: Path) -> tuple[list[RunRecord], int]:
    records: list[RunRecord] = []
    unreadable = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            record = _parse_record(payload)
            if record is None:
                unreadable += 1
                continue
            records.append(record)
    return records, unreadable


def _summarize(records: list[RunRecord]) -> HistorySummary:
    success = sum(1 for record in records if record.outcome == "success")
    failure = len(records) - success
    changes = sum(1 for record in records if record.outcome == "success" and record.changed is True)

    per_kvm: list[KvmOutcome] = []
    for kvm in sorted({record.kvm for record in records}):
        owned = [record for record in records if record.kvm == kvm]
        kvm_success = sum(1 for record in owned if record.outcome == "success")
        latest = max(owned, key=lambda record: record.timestamp)
        per_kvm.append(
            KvmOutcome(
                kvm=kvm,
                total=len(owned),
                success=kvm_success,
                failure=len(owned) - kvm_success,
                success_rate=kvm_success / len(owned) if owned else 0.0,
                last_outcome=latest.outcome,
                last_observed=latest.observed,
                last_at=latest.timestamp,
            )
        )

    failures = [record for record in records if record.outcome == "failure"]
    counter = Counter(
        record.error.strip() for record in failures if record.error and record.error.strip()
    )
    reasons = [
        FailureReason(reason=reason, count=count)
        for reason, count in counter.most_common(_MAX_REASONS)
    ]

    grouped: dict[str, tuple[str, int]] = {}
    for record in failures:
        category, label = _categorize(record)
        existing = grouped.get(category)
        grouped[category] = (label, (existing[1] if existing else 0) + 1)
    categories = [
        FailureCategory(category=category, label=label, count=count)
        for category, (label, count) in sorted(
            grouped.items(), key=lambda item: (-item[1][1], item[0])
        )
    ]

    timestamps = [record.timestamp for record in records]
    return HistorySummary(
        total=len(records),
        success=success,
        failure=failure,
        success_rate=success / len(records) if records else 0.0,
        changes_applied=changes,
        no_ops=sum(
            1
            for record in records
            if record.outcome == "success"
            and record.desired is not None
            and record.changed is False
        ),
        reads=sum(1 for record in records if record.desired is None),
        per_kvm=per_kvm,
        failure_reasons=reasons,
        failure_categories=categories,
        first_at=min(timestamps) if timestamps else None,
        last_at=max(timestamps) if timestamps else None,
    )


def read_history(
    path: Path,
    *,
    kvm: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> HistoryResponse:
    """Read the sanitized operation log, tolerating partially written lines."""

    if not path.is_file():
        return HistoryResponse(
            records=[],
            summary=_summarize([]),
            log_present=False,
            unreadable_lines=0,
        )
    try:
        records, unreadable = _read_lines(path)
    except OSError:
        return HistoryResponse(
            records=[],
            summary=_summarize([]),
            log_present=False,
            unreadable_lines=0,
        )

    if kvm is not None:
        records = [record for record in records if record.kvm == kvm]
    if since is not None:
        records = [record for record in records if record.timestamp >= since]
    records.sort(key=lambda record: record.timestamp, reverse=True)
    # The summary covers every record in the scope; the list is the most recent page of it.
    return HistoryResponse(
        records=records[: max(0, limit)],
        summary=_summarize(records),
        log_present=True,
        unreadable_lines=unreadable,
    )
