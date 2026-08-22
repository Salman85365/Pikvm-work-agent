from __future__ import annotations

import argparse
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from work_agent.agenda.cli import default_agenda_service
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.dashboard.fleet import ReachabilityCache, kvm_statuses
from work_agent.dashboard.jobs import Emit, JobOutcome
from work_agent.dashboard.models import (
    AgentState,
    JobResultLine,
    KvmProfile,
    LastRun,
    MeetingActionItemCard,
    MeetingRecorderCard,
    MeetingSessionCard,
    MeetingSessionDetailCard,
    MeetingsSnapshot,
    MeetingTranscriptLine,
    ProfileCard,
    ScheduleAction,
    ScheduleSnapshot,
)
from work_agent.meeting.cli import default_meeting_service
from work_agent.meeting.config import MeetingProvider, MeetingSettings
from work_agent.meeting.errors import MeetingError
from work_agent.meeting.library import MeetingLibrary, MeetingSessionDetail, MeetingSessionSummary
from work_agent.meeting.service import MeetingService, MeetingStatusResult
from work_agent.meeting.state import RecorderPhase
from work_agent.pikvm import (
    PiKVMConfigurationError,
    PiKVMError,
    PiKVMSettings,
    PiKVMTotpError,
    TotpProviderKind,
    build_totp_provider,
    configured_pikvm_profiles,
)
from work_agent.pikvm.profile_service import ProfileService, ProfileView
from work_agent.schedule.cli import RetryWaitInterrupted, execute_schedule_command
from work_agent.schedule.launchd import LaunchAgentStatus, SlackAvailabilityLaunchdManager
from work_agent.schedule.reconcile import (
    KARACHI_TIMEZONE,
    desired_availability,
    last_transition,
    next_transition,
)
from work_agent.schedule.runlog import STOP_CODE_CATEGORY_LABELS, read_failure_streaks
from work_agent.schedule.state import ReconciliationStateStore
from work_agent.slack.cli import (
    default_slack_availability_service,
    default_slack_triage_service,
)
from work_agent.slack.models import Availability, AvailabilityResult

_LABEL_PREFIX = "com.pikvm-work-agent.slack-availability."

# A person is watching a dashboard job, so it retries once after a short wait instead of the
# scheduler's two five-minute waits, and the wait can be cancelled from the page.
DASHBOARD_RETRY_COUNT = 1
DASHBOARD_RETRY_DELAY_SECONDS = 30.0
# Outcome-based schedule health: this many consecutive failed runs on one KVM, or a desired
# state left unapplied this long after the boundary, means the schedule is not working.
UNHEALTHY_FAILURE_STREAK = 3
UNAPPLIED_GRACE = timedelta(hours=2)


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


def profile_service() -> ProfileService:
    """The same service `pikvm-agent profiles ...` uses; the dashboard adds no authority."""

    return ProfileService()


def profile_card(view: ProfileView) -> ProfileCard:
    return ProfileCard(
        name=view.name,
        host=view.host,
        url=view.url,
        username=view.username,
        source=view.source,
        enabled=view.enabled,
        totp_required=view.totp_required,
        totp_enrolled=view.totp_enrolled,
        verify_ssl=view.verify_ssl,
        removable=view.removable,
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
    *,
    preflight_results: tuple[JobResultLine, ...] = (),
) -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        if not targets and preflight_results:
            verb = "Read" if desired is None else f"Applied {desired.value} to"
            return JobOutcome(
                ok=False,
                summary=f"{verb} 0 of {len(preflight_results)} KVM(s).",
                results=preflight_results,
            )

        service = default_slack_availability_service(trace_output=emit)
        batch = service.run(targets, desired)
        completed = tuple(
            JobResultLine(kvm=item.kvm, ok=item.success, text=_availability_text(item))
            for item in batch.results
        )
        results = completed + preflight_results
        for item in batch.results:
            if item.log_error is not None:
                emit(f"{item.kvm}  ! {item.log_error}")
        verb = "Read" if desired is None else f"Applied {desired.value} to"
        succeeded = sum(1 for item in results if item.ok)
        summary = f"{verb} {succeeded} of {len(results)} KVM(s)."
        return JobOutcome(
            ok=bool(results) and all(item.ok for item in results),
            summary=summary,
            results=results,
        )

    return run


