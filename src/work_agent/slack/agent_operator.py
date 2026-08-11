from __future__ import annotations

import argparse
from collections.abc import Callable

from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.cli import execute_agent_command
from work_agent.agent.errors import AgentError
from work_agent.agent.models import (
    AgentFinalStatus,
    AgentSessionResult,
    ApprovalMode,
    ClickElementAction,
    RequestUserAction,
)
from work_agent.pikvm import PiKVMError
from work_agent.slack.analyzer import SlackAvailabilityScreenAnalyzer
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.models import Availability, AvailabilityResult
from work_agent.slack.objectives import availability_objective
from work_agent.slack.policy import SlackAvailabilityPolicyEngine
from work_agent.slack.state import AvailabilityTracker, is_availability_toggle_reference
from work_agent.vision import ImageDetail, VisionError


class AgentAvailabilityOperator:
    def __init__(
        self,
        *,
        executor: Callable[..., AgentSessionResult] = execute_agent_command,
        trace_output: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = executor
        self._trace_output = trace_output or (lambda _: None)

    def execute(self, kvm: str, desired: Availability | None) -> AvailabilityResult:
        tracker = AvailabilityTracker(desired)
        try:
            session = self._run_controller(
                kvm,
                objective=availability_objective(desired),
                tracker=tracker,
            )
            if self._needs_read_only_verification(session, desired):
                assert desired is not None
                self._trace_output(
                    f"{kvm}  | Starting one read-only final-state verification pass."
                )
                verifier = AvailabilityTracker(None)
                verification_session = self._run_controller(
                    kvm,
                    objective=availability_objective(None),
                    tracker=verifier,
                )
                return self._read_back_result(
                    kvm=kvm,
                    desired=desired,
                    first_observed=tracker.first,
                    session=verification_session,
                    observed=verifier.final,
                )
        except (AgentError, PiKVMError, VisionError) as exc:
            raise SlackAvailabilityError(str(exc)) from None
        except (OSError, ValueError):
            raise SlackAvailabilityError(
                "The local Slack availability controller could not be started."
            ) from None

        success = session.status is AgentFinalStatus.SUCCESS and tracker.final is not None
        changed: bool | None = None
        if desired is None:
            changed = False
        elif success and tracker.first is not None:
            changed = tracker.first is not desired
        error = None
        if not success:
            error = _controller_failure_reason(session)
        return AvailabilityResult(
            kvm=kvm,
            desired=desired,
            observed=tracker.final,
            changed=changed,
            success=success,
            error=error,
        )

    def _run_controller(
        self,
        kvm: str,
        *,
        objective: str,
        tracker: AvailabilityTracker,
    ) -> AgentSessionResult:
        args = argparse.Namespace(
            command="agent-run",
            profile=kvm,
            objective=objective,
            timeout=None,
            approval_mode=ApprovalMode.SAFE,
            debug_dir=None,
            vision_model=None,
            planner_model=None,
            max_steps=None,
            step=False,
            dry_run=False,
        )
        return self._executor(
            args,
            output=lambda message: self._trace_output(f"{kvm}  | {message}"),
            approval_provider=NonInteractiveApprovalProvider(),
            observation_sink=tracker.observe,
            completion_validator=tracker.validate_completion,
            policy_engine=SlackAvailabilityPolicyEngine(),
            vision_detail=ImageDetail.HIGH,
            verification_override=tracker.override_uncertain_verification,
            analyzer_transform=lambda analyzer, settings: SlackAvailabilityScreenAnalyzer(
                analyzer,
                settings,
                event_sink=lambda message: self._trace_output(f"{kvm}  | {message}"),
            ),
        )

    @staticmethod
    def _needs_read_only_verification(
        session: AgentSessionResult,
        desired: Availability | None,
    ) -> bool:
        return (
            desired is not None
            and session.status is AgentFinalStatus.FAILED
            and session.summary == "Previous action was not verified; it will not be repeated."
            and session.telemetry.hid_actions > 0
            and AgentAvailabilityOperator._last_action_is_availability_toggle(session)
        )

    @staticmethod
    def _last_action_is_availability_toggle(session: AgentSessionResult) -> bool:
        if not session.history:
            return False
        action = session.history[-1].proposal.action
        if not isinstance(action, ClickElementAction):
            return False
        return is_availability_toggle_reference(action.element_id)

    @staticmethod
    def _read_back_result(
        *,
        kvm: str,
        desired: Availability,
        first_observed: Availability | None,
        session: AgentSessionResult,
        observed: Availability | None,
    ) -> AvailabilityResult:
        success = session.status is AgentFinalStatus.SUCCESS and observed is desired
        if success:
            error = None
            changed = None if first_observed is None else first_observed is not desired
        elif session.status is not AgentFinalStatus.SUCCESS:
            error = "Read-only final-state verification did not complete: " + (
                _controller_failure_reason(session)
            )
            changed = None
        elif observed is None:
            error = "Read-only final-state verification found no manual availability evidence."
            changed = None
        else:
            error = (
                f"Read-only final-state verification found {observed.value}, "
                f"not requested {desired.value}."
            )
            changed = False
        return AvailabilityResult(
            kvm=kvm,
            desired=desired,
            observed=observed,
            changed=changed,
            success=success,
            error=error,
        )


def _controller_failure_reason(session: AgentSessionResult) -> str:
    if session.status is AgentFinalStatus.PAUSED:
        if session.history and isinstance(session.history[-1].proposal.action, RequestUserAction):
            return (
                "The planner requested user assistance instead of guessing from the visible screen."
            )
        known_reasons = {
            "Agent runtime limit reached.": "The controller reached its runtime limit.",
            "Agent runtime limit reached before action execution.": (
                "The controller reached its runtime limit before executing the next action."
            ),
            "Maximum step count reached after verifying the last action.": (
                "The controller reached its maximum verified step count."
            ),
            "Screen confidence is below the configured action threshold.": (
                "The screen observation confidence was below the safety threshold."
            ),
            "Planner confidence is below the configured action threshold.": (
                "The next-action confidence was below the safety threshold."
            ),
            "User rejected or did not provide approval.": (
                "Local policy required interactive approval, which unattended Slack workflows deny."
            ),
            "Step execution was cancelled by the user.": (
                "The controller step was cancelled before execution."
            ),
        }
        if session.summary in known_reasons:
            return known_reasons[session.summary]
        if session.telemetry.planner_calls == 0:
            return "The screen analyzer marked the visible state unsafe for unattended navigation."
        return "The bounded controller paused before Slack availability could be verified."
    if session.status is AgentFinalStatus.INTERRUPTED:
        return "The Slack availability controller was interrupted before completion."
    if session.status is AgentFinalStatus.FAILED:
        known_failures = {
            "Previous action was not verified; it will not be repeated.": (
                "The last action's expected result could not be visually verified; the action was "
                "not repeated."
            ),
            "Agent appears stuck; the repeated action was not executed.": (
                "The controller stopped before repeating the same action on an unchanged screen."
            ),
            "Agent appears stuck because the screen did not change.": (
                "The controller stopped because a sent action produced no visible screen change."
            ),
        }
        if session.summary in known_failures:
            return known_failures[session.summary]
    return f"The verified controller stopped with status {session.status.value}."
