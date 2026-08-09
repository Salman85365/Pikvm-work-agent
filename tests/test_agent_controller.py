from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import cast

import pytest
from PIL import Image

from work_agent.agent.approval import ApprovalProvider
from work_agent.agent.config import AgentSettings
from work_agent.agent.controller import AgentController, ControllerOptions
from work_agent.agent.executor import ActionExecutor
from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    AgentStepSummary,
    ExecutionResult,
    FinishAction,
    PlanningResult,
    PolicyDecision,
    PressKeyAction,
    RiskCategory,
    TextPurpose,
    TypeTextAction,
    zero_usage,
)
from work_agent.agent.planner import ActionPlanner
from work_agent.agent.policy import PolicyEngine
from work_agent.agent.screen_change import PreActionGuard, ScreenSettleDetector, SettleResult
from work_agent.pikvm import Screenshot, ScreenSize
from work_agent.vision import (
    ActionVerification,
    ImageDetail,
    ObservationContext,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenAnalyzer,
    ScreenObservation,
    ScreenState,
    ServiceTier,
    VerificationStatus,
)


def _jpeg(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _screenshot(color: str = "white") -> Screenshot:
    return Screenshot(
        content=_jpeg(color),
        size=ScreenSize(64, 32),
        captured_at=datetime.now(UTC),
    )


def _analysis(
    *,
    confidence: float = 0.95,
    state: ScreenState = ScreenState.APPLICATION,
    safe: bool = True,
) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Open Slack",
        application="Desktop",
        screen_state=state,
        summary="Desktop is visible.",
        target_found=False,
        target=None,
        relevant_elements=[],
        warnings=[],
        safe_to_continue=safe,
        stop_reason=None if safe else "Human review is required.",
        confidence=confidence,
        screenshot_width=64,
        screenshot_height=32,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.AUTO,
        reasoning_effort=ReasoningEffort.LOW,
        usage=zero_usage(),
        latency_seconds=0.1,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


def _verification(status: VerificationStatus) -> ActionVerification:
    return ActionVerification(
        status=status,
        confidence=0.95,
        evidence="Visible state checked.",
        expected_outcome_observed=status is VerificationStatus.SUCCESS,
    )


class _Analyzer:
    def __init__(self, observations: list[ScreenObservation | BaseException]) -> None:
        self.observations = observations
        self.contexts: list[ObservationContext] = []

    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: object | None = None,
    ) -> ScreenAnalysis:
        raise AssertionError("The controller must use combined observation and verification.")

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: object | None = None,
    ) -> ScreenObservation:
        self.contexts.append(context)
        result = self.observations.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Planner:
    def __init__(self, proposals: list[ActionProposal | BaseException]) -> None:
        self.proposals = proposals
        self.calls = 0

    def plan(
        self,
        *,
        objective: str,
        screen: ScreenAnalysis,
        previous_action: ExecutionResult | None,
        previous_verification: ActionVerification | None,
        history: Sequence[AgentStepSummary],
        remaining_steps: int,
    ) -> PlanningResult:
        self.calls += 1
        proposal = self.proposals.pop(0)
        if isinstance(proposal, BaseException):
            raise proposal
        return PlanningResult(
            proposal=proposal,
            requested_model="planner",
            model="planner",
            requested_service_tier="default",
            service_tier="default",
            reasoning_effort="low",
            usage=zero_usage(),
            latency_seconds=0.1,
            retries=0,
        )


class _Remote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def press_key(self, key: str) -> None:
        self.calls.append(("press_key", key))

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", keys))

    def type_text(self, text: str, *, keymap: str | None = None, delay: float = 0.0) -> None:
        self.calls.append(("type_text", len(text)))

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        self.calls.append(("move_mouse", (x, y)))

    def click(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("click", args))

    def double_click(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("double_click", args))

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        self.calls.append(("scroll", delta_y))


class _Approval:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.requests = 0

    def approve(
        self,
        *,
        proposal: ActionProposal,
        policy: PolicyDecision,
        objective: str,
    ) -> bool:
        self.requests += 1
        return self.approved

    def confirm_step(self, *, proposal: ActionProposal, policy: PolicyDecision) -> bool:
        self.requests += 1
        return self.approved


class _Settler:
    def __init__(self, screenshots: list[Screenshot]) -> None:
        self.screenshots = screenshots
        self.calls = 0

    def wait_for_settle(
        self,
        capture: Callable[[], Screenshot],
        *,
        before: Screenshot,
    ) -> SettleResult:
        self.calls += 1
        return SettleResult(
            screenshot=self.screenshots.pop(0),
            changed=True,
            stable=True,
            timed_out=False,
            polls=2,
            elapsed_seconds=0.2,
            difference=0.5,
        )


