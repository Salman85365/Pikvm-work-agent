from __future__ import annotations

from work_agent.agent.executor import ActionExecutor
from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    ExecutionTransportStatus,
    PressKeyAction,
    RiskCategory,
    TextPurpose,
    TypeTextAction,
    zero_usage,
)
from work_agent.pikvm import PiKVMTimeoutError, ScreenSize
from work_agent.vision import (
    BoundingBox,
    ImageDetail,
    NormalizedPoint,
    ReasoningEffort,
    ScreenAnalysis,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
)


class _Client:
    def __init__(self, *, fail_key: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.fail_key = fail_key

    def press_key(self, key: str) -> None:
        self.calls.append(("press_key", (key,), {}))
        if self.fail_key:
            raise PiKVMTimeoutError("outcome uncertain")

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", keys, {}))

    def type_text(self, text: str, *, keymap: str | None = None, delay: float = 0.0) -> None:
        self.calls.append(("type_text", (text,), {"keymap": keymap, "delay": delay}))

    def move_mouse(self, x: int, y: int, *, screen_size: ScreenSize) -> None:
        self.calls.append(("move_mouse", (x, y), {"screen_size": screen_size}))

    def click(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("click", args, kwargs))

    def double_click(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("double_click", args, kwargs))

    def scroll(self, delta_y: int, *, delta_x: int = 0) -> None:
        self.calls.append(("scroll", (delta_y,), {"delta_x": delta_x}))


def _screen(*, click_point: NormalizedPoint | None = None) -> ScreenAnalysis:
    element = UIElement(
        id="profile",
        label="Profile",
        role=UIElementRole.BUTTON,
        visible_text="",
        bounding_box=BoundingBox(x1=900, y1=800, x2=950, y2=900),
        click_point=click_point,
        confidence=0.95,
    )
    return ScreenAnalysis(
        objective="Open profile",
        application="Slack",
        screen_state=ScreenState.APPLICATION,
        summary="Slack is visible.",
        target_found=True,
        target=element,
        relevant_elements=[element],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.95,
        screenshot_width=1920,
        screenshot_height=1080,
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


def _proposal(action: object) -> ActionProposal:
    return ActionProposal.model_validate(
        {
            "action": action,
            "expected_outcome": "Visible state changes.",
            "confidence": 0.95,
            "risk": RiskCategory.NAVIGATION,
            "reason_summary": "Navigate.",
        }
    )


def test_click_element_resolves_current_normalized_point_once() -> None:
    client = _Client()
    executor = ActionExecutor(client)
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="profile", button="left")
    )

    result = executor.execute(proposal, _screen(click_point=NormalizedPoint(x=500, y=500)))

    assert result.transport_status is ExecutionTransportStatus.SENT
    assert len(client.calls) == 1
    name, args, kwargs = client.calls[0]
    assert name == "click"
    assert args == (960, 540)
    assert kwargs["screen_size"] == ScreenSize(1920, 1080)


def test_missing_click_point_fails_without_hid() -> None:
    client = _Client()
    executor = ActionExecutor(client)
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="profile", button="left")
    )

    result = executor.execute(proposal, _screen(click_point=None))

    assert result.transport_status is ExecutionTransportStatus.FAILED
    assert client.calls == []


def test_ambiguous_hid_failure_is_not_retried() -> None:
    client = _Client(fail_key=True)
    executor = ActionExecutor(client)
    proposal = _proposal(PressKeyAction(type="press_key", key="Escape"))

    result = executor.execute(proposal, _screen())

    assert result.transport_status is ExecutionTransportStatus.UNCERTAIN
    assert client.calls == [("press_key", ("Escape",), {})]


def test_type_text_does_not_append_enter_or_echo_text_in_result() -> None:
    client = _Client()
    executor = ActionExecutor(client)
    sensitive = "draft content"
    proposal = _proposal(
        TypeTextAction(
            type="type_text",
            text=sensitive,
            purpose=TextPurpose.LOCAL_INPUT,
        )
    )

    result = executor.execute(proposal, _screen())

    assert result.transport_status is ExecutionTransportStatus.SENT
    assert client.calls == [("type_text", (sensitive,), {"keymap": None, "delay": 0.0})]
    assert result.sanitized_error is None
