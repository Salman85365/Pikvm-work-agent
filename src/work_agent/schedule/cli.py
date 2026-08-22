from __future__ import annotations

import argparse
import logging
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from work_agent.pikvm import configured_pikvm_profiles
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.launchd import (
    LaunchAgentStatus,
    ScheduleHealth,
    SlackAvailabilityLaunchdManager,
)
from work_agent.schedule.reconcile import desired_availability, last_transition
from work_agent.schedule.runlog import (
    CONNECTIVITY_STOP_CODES,
    STOP_CODE_CATEGORY_LABELS,
    read_failure_streaks,
)
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.cli import default_slack_availability_service, format_availability_batch
from work_agent.slack.logging import JsonlAvailabilityLogger
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.slack.service import SlackAvailabilityService

_LOGGER = logging.getLogger(__name__)

_SCHEDULE_RETRY_COUNT = 2
_SCHEDULE_RETRY_DELAY_SECONDS = 300.0
# A KVM that has failed this many scheduled runs in a row is not going to fix itself.
NOTIFY_FAILURE_STREAK = 3
# Set to 0/false/no/off to keep scheduled runs from posting macOS notifications.
NOTIFICATIONS_ENV = "PIKVM_AGENT_NOTIFICATIONS"
_NOTIFICATION_TITLE = "PiKVM Work Agent"
_NOTIFICATION_TIMEOUT_SECONDS = 5.0

Notifier = Callable[[str, str], None]


class RetryWaitInterrupted(Exception):
    """Raised by a sleeper to abandon the remaining retries without losing recorded results."""


def add_schedule_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    schedule = subparsers.add_parser(
        "schedule",
        help="Manage Mac-local workflow schedules.",
    )
    schedule_commands = schedule.add_subparsers(dest="schedule_workflow", required=True)
    slack = schedule_commands.add_parser(
        "slack-availability",
        help="Manage the Asia/Karachi Slack availability schedule.",
    )
    actions = slack.add_subparsers(dest="schedule_action", required=True)
    for name, help_text in (
        ("install", "Install and load user LaunchAgents."),
        ("status", "Show generated LaunchAgent installation/load state."),
        ("uninstall", "Unload and remove only generated LaunchAgents."),
    ):
        actions.add_parser(name, help=help_text)

    run_now = actions.add_parser(
        "run-now",
        help="Apply a supplied or currently desired state to all KVMs now.",
    )
    run_now.add_argument(
        "--availability",
        type=Availability,
        choices=list(Availability),
        default=None,
        help="Force active/away; default is current Asia/Karachi desired state.",
    )

    reconcile = actions.add_parser(
        "reconcile",
        help="Calculate and apply the current Asia/Karachi desired state.",
    )
    reconcile.add_argument("--if-due", action="store_true", help=argparse.SUPPRESS)
    reconcile.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not post a macOS notification when a scheduled run keeps failing.",
    )


