from __future__ import annotations

import pytest

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    PolicyDecisionKind,
    RiskCategory,
    zero_usage,
)
from work_agent.slack.policy import SlackAvailabilityPolicyEngine
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


def _element(label: str, role: UIElementRole) -> UIElement:
    return UIElement(
        id="target",
        label=label,
        role=role,
        visible_text="",
        bounding_box=BoundingBox(x1=100, y1=100, x2=200, y2=200),
        click_point=NormalizedPoint(x=150, y=150),
        confidence=0.98,
    )


def _screen(element: UIElement, *, application: str) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Manage Slack manual availability",
        application=application,
        screen_state=ScreenState.APPLICATION,
        summary="A normal application window is visible.",
        target_found=True,
        target=element,
        relevant_elements=[element],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.98,
        screenshot_width=1920,
        screenshot_height=1080,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.HIGH,
        reasoning_effort=ReasoningEffort.LOW,
        usage=zero_usage(),
        latency_seconds=0,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


def _proposal(risk: RiskCategory) -> ActionProposal:
    return ActionProposal(
        action=ClickElementAction(type="click_element", element_id="target", button="left"),
        expected_outcome="The next Slack navigation state becomes visible.",
        confidence=0.98,
        risk=risk,
        reason_summary="Navigate Slack.",
    )


@pytest.mark.parametrize("risk", [RiskCategory.LOCAL_EDIT, RiskCategory.UNKNOWN])
def test_slack_icon_navigation_is_allowed_when_planner_overclassifies_risk(
    risk: RiskCategory,
) -> None:
    screen = _screen(_element("Slack", UIElementRole.ICON), application="Cursor")

    decision = SlackAvailabilityPolicyEngine().evaluate(_proposal(risk), screen)

    assert decision.decision is PolicyDecisionKind.ALLOW


def test_slack_profile_navigation_is_allowed_when_planner_overclassifies_risk() -> None:
    screen = _screen(
        _element("Slack profile avatar", UIElementRole.BUTTON),
        application="Slack",
    )

    decision = SlackAvailabilityPolicyEngine().evaluate(
        _proposal(RiskCategory.LOCAL_EDIT),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.ALLOW


@pytest.mark.parametrize("label", ["Send message", "Edit profile", "Set status"])
def test_other_slack_edits_remain_approval_gated(label: str) -> None:
    screen = _screen(_element(label, UIElementRole.BUTTON), application="Slack")

    decision = SlackAvailabilityPolicyEngine().evaluate(
        _proposal(RiskCategory.LOCAL_EDIT),
        screen,
    )

    assert decision.decision is PolicyDecisionKind.REQUIRE_APPROVAL
