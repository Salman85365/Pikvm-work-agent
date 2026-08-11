from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.dashboard.jobs import Emit, JobOutcome
from work_agent.dashboard.models import (
    AgentState,
    JobResultLine,
    KvmProfile,
    ScheduleAction,
    ScheduleSnapshot,
)
from work_agent.pikvm import (
    PiKVMConfigurationError,
    PiKVMError,
    PiKVMSettings,
    PiKVMTotpError,
    TotpProviderKind,
    build_totp_provider,
    configured_pikvm_profiles,
)
from work_agent.schedule.cli import execute_schedule_command
from work_agent.schedule.launchd import LaunchAgentStatus, SlackAvailabilityLaunchdManager
from work_agent.schedule.reconcile import KARACHI_TIMEZONE, desired_availability, next_transition
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.cli import (
    default_slack_availability_service,
    default_slack_triage_service,
)
from work_agent.slack.models import Availability, AvailabilityResult

_LABEL_PREFIX = "com.pikvm-work-agent.slack-availability."


def _refuse_terminal_prompt(_: str) -> str:
    raise PiKVMTotpError(
        "This PiKVM profile is configured for interactive TOTP entry, which a background "
        "dashboard cannot answer. Enroll its seed with "
        "`pikvm-agent --profile NAME auth keychain import-qr`."
    )


def _uses_interactive_totp(settings: PiKVMSettings) -> bool:
    if not settings.totp_required:
        return False
    return (
        settings.totp_provider is TotpProviderKind.INTERACTIVE or settings.totp_interactive_fallback
    )


def profile_snapshots() -> list[KvmProfile]:
    """Describe every configured profile without exposing passwords or seeds."""

    profiles: list[KvmProfile] = []
    for name in configured_pikvm_profiles():
        try:
            settings = PiKVMSettings.from_env(name)
        except (PiKVMConfigurationError, OSError, ValueError) as exc:
            profiles.append(
                KvmProfile(
                    name=name,
                    endpoint=None,
                    totp_required=None,
                    verify_ssl=None,
                    interactive_totp=False,
                    configured=False,
                    problem=str(exc),
                )
            )
            continue
        profiles.append(
            KvmProfile(
                name=name,
                endpoint=settings.base_url,
                totp_required=settings.totp_required,
                verify_ssl=settings.verify_ssl,
                interactive_totp=_uses_interactive_totp(settings),
                configured=True,
                problem=None,
            )
        )
    return profiles


def default_profile(profiles: list[KvmProfile]) -> str | None:
    selected = os.getenv("PIKVM_PROFILE")
    names = [profile.name for profile in profiles]
    if selected is not None:
        normalized = selected.strip().lower().replace("-", "_")
        for name in names:
            if name.replace("-", "_") == normalized:
                return name
    return names[0] if len(names) == 1 else None


def capture_screenshot(profile: str) -> tuple[bytes, int, int]:
    settings = PiKVMSettings.from_env(profile)
    if _uses_interactive_totp(settings):
        _refuse_terminal_prompt("")
    provider = build_totp_provider(settings, interactive_prompt=_refuse_terminal_prompt)
    with PiKVMSession(settings, totp_provider=provider) as session:
        screenshot = session.get_screenshot()
    return screenshot.content, screenshot.size.width, screenshot.size.height


def ensure_runnable(targets: tuple[str, ...]) -> None:
    """Fail before any job starts if a target cannot authenticate unattended."""

    for name in targets:
        settings = PiKVMSettings.from_env(name)
        if _uses_interactive_totp(settings):
            raise PiKVMTotpError(
                f"Profile {name} is configured for interactive TOTP entry, which the dashboard "
                "cannot answer. Enroll its seed in Keychain first."
            )


def _availability_text(result: AvailabilityResult) -> str:
    if not result.success:
        return result.error or "availability unavailable"
    observed = result.observed.value if result.observed is not None else "unknown"
    if result.desired is None:
        return f"currently {observed}"
    if result.changed is True:
        return f"changed to {observed}"
    if result.changed is False:
        return f"already {observed}; no click sent"
    return f"verified {observed}"


def availability_work(
    targets: tuple[str, ...],
    desired: Availability | None,
) -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        service = default_slack_availability_service(trace_output=emit)
        batch = service.run(targets, desired)
        results = tuple(
            JobResultLine(kvm=item.kvm, ok=item.success, text=_availability_text(item))
            for item in batch.results
        )
        for item in batch.results:
            if item.log_error is not None:
                emit(f"{item.kvm}  ! {item.log_error}")
        verb = "Read" if desired is None else f"Applied {desired.value} to"
        succeeded = sum(1 for item in batch.results if item.success)
        summary = f"{verb} {succeeded} of {len(batch.results)} KVM(s)."
        return JobOutcome(ok=batch.success, summary=summary, results=results)

    return run


