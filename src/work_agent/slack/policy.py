from __future__ import annotations

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    HotkeyAction,
    PolicyDecision,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
    TextPurpose,
    TypeTextAction,
)
from work_agent.agent.policy import PolicyEngine
from work_agent.vision import ScreenAnalysis, UIElement, UIElementRole


class SlackAvailabilityPolicyEngine(PolicyEngine):
    """Permit only bounded Slack availability navigation when risk is overclassified."""

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        decision = super().evaluate(proposal, screen)
        if decision.decision is not PolicyDecisionKind.REQUIRE_APPROVAL:
            return decision
        if not self._is_scoped_navigation(proposal, screen):
            return decision

        navigation_proposal = proposal.model_copy(update={"risk": RiskCategory.NAVIGATION})
        reconsidered = super().evaluate(navigation_proposal, screen)
        if reconsidered.decision is not PolicyDecisionKind.ALLOW:
            return decision
        return PolicyDecision(
            decision=PolicyDecisionKind.ALLOW,
            reason="The action is bounded Slack availability navigation.",
            inferred_risk=RiskCategory.NAVIGATION,
        )

    @classmethod
    def _is_scoped_navigation(
        cls,
        proposal: ActionProposal,
        screen: ScreenAnalysis,
    ) -> bool:
        action = proposal.action
        if isinstance(action, ClickElementAction):
            element = cls._visible_element(action.element_id, screen)
            return element is not None and cls._is_navigation_element(element, screen)
        if isinstance(action, HotkeyAction):
            return tuple(action.keys) == ("MetaLeft", "Space")
        if isinstance(action, TypeTextAction):
            return (
                action.purpose is TextPurpose.NAVIGATION_SEARCH
                and action.text.strip().casefold() == "slack"
            )
        return isinstance(action, PressKeyAction) and action.key == "Enter"

    @staticmethod
    def _visible_element(element_id: str, screen: ScreenAnalysis) -> UIElement | None:
        if screen.target is not None and screen.target.id == element_id:
            return screen.target
        return next(
            (element for element in screen.relevant_elements if element.id == element_id),
            None,
        )

    @staticmethod
    def _is_navigation_element(element: UIElement, screen: ScreenAnalysis) -> bool:
        if element.click_point is None or element.confidence < 0.8:
            return False
        label = f"{element.label} {element.visible_text}".strip().casefold()
        forbidden = ("edit", "send", "message", "set status", "clear status")
        if any(term in label for term in forbidden):
            return False
        if element.role in {
            UIElementRole.ICON,
            UIElementRole.BUTTON,
            UIElementRole.LIST_ITEM,
        } and element.label.strip().casefold() in {"slack", "slack app", "slack application"}:
            return True
        return (
            screen.application.strip().casefold() == "slack"
            and element.role in {UIElementRole.ICON, UIElementRole.BUTTON, UIElementRole.MENU}
            and any(term in label for term in ("profile", "avatar"))
        )
