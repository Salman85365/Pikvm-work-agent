from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from dataclasses import replace

from work_agent.agent.approval import NonInteractiveApprovalProvider
from work_agent.agent.cli import execute_agent_command
from work_agent.agent.errors import AgentError, ControllerLockError
from work_agent.agent.lock import DEFAULT_LOCK_WAIT_SECONDS, ControllerLock
from work_agent.agent.models import (
    AgentFinalStatus,
    AgentSessionResult,
    ApprovalMode,
    ClickElementAction,
    ExecutionTransportStatus,
    StopCode,
)
from work_agent.agent.pikvm_session import PiKVMSession
from work_agent.pikvm import PiKVMError, PiKVMSettings, build_totp_provider
from work_agent.slack.analyzer import SlackAvailabilityScreenAnalyzer
from work_agent.slack.errors import SlackAvailabilityError
from work_agent.slack.models import Availability, AvailabilityResult, WorkflowTelemetry
from work_agent.slack.objectives import availability_objective
from work_agent.slack.policy import SlackAvailabilityPolicyEngine
from work_agent.slack.state import (
    AvailabilityTracker,
    is_availability_toggle_reference,
    is_profile_menu_outcome_reference,
    is_profile_navigation_reference,
)
from work_agent.vision import ImageDetail, VisionError

_LOGGER = logging.getLogger(__name__)
LOCK_BUSY_STOP_CODE = "lock_busy"


