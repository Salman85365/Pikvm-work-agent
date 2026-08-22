from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from work_agent.agent import cli as agent_cli
from work_agent.agent.config import AgentSettings
from work_agent.agent.lock import ControllerLock
from work_agent.agent.models import (
    ActionProposal,
    AgentFinalStatus,
    AgentStepSummary,
    ApprovalMode,
    ExecutionResult,
    PlanningResult,
    PressKeyAction,
    RiskCategory,
    zero_usage,
)
from work_agent.agent.screen_change import SettleResult
from work_agent.pikvm import PiKVMSettings, Screenshot, ScreenSize
from work_agent.vision import (
    ActionVerification,
    ImageDetail,
    ObservationContext,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenObservation,
    ScreenState,
    ServiceTier,
    VerificationStatus,
    VisionSettings,
)


def _screenshot(color: str) -> Screenshot:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), color).save(buffer, format="JPEG")
    return Screenshot(
        content=buffer.getvalue(),
        size=ScreenSize(64, 32),
        captured_at=datetime.now(UTC),
    )


def _analysis() -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Open Slack",
        application="Desktop",
        screen_state=ScreenState.DESKTOP,
        summary="Desktop is visible.",
        target_found=False,
        target=None,
        relevant_elements=[],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.95,
        screenshot_width=64,
        screenshot_height=32,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.AUTO,
        reasoning_effort=ReasoningEffort.LOW,
        usage=zero_usage(),
        latency_seconds=0,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


class _Analyzer:
    observations: ClassVar[list[ScreenObservation]] = []

    def __init__(self, _: object) -> None:
        pass

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: object,
    ) -> ScreenObservation:
        return type(self).observations.pop(0)


class _Planner:
    proposals: ClassVar[list[ActionProposal]] = []

    def __init__(self, _: object) -> None:
        pass

    def plan(
        self,
        *,
        objective: str,
        screen: ScreenAnalysis,
        previous_action: ExecutionResult | None,
        previous_verification: ActionVerification | None,
        history: list[AgentStepSummary],
        remaining_steps: int,
        feedback: str | None = None,
    ) -> PlanningResult:
        return PlanningResult(
            proposal=type(self).proposals.pop(0),
            requested_model="planner",
            model="planner",
            requested_service_tier="default",
            service_tier="default",
            reasoning_effort="low",
            usage=zero_usage(),
            latency_seconds=0,
            retries=0,
        )


