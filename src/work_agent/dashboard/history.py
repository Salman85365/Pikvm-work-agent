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

# Ordered most-specific first; the first matching fragment wins. Fragments must match the
# sanitized strings that work_agent.slack.agent_operator actually writes to the log.
_FAILURE_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "verification",
        "Could not verify the result",
        (
            "could not be visually verified",
            "was not verified",
            "no manual availability evidence",
            "verification did not complete",
        ),
    ),
    (
        "approval",
        "Needed interactive approval",
        ("interactive approval",),
    ),
    (
        "unsafe_screen",
        "Screen unsafe for unattended use",
        ("unsafe for unattended", "requested user assistance"),
    ),
    (
        "low_confidence",
        "Below the confidence threshold",
        ("confidence",),
    ),
    (
        "stuck",
        "Stopped instead of repeating an action",
        ("unchanged screen", "no visible screen change", "stuck", "did not change"),
    ),
    (
        "limit",
        "Hit a step or runtime limit",
        ("runtime limit", "maximum verified step", "step count"),
    ),
    (
        "wrong_state",
        "Ended in the wrong visible state",
        ("not requested",),
    ),
    (
        "controller_failed",
        "Controller failed without a specific reason",
        ("stopped with status failed",),
    ),
    (
        "controller_paused",
        "Controller paused before finishing",
        ("stopped with status paused", "paused before slack availability"),
    ),
)


def _categorize(reason: str) -> tuple[str, str]:
    lowered = reason.casefold()
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
    return RunRecord(
        timestamp=timestamp,
        kvm=kvm,
        desired=desired if isinstance(desired, str) else None,
        observed=observed if isinstance(observed, str) else "unknown",
        changed=changed if isinstance(changed, bool) else None,
        outcome=outcome,
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

    stops = [
        record.error.strip()
        for record in records
        if record.outcome == "failure" and record.error and record.error.strip()
    ]
    counter = Counter(stops)
    reasons = [
        FailureReason(reason=reason, count=count)
        for reason, count in counter.most_common(_MAX_REASONS)
    ]

    grouped: dict[str, tuple[str, int]] = {}
    for reason in stops:
        category, label = _categorize(reason)
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
