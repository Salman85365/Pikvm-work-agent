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
    StopCode,
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
        stop_code = None
        if not success:
            error = _controller_failure_reason(session)
            stop_code = session.stop_code.value
        return AvailabilityResult(
            kvm=kvm,
            desired=desired,
            observed=tracker.final,
            changed=changed,
            success=success,
            error=error,
            stop_code=stop_code,
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
            and session.stop_code is StopCode.VERIFICATION_FAILED
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
        stop_code: str | None = None
        if success:
            error = None
            changed = None if first_observed is None else first_observed is not desired
        elif session.status is not AgentFinalStatus.SUCCESS:
            error = "Read-only final-state verification did not complete: " + (
                _controller_failure_reason(session)
            )
            stop_code = session.stop_code.value
            changed = None
        elif observed is None:
            error = "Read-only final-state verification found no manual availability evidence."
            stop_code = StopCode.COMPLETION_UNVERIFIED.value
            changed = None
        else:
            error = (
                f"Read-only final-state verification found {observed.value}, "
                f"not requested {desired.value}."
            )
            stop_code = StopCode.COMPLETION_UNVERIFIED.value
            changed = False
        return AvailabilityResult(
            kvm=kvm,
            desired=desired,
            observed=observed,
            changed=changed,
            success=success,
            error=error,
            stop_code=stop_code,
        )


# Exhaustive by StopCode, so no cause can be flattened into a generic string. Every entry
# must stay sanitized: no screen content, credentials, or provider detail.
_STOP_REASONS: dict[StopCode, str] = {
    StopCode.RUNTIME_LIMIT: "The controller reached its runtime limit.",
    StopCode.STEP_LIMIT: "The controller reached its maximum verified step count.",
    StopCode.SCREEN_UNSAFE: (
        "The screen analyzer marked the visible state unsafe for unattended navigation."
    ),
    StopCode.SCREEN_LOW_CONFIDENCE: (
        "The screen observation confidence was below the safety threshold."
    ),
    StopCode.PLANNER_LOW_CONFIDENCE: "The next-action confidence was below the safety threshold.",
    StopCode.VERIFICATION_FAILED: (
        "The last action's expected result could not be visually verified; the action was not "
        "repeated."
    ),
    StopCode.VERIFICATION_MISSING: (
        "The post-action observation returned no verification, so the result was not assumed."
    ),
    StopCode.COMPLETION_UNVERIFIED: (
        "The planner reported completion, but the final screen did not visibly prove the Slack "
        "availability state."
    ),
    StopCode.POLICY_DENIED: "Local policy denied the proposed action on the visible screen.",
    StopCode.APPROVAL_DENIED: (
        "Local policy required interactive approval, which unattended Slack workflows deny."
    ),
    StopCode.STEP_CANCELLED: "The controller step was cancelled before execution.",
    StopCode.USER_ASSISTANCE_REQUESTED: (
        "The planner requested user assistance instead of guessing from the visible screen."
    ),
    StopCode.TRANSPORT_FAILED: (
        "PiKVM could not accept the HID action; it was not retried or replayed."
    ),
    StopCode.STUCK_REPEATED_ACTION: (
        "The controller stopped before repeating the same action on an unchanged screen."
    ),
    StopCode.STUCK_NO_SCREEN_CHANGE: (
        "The controller stopped because a sent action produced no visible screen change."
    ),
    StopCode.INTERRUPTED: "The Slack availability controller was interrupted before completion.",
    StopCode.INTERNAL_ERROR: "A sanitized local error stopped the controller.",
    StopCode.COMPLETED: "The controller completed.",
    StopCode.DRY_RUN: "The controller ran in dry-run mode and issued no action.",
}


def _controller_failure_reason(session: AgentSessionResult) -> str:
    reason = _STOP_REASONS.get(session.stop_code)
    if reason is not None:
        return reason
    # Unreachable while _STOP_REASONS covers StopCode; kept so a new code degrades safely.
    return f"The verified controller stopped with status {session.status.value}."
