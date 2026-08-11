from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from work_agent.agent.approval import ApprovalProvider
from work_agent.agent.config import (
    HARD_MAX_RUNTIME_SECONDS,
    HARD_MAX_STEPS,
    AgentSettings,
)
from work_agent.agent.debug import DebugArtifacts
from work_agent.agent.errors import AgentError
from work_agent.agent.executor import ActionExecutor
from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    AgentSessionResult,
    AgentStep,
    AgentStepSummary,
    ApprovalMode,
    ClickElementAction,
    ControllerState,
    DoubleClickElementAction,
    ExecutionResult,
    ExecutionTransportStatus,
    FinishAction,
    MoveMouseAction,
    PolicyDecision,
    PolicyDecisionKind,
    RequestUserAction,
    SessionTelemetry,
    StopCode,
    action_fingerprint,
    action_summary,
    is_hid_action,
    zero_usage,
)
from work_agent.agent.planner import ActionPlanner
from work_agent.agent.policy import PolicyEngine
from work_agent.agent.screen_change import (
    GuardStatus,
    PreActionGuard,
    ScreenSettleDetector,
    signature,
)
from work_agent.pikvm import PiKVMError, Screenshot
from work_agent.vision import (
    ActionVerification,
    AnalysisOptions,
    AnalysisUsage,
    ImageDetail,
    ObservationContext,
    ScreenAnalysis,
    ScreenAnalyzer,
    ScreenObservation,
    VerificationStatus,
    VisionError,
    normalized_to_pixel,
)


@dataclass(frozen=True, slots=True)
class ControllerOptions:
    max_steps: int
    timeout_seconds: float
    approval_mode: ApprovalMode = ApprovalMode.SAFE
    step_mode: bool = False
    dry_run: bool = False
    vision_model: str | None = None
    vision_detail: ImageDetail | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= HARD_MAX_STEPS:
            raise ValueError(f"max steps must be between 1 and the hard cap of {HARD_MAX_STEPS}")
        if not 0 < self.timeout_seconds <= HARD_MAX_RUNTIME_SECONDS:
            raise ValueError(
                "timeout must be greater than zero and no more than "
                f"{HARD_MAX_RUNTIME_SECONDS:g} seconds"
            )
        if not isinstance(self.approval_mode, ApprovalMode):
            raise ValueError("approval mode must be safe or every")
        if self.vision_model is not None:
            normalized = self.vision_model.strip()
            if not normalized or any(character.isspace() for character in normalized):
                raise ValueError("vision model must be a non-empty identifier without whitespace")
            object.__setattr__(self, "vision_model", normalized)

    @classmethod
    def from_settings(
        cls,
        settings: AgentSettings,
        *,
        max_steps: int | None = None,
        timeout_seconds: float | None = None,
        approval_mode: ApprovalMode = ApprovalMode.SAFE,
        step_mode: bool = False,
        dry_run: bool = False,
        vision_model: str | None = None,
        vision_detail: ImageDetail | None = None,
    ) -> ControllerOptions:
        configured_steps = settings.max_steps if max_steps is None else max_steps
        configured_timeout = (
            settings.max_runtime_seconds if timeout_seconds is None else timeout_seconds
        )
        return cls(
            max_steps=configured_steps,
            timeout_seconds=configured_timeout,
            approval_mode=approval_mode,
            step_mode=step_mode,
            dry_run=dry_run,
            vision_model=vision_model,
            vision_detail=vision_detail,
        )


@dataclass(slots=True)
class _Counters:
    hid_actions: int = 0
    vision_calls: int = 0
    planner_calls: int = 0
    retries: int = 0
    approval_requests: int = 0
    stale_action_cancellations: int = 0
    verification_failures: int = 0
    screen_settle_seconds: float = 0.0
    model_latency_seconds: float = 0.0
    vision_usage: AnalysisUsage = field(default_factory=zero_usage)
    planner_usage: AnalysisUsage = field(default_factory=zero_usage)