def execute_schedule_command(
    args: argparse.Namespace,
    *,
    manager: SlackAvailabilityLaunchdManager | None = None,
    service: SlackAvailabilityService | None = None,
    state_store: ReconciliationStateStore | None = None,
    profiles: tuple[str, ...] | None = None,
    now: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    retry_count: int = _SCHEDULE_RETRY_COUNT,
    retry_delay_seconds: float = _SCHEDULE_RETRY_DELAY_SECONDS,
    notifier: Notifier | None = None,
    run_log_path: Path | None = None,
) -> tuple[str, int]:
    if args.schedule_workflow != "slack-availability":
        raise AssertionError(f"Unhandled schedule workflow: {args.schedule_workflow}")
    selected_manager = manager or SlackAvailabilityLaunchdManager()

    if args.schedule_action == "install":
        statuses = selected_manager.install()
        output = _format_schedule_status(
            statuses,
            heading="Installed Slack availability schedule",
            notes=selected_manager.last_install_notes,
        )
        return output, 0
    if args.schedule_action == "status":
        statuses = selected_manager.status()
        health = selected_manager.health()
        success = all(item.installed and item.loaded for item in statuses) and health.healthy
        output = _format_schedule_status(
            statuses,
            heading="Slack availability schedule",
            health=health,
        )
        return output, 0 if success else 1
    if args.schedule_action == "uninstall":
        statuses = selected_manager.uninstall()
        success = all(not item.installed and not item.loaded for item in statuses)
        output = _format_schedule_status(
            statuses,
            heading="Removed Slack availability schedule",
        )
        return output, 0 if success else 1

    clock = now or (lambda: datetime.now(UTC))
    forced = (
        args.availability
        if args.schedule_action == "run-now" and args.availability is not None
        else None
    )
    desired = forced if forced is not None else desired_availability(clock())
    selected_profiles = configured_pikvm_profiles() if profiles is None else profiles
    if not selected_profiles:
        raise ScheduleError("Slack availability scheduling requires names in PIKVM_PROFILES.")
    selected_state = state_store or ReconciliationStateStore()
    # Entries for profiles removed from PIKVM_PROFILES are dropped on the next write; the
    # explicit target list is included so a narrowed dashboard run never prunes a real profile.
    known_profiles = tuple(dict.fromkeys((*configured_pikvm_profiles(), *selected_profiles)))
    if_due = args.schedule_action == "reconcile" and args.if_due
    targets = selected_profiles
    if if_due:
        targets = selected_state.profiles_requiring_reconciliation(
            selected_profiles,
            desired,
            verified_after=last_transition(clock())[0],
        )
        if not targets:
            return (
                f"Schedule already reconciled locally: {desired.value}; no PiKVM call required.",
                0,
            )

    batch, attempt_output, desired = _run_with_retries(
        service or default_slack_availability_service(),
        targets=targets,
        desired=desired,
        state_store=selected_state,
        sleeper=sleeper,
        retry_count=retry_count,
        retry_delay_seconds=retry_delay_seconds,
        # A forced run-now keeps its value; scheduled runs re-read the clock after each wait
        # so a retry that crosses 18:00 or 02:00 does not apply the state that just expired.
        desired_provider=None if forced is not None else (lambda: desired_availability(clock())),
        if_due=if_due,
        all_profiles=selected_profiles,
        boundary_provider=lambda: last_transition(clock())[0],
        known_profiles=known_profiles,
    )
    heading = f"Desired Slack availability: {desired.value} (Asia/Karachi)"
    output = heading + "\n" + attempt_output
    if if_due and not getattr(args, "no_notify", False) and _notifications_enabled():
        _notify_persistent_failures(
            batch,
            profiles=selected_profiles,
            run_log_path=run_log_path or JsonlAvailabilityLogger().path,
            notifier=notifier or _display_macos_notification,
        )
    return output, 0 if batch.success else 1


