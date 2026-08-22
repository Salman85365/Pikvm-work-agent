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
from work_agent.slack.state import normalize_label
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
        # A generic DENY (deny terms, dialog rule, auth screen) still wins. A generic
        # "needs approval" for an unknown element role does not: the scoped-navigation check
        # above already established what the element is.
        if reconsidered.decision is PolicyDecisionKind.DENY:
            return reconsidered
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
        if element.click_point is None or element.confidence < 0.7:
            return False
        launcher_roles = {
            UIElementRole.ICON,
            UIElementRole.BUTTON,
            UIElementRole.LIST_ITEM,
            UIElementRole.UNKNOWN,
        }
        # A Dock/taskbar/launcher entry for Slack, however the model decorated it: "Slack",
        # "Slack (2 unread)", "Slack app icon in Dock". Requiring the exact word "slack" alone
        # denied the very click that brings Slack forward. Only applies while Slack is not
        # already the foreground application, so it can never match an in-app control.
        words = normalize_label(element.label).split()
        if (
            "slack" not in screen.application.strip().casefold()
            and element.role in launcher_roles
            and words
            and words[0] == "slack"
            and len(words) <= 8
        ):
            return True
        label = normalize_label(f"{element.label} {element.visible_text}")
        forbidden = ("edit", "send", "message", "set status", "clear status", "compose")
        if any(term in label for term in forbidden):
            return False
        return (
            "slack" in screen.application.strip().casefold()
            and element.role in {*launcher_roles, UIElementRole.MENU}
            and any(term in label for term in ("profile", "avatar", "account"))
        )