def triage_work(
    targets: tuple[str, ...],
    *,
    preflight_results: tuple[JobResultLine, ...] = (),
) -> Callable[[Emit], JobOutcome]:
    """Read visible Slack unread state. Conversation names reach the browser but never disk."""

    def run(emit: Emit) -> JobOutcome:
        if not targets and preflight_results:
            return JobOutcome(
                ok=False,
                summary=(
                    f"0 conversation(s) need attention across {len(preflight_results)} KVM(s)."
                ),
                results=preflight_results,
                payload={"reports": []},
            )

        service = default_slack_triage_service(trace_output=emit)
        batch = service.run(targets)
        completed = tuple(
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
        results = completed + preflight_results
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
                    "sidebar_obstructed": report.sidebar_obstructed,
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
            ok=bool(results) and all(item.ok for item in results),
            summary=(f"{attention} conversation(s) need attention across {len(results)} KVM(s)."),
            results=results,
            payload=payload,
        )

    return run


def agenda_work(
    targets: tuple[str, ...],
    *,
    preflight_results: tuple[JobResultLine, ...] = (),
) -> Callable[[Emit], JobOutcome]:
    """Read today's visible calendar. Meeting details reach the browser but never disk."""

    def run(emit: Emit) -> JobOutcome:
        if not targets and preflight_results:
            return JobOutcome(
                ok=False,
                summary=_agenda_summary(0, preflight_results),
                results=preflight_results,
                payload={
                    "reports": [
                        {"kvm": result.kvm, "success": False, "error": result.text}
                        for result in preflight_results
                    ]
                },
            )

        service = default_agenda_service(trace_output=emit)
        batch = service.run(targets)
        completed = tuple(
            JobResultLine(
                kvm=report.kvm,
                ok=report.success,
                text=(
                    f"{len(report.upcoming)} still ahead, {len(report.earlier)} earlier"
                    if report.success
                    else (report.error or "calendar unavailable")
                ),
            )
            for report in batch.reports
        )
        results = completed + preflight_results
        for report in batch.reports:
            if report.log_error is not None:
                emit(f"{report.kvm}  ! {report.log_error}")
        reports: list[dict[str, object]] = [
            {
                "kvm": report.kvm,
                "success": report.success,
                "error": report.error,
                "surface": report.surface.value,
                "date_text": report.date_text,
                "current_time_text": report.current_time_text,
                "clock_read": report.clock_read,
                "later_truncated": report.later_truncated,
                "earlier_truncated": report.earlier_truncated,
                "obstructed": report.obstructed,
                "scrolled": report.scrolled,
                "confidence": report.confidence,
                "items": [
                    {
                        "title": item.title,
                        "start_text": item.start_text,
                        "end_text": item.end_text,
                        "status": item.status.value,
                        "all_day": item.all_day,
                        "location": item.location,
                        "organizer": item.organizer,
                        "is_online": item.is_online,
                        "declined": item.declined,
                    }
                    for item in report.items
                ],
            }
            for report in batch.reports
        ]
        reports.extend(
            {"kvm": result.kvm, "success": False, "error": result.text}
            for result in preflight_results
        )
        payload: dict[str, object] = {"reports": reports}
        ahead = sum(len(report.upcoming) for report in batch.reports)
        return JobOutcome(
            ok=bool(results) and all(item.ok for item in results),
            summary=_agenda_summary(ahead, results),
            results=results,
            payload=payload,
        )

    return run


def _agenda_summary(ahead: int, results: tuple[JobResultLine, ...]) -> str:
    """Describe only calendar reads that actually succeeded."""

    total = len(results)
    succeeded = sum(result.ok for result in results)
    stopped = total - succeeded
    if succeeded == 0:
        environment = "environment" if total == 1 else "environments"
        return f"Calendar read stopped on {total} {environment}."

    meeting = "meeting" if ahead == 1 else "meetings"
    if stopped:
        return (
            f"{ahead} {meeting} still ahead from {succeeded} of {total} environments; "
            f"{stopped} stopped."
        )
    environment = "environment" if total == 1 else "environments"
    return f"{ahead} {meeting} still ahead across {total} {environment}."


