from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from work_agent.meeting.config import MeetingSettings
from work_agent.meeting.service import (
    MeetingAbandonResult,
    MeetingLifecycleError,
    MeetingService,
    MeetingStartResult,
    MeetingStateResetResult,
    MeetingStatusResult,
    MeetingStopResult,
)
from work_agent.meeting.state import RecorderPhase

if TYPE_CHECKING:
    from work_agent.meeting.library import MeetingSessionDetail, MeetingSessionSummary


def add_meeting_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    meeting = subparsers.add_parser(
        "meeting",
        help="Record incoming PiKVM WebRTC audio and create a local meeting report.",
    )
    commands = meeting.add_subparsers(
        dest="meeting_command",
        required=True,
        metavar="{start,stop,status,list,show,validate,abandon}",
    )
    start = commands.add_parser(
        "start",
        help="Start one explicit incoming-audio recording.",
    )
    start.add_argument("--kvm", required=True, help="One named PiKVM profile.")
    commands.add_parser("stop", help="Stop, transcribe, and process the active recording.")
    commands.add_parser("status", help="Show recorder state without contacting PiKVM or OpenAI.")
    commands.add_parser("list", help="List recorded sessions and their processing stage.")
    show = commands.add_parser(
        "show", help="Print one session's summary, action items, and report path."
    )
    show.add_argument("--session-id", required=True)
    validate = commands.add_parser(
        "validate",
        help=(
            "One-command hardware check: record for --seconds, stop, transcribe, analyse, and "
            "print the result. Play audio you are allowed to record on the remote first."
        ),
    )
    validate.add_argument("--kvm", required=True, help="One named PiKVM profile.")
    validate.add_argument(
        "--seconds", type=float, default=60.0, help="How long to record (default 60)."
    )
    abandon = commands.add_parser(
        "abandon",
        help="Release one exact stopped session without deleting its artifacts.",
    )
    abandon.add_argument(
        "--session-id",
        help="Exact session ID shown by meeting status.",
    )
    abandon.add_argument(
        "--reset-corrupt",
        action="store_true",
        help=(
            "Move an unreadable recorder state file aside (nothing is deleted) so meeting "
            "commands work again. Refused when the state is readable."
        ),
    )
    worker = commands.add_parser("_capture", help=argparse.SUPPRESS)
    worker.add_argument("--session-id", required=True, help=argparse.SUPPRESS)
    commands._choices_actions.pop()


def default_meeting_service() -> MeetingService:
    return MeetingService(MeetingSettings.from_env())


def execute_meeting_command(
    args: argparse.Namespace,
    *,
    service: MeetingService | None = None,
) -> tuple[str, int]:
    if args.meeting_command == "_capture":
        # The recorder pulls in aiortc, av, and websockets; keep them out of every other
        # command's import path so Slack and calendar runs never depend on those wheels.
        from work_agent.meeting.worker import run_capture_worker

        return "", run_capture_worker(args.session_id)
    selected = service or default_meeting_service()
    if args.meeting_command == "start":
        return format_meeting_start(selected.start(args.kvm)), 0
    if args.meeting_command == "stop":
        return format_meeting_stop(selected.stop()), 0
    if args.meeting_command == "status":
        return format_meeting_status(selected.status()), 0
    if args.meeting_command == "list":
        from work_agent.meeting.library import MeetingLibrary

        return format_meeting_list(MeetingLibrary().list_sessions()), 0
    if args.meeting_command == "show":
        from work_agent.meeting.library import MeetingLibrary

        detail = MeetingLibrary().detail(args.session_id)
        if detail is None:
            raise MeetingLifecycleError(f"No recorded session {args.session_id!r} was found.")
        return format_meeting_detail(detail), 0
    if args.meeting_command == "validate":
        return run_meeting_validation(
            selected,
            kvm=args.kvm,
            seconds=args.seconds,
            output=getattr(args, "_output", print),
            sleeper=getattr(args, "_sleeper", None),
        )
    if args.meeting_command == "abandon":
        if getattr(args, "reset_corrupt", False):
            if args.session_id is not None:
                raise MeetingLifecycleError("Use either --session-id or --reset-corrupt, not both.")
            return format_meeting_state_reset(selected.reset_corrupt_state()), 0
        if args.session_id is None:
            raise MeetingLifecycleError(
                "meeting abandon requires --session-id, or --reset-corrupt for an unreadable "
                "state file."
            )
        return format_meeting_abandon(selected.abandon(args.session_id)), 0
    raise AssertionError(f"Unhandled meeting command: {args.meeting_command}")


def run_meeting_validation(
    service: MeetingService,
    *,
    kvm: str,
    seconds: float,
    output: Callable[[str], None] = print,
    sleeper: Callable[[float], None] | None = None,
) -> tuple[str, int]:
    """Record, stop, process, and summarise in one go - the real-hardware acceptance check."""

    if not 5 <= seconds <= 3600:
        raise MeetingLifecycleError("--seconds must be between 5 and 3600.")
    sleep = sleeper or time.sleep
    started = service.start(kvm)
    output(format_meeting_start(started))
    output("")
    output(f"Recording for {_duration(seconds)}; keep the audio playing on the remote computer…")
    remaining = seconds
    while remaining > 0:
        step = min(15.0, remaining)
        sleep(step)
        remaining -= step
        status = service.status()
        phase = status.state.phase.value if status.state is not None else "unknown"
        output(f"  {_duration(seconds - remaining)} elapsed · recorder {phase}")
        if status.state is not None and status.state.phase.terminal:
            break
    output("")
    output("Stopping and processing (transcription, then action items)…")
    stopped = service.stop()
    lines = [format_meeting_stop(stopped), ""]
    from work_agent.meeting.library import MeetingLibrary

    detail = MeetingLibrary().detail(stopped.session_id)
    if detail is not None:
        lines.append(format_meeting_detail(detail))
    return "\n".join(lines), 0


