from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.pikvm import configured_pikvm_profiles
from work_agent.slack.agent_operator import AgentAvailabilityOperator
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.logging import JsonlAvailabilityLogger
from work_agent.slack.models import Availability, AvailabilityBatchResult, AvailabilityResult
from work_agent.slack.service import SlackAvailabilityService


def add_slack_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    slack = subparsers.add_parser(
        "slack",
        help="Run bounded Slack GUI workflows through PiKVM.",
    )
    slack_commands = slack.add_subparsers(dest="slack_command", required=True)
    availability = slack_commands.add_parser(
        "availability",
        help="Read or set Slack's manual Active/Away availability.",
    )
    actions = availability.add_subparsers(dest="availability_action", required=True)

    get = actions.add_parser("get", help="Visually read current Slack availability.")
    _add_kvm_target(get)

    set_availability = actions.add_parser(
        "set",
        help="Set and visually verify Slack availability.",
    )
    set_availability.add_argument("availability", type=Availability, choices=list(Availability))
    _add_kvm_target(set_availability)


def _add_kvm_target(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--kvm", default=None, help="One named PiKVM profile.")
    target.add_argument(
        "--all-kvms",
        action="store_true",
        help="Process every PIKVM_PROFILES entry sequentially.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print sanitized controller states, proposals, policy, and verification events.",
    )


def default_slack_availability_service(
    *,
    trace_output: Callable[[str], None] | None = None,
) -> SlackAvailabilityService:
    return SlackAvailabilityService(
        AgentAvailabilityOperator(trace_output=trace_output),
        JsonlAvailabilityLogger(),
    )


def execute_slack_command(
    args: argparse.Namespace,
    *,
    service: SlackAvailabilityService | None = None,
) -> AvailabilityBatchResult:
    if args.slack_command != "availability":
        raise AssertionError(f"Unhandled Slack command: {args.slack_command}")
    profiles = configured_pikvm_profiles()
    if not profiles:
        raise SlackAvailabilityError(
            "Slack availability commands require at least one name in PIKVM_PROFILES."
        )
    if args.all_kvms:
        targets = profiles
    else:
        target = args.kvm.strip().lower()
        if not target:
            raise SlackAvailabilityError("--kvm requires a non-empty named PiKVM profile.")
        if target not in profiles:
            raise SlackAvailabilityError(f"Unknown PiKVM profile {target!r}.")
        targets = (target,)

    desired = args.availability if args.availability_action == "set" else None
    selected_service = service or default_slack_availability_service(
        trace_output=print if getattr(args, "trace", False) else None
    )
    return selected_service.run(targets, desired)


def format_availability_batch(result: AvailabilityBatchResult) -> str:
    lines = [_format_result(item) for item in result.results]
    for item in result.results:
        if item.log_error is not None:
            lines.append(f"{item.kvm}  ! {item.log_error}")
    return "\n".join(lines)


def _format_result(result: AvailabilityResult) -> str:
    if not result.success:
        return f"{result.kvm}  ✗ {result.error or 'availability unavailable'}"
    observed = result.observed.value if result.observed is not None else "unknown"
    if result.desired is None:
        suffix = ""
    elif result.changed is True:
        suffix = " (changed)"
    elif result.changed is False:
        suffix = " (already set; no-op)"
    else:
        suffix = " (verified)"
    return f"{result.kvm}  ✓ {observed}{suffix}"