def _proposal(action: PressKeyAction | FinishAction, confidence: float = 0.95) -> ActionProposal:
    return ActionProposal(
        action=action,
        expected_outcome="The expected visible state appears.",
        confidence=confidence,
        risk=RiskCategory.NAVIGATION,
        reason_summary="This is the next navigation step.",
    )


def _controller(
    *,
    screenshots: list[Screenshot],
    observations: list[ScreenObservation | BaseException],
    proposals: list[ActionProposal | BaseException],
    settled: list[Screenshot] | None = None,
    options: ControllerOptions | None = None,
    approval: _Approval | None = None,
    clock: Callable[[], float] | None = None,
    events: list[str] | None = None,
) -> tuple[AgentController, _Remote, _Planner, _Analyzer]:
    captures = iter(screenshots)
    remote = _Remote()
    analyzer = _Analyzer(observations)
    planner = _Planner(proposals)
    settings = AgentSettings(api_key="test-key", max_no_change_steps=2)
    controller = AgentController(
        capture=lambda: next(captures),
        analyzer=cast(ScreenAnalyzer, analyzer),
        planner=cast(ActionPlanner, planner),
        policy=PolicyEngine(),
        executor=ActionExecutor(remote),
        guard=PreActionGuard(material_change_threshold=0.06),
        settle_detector=cast(
            ScreenSettleDetector,
            _Settler(settled or []),
        ),
        approval_provider=cast(ApprovalProvider, approval or _Approval()),
        settings=settings,
        options=options or ControllerOptions.from_settings(settings),
        event_sink=(events.append if events is not None else None),
        clock=clock or (lambda: 0.0),
    )
    return controller, remote, planner, analyzer


def test_objective_already_satisfied_finishes_without_hid() -> None:
    events: list[str] = []
    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[_proposal(FinishAction(type="finish", summary="Slack is open."))],
        events=events,
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.SUCCESS
    assert result.summary == "Slack is open."
    assert result.telemetry.hid_actions == 0
    assert remote.calls == []
    assert planner.calls == 1
    assert events.count("State: observing") == 1


def test_failed_planner_attempt_is_counted() -> None:
    from work_agent.agent.errors import PlannerRequestError

    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[PlannerRequestError("OpenAI rejected the planner request.")],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.FAILED
    assert result.telemetry.planner_calls == 1
    assert result.telemetry.model_calls == 2
    assert planner.calls == 1
    assert remote.calls == []


