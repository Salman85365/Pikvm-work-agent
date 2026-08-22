from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.agenda.errors import AgendaError
from work_agent.agenda.models import AgendaBatchResult, AgendaReport, MeetingItem, MeetingStatus
from work_agent.agenda.operator import AgendaOperator
from work_agent.agenda.service import AgendaService, JsonlAgendaLogger
from work_agent.pikvm import configured_pikvm_profiles
from work_agent.pikvm.profiles import describe_unknown_profile


def add_agenda_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    calendar = subparsers.add_parser(
        "calendar",
        help="Read an already-open calendar through PiKVM without opening any meeting.",
    )
    calendar_commands = calendar.add_subparsers(dest="calendar_command", required=True)
    today = calendar_commands.add_parser(
        "today",
        help="Report today's remaining meetings from the visible calendar.",
    )
    target = today.add_mutually_exclusive_group(required=True)
    target.add_argument("--kvm", default=None, help="One named PiKVM profile.")
    target.add_argument(
        "--all-kvms",
        action="store_true",
        help="Process every PIKVM_PROFILES entry sequentially.",
    )
    today.add_argument(
        "--trace",
        action="store_true",
        help="Print sanitized controller states, proposals, policy, and verification events.",
    )


def default_agenda_service(
    *,
    trace_output: Callable[[str], None] | None = None,
) -> AgendaService:
    return AgendaService(AgendaOperator(trace_output=trace_output), JsonlAgendaLogger())


def _resolve_targets(args: argparse.Namespace) -> tuple[str, ...]:
    profiles = configured_pikvm_profiles()
    if not profiles:
        raise AgendaError("Calendar commands require at least one name in PIKVM_PROFILES.")
    if args.all_kvms:
        return profiles
    target = args.kvm.strip().lower()
    if not target:
        raise AgendaError("--kvm requires a non-empty named PiKVM profile.")
    if target not in profiles:
        raise AgendaError(describe_unknown_profile(target))
    return (target,)


def execute_agenda_command(
    args: argparse.Namespace,
    *,
    service: AgendaService | None = None,
) -> AgendaBatchResult:
    if args.calendar_command != "today":
        raise AssertionError(f"Unhandled calendar command: {args.calendar_command}")
    targets = _resolve_targets(args)
    selected = service or default_agenda_service(
        trace_output=print if getattr(args, "trace", False) else None
    )
    return selected.run(targets)


def _when(item: MeetingItem) -> str:
    if item.all_day:
        return "all day"
    if item.start_text is None:
        return "time unclear"
    if item.end_text is None:
        return item.start_text
    return f"{item.start_text}\u2013{item.end_text}"


def _meeting_line(item: MeetingItem) -> str:
    marker = "▸" if item.status is MeetingStatus.IN_PROGRESS else "·"
    details = [_when(item), item.title]
    line = f"    {marker} {'  '.join(details)}"
    notes = []
    if item.location:
        notes.append(item.location)
    if item.is_online:
        notes.append("online")
    if item.declined:
        notes.append("declined")
    if item.status is MeetingStatus.IN_PROGRESS:
        notes.append("in progress")
    return f"{line} ({', '.join(notes)})" if notes else line


def _report_lines(report: AgendaReport) -> list[str]:
    if not report.success:
        return [f"{report.kvm}  ✗ {report.error or 'calendar unavailable'}"]

    upcoming = report.upcoming
    heading = [report.date_text or "today"]
    if report.current_time_text:
        heading.append(f"clock {report.current_time_text}")
    lines = [f"{report.kvm}  ✓ {' · '.join(heading)} — {len(upcoming)} still ahead"]
    if upcoming:
        lines.extend(_meeting_line(item) for item in upcoming)
    else:
        lines.append("    nothing further scheduled")

    earlier = report.earlier
    if earlier:
        lines.append(f"  Earlier today: {len(earlier)}")
        lines.extend(_meeting_line(item) for item in earlier)

    if not report.clock_read:
        lines.append(
            "    ! the remote clock could not be read, so nothing is marked as already over"
        )
    if report.obstructed:
        lines.append("    ! something covered part of the calendar; this list may be incomplete")
    if report.later_truncated:
        lines.append("    ! the day was still clipped; later meetings may exist below")
    if report.earlier_truncated:
        lines.append("    ! the day was clipped above; earlier meetings may exist")
    return lines


def format_agenda_batch(result: AgendaBatchResult) -> str:
    lines: list[str] = []
    for report in result.reports:
        lines.append("")
        lines.extend(_report_lines(report))
    for report in result.reports:
        if report.log_error is not None:
            lines.append(f"{report.kvm}  ! {report.log_error}")
    return "\n".join(lines).strip("\n")