def schedule_work(
    action: ScheduleAction,
    availability: Availability | None,
    *,
    manager: SlackAvailabilityLaunchdManager | None = None,
    state_store: ReconciliationStateStore | None = None,
    targets: tuple[str, ...] | None = None,
    preflight_results: tuple[JobResultLine, ...] = (),
    cancel: threading.Event | None = None,
) -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        if targets == () and preflight_results:
            return JobOutcome(
                ok=False,
                summary=f"No eligible environment; skipped {len(preflight_results)} KVM(s).",
                results=preflight_results,
            )

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

        stop = cancel or threading.Event()

        def sleeper(seconds: float) -> None:
            emit(f"Waiting {seconds:g} s before retrying only the failed profiles (Cancel skips)…")
            if stop.wait(seconds):
                emit("Cancelled; skipping the retry.")
                raise RetryWaitInterrupted

        output, exit_code = execute_schedule_command(
            args,
            manager=selected_manager,
            service=default_slack_availability_service(trace_output=emit),
            state_store=selected_store,
            profiles=targets,
            sleeper=sleeper,
            retry_count=DASHBOARD_RETRY_COUNT,
            retry_delay_seconds=DASHBOARD_RETRY_DELAY_SECONDS,
        )
        lines = output.splitlines()
        for line in lines:
            emit(line)
        results = _schedule_result_lines(lines, targets, exit_code == 0) + preflight_results
        cancelled = stop.is_set()
        return JobOutcome(
            ok=exit_code == 0 and all(item.ok for item in preflight_results),
            summary=(
                "Cancelled during the retry wait; earlier results are recorded."
                if cancelled
                else (lines[0] if lines else f"Schedule {action.value} finished.")
            ),
            results=results,
            cancelled=cancelled,
        )

    return run


def _schedule_result_lines(
    lines: list[str],
    targets: tuple[str, ...] | None,
    command_succeeded: bool,
) -> tuple[JobResultLine, ...]:
    if targets is None:
        return ()
    results: list[JobResultLine] = []
    for target in targets:
        prefix = f"{target}  "
        recorded = next(
            (
                line
                for line in reversed(lines)
                if line.startswith(prefix)
                and line.removeprefix(prefix).lstrip().startswith(("✓", "✗"))
            ),
            None,
        )
        if recorded is None:
            results.append(
                JobResultLine(
                    kvm=target,
                    ok=command_succeeded,
                    text=(
                        "synchronized with the schedule"
                        if command_succeeded
                        else "schedule synchronization stopped"
                    ),
                )
            )
            continue
        remainder = recorded.removeprefix(prefix).strip()
        ok = remainder.startswith("✓")
        text = remainder.removeprefix("✓").removeprefix("✗").strip()
        results.append(JobResultLine(kvm=target, ok=ok, text=text))
    return tuple(results)


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
    profiles: list[KvmProfile] | None = None,
    log_path: Path | None = None,
    reachability: ReachabilityCache | None = None,
    lock_directory: Path | None = None,
) -> ScheduleSnapshot:
    """Describe scheduler health from what actually happened, not only what is installed.

    ``profiles`` enables per-KVM status; ``reachability`` enables the unauthenticated endpoint
    probe (never on by default so tests and CLI callers cannot touch the network by accident).
    """

    selected_manager = manager or SlackAvailabilityLaunchdManager()
    selected_store = state_store or ReconciliationStateStore()
    moment = now or datetime.now(UTC)

    try:
        statuses = selected_manager.status()
    except PiKVMError:  # pragma: no cover - launchctl surfaces ScheduleError instead
        statuses = ()
    health = selected_manager.health()
    applied, applied_updated_at = selected_store.applied_state()
    profile_states = selected_store.profile_states()
    boundary, boundary_state = next_transition(moment)
    previous_boundary, _ = last_transition(moment)
    desired_now = desired_availability(moment)
    agents = [_agent_state(status) for status in statuses]
    installed = bool(agents) and all(agent.installed for agent in agents)
    problems = list(health.problems)
    if not installed:
        problems.append(
            "The Slack availability LaunchAgents are not installed, so nothing runs on a schedule."
        )
    elif not all(agent.loaded for agent in agents):
        problems.append("At least one installed LaunchAgent is not loaded by launchd.")

    streaks = read_failure_streaks(log_path) if log_path is not None else {}
    fleet = kvm_statuses(
        profiles or [],
        streaks=streaks,
        reachability=reachability,
        lock_directory=lock_directory,
    )
    for status in fleet:
        if status.consecutive_failures >= UNHEALTHY_FAILURE_STREAK:
            reason = STOP_CODE_CATEGORY_LABELS.get(
                status.last_stop_code or "", (status.last_stop_code or "unknown reason")
            )
            since = status.unreachable_since
            suffix = f"; unreachable since {_karachi(since)}" if since is not None else ""
            problems.append(
                f"{status.name} has failed its last {status.consecutive_failures} runs "
                f"({reason}{suffix})."
            )

    # Desired ≠ applied is normal for a while after a boundary (the reconciler retries for up
    # to ten minutes); persisting well past it means the schedule is not taking effect.
    overdue = moment - previous_boundary > UNAPPLIED_GRACE
    checked_names = (
        [profile.name for profile in profiles if profile.configured]
        if profiles is not None
        else list(applied)
    )
    for name in checked_names:
        recorded = profile_states.get(name)
        matches = (
            recorded is not None
            and recorded.availability == desired_now.value
            and recorded.verified_at is not None
            and recorded.verified_at > previous_boundary
        )
        if overdue and not matches:
            hours = (moment - previous_boundary).total_seconds() / 3600
            problems.append(
                f"{name} has not been verified {desired_now.value} since the "
                f"{_karachi(previous_boundary)} boundary ({hours:.0f} h ago)."
            )

    latest = max(
        (streak for streak in streaks.values() if streak.latest_at is not None),
        key=lambda streak: streak.latest_at or moment,
        default=None,
    )
    last_run = (
        LastRun(
            kvm=latest.kvm,
            at=latest.latest_at or moment,
            outcome="success" if latest.latest_success else "failure",
            stop_code=latest.last_stop_code if latest.latest_success is False else None,
        )
        if latest is not None
        else None
    )

    return ScheduleSnapshot(
        agents=agents,
        interpreter=str(health.interpreter) if health.interpreter is not None else None,
        interpreter_can_run=health.interpreter_can_run,
        timezone_name=health.timezone_name,
        timezone_ok=health.timezone_ok,
        problems=problems,
        healthy=not problems,
        installed=installed,
        desired_now=desired_now,
        next_transition_at=boundary.astimezone(KARACHI_TIMEZONE),
        next_transition_to=boundary_state,
        applied=applied,
        applied_verified_at={
            profile: details.verified_at
            for profile, details in profile_states.items()
            if details.verified_at is not None
        },
        applied_updated_at=applied_updated_at,
        last_transition_at=previous_boundary,
        last_run=last_run,
        kvms=fleet,
    )