class AgentAvailabilityOperator:
    def __init__(
        self,
        *,
        executor: Callable[..., AgentSessionResult] = execute_agent_command,
        trace_output: Callable[[str], None] | None = None,
        controller_lock_factory: Callable[[str], ControllerLock] | None = None,
        controller_lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
        menu_closer: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = executor
        self._trace_output = trace_output or (lambda _: None)
        self._controller_lock_factory = controller_lock_factory or _controller_lock_for_profile
        self._controller_lock_wait_seconds = controller_lock_wait_seconds
        self._menu_closer = menu_closer or _press_escape

    def execute(self, kvm: str, desired: Availability | None) -> AvailabilityResult:
        # One lease for the whole workflow: the recovery and read-back sessions must not lose
        # the endpoint to a launchd run between phases and then report a click as failed.
        try:
            controller_lock = self._controller_lock_factory(kvm)
            controller_lock.acquire(
                timeout_seconds=self._controller_lock_wait_seconds,
                on_wait=lambda: self._trace_output(
                    f"{kvm}  | Another local workflow is using this PiKVM; waiting for it."
                ),
            )
        except ControllerLockError:
            return AvailabilityResult(
                kvm=kvm,
                desired=desired,
                observed=None,
                changed=None,
                success=False,
                error=(
                    "Another local workflow kept using this PiKVM for the whole wait, so this "
                    "run was skipped rather than sending input alongside it."
                ),
                stop_code=LOCK_BUSY_STOP_CODE,
            )
        except (PiKVMError, OSError, ValueError) as exc:
            raise SlackAvailabilityError(str(exc)) from None
        try:
            return self._execute_locked(kvm, desired, controller_lock)
        finally:
            controller_lock.release()

    def _execute_locked(
        self,
        kvm: str,
        desired: Availability | None,
        controller_lock: ControllerLock,
    ) -> AvailabilityResult:
        last_tracker: list[AvailabilityTracker] = []
        try:
            return self._run_workflow(kvm, desired, controller_lock, last_tracker)
        finally:
            # The workflow proves its result from the open profile menu; leaving that menu
            # open on the work computer is untidy and hides the sidebar for the next read.
            if last_tracker and last_tracker[-1].menu_open_at_last_observation:
                self._close_menu(kvm)

    def _close_menu(self, kvm: str) -> None:
        try:
            self._menu_closer(kvm)
        except (PiKVMError, OSError, ValueError) as exc:
            _LOGGER.warning("Could not close the Slack profile menu on %s: %s", kvm, exc)
            self._trace_output(f"{kvm}  | Could not close the profile menu: {exc}")
        else:
            self._trace_output(f"{kvm}  | Closed the profile menu with Escape.")

    def _run_workflow(
        self,
        kvm: str,
        desired: Availability | None,
        controller_lock: ControllerLock,
        trackers: list[AvailabilityTracker],
    ) -> AvailabilityResult:
        tracker = AvailabilityTracker(desired)
        trackers.append(tracker)
        first_observed: Availability | None = None
        sessions: list[AgentSessionResult] = []
        try:
            session = self._run_controller(
                kvm,
                objective=availability_objective(desired),
                tracker=tracker,
                controller_lock=controller_lock,
            )
            sessions.append(session)
            first_observed = tracker.first
            if self._needs_navigation_recovery(session, desired):
                self._trace_output(
                    f"{kvm}  | Starting one fresh observe-first recovery after uncertain "
                    "Slack navigation."
                )
                tracker = AvailabilityTracker(desired)
                trackers.append(tracker)
                session = self._run_controller(
                    kvm,
                    objective=availability_objective(desired),
                    tracker=tracker,
                    controller_lock=controller_lock,
                )
                sessions.append(session)
                if first_observed is None:
                    first_observed = tracker.first
            if self._needs_read_only_verification(session, desired):
                assert desired is not None
                self._trace_output(
                    f"{kvm}  | Starting one read-only final-state verification pass."
                )
                verifier = AvailabilityTracker(None)
                trackers.append(verifier)
                verification_session = self._run_controller(
                    kvm,
                    objective=availability_objective(None),
                    tracker=verifier,
                    controller_lock=controller_lock,
                )
                sessions.append(verification_session)
                return self._read_back_result(
                    kvm=kvm,
                    desired=desired,
                    first_observed=first_observed,
                    session=verification_session,
                    observed=verifier.final,
                    telemetry=_telemetry(sessions),
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
        elif success and first_observed is not None:
            changed = first_observed is not desired
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
            telemetry=_telemetry(sessions),
        )

    def _run_controller(
        self,
        kvm: str,
        *,
        objective: str,
        tracker: AvailabilityTracker,
        controller_lock: ControllerLock,
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
            controller_lock=controller_lock,
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
    def _needs_navigation_recovery(
        session: AgentSessionResult,
        desired: Availability | None,
    ) -> bool:
        return (
            desired is not None
            and session.status is AgentFinalStatus.FAILED
            and session.stop_code is StopCode.VERIFICATION_FAILED
            and session.telemetry.hid_actions > 0
            and AgentAvailabilityOperator._last_action_is_availability_navigation(session)
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
    def _last_action_is_availability_navigation(session: AgentSessionResult) -> bool:
        if not session.history:
            return False
        step = session.history[-1]
        action = step.proposal.action
        if not isinstance(action, ClickElementAction):
            return False
        if not is_profile_navigation_reference(action.element_id):
            return False
        if not is_profile_menu_outcome_reference(step.proposal.expected_outcome):
            return False
        execution = step.execution_result
        return (
            execution is not None
            and execution.hid_action
            and execution.transport_status
            in {ExecutionTransportStatus.SENT, ExecutionTransportStatus.UNCERTAIN}
        )

    @staticmethod
    def _read_back_result(
        *,
        kvm: str,
        desired: Availability,
        first_observed: Availability | None,
        session: AgentSessionResult,
        observed: Availability | None,
        telemetry: WorkflowTelemetry | None = None,
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
            telemetry=telemetry,
        )


def _controller_lock_for_profile(kvm: str) -> ControllerLock:
    return ControllerLock.for_endpoint(PiKVMSettings.from_env(kvm).base_url)


def _press_escape(kvm: str) -> None:
    """One deterministic Escape to close the profile menu the workflow opened.

    Escape is on the generic navigation allowlist and cannot select a menu item; it is sent
    inside the workflow's endpoint lease, once, without a model call.
    """

    settings = PiKVMSettings.from_env(kvm)
    with PiKVMSession(settings, totp_provider=build_totp_provider(settings)) as session:
        session.press_key("Escape")


def _telemetry(sessions: list[AgentSessionResult]) -> WorkflowTelemetry:
    summed = WorkflowTelemetry()
    for session in sessions:
        record = getattr(session, "telemetry", None)
        if record is None:
            summed = replace(summed, sessions=summed.sessions + 1)
            continue
        vision_usage = getattr(record, "vision_usage", None)
        planner_usage = getattr(record, "planner_usage", None)
        summed = WorkflowTelemetry(
            sessions=summed.sessions + 1,
            steps=summed.steps + int(getattr(record, "steps", 0)),
            hid_actions=summed.hid_actions + int(getattr(record, "hid_actions", 0)),
            vision_calls=summed.vision_calls + int(getattr(record, "vision_calls", 0)),
            planner_calls=summed.planner_calls + int(getattr(record, "planner_calls", 0)),
            total_tokens=(
                summed.total_tokens
                + int(getattr(vision_usage, "total_tokens", 0))
                + int(getattr(planner_usage, "total_tokens", 0))
            ),
            runtime_seconds=summed.runtime_seconds + float(getattr(record, "runtime_seconds", 0.0)),
        )
    return summed


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
    StopCode.PIKVM_UNREACHABLE: "The PiKVM could not be reached.",
    StopCode.PIKVM_AUTH_FAILED: "The PiKVM rejected this Mac's credentials.",
    StopCode.MODEL_PROVIDER_ERROR: "The OpenAI API could not be used.",
    StopCode.MODEL_OUTPUT_INVALID: "OpenAI returned output the local schema could not accept.",
    StopCode.INTERNAL_ERROR: "A local error stopped the controller.",
    StopCode.COMPLETED: "The controller completed.",
    StopCode.DRY_RUN: "The controller ran in dry-run mode and issued no action.",
}

# For these codes the controller summary is the project's own sanitized exception message
# (never model prose or screen content), and it is the only thing that says *which* KVM
# problem or provider problem occurred, so it is appended rather than discarded.
_DETAILED_STOP_CODES = frozenset(
    {
        StopCode.PIKVM_UNREACHABLE,
        StopCode.PIKVM_AUTH_FAILED,
        StopCode.MODEL_PROVIDER_ERROR,
        StopCode.MODEL_OUTPUT_INVALID,
        StopCode.INTERNAL_ERROR,
    }
)


def _controller_failure_reason(session: AgentSessionResult) -> str:
    reason = _STOP_REASONS.get(session.stop_code)
    if reason is not None:
        detail = (getattr(session, "summary", "") or "").strip()
        if session.stop_code in _DETAILED_STOP_CODES and detail and detail != reason:
            return f"{reason} {detail}"
        return reason
    # Unreachable while _STOP_REASONS covers StopCode; kept so a new code degrades safely.
    return f"The verified controller stopped with status {session.status.value}."