class _Session:
    instance: ClassVar[_Session | None] = None

    def __init__(self, _: object, *, totp_provider: object) -> None:
        type(self).instance = self
        self.totp_provider = totp_provider
        self.screenshots: list[Screenshot] = []
        self.hid_calls: list[str] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get_screenshot(self) -> Screenshot:
        return self.screenshots.pop(0)

    def press_key(self, key: str) -> None:
        self.hid_calls.append(f"press_key {key}")

    def hotkey(self, *keys: str) -> None:
        raise AssertionError("unexpected HID")

    def type_text(self, text: str, *, keymap: str | None = None, delay: float = 0.0) -> None:
        raise AssertionError("unexpected HID")

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        raise AssertionError("unexpected HID")

    def click(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected HID")

    def double_click(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected HID")

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        raise AssertionError("unexpected HID")


class _Settler:
    def __init__(self, **_: object) -> None:
        pass

    def wait_for_settle(self, capture: object, *, before: Screenshot) -> SettleResult:
        assert _Session.instance is not None
        screenshot = _Session.instance.get_screenshot()
        return SettleResult(
            screenshot=screenshot,
            changed=True,
            stable=True,
            timed_out=False,
            polls=1,
            elapsed_seconds=0,
            difference=0.5,
        )


class _TotpProvider:
    def current_code(self) -> str:
        return "123456"


class _Approval:
    confirmations: ClassVar[int] = 0

    def __init__(self, **_: object) -> None:
        pass

    def approve(self, **_: object) -> bool:
        type(self).confirmations += 1
        return True

    def confirm_step(self, **_: object) -> bool:
        type(self).confirmations += 1
        return True


def _proposal() -> ActionProposal:
    return ActionProposal(
        action=PressKeyAction(type="press_key", key="Escape"),
        expected_outcome="Menu closes.",
        confidence=0.95,
        risk=RiskCategory.NAVIGATION,
        reason_summary="Close the menu.",
    )


def _configure(monkeypatch: pytest.MonkeyPatch, screenshots: list[Screenshot]) -> None:
    settings = AgentSettings(api_key="test-key")
    vision = VisionSettings(api_key="test-key")
    pikvm = PiKVMSettings(
        base_url="https://pikvm.test",
        username="user",
        password="password",
        totp_required=True,
    )
    monkeypatch.setattr(agent_cli.AgentSettings, "from_env", lambda: settings)
    monkeypatch.setattr(agent_cli.VisionSettings, "from_env", lambda: vision)
    monkeypatch.setattr(agent_cli.PiKVMSettings, "from_env", lambda _profile=None: pikvm)
    lock_directory = Path(tempfile.mkdtemp(prefix="pikvm-agent-test-locks-"))
    monkeypatch.setattr(
        agent_cli.ControllerLock,
        "for_endpoint",
        classmethod(
            lambda cls, endpoint, directory=None: ControllerLock(lock_directory / "endpoint.lock")
        ),
    )
    monkeypatch.setattr(agent_cli, "PiKVMSession", _Session)
    monkeypatch.setattr(agent_cli, "OpenAIScreenAnalyzer", _Analyzer)
    monkeypatch.setattr(agent_cli, "OpenAIActionPlanner", _Planner)
    monkeypatch.setattr(agent_cli, "ScreenSettleDetector", _Settler)
    monkeypatch.setattr(agent_cli, "TerminalApprovalProvider", _Approval)
    _Session.instance = None
    _Analyzer.observations = []
    _Planner.proposals = []
    _Approval.confirmations = 0
    original_init = _Session.__init__

    def init_with_screens(self: _Session, config: object, *, totp_provider: object) -> None:
        original_init(self, config, totp_provider=totp_provider)
        self.screenshots = list(screenshots)

    monkeypatch.setattr(_Session, "__init__", init_with_screens)


def test_agent_run_dry_run_performs_zero_hid_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, [_screenshot("white")])
    _Analyzer.observations = [
        ScreenObservation(analysis=_analysis(), previous_action_verification=None)
    ]
    _Planner.proposals = [_proposal()]
    args = argparse.Namespace(
        command="agent-run",
        objective="Open Slack",
        timeout=None,
        approval_mode=ApprovalMode.SAFE,
        debug_dir=None,
        vision_model=None,
        planner_model=None,
        max_steps=8,
        step=False,
        dry_run=True,
    )

    provider = _TotpProvider()
    monkeypatch.setattr(agent_cli, "build_totp_provider", lambda _: provider)

    result = agent_cli.execute_agent_command(args)

    assert result.status is AgentFinalStatus.DRY_RUN
    assert _Session.instance is not None
    assert _Session.instance.totp_provider is provider
    assert _Session.instance.hid_calls == []


def test_agent_run_propagates_named_pikvm_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, [_screenshot("white")])
    _Analyzer.observations = [
        ScreenObservation(analysis=_analysis(), previous_action_verification=None)
    ]
    _Planner.proposals = [_proposal()]
    selected_profiles: list[str | None] = []
    pikvm = PiKVMSettings(
        base_url="https://lab-pikvm.test",
        username="user",
        password="password",
        profile="lab-kvm",
        totp_required=False,
    )
    monkeypatch.setattr(
        agent_cli.PiKVMSettings,
        "from_env",
        lambda profile=None: selected_profiles.append(profile) or pikvm,
    )
    args = argparse.Namespace(
        command="agent-run",
        profile="lab-kvm",
        objective="Open Slack",
        timeout=None,
        approval_mode=ApprovalMode.SAFE,
        debug_dir=None,
        vision_model=None,
        planner_model=None,
        max_steps=8,
        step=False,
        dry_run=True,
    )

    result = agent_cli.execute_agent_command(args, totp_provider=_TotpProvider())

    assert result.status is AgentFinalStatus.DRY_RUN
    assert selected_profiles == ["lab-kvm"]


def test_agent_step_execute_sends_at_most_one_action_and_verifies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _screenshot("white")
    after = _screenshot("black")
    _configure(monkeypatch, [initial, initial, after])
    _Analyzer.observations = [
        ScreenObservation(analysis=_analysis(), previous_action_verification=None),
        ScreenObservation(
            analysis=_analysis(),
            previous_action_verification=ActionVerification(
                status=VerificationStatus.SUCCESS,
                confidence=0.95,
                evidence="Menu is closed.",
                expected_outcome_observed=True,
            ),
        ),
    ]
    _Planner.proposals = [_proposal()]
    args = argparse.Namespace(
        command="agent-step",
        objective="Close menu",
        timeout=None,
        approval_mode=ApprovalMode.SAFE,
        debug_dir=None,
        vision_model=None,
        planner_model=None,
        execute=True,
    )

    result = agent_cli.execute_agent_command(args, totp_provider=_TotpProvider())

    assert result.status is AgentFinalStatus.PAUSED
    assert result.history[0].verification is not None
    assert _Session.instance is not None
    assert _Session.instance.hid_calls == ["press_key Escape"]
    assert _Approval.confirmations == 1