def triage_work(targets: tuple[str, ...]) -> Callable[[Emit], JobOutcome]:
    """Read visible Slack unread state. Conversation names reach the browser but never disk."""

    def run(emit: Emit) -> JobOutcome:
        service = default_slack_triage_service(trace_output=emit)
        batch = service.run(targets)
        results = tuple(
            JobResultLine(
                kvm=report.kvm,
                ok=report.success,
                text=(
                    f"{len(report.needs_attention)} need attention, {len(report.informational)} FYI"
                    if report.success
                    else (report.error or "triage unavailable")
                ),
            )
            for report in batch.reports
        )
        for report in batch.reports:
            if report.log_error is not None:
                emit(f"{report.kvm}  ! {report.log_error}")
        payload: dict[str, object] = {
            "reports": [
                {
                    "kvm": report.kvm,
                    "success": report.success,
                    "error": report.error,
                    "sidebar_truncated": report.sidebar_truncated,
                    "confidence": report.confidence,
                    "total_unread_badge": report.total_unread_badge,
                    "items": [
                        {
                            "name": item.name,
                            "kind": item.kind.value,
                            "unread_count": item.unread_count,
                            "has_mention": item.has_mention,
                            "attention": item.attention.value,
                        }
                        for item in report.items
                    ],
                }
                for report in batch.reports
            ]
        }
        attention = sum(len(report.needs_attention) for report in batch.reports)
        return JobOutcome(
            ok=batch.success,
            summary=f"{attention} conversation(s) need attention across {len(targets)} KVM(s).",
            results=results,
            payload=payload,
        )

    return run


def schedule_work(
    action: ScheduleAction,
    availability: Availability | None,
    *,
    manager: SlackAvailabilityLaunchdManager | None = None,
    state_store: ReconciliationStateStore | None = None,
) -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        selected_manager = manager or SlackAvailabilityLaunchdManager()
        selected_store = state_store or ReconciliationStateStore()

        if action in {ScheduleAction.INSTALL, ScheduleAction.UNINSTALL}:
            args = argparse.Namespace(
                schedule_workflow="slack-availability",
                schedule_action=action.value,
            )
            output, exit_code = execute_schedule_command(args, manager=selected_manager)
            for line in output.splitlines():
                emit(line)
            verb = "installed" if action is ScheduleAction.INSTALL else "removed"
            return JobOutcome(
                ok=exit_code == 0,
                summary=f"LaunchAgents {verb}."
                if exit_code == 0
                else f"LaunchAgents could not be {verb}.",
            )

        args = argparse.Namespace(
            schedule_workflow="slack-availability",
            schedule_action=action.value,
            **(
                {"availability": availability}
                if action is ScheduleAction.RUN_NOW
                else {"if_due": False}
            ),
        )

        def sleeper(seconds: float) -> None:
            emit(f"Waiting {seconds / 60:.0f} minute(s) before retrying only the failed profiles…")
            time.sleep(seconds)

        output, exit_code = execute_schedule_command(
            args,
            manager=selected_manager,
            service=default_slack_availability_service(trace_output=emit),
            state_store=selected_store,
            sleeper=sleeper,
        )
        lines = output.splitlines()
        for line in lines:
            emit(line)
        return JobOutcome(
            ok=exit_code == 0,
            summary=lines[0] if lines else f"Schedule {action.value} finished.",
        )

    return run


def _agent_state(status: LaunchAgentStatus) -> AgentState:
    return AgentState(
        label=status.label,
        short_label=status.label.removeprefix(_LABEL_PREFIX),
        path=str(status.path),
        installed=status.installed,
        loaded=status.loaded,
    )


def schedule_snapshot(
    *,
    manager: SlackAvailabilityLaunchdManager | None = None,
    state_store: ReconciliationStateStore | None = None,
    now: datetime | None = None,
) -> ScheduleSnapshot:
    selected_manager = manager or SlackAvailabilityLaunchdManager()
    selected_store = state_store or ReconciliationStateStore()
    moment = now or datetime.now(UTC)

    try:
        statuses = selected_manager.status()
    except PiKVMError:  # pragma: no cover - launchctl surfaces ScheduleError instead
        statuses = ()
    health = selected_manager.health()
    applied, applied_updated_at = selected_store.applied_state()
    boundary, boundary_state = next_transition(moment)
    agents = [_agent_state(status) for status in statuses]
    installed = bool(agents) and all(agent.installed for agent in agents)
    problems = list(health.problems)
    if not installed:
        problems.append(
            "The Slack availability LaunchAgents are not installed, so nothing runs on a schedule."
        )
    elif not all(agent.loaded for agent in agents):
        problems.append("At least one installed LaunchAgent is not loaded by launchd.")

    return ScheduleSnapshot(
        agents=agents,
        interpreter=str(health.interpreter) if health.interpreter is not None else None,
        interpreter_can_run=health.interpreter_can_run,
        timezone_name=health.timezone_name,
        timezone_ok=health.timezone_ok,
        problems=problems,
        healthy=not problems,
        installed=installed,
        desired_now=desired_availability(moment),
        next_transition_at=boundary.astimezone(KARACHI_TIMEZONE),
        next_transition_to=boundary_state,
        applied=applied,
        applied_updated_at=applied_updated_at,
    )