class AgentController:
    """Run one locally gated, observed, and verified action at a time."""

    def __init__(
        self,
        *,
        capture: Callable[[], Screenshot],
        analyzer: ScreenAnalyzer,
        planner: ActionPlanner,
        policy: PolicyEngine,
        executor: ActionExecutor,
        guard: PreActionGuard,
        settle_detector: ScreenSettleDetector,
        approval_provider: ApprovalProvider,
        settings: AgentSettings,
        options: ControllerOptions,
        debug_artifacts: DebugArtifacts | None = None,
        event_sink: Callable[[str], None] | None = None,
        observation_sink: Callable[[ScreenAnalysis], None] | None = None,
        completion_validator: Callable[[ScreenAnalysis], str | None] | None = None,
        verification_override: (
            Callable[[ScreenAnalysis, ActionVerification], ActionVerification | None] | None
        ) = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._capture = capture
        self._analyzer = analyzer
        self._planner = planner
        self._policy = policy
        self._executor = executor
        self._guard = guard
        self._settle = settle_detector
        self._approval = approval_provider
        self._settings = settings
        self._options = options
        self._debug = debug_artifacts or DebugArtifacts(None)
        self._event_sink = event_sink or (lambda _: None)
        self._observation_sink = observation_sink or (lambda _: None)
        self._completion_validator = completion_validator
        self._verification_override = verification_override
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._state = ControllerState.INITIALIZING

    def run(self, objective: str) -> AgentSessionResult:
        normalized_objective = objective.strip()
        if not normalized_objective:
            raise ValueError("agent objective must not be empty")

        session_id = uuid.uuid4().hex
        started_at = self._now()
        started_clock = self._clock()
        counters = _Counters()
        history: list[AgentStep] = []
        previous_execution: ExecutionResult | None = None
        previous_proposal: ActionProposal | None = None
        current: Screenshot | None = None
        no_change_steps = 0

        try:
            current = self._capture()
            while True:
                planner_previous_execution: ExecutionResult | None = None
                planner_previous_verification: ActionVerification | None = None
                if self._expired(started_clock):
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=StopCode.RUNTIME_LIMIT,
                        summary="Agent runtime limit reached.",
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )

                observation = self._observe(
                    current,
                    objective=normalized_objective,
                    previous_execution=previous_execution,
                    previous_proposal=previous_proposal,
                    counters=counters,
                )
                analysis = observation.analysis
                self._observation_sink(analysis)

                if previous_execution is not None:
                    verification = observation.previous_action_verification
                    if verification is None:
                        return self._failure(
                            "The post-action observation omitted required verification.",
                            normalized_objective,
                            session_id,
                            started_at,
                            started_clock,
                            counters,
                            history,
                            stop_code=StopCode.VERIFICATION_MISSING,
                        )
                    if self._verification_override is not None:
                        overridden = self._verification_override(analysis, verification)
                        if overridden is not None:
                            verification = overridden
                    self._transition(ControllerState.VERIFYING)
                    history[-1] = history[-1].model_copy(update={"verification": verification})
                    self._debug.save_verification(history[-1].step_number, verification)
                    self._event_sink(
                        f"Verification: {verification.status.value} "
                        f"(confidence {verification.confidence:.2f})"
                    )
                    if (
                        verification.status is not VerificationStatus.SUCCESS
                        or not verification.expected_outcome_observed
                        or verification.confidence < self._settings.min_action_confidence
                    ):
                        counters.verification_failures += 1
                        return self._failure(
                            "Previous action was not verified; it will not be repeated.",
                            normalized_objective,
                            session_id,
                            started_at,
                            started_clock,
                            counters,
                            history,
                            stop_code=StopCode.VERIFICATION_FAILED,
                        )
                    planner_previous_execution = previous_execution
                    planner_previous_verification = verification
                    previous_execution = None
                    previous_proposal = None

                if len(history) >= self._options.max_steps:
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=StopCode.STEP_LIMIT,
                        summary="Maximum step count reached after verifying the last action.",
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )

                unsafe = self._unsafe_reason(analysis)
                if unsafe is not None:
                    unsafe_reason, unsafe_code = unsafe
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=unsafe_code,
                        summary=unsafe_reason,
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )

                step_number = len(history) + 1
                screen_signature = signature(current.content)
                self._debug.save_before(step_number, current, analysis)
                self._transition(ControllerState.PLANNING)
                counters.planner_calls += 1
                planning = self._planner.plan(
                    objective=normalized_objective,
                    screen=analysis,
                    previous_action=planner_previous_execution,
                    previous_verification=planner_previous_verification,
                    history=self._summaries(history),
                    remaining_steps=self._options.max_steps - len(history),
                )
                counters.planner_usage += planning.usage
                counters.retries += planning.retries
                counters.model_latency_seconds += planning.latency_seconds
                proposal = planning.proposal
                target_coordinates = self._target_coordinates(proposal, analysis)
                self._event_sink(
                    f"Proposal: {action_summary(proposal.action)} "
                    f"{target_coordinates}"
                    f"(confidence {proposal.confidence:.2f}, risk {proposal.risk.value})"
                )

                if self._expired(started_clock):
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=StopCode.RUNTIME_LIMIT,
                        summary="Agent runtime limit reached before action execution.",
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )
                if proposal.confidence < self._settings.min_action_confidence:
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=StopCode.PLANNER_LOW_CONFIDENCE,
                        summary="Planner confidence is below the configured action threshold.",
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )

                self._transition(ControllerState.POLICY_CHECK)
                decision = self._policy.evaluate(proposal, analysis)
                if (
                    self._options.approval_mode is ApprovalMode.EVERY
                    and is_hid_action(proposal.action)
                    and decision.decision is PolicyDecisionKind.ALLOW
                ):
                    decision = PolicyDecision(
                        decision=PolicyDecisionKind.REQUIRE_APPROVAL,
                        reason="Approval mode requires confirmation before every HID action.",
                        inferred_risk=decision.inferred_risk,
                    )
                self._event_sink(f"Policy: {decision.decision.value} — {decision.reason}")
                self._debug.save_proposal(step_number, proposal, decision)
                step = AgentStep(
                    step_number=step_number,
                    observed_at=current.captured_at,
                    screen_fingerprint=screen_signature.fingerprint,
                    application=analysis.application,
                    screen_state=analysis.screen_state,
                    proposal=proposal,
                    policy_decision=decision,
                    execution_result=None,
                    verification=None,
                )
                history.append(step)

                if isinstance(proposal.action, FinishAction):
                    if self._completion_validator is not None:
                        validation_error = self._completion_validator(analysis)
                        if validation_error is not None:
                            return self._failure(
                                validation_error,
                                normalized_objective,
                                session_id,
                                started_at,
                                started_clock,
                                counters,
                                history,
                                stop_code=StopCode.COMPLETION_UNVERIFIED,
                            )
                    return self._finish(
                        status=AgentFinalStatus.SUCCESS,
                        stop_code=StopCode.COMPLETED,
                        summary=proposal.action.summary,
                        state=ControllerState.FINISHED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )
                if isinstance(proposal.action, RequestUserAction):
                    return self._finish(
                        status=AgentFinalStatus.PAUSED,
                        stop_code=StopCode.USER_ASSISTANCE_REQUESTED,
                        summary=proposal.action.message,
                        state=ControllerState.PAUSED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )
                if decision.decision is PolicyDecisionKind.DENY:
                    return self._failure(
                        f"Policy denied the proposed action: {decision.reason}",
                        normalized_objective,
                        session_id,
                        started_at,
                        started_clock,
                        counters,
                        history,
                        stop_code=StopCode.POLICY_DENIED,
                    )
                if self._appears_stuck(history):
                    return self._failure(
                        "Agent appears stuck; the repeated action was not executed.",
                        normalized_objective,
                        session_id,
                        started_at,
                        started_clock,
                        counters,
                        history,
                        stop_code=StopCode.STUCK_REPEATED_ACTION,
                    )
                if self._options.dry_run:
                    return self._finish(
                        status=AgentFinalStatus.DRY_RUN,
                        stop_code=StopCode.DRY_RUN,
                        summary=f"Dry run: would {action_summary(proposal.action)}.",
                        state=ControllerState.FINISHED,
                        objective=normalized_objective,
                        session_id=session_id,
                        started_at=started_at,
                        started_clock=started_clock,
                        counters=counters,
                        history=history,
                    )

                if decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL:
                    self._transition(ControllerState.WAITING_APPROVAL)
                    counters.approval_requests += 1
                    if not self._approval.approve(
                        proposal=proposal,
                        policy=decision,
                        objective=normalized_objective,
                    ):
                        return self._finish(
                            status=AgentFinalStatus.PAUSED,
                            stop_code=StopCode.APPROVAL_DENIED,
                            summary="User rejected or did not provide approval.",
                            state=ControllerState.PAUSED,
                            objective=normalized_objective,
                            session_id=session_id,
                            started_at=started_at,
                            started_clock=started_clock,
                            counters=counters,
                            history=history,
                        )
                if self._options.step_mode:
                    self._transition(ControllerState.WAITING_APPROVAL)
                    if not self._approval.confirm_step(proposal=proposal, policy=decision):
                        return self._finish(
                            status=AgentFinalStatus.PAUSED,
                            stop_code=StopCode.STEP_CANCELLED,
                            summary="Step execution was cancelled by the user.",
                            state=ControllerState.PAUSED,
                            objective=normalized_objective,
                            session_id=session_id,
                            started_at=started_at,
                            started_clock=started_clock,
                            counters=counters,
                            history=history,
                        )

                planned_screen = current
                if is_hid_action(proposal.action):
                    self._transition(ControllerState.PRE_ACTION_CHECK)
                    fresh_screen = self._capture()
                    guard_result = self._guard.check(
                        planned=planned_screen,
                        current=fresh_screen,
                        action=proposal.action,
                        screen=analysis,
                    )
                    self._event_sink(
                        f"Pre-action guard: {guard_result.status.value} "
                        f"(difference {guard_result.difference:.4f})"
                    )
                    if guard_result.status is not GuardStatus.ALLOW:
                        counters.stale_action_cancellations += 1
                        current = fresh_screen
                        continue
                    planned_screen = fresh_screen

                self._transition(ControllerState.EXECUTING)
                execution = self._executor.execute(proposal, analysis)
                history[-1] = history[-1].model_copy(update={"execution_result": execution})
                if execution.hid_action:
                    counters.hid_actions += 1
                self._event_sink(f"Transport: {execution.transport_status.value}")
                if execution.transport_status is ExecutionTransportStatus.FAILED:
                    return self._failure(
                        execution.sanitized_error or "The action could not be sent.",
                        normalized_objective,
                        session_id,
                        started_at,
                        started_clock,
                        counters,
                        history,
                        stop_code=StopCode.TRANSPORT_FAILED,
                    )

                self._transition(ControllerState.WAITING_FOR_SCREEN)
                settled = self._settle.wait_for_settle(
                    self._capture,
                    before=planned_screen,
                )
                counters.screen_settle_seconds += settled.elapsed_seconds
                self._debug.save_after(step_number, settled.screenshot)
                self._event_sink(
                    f"Screen settle: changed={'yes' if settled.changed else 'no'}, "
                    f"stable={'yes' if settled.stable else 'no'}, "
                    f"difference={settled.difference:.4f}, "
                    f"timed_out={'yes' if settled.timed_out else 'no'}"
                )
                if settled.changed:
                    no_change_steps = 0
                else:
                    no_change_steps += 1
                if no_change_steps > self._settings.max_no_change_steps:
                    return self._failure(
                        "Agent appears stuck because the screen did not change.",
                        normalized_objective,
                        session_id,
                        started_at,
                        started_clock,
                        counters,
                        history,
                        stop_code=StopCode.STUCK_NO_SCREEN_CHANGE,
                    )
                current = settled.screenshot
                previous_execution = execution
                previous_proposal = proposal
        except KeyboardInterrupt:
            return self._finish(
                status=AgentFinalStatus.INTERRUPTED,
                stop_code=StopCode.INTERRUPTED,
                summary="Agent interrupted; no further HID actions were issued.",
                state=ControllerState.PAUSED,
                objective=normalized_objective,
                session_id=session_id,
                started_at=started_at,
                started_clock=started_clock,
                counters=counters,
                history=history,
            )
        except (AgentError, VisionError, PiKVMError, OSError, ValueError) as exc:
            return self._failure(
                str(exc),
                normalized_objective,
                session_id,
                started_at,
                started_clock,
                counters,
                history,
                stop_code=StopCode.INTERNAL_ERROR,
            )

    def _observe(
        self,
        screenshot: Screenshot,
        *,
        objective: str,
        previous_execution: ExecutionResult | None,
        previous_proposal: ActionProposal | None,
        counters: _Counters,
    ) -> ScreenObservation:
        self._transition(ControllerState.OBSERVING)
        context = ObservationContext(
            objective=objective,
            previous_action=(
                action_summary(previous_execution.action)
                if previous_execution is not None
                else None
            ),
            expected_outcome=(
                previous_proposal.expected_outcome if previous_proposal is not None else None
            ),
        )
        observation = self._analyzer.observe(
            screenshot.content,
            context=context,
            width=screenshot.size.width,
            height=screenshot.size.height,
            options=AnalysisOptions(
                model=self._options.vision_model,
                image_detail=self._options.vision_detail,
            ),
        )
        counters.vision_calls += observation.vision_calls
        counters.vision_usage += observation.analysis.usage
        counters.retries += observation.analysis.retries
        counters.model_latency_seconds += observation.analysis.latency_seconds
        return observation

    def _unsafe_reason(self, analysis: ScreenAnalysis) -> tuple[str, StopCode] | None:
        if analysis.confidence < self._settings.min_action_confidence:
            return (
                "Screen confidence is below the configured action threshold.",
                StopCode.SCREEN_LOW_CONFIDENCE,
            )
        if not analysis.safe_to_continue:
            return (
                analysis.stop_reason or "The current screen is unsafe to continue from.",
                StopCode.SCREEN_UNSAFE,
            )
        return None

    @staticmethod
    def _target_coordinates(proposal: ActionProposal, analysis: ScreenAnalysis) -> str:
        action = proposal.action
        if not isinstance(
            action,
            (ClickElementAction, DoubleClickElementAction, MoveMouseAction),
        ):
            return ""
        elements = ([analysis.target] if analysis.target is not None else []) + list(
            analysis.relevant_elements
        )
        element = next((item for item in elements if item.id == action.element_id), None)
        if element is None or element.click_point is None:
            return "target=unresolved "
        point = element.click_point
        pixel = normalized_to_pixel(
            point,
            width=analysis.screenshot_width,
            height=analysis.screenshot_height,
        )
        return f"target=({point.x},{point.y})→({pixel.x},{pixel.y}px) "

    def _appears_stuck(self, history: list[AgentStep]) -> bool:
        current = history[-1]
        current_action = action_fingerprint(current.proposal.action)
        same_screen_action = sum(
            1
            for step in history
            if step.screen_fingerprint == current.screen_fingerprint
            and action_fingerprint(step.proposal.action) == current_action
        )
        if same_screen_action > self._settings.max_repeated_actions:
            return True
        if len(history) >= 4:
            recent = [action_fingerprint(step.proposal.action) for step in history[-4:]]
            if recent[0] == recent[2] and recent[1] == recent[3] and recent[0] != recent[1]:
                return True
        return False

    @staticmethod
    def _summaries(history: list[AgentStep]) -> list[AgentStepSummary]:
        return [
            AgentStepSummary(
                step_number=step.step_number,
                screen_fingerprint=step.screen_fingerprint,
                application=step.application,
                screen_state=step.screen_state,
                action_summary=action_summary(step.proposal.action),
                policy_decision=step.policy_decision.decision,
                transport_status=(
                    step.execution_result.transport_status
                    if step.execution_result is not None
                    else None
                ),
                verification_status=(step.verification.status if step.verification else None),
            )
            for step in history[-5:]
        ]

    def _expired(self, started_clock: float) -> bool:
        return self._clock() - started_clock >= self._options.timeout_seconds

    def _transition(self, state: ControllerState) -> None:
        self._state = state
        self._event_sink(f"State: {state.value}")

    def _failure(
        self,
        summary: str,
        objective: str,
        session_id: str,
        started_at: datetime,
        started_clock: float,
        counters: _Counters,
        history: list[AgentStep],
        *,
        stop_code: StopCode,
    ) -> AgentSessionResult:
        return self._finish(
            status=AgentFinalStatus.FAILED,
            stop_code=stop_code,
            summary=summary or "Agent stopped after a sanitized controller failure.",
            state=ControllerState.FAILED,
            objective=objective,
            session_id=session_id,
            started_at=started_at,
            started_clock=started_clock,
            counters=counters,
            history=history,
        )

    def _finish(
        self,
        *,
        status: AgentFinalStatus,
        stop_code: StopCode,
        summary: str,
        state: ControllerState,
        objective: str,
        session_id: str,
        started_at: datetime,
        started_clock: float,
        counters: _Counters,
        history: list[AgentStep],
    ) -> AgentSessionResult:
        self._transition(state)
        finished_at = self._now()
        telemetry = SessionTelemetry(
            session_id=session_id,
            objective=objective,
            started_at=started_at,
            finished_at=finished_at,
            final_status=status,
            steps=len(history),
            hid_actions=counters.hid_actions,
            model_calls=counters.vision_calls + counters.planner_calls,
            vision_calls=counters.vision_calls,
            planner_calls=counters.planner_calls,
            vision_usage=counters.vision_usage,
            planner_usage=counters.planner_usage,
            retries=counters.retries,
            approval_requests=counters.approval_requests,
            stale_action_cancellations=counters.stale_action_cancellations,
            verification_failures=counters.verification_failures,
            screen_settle_seconds=counters.screen_settle_seconds,
            total_model_latency_seconds=counters.model_latency_seconds,
            runtime_seconds=max(0.0, self._clock() - started_clock),
        )
        return AgentSessionResult(
            status=status,
            stop_code=stop_code,
            summary=summary,
            telemetry=telemetry,
            history=list(history),
            final_state=state,
        )