def _karachi(moment: datetime) -> str:
    return moment.astimezone(KARACHI_TIMEZONE).strftime("%a %H:%M")


# ----- meetings -----------------------------------------------------------------------------


def meeting_service() -> MeetingService:
    """The same service `pikvm-agent meeting ...` uses; the dashboard adds no authority."""

    return default_meeting_service()


def meeting_library() -> MeetingLibrary:
    return MeetingLibrary()


def meeting_settings() -> MeetingSettings:
    return MeetingSettings.from_env()


def _next_meeting_step(status: MeetingStatusResult) -> str | None:
    state = status.state
    if state is None:
        return None
    if status.worker_stale:
        return "The recorder process is gone; Stop recovers the finalized audio."
    if state.phase in {
        RecorderPhase.READY_FOR_PROCESSING,
        RecorderPhase.DISCONNECTED,
        RecorderPhase.INTERRUPTED,
        RecorderPhase.PROCESSING_FAILED,
    }:
        return "Audio is saved; Stop finishes transcription and the report."
    if state.phase is RecorderPhase.RECORDING:
        return "Recording the remote computer's HDMI audio; Stop when the meeting ends."
    if state.phase is RecorderPhase.STARTING:
        return "Negotiating the PiKVM audio session…"
    if state.phase in {RecorderPhase.TRANSCRIBING, RecorderPhase.ANALYZING}:
        return "Processing; the report appears in the list when it is done."
    return None


def meeting_recorder_card(status: MeetingStatusResult) -> MeetingRecorderCard:
    state = status.state
    return MeetingRecorderCard(
        active=status.active,
        phase=state.phase.value if state is not None else None,
        kvm=state.kvm if state is not None else None,
        session_id=state.session_id if state is not None else None,
        elapsed_seconds=status.elapsed_seconds,
        worker_alive=status.worker_alive,
        worker_stale=status.worker_stale,
        error_code=state.error_code if state is not None else None,
        next_step=_next_meeting_step(status),
    )