def format_meeting_list(sessions: list[MeetingSessionSummary]) -> str:
    if not sessions:
        return "No recorded meetings yet. Start one with `pikvm-agent meeting start --kvm NAME`."
    lines = []
    for item in sessions:
        when = item.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        stage = item.stage.replace("_", " ")
        counts = ""
        if item.our_action_items is not None:
            counts = (
                f" · ours {item.our_action_items} / possible {item.possible_our_action_items}"
                f" / decisions {item.decisions}"
            )
        lines.append(
            f"{item.session_id:<28} {item.kvm:<10} {when}  {_duration(item.duration_seconds):>8}"
            f"  {stage}{counts}"
        )
    return "\n".join(lines)


def format_meeting_detail(detail: MeetingSessionDetail) -> str:
    summary = detail.summary
    lines = [
        f"Session: {summary.session_id}",
        f"KVM: {summary.kvm}",
        f"Started: {summary.started_at.astimezone().isoformat(timespec='minutes')}",
        f"Duration: {_duration(summary.duration_seconds)}",
        f"Stage: {summary.stage.replace('_', ' ')}",
        f"Artifacts: {_display_path(summary.directory)}",
    ]
    if detail.meeting_summary:
        lines.extend(["", "Summary:", detail.meeting_summary])
    ours = [item for item in detail.action_items if item.owner_category == "our_identity"]
    possible = [
        item for item in detail.action_items if item.owner_category == "possibly_our_identity"
    ]
    others = [item for item in detail.action_items if item not in ours and item not in possible]
    for title, items in (
        ("OUR ACTION ITEMS", ours),
        ("POSSIBLE OUR ACTION ITEMS", possible),
        ("OTHER ACTION ITEMS", others),
    ):
        if items:
            lines.extend(["", f"{title}:"])
            for item in items:
                owner = f" ({item.owner})" if item.owner else ""
                due = f" - due {item.due_text}" if item.due_text else ""
                lines.append(f"  - {item.task}{owner}{due}")
    if detail.decisions:
        lines.extend(["", "Decisions:", *[f"  - {text}" for text in detail.decisions]])
    if summary.has_report:
        lines.extend(["", "Report:", _display_path(summary.directory / "report.md")])
    return "\n".join(lines)


def format_meeting_start(result: MeetingStartResult) -> str:
    return "\n".join(
        [
            "Meeting recording started.",
            "",
            f"KVM: {result.kvm}",
            "Audio source: PiKVM WebRTC incoming HDMI audio only",
            f"Session: {result.session_id}",
            f"Artifacts: {_display_path(result.directory)}",
        ]
    )


def format_meeting_stop(result: MeetingStopResult) -> str:
    lines = [
        "Meeting processed successfully.",
        "",
        f"KVM: {result.kvm}",
        f"Duration: {_duration(result.duration_seconds)}",
    ]
    if result.interrupted:
        lines.append("Capture: interrupted; finalized audio was processed")
    lines.extend(
        [
            "",
            f"Our action items: {result.our_action_items}",
            f"Possible our action items: {result.possible_our_action_items}",
            f"Decisions: {result.decisions}",
            f"Blockers: {result.blockers}",
            "",
            "Report:",
            _display_path(result.report_path),
        ]
    )
    return "\n".join(lines)


def format_meeting_status(result: MeetingStatusResult) -> str:
    state = result.state
    if state is None:
        return "No meeting recording is active."

    status = state.phase.value.replace("_", " ")
    if result.worker_stale:
        status = "interrupted (recorder process is no longer running)"
    lines = [
        "Meeting recorder status",
        "",
        f"KVM: {state.kvm}",
        f"Status: {status}",
        f"Duration: {_duration(result.elapsed_seconds)}",
        f"Session: {state.session_id}",
        f"Artifacts: {_display_path(state.session_directory)}",
    ]
    if state.phase in {
        RecorderPhase.READY_FOR_PROCESSING,
        RecorderPhase.DISCONNECTED,
        RecorderPhase.INTERRUPTED,
        RecorderPhase.PROCESSING_FAILED,
    }:
        lines.append("Next: run `pikvm-agent meeting stop` to finish or retry processing.")
    elif result.worker_stale:
        lines.extend(
            [
                "Next: run `pikvm-agent meeting stop` to recover finalized audio.",
                "If recovery is not possible, preserve the artifacts and release only "
                "this session:",
                f"`pikvm-agent meeting abandon --session-id {state.session_id}`",
            ]
        )
    if state.error_code is not None:
        lines.append(f"Error code: {state.error_code}")
    if state.report_path is not None:
        lines.extend(["Report:", _display_path(state.report_path)])
    return "\n".join(lines)


def format_meeting_state_reset(result: MeetingStateResetResult) -> str:
    return "\n".join(
        [
            "Corrupt meeting recorder state moved aside; no artifacts were deleted.",
            "",
            f"State: {_display_path(result.state_path)}",
            f"Moved to: {_display_path(result.moved_to)}",
            "Meeting commands can be used again. Inspect the moved file if a recording needs "
            "to be located by hand.",
        ]
    )


def format_meeting_abandon(result: MeetingAbandonResult) -> str:
    return "\n".join(
        [
            "Meeting session abandoned; local artifacts were preserved.",
            "",
            f"KVM: {result.kvm}",
            f"Session: {result.session_id}",
            f"Artifacts: {_display_path(result.directory)}",
        ]
    )


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def _duration(seconds: float) -> str:
    whole = max(0, round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
