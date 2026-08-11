from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from datetime import UTC, datetime

from work_agent.pikvm import configured_pikvm_profiles
from work_agent.schedule.errors import ScheduleError
from work_agent.schedule.launchd import LaunchAgentStatus, SlackAvailabilityLaunchdManager
from work_agent.schedule.reconcile import desired_availability
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.cli import default_slack_availability_service, format_availability_batch
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.slack.service import SlackAvailabilityService

_SCHEDULE_RETRY_COUNT = 2
_SCHEDULE_RETRY_DELAY_SECONDS = 300.0


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


def execute_schedule_command(
    args: argparse.Namespace,
    *,
    manager: SlackAvailabilityLaunchdManager | None = None,
    service: SlackAvailabilityService | None = None,
    state_store: ReconciliationStateStore | None = None,
    now: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    retry_count: int = _SCHEDULE_RETRY_COUNT,
    retry_delay_seconds: float = _SCHEDULE_RETRY_DELAY_SECONDS,
) -> tuple[str, int]:
    if args.schedule_workflow != "slack-availability":
        raise AssertionError(f"Unhandled schedule workflow: {args.schedule_workflow}")
    selected_manager = manager or SlackAvailabilityLaunchdManager()

    if args.schedule_action == "install":
        statuses = selected_manager.install()
        output = _format_schedule_status(
            statuses,
            heading="Installed Slack availability schedule",
        )
        return output, 0
    if args.schedule_action == "status":
        statuses = selected_manager.status()
        success = all(item.installed and item.loaded for item in statuses)
        output = _format_schedule_status(statuses, heading="Slack availability schedule")
        return output, 0 if success else 1
    if args.schedule_action == "uninstall":
        statuses = selected_manager.uninstall()
        success = all(not item.installed and not item.loaded for item in statuses)
        output = _format_schedule_status(
            statuses,
            heading="Removed Slack availability schedule",
        )
        return output, 0 if success else 1

    moment = (now or (lambda: datetime.now(UTC)))()
    desired = (
        args.availability
        if args.schedule_action == "run-now" and args.availability is not None
        else desired_availability(moment)
    )
    profiles = configured_pikvm_profiles()
    if not profiles:
        raise ScheduleError("Slack availability scheduling requires names in PIKVM_PROFILES.")
    selected_state = state_store or ReconciliationStateStore()
    targets = profiles
    if args.schedule_action == "reconcile" and args.if_due:
        targets = selected_state.profiles_requiring_reconciliation(profiles, desired)
        if not targets:
            return (
                f"Schedule already reconciled locally: {desired.value}; no PiKVM call required.",
                0,
            )

    batch, attempt_output = _run_with_retries(
        service or default_slack_availability_service(),
        targets=targets,
        desired=desired,
        state_store=selected_state,
        sleeper=sleeper,
        retry_count=retry_count,
        retry_delay_seconds=retry_delay_seconds,
    )
    heading = f"Desired Slack availability: {desired.value} (Asia/Karachi)"
    output = heading + "\n" + attempt_output
    return output, 0 if batch.success else 1


def _run_with_retries(
    service: SlackAvailabilityService,
    *,
    targets: tuple[str, ...],
    desired: Availability,
    state_store: ReconciliationStateStore,
    sleeper: Callable[[float], None],
    retry_count: int,
    retry_delay_seconds: float,
) -> tuple[AvailabilityBatchResult, str]:
    if retry_count < 0:
        raise ValueError("Scheduler retry count cannot be negative.")
    if retry_delay_seconds < 0:
        raise ValueError("Scheduler retry delay cannot be negative.")

    latest: dict[str, AvailabilityResult] = {}
    attempts: list[tuple[int, AvailabilityBatchResult]] = []
    remaining = targets
    for attempt in range(retry_count + 1):
        if attempt > 0:
            sleeper(retry_delay_seconds)
        current = service.run(remaining, desired)
        attempts.append((attempt, current))
        state_store.record_successes(current)
        latest.update((result.kvm, result) for result in current.results)
        remaining = tuple(kvm for kvm in remaining if not latest[kvm].success)
        if not remaining:
            break

    final = AvailabilityBatchResult(results=tuple(latest[kvm] for kvm in targets))
    if len(attempts) == 1:
        return final, format_availability_batch(final)

    lines: list[str] = []
    for attempt, result in attempts:
        if attempt == 0:
            lines.append("Initial attempt:")
        else:
            lines.append(
                f"Retry {attempt} of {retry_count} after {_format_delay(retry_delay_seconds)}:"
            )
        lines.append(format_availability_batch(result))
    lines.append("Final result:")
    lines.append(format_availability_batch(final))
    return final, "\n".join(lines)


def _format_delay(seconds: float) -> str:
    if seconds > 0 and seconds % 60 == 0:
        minutes = int(seconds / 60)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds:g} {unit}"


def _format_schedule_status(statuses: tuple[LaunchAgentStatus, ...], *, heading: str) -> str:
    lines = [heading, "Timezone: Asia/Karachi"]
    for item in statuses:
        lines.append(
            f"{item.label}: installed={'yes' if item.installed else 'no'}, "
            f"loaded={'yes' if item.loaded else 'no'}, path={item.path}"
        )
    return "\n".join(lines)