def meeting_session_card(summary: MeetingSessionSummary) -> MeetingSessionCard:
    return MeetingSessionCard(
        session_id=summary.session_id,
        kvm=summary.kvm,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        duration_seconds=summary.duration_seconds,
        stage=summary.stage,
        has_report=summary.has_report,
        interrupted=summary.interrupted,
        parts=summary.parts,
        our_action_items=summary.our_action_items,
        possible_our_action_items=summary.possible_our_action_items,
        decisions=summary.decisions,
        problem=summary.problem,
    )


def meeting_detail_card(detail: MeetingSessionDetail) -> MeetingSessionDetailCard:
    return MeetingSessionDetailCard(
        session=meeting_session_card(detail.summary),
        meeting_summary=detail.meeting_summary,
        action_items=[
            MeetingActionItemCard(
                task=item.task,
                owner=item.owner,
                owner_category=item.owner_category,
                requested_by=item.requested_by,
                due_text=item.due_text,
                reason=item.reason,
                timestamp_seconds=item.timestamp_seconds,
            )
            for item in detail.action_items
        ],
        decisions=list(detail.decisions),
        blockers=list(detail.blockers),
        open_questions=list(detail.open_questions),
        follow_ups=list(detail.follow_ups),
        transcript=[
            MeetingTranscriptLine(
                start_seconds=line.start_seconds, speaker=line.speaker, text=line.text
            )
            for line in detail.transcript
        ],
        report_markdown=detail.report_markdown,
    )


def meetings_snapshot(profiles: list[str]) -> MeetingsSnapshot:
    settings = meeting_settings()
    provider = str(settings.transcription_provider.value)
    configured = (
        bool(settings.deepgram_api_key)
        if provider == MeetingProvider.DEEPGRAM.value
        else bool(settings.openai_api_key)
    )
    identity: dict[str, bool] = {}
    for name in profiles:
        try:
            identity[name] = PiKVMSettings.from_env(name).work_identity is not None
        except (PiKVMError, OSError, ValueError):
            identity[name] = False
    return MeetingsSnapshot(
        recorder=meeting_recorder_card(meeting_service().status()),
        sessions=[meeting_session_card(item) for item in meeting_library().list_sessions()],
        transcription_provider=provider,
        transcription_configured=configured,
        identity_configured=identity,
        data_directory=str(settings.data_directory),
    )


def meeting_start_work(kvm: str) -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        emit(f"{kvm}  | Opening a receive-only PiKVM audio session…")
        try:
            result = meeting_service().start(kvm)
        except MeetingError as exc:
            return JobOutcome(
                ok=False,
                summary=str(exc),
                results=(JobResultLine(kvm=kvm, ok=False, text=str(exc)),),
            )
        emit(f"{kvm}  | Recording {result.session_id}.")
        return JobOutcome(
            ok=True,
            summary=f"Recording {kvm} · {result.session_id}",
            results=(JobResultLine(kvm=kvm, ok=True, text="recording started"),),
            payload={"session_id": result.session_id, "kvm": result.kvm},
        )

    return run


def meeting_stop_work() -> Callable[[Emit], JobOutcome]:
    def run(emit: Emit) -> JobOutcome:
        service = meeting_service()
        status = service.status()
        kvm = status.state.kvm if status.state is not None else "meeting"
        emit(f"{kvm}  | Stopping the recorder and finalizing audio…")
        try:
            result = service.stop()
        except MeetingError as exc:
            return JobOutcome(
                ok=False,
                summary=str(exc),
                results=(JobResultLine(kvm=kvm, ok=False, text=str(exc)),),
            )
        text = (
            f"{_minutes(result.duration_seconds)} · ours {result.our_action_items}, possible "
            f"{result.possible_our_action_items}, decisions {result.decisions}"
        )
        emit(f"{result.kvm}  | Report ready: {result.session_id}")
        return JobOutcome(
            ok=True,
            summary=f"Processed {result.session_id}",
            results=(JobResultLine(kvm=result.kvm, ok=True, text=text),),
            payload={"session_id": result.session_id, "kvm": result.kvm},
        )

    return run


def _minutes(seconds: float) -> str:
    whole = max(0, round(seconds))
    minutes, secs = divmod(whole, 60)
    return f"{minutes}m {secs:02d}s"