def test_one_action_is_observed_verified_then_finished() -> None:
    initial = _screenshot("white")
    after = _screenshot("black")
    controller, remote, planner, analyzer = _controller(
        screenshots=[initial, initial],
        settled=[after],
        observations=[
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
            ScreenObservation(
                analysis=_analysis(),
                previous_action_verification=_verification(VerificationStatus.SUCCESS),
            ),
        ],
        proposals=[
            _proposal(PressKeyAction(type="press_key", key="Escape")),
            _proposal(FinishAction(type="finish", summary="Done.")),
        ],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.SUCCESS
    assert remote.calls == [("press_key", "Escape")]
    assert planner.calls == 2
    assert len(analyzer.contexts) == 2
    assert analyzer.contexts[0].previous_action is None
    assert analyzer.contexts[1].previous_action == "press_key Escape"
    assert result.history[0].verification is not None
    assert result.history[0].verification.status is VerificationStatus.SUCCESS


def test_dry_run_never_reaches_hid_or_preaction_capture() -> None:
    settings = AgentSettings(api_key="test-key")
    controller, remote, _, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[_proposal(PressKeyAction(type="press_key", key="Escape"))],
        options=ControllerOptions.from_settings(settings, dry_run=True),
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.DRY_RUN
    assert result.telemetry.hid_actions == 0
    assert remote.calls == []


@pytest.mark.parametrize("status", [VerificationStatus.FAILURE, VerificationStatus.UNCERTAIN])
def test_failed_or_uncertain_verification_stops_without_repeating(
    status: VerificationStatus,
) -> None:
    initial = _screenshot("white")
    controller, remote, planner, _ = _controller(
        screenshots=[initial, initial],
        settled=[_screenshot("black")],
        observations=[
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
            ScreenObservation(
                analysis=_analysis(),
                previous_action_verification=_verification(status),
            ),
        ],
        proposals=[_proposal(PressKeyAction(type="press_key", key="Escape"))],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.FAILED
    assert remote.calls == [("press_key", "Escape")]
    assert planner.calls == 1
    assert result.telemetry.verification_failures == 1


def test_low_screen_confidence_stops_before_planner() -> None:
    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[
            ScreenObservation(
                analysis=_analysis(confidence=0.5),
                previous_action_verification=None,
            )
        ],
        proposals=[],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert planner.calls == 0
    assert remote.calls == []


def test_low_planner_confidence_stops_before_hid() -> None:
    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[
            _proposal(
                PressKeyAction(type="press_key", key="Escape"),
                confidence=0.5,
            )
        ],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert planner.calls == 1
    assert remote.calls == []


@pytest.mark.parametrize("state", [ScreenState.AUTHENTICATION, ScreenState.DIALOG])
def test_unsafe_screen_stops_before_planner(state: ScreenState) -> None:
    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[
            ScreenObservation(
                analysis=_analysis(state=state, safe=False),
                previous_action_verification=None,
            )
        ],
        proposals=[],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert planner.calls == 0
    assert remote.calls == []


def test_max_step_stops_only_after_last_action_is_verified() -> None:
    settings = AgentSettings(api_key="test-key")
    initial = _screenshot("white")
    controller, remote, planner, _ = _controller(
        screenshots=[initial, initial],
        settled=[_screenshot("black")],
        observations=[
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
            ScreenObservation(
                analysis=_analysis(),
                previous_action_verification=_verification(VerificationStatus.SUCCESS),
            ),
        ],
        proposals=[_proposal(PressKeyAction(type="press_key", key="Escape"))],
        options=ControllerOptions.from_settings(settings, max_steps=1),
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert result.history[0].verification is not None
    assert remote.calls == [("press_key", "Escape")]
    assert planner.calls == 1


def test_runtime_limit_stops_before_model_or_hid() -> None:
    times: Iterator[float] = iter([0.0, 1.0, 1.0])
    settings = AgentSettings(api_key="test-key")
    controller, remote, planner, analyzer = _controller(
        screenshots=[_screenshot()],
        observations=[],
        proposals=[],
        options=ControllerOptions.from_settings(settings, timeout_seconds=0.5),
        clock=lambda: next(times),
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert planner.calls == 0
    assert analyzer.contexts == []
    assert remote.calls == []


def test_changed_screen_cancels_stale_action_and_reobserves() -> None:
    initial = _screenshot("white")
    changed = _screenshot("black")
    controller, remote, planner, _ = _controller(
        screenshots=[initial, changed],
        observations=[
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
        ],
        proposals=[
            _proposal(PressKeyAction(type="press_key", key="Escape")),
            _proposal(FinishAction(type="finish", summary="Stopped on fresh state.")),
        ],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.SUCCESS
    assert result.telemetry.stale_action_cancellations == 1
    assert remote.calls == []
    assert planner.calls == 2


def test_approval_rejection_stops_consequential_text_without_hid() -> None:
    approval = _Approval(approved=False)
    proposal = ActionProposal(
        action=TypeTextAction(
            type="type_text",
            text="draft",
            purpose=TextPurpose.EXTERNAL_COMMUNICATION,
        ),
        expected_outcome="Draft text appears.",
        confidence=0.95,
        risk=RiskCategory.EXTERNAL_COMMUNICATION,
        reason_summary="Enter a draft.",
    )
    controller, remote, _, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[proposal],
        approval=approval,
    )

    result = controller.run("Draft a message")

    assert result.status is AgentFinalStatus.PAUSED
    assert approval.requests == 1
    assert result.telemetry.approval_requests == 1
    assert remote.calls == []


def test_step_mode_rejection_stops_safe_action_without_hid() -> None:
    settings = AgentSettings(api_key="test-key")
    approval = _Approval(approved=False)
    controller, remote, _, _ = _controller(
        screenshots=[_screenshot()],
        observations=[ScreenObservation(analysis=_analysis(), previous_action_verification=None)],
        proposals=[_proposal(PressKeyAction(type="press_key", key="Escape"))],
        options=ControllerOptions.from_settings(settings, step_mode=True),
        approval=approval,
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.PAUSED
    assert approval.requests == 1
    assert remote.calls == []


def test_same_screen_and_action_loop_is_stopped_before_repeat() -> None:
    same = _screenshot("white")
    action = _proposal(PressKeyAction(type="press_key", key="Escape"))
    controller, remote, planner, _ = _controller(
        screenshots=[same, same],
        settled=[same],
        observations=[
            ScreenObservation(analysis=_analysis(), previous_action_verification=None),
            ScreenObservation(
                analysis=_analysis(),
                previous_action_verification=_verification(VerificationStatus.SUCCESS),
            ),
        ],
        proposals=[action, action],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.FAILED
    assert "stuck" in result.summary.lower()
    assert planner.calls == 2
    assert remote.calls == [("press_key", "Escape")]


def test_keyboard_interrupt_stops_without_hid() -> None:
    controller, remote, planner, _ = _controller(
        screenshots=[_screenshot()],
        observations=[KeyboardInterrupt()],
        proposals=[],
    )

    result = controller.run("Open Slack")

    assert result.status is AgentFinalStatus.INTERRUPTED
    assert planner.calls == 0
    assert remote.calls == []