def _notifications_enabled() -> bool:
    return os.getenv(NOTIFICATIONS_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def failure_notification_text(
    batch: AvailabilityBatchResult,
    *,
    profiles: tuple[str, ...],
    run_log_path: Path,
) -> str | None:
    """Describe KVMs that could not be used or keep failing; None when nothing warrants a ping.

    Only the KVM name and a reason category appear: never screen content, model output, or an
    exception message.
    """

    reasons: dict[str, str] = {}
    for result in batch.results:
        if result.success or result.stop_code not in CONNECTIVITY_STOP_CODES:
            continue
        reasons[result.kvm] = STOP_CODE_CATEGORY_LABELS.get(result.stop_code, result.stop_code)

    streaks = read_failure_streaks(run_log_path)
    for kvm in profiles:
        streak = streaks.get(kvm)
        if streak is None or streak.count < NOTIFY_FAILURE_STREAK:
            continue
        streak_text = f"failed {streak.count} scheduled runs in a row"
        reasons[kvm] = f"{reasons[kvm]} ({streak_text})" if kvm in reasons else streak_text

    if not reasons:
        return None
    return "; ".join(f"{kvm}: {reason}" for kvm, reason in reasons.items())


def _notify_persistent_failures(
    batch: AvailabilityBatchResult,
    *,
    profiles: tuple[str, ...],
    run_log_path: Path,
    notifier: Notifier,
) -> None:
    text = failure_notification_text(batch, profiles=profiles, run_log_path=run_log_path)
    if text is None:
        return
    try:
        notifier(_NOTIFICATION_TITLE, text)
    except Exception as exc:  # A notification is best-effort; it must never fail the run.
        _LOGGER.warning("macOS notification could not be posted: %s", type(exc).__name__)


def _display_macos_notification(title: str, text: str) -> None:
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {quoted(text)} with title {quoted(title)}"
    subprocess.run(
        ("osascript", "-e", script),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_NOTIFICATION_TIMEOUT_SECONDS,
    )


def _run_with_retries(
    service: SlackAvailabilityService,
    *,
    targets: tuple[str, ...],
    desired: Availability,
    state_store: ReconciliationStateStore,
    sleeper: Callable[[float], None],
    retry_count: int,
    retry_delay_seconds: float,
    desired_provider: Callable[[], Availability] | None = None,
    if_due: bool = False,
    all_profiles: tuple[str, ...] | None = None,
    boundary_provider: Callable[[], datetime] | None = None,
    known_profiles: tuple[str, ...] | None = None,
) -> tuple[AvailabilityBatchResult, str, Availability]:
    if retry_count < 0:
        raise ValueError("Scheduler retry count cannot be negative.")
    if retry_delay_seconds < 0:
        raise ValueError("Scheduler retry delay cannot be negative.")

    latest: dict[str, AvailabilityResult] = {}
    attempts: list[tuple[int, AvailabilityBatchResult]] = []
    notes: dict[int, str] = {}
    remaining = targets
    for attempt in range(retry_count + 1):
        if attempt > 0:
            try:
                sleeper(retry_delay_seconds)
            except RetryWaitInterrupted:
                notes[attempt] = "Cancelled during the retry wait; remaining retries skipped."
                break
            if desired_provider is not None:
                current_desired = desired_provider()
                if current_desired is not desired:
                    desired = current_desired
                    targets = all_profiles or targets
                    remaining = targets
                    latest.clear()
                    notes[attempt] = (
                        f"Desired state changed to {desired.value} during the wait; "
                        "re-evaluating every profile."
                    )
            if if_due:
                remaining = state_store.profiles_requiring_reconciliation(
                    remaining,
                    desired,
                    verified_after=(boundary_provider() if boundary_provider is not None else None),
                )
                if not remaining:
                    notes[attempt] = notes.get(attempt, "") + (
                        " Already reconciled locally; nothing left to retry."
                    )
                    break
        current = service.run(remaining, desired)
        attempts.append((attempt, current))
        state_store.record_successes(current, known_profiles=known_profiles)
        latest.update((result.kvm, result) for result in current.results)
        remaining = tuple(kvm for kvm in remaining if not latest[kvm].success)
        if not remaining:
            break

    final = AvailabilityBatchResult(results=tuple(latest[kvm] for kvm in targets if kvm in latest))
    if len(attempts) == 1 and not notes:
        return final, format_availability_batch(final), desired

    lines: list[str] = []
    for attempt, result in attempts:
        if attempt == 0:
            lines.append("Initial attempt:")
        else:
            lines.append(
                f"Retry {attempt} of {retry_count} after {_format_delay(retry_delay_seconds)}:"
            )
        if attempt in notes:
            lines.append(notes[attempt].strip())
        lines.append(format_availability_batch(result))
    for attempt in sorted(notes):
        if attempt > len(attempts) - 1:
            lines.append(
                f"Retry {attempt} of {retry_count} after {_format_delay(retry_delay_seconds)}:"
            )
            lines.append(notes[attempt].strip())
    lines.append("Final result:")
    lines.append(format_availability_batch(final))
    return final, "\n".join(lines), desired


def _format_delay(seconds: float) -> str:
    if seconds > 0 and seconds % 60 == 0:
        minutes = int(seconds / 60)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds:g} {unit}"


def _format_schedule_status(
    statuses: tuple[LaunchAgentStatus, ...],
    *,
    heading: str,
    health: ScheduleHealth | None = None,
    notes: tuple[str, ...] = (),
) -> str:
    lines = [heading, "Timezone: Asia/Karachi"]
    for item in statuses:
        lines.append(
            f"{item.label}: installed={'yes' if item.installed else 'no'}, "
            f"loaded={'yes' if item.loaded else 'no'}, path={item.path}"
        )
    if health is not None:
        if health.interpreter is not None:
            lines.append(
                f"Interpreter: {health.interpreter} "
                f"(runnable={'yes' if health.interpreter_can_run else 'no'})"
            )
        lines.extend(f"! {problem}" for problem in health.problems)
    lines.extend(notes)
    return "\n".join(lines)
