from __future__ import annotations

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    FinishAction,
    HotkeyAction,
    MoveMouseAction,
    PolicyDecision,
    PolicyDecisionKind,
    PressKeyAction,
    RequestUserAction,
    RiskCategory,
    TextPurpose,
    TypeTextAction,
    WaitAction,
)
from work_agent.agent.policy import PolicyEngine
from work_agent.vision import ScreenAnalysis, UIElement, UIElementRole

# Only an application launcher, never a conversation. Matched exactly: a sidebar entry named
# "Slack" would otherwise be indistinguishable from the Dock icon.
_LAUNCHER_LABELS = frozenset({"slack", "slack app", "slack application"})
_LAUNCHER_ROLES = frozenset({UIElementRole.ICON, UIElementRole.BUTTON, UIElementRole.LIST_ITEM})
_ALLOWED_KEYS = frozenset({"Enter", "Escape"})


class SlackTriagePolicyEngine(PolicyEngine):
    """Allowlist-only policy: triage may bring Slack forward and nothing else.

    Opening a conversation marks it read, which cannot be reliably undone and destroys the
    unread state triage exists to report. That boundary is enforced here rather than requested
    in the prompt, because model output is untrusted input.
    """

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        action = proposal.action
        base = super().evaluate(proposal, screen)

        if isinstance(action, (FinishAction, RequestUserAction, WaitAction)):
            return base
        # A conservative stop from the generic engine is never overridden.
        if base.decision is PolicyDecisionKind.DENY:
            return base

        if not self._is_foregrounding_action(action, screen):
            return PolicyDecision(
                decision=PolicyDecisionKind.DENY,
                reason=(
                    "Slack triage may only bring Slack to the foreground. Opening or selecting a "
                    "conversation would mark it read."
                ),
                inferred_risk=RiskCategory.LOCAL_EDIT,
            )
        if base.decision is PolicyDecisionKind.ALLOW:
            return base

        navigation = proposal.model_copy(update={"risk": RiskCategory.NAVIGATION})
        reconsidered = super().evaluate(navigation, screen)
        if reconsidered.decision is not PolicyDecisionKind.ALLOW:
            return base
        return PolicyDecision(
            decision=PolicyDecisionKind.ALLOW,
            reason="The action only brings Slack to the foreground.",
            inferred_risk=RiskCategory.NAVIGATION,
        )

    @classmethod
    def _is_foregrounding_action(cls, action: object, screen: ScreenAnalysis) -> bool:
        if isinstance(action, MoveMouseAction):
            return True
        if isinstance(action, PressKeyAction):
            return action.key in _ALLOWED_KEYS
        if isinstance(action, HotkeyAction):
            return tuple(action.keys) == ("MetaLeft", "Space")
        if isinstance(action, TypeTextAction):
            return (
                action.purpose is TextPurpose.NAVIGATION_SEARCH
                and action.text.strip().casefold() == "slack"
            )
        if isinstance(action, ClickElementAction):
            element = cls._visible_element(action.element_id, screen)
            return element is not None and cls._is_launcher(element)
        # Double-click, scroll, and anything else are outside the workflow's needs.
        return False

    @staticmethod
    def _visible_element(element_id: str, screen: ScreenAnalysis) -> UIElement | None:
        if screen.target is not None and screen.target.id == element_id:
            return screen.target
        return next(
            (element for element in screen.relevant_elements if element.id == element_id),
            None,
        )

    @staticmethod
    def _is_launcher(element: UIElement) -> bool:
        if element.click_point is None or element.confidence < 0.8:
            return False
        if element.role not in _LAUNCHER_ROLES:
            return False
        label = element.label.strip().casefold()
        visible = element.visible_text.strip().casefold()
        if label not in _LAUNCHER_LABELS:
            return False
        # A sidebar row usually carries extra visible text (a badge, a preview, a channel hash).
        return visible in _LAUNCHER_LABELS or not visible
