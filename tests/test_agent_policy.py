from __future__ import annotations

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    HotkeyAction,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
    TextPurpose,
    TypeTextAction,
    zero_usage,
)
from work_agent.agent.policy import PolicyEngine
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


def _element(
    label: str,
    *,
    element_id: str = "target",
    role: UIElementRole = UIElementRole.BUTTON,
) -> UIElement:
    return UIElement(
        id=element_id,
        label=label,
        role=role,
        visible_text=label,
        bounding_box=BoundingBox(x1=100, y1=100, x2=200, y2=200),
        click_point=NormalizedPoint(x=150, y=150),
        confidence=0.95,
    )


def _screen(
    *,
    element: UIElement | None = None,
    state: ScreenState = ScreenState.APPLICATION,
    safe: bool = True,
    summary: str = "Application window is visible.",
    application: str = "Browser",
) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Navigate",
        application=application,
        screen_state=state,
        summary=summary,
        target_found=element is not None,
        target=element,
        relevant_elements=[] if element is None else [element],
        warnings=[],
        safe_to_continue=safe,
        stop_reason=None if safe else "Review required.",
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


def _proposal(action: object, risk: RiskCategory = RiskCategory.NAVIGATION) -> ActionProposal:
    return ActionProposal.model_validate(
        {
            "action": action,
            "expected_outcome": "The screen changes.",
            "confidence": 0.95,
            "risk": risk,
            "reason_summary": "Navigate.",
        }
    )


def test_ordinary_navigation_click_is_allowed() -> None:
    screen = _screen(element=_element("Profile menu"))
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="target", button="left")
    )

    decision = PolicyEngine().evaluate(proposal, screen)

    assert decision.decision is PolicyDecisionKind.ALLOW


def test_consequential_target_requires_approval() -> None:
    screen = _screen(element=_element("Send message"))
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="target", button="left")
    )

    decision = PolicyEngine().evaluate(proposal, screen)

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL


def test_visible_slack_availability_toggle_is_the_only_local_edit_click_allowed() -> None:
    toggle = _screen(
        element=_element("Set yourself as away", role=UIElementRole.MENU_ITEM),
        application="Slack",
    )
    other_slack_edit = _screen(
        element=_element("Clear status", role=UIElementRole.MENU_ITEM),
        application="Slack",
    )
    ambiguous_toggle = _screen(
        element=UIElement(
            id="target",
            label="Set yourself as away",
            role=UIElementRole.MENU_ITEM,
            visible_text="Set yourself as active",
            bounding_box=BoundingBox(x1=100, y1=100, x2=200, y2=200),
            click_point=NormalizedPoint(x=150, y=150),
            confidence=0.95,
        ),
        application="Slack",
    )
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="target", button="left"),
        RiskCategory.LOCAL_EDIT,
    )

    assert PolicyEngine().evaluate(proposal, toggle).decision is PolicyDecisionKind.ALLOW
    assert (
        PolicyEngine().evaluate(proposal, other_slack_edit).decision
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )
    assert (
        PolicyEngine().evaluate(proposal, ambiguous_toggle).decision
        is PolicyDecisionKind.REQUIRE_APPROVAL
    )


def test_power_target_is_denied() -> None:
    screen = _screen(element=_element("Restart computer"))
    proposal = _proposal(
        ClickElementAction(type="click_element", element_id="target", button="left")
    )

    decision = PolicyEngine().evaluate(proposal, screen)

    assert decision.decision is PolicyDecisionKind.DENY


def test_external_communication_and_unknown_risk_are_conservative() -> None:
    screen = _screen()
    external = _proposal(
        TypeTextAction(
            type="type_text",
            text="hello",
            purpose=TextPurpose.EXTERNAL_COMMUNICATION,
        ),
        RiskCategory.EXTERNAL_COMMUNICATION,
    )
    unknown = _proposal(PressKeyAction(type="press_key", key="KeyA"), RiskCategory.UNKNOWN)

    assert PolicyEngine().evaluate(external, screen).decision is PolicyDecisionKind.REQUIRE_APPROVAL
    assert PolicyEngine().evaluate(unknown, screen).decision is PolicyDecisionKind.REQUIRE_APPROVAL


def test_authentication_screen_and_secret_text_are_denied() -> None:
    auth_screen = _screen(state=ScreenState.AUTHENTICATION)
    proposal = _proposal(PressKeyAction(type="press_key", key="Tab"))
    secret = _proposal(
        TypeTextAction(
            type="type_text",
            text="123456",
            purpose=TextPurpose.NAVIGATION_SEARCH,
        )
    )

    assert PolicyEngine().evaluate(proposal, auth_screen).decision is PolicyDecisionKind.DENY
    assert PolicyEngine().evaluate(secret, _screen()).decision is PolicyDecisionKind.DENY


def test_enter_is_allowed_for_search_but_requires_approval_for_message() -> None:
    enter = _proposal(PressKeyAction(type="press_key", key="Enter"))
    search = _screen(
        element=_element("Search", role=UIElementRole.TEXT_FIELD),
        summary="A search field is active.",
    )
    message = _screen(summary="Slack message composer is focused.")

    assert PolicyEngine().evaluate(enter, search).decision is PolicyDecisionKind.ALLOW
    assert PolicyEngine().evaluate(enter, message).decision is PolicyDecisionKind.REQUIRE_APPROVAL


def test_non_allowlisted_save_hotkey_requires_approval() -> None:
    proposal = _proposal(
        HotkeyAction(type="hotkey", keys=["MetaLeft", "KeyS"]),
    )

    decision = PolicyEngine().evaluate(proposal, _screen())

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL


def test_power_key_and_lock_hotkey_are_denied() -> None:
    power = _proposal(PressKeyAction(type="press_key", key="Power"))
    lock = _proposal(HotkeyAction(type="hotkey", keys=["MetaLeft", "KeyL"]))

    assert PolicyEngine().evaluate(power, _screen()).decision is PolicyDecisionKind.DENY
    assert PolicyEngine().evaluate(lock, _screen()).decision is PolicyDecisionKind.DENY


def test_terminal_typing_is_denied_even_when_planner_calls_it_navigation() -> None:
    screen = _screen(summary="A PowerShell terminal window is active.")
    proposal = _proposal(
        TypeTextAction(
            type="type_text",
            text="dir",
            purpose=TextPurpose.NAVIGATION_SEARCH,
        )
    )

    decision = PolicyEngine().evaluate(proposal, screen)

    assert decision.decision is PolicyDecisionKind.DENY
