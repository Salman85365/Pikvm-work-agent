from __future__ import annotations

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    DoubleClickElementAction,
    PolicyDecision,
    PolicyDecisionKind,
    RiskCategory,
    is_hid_action,
)
from work_agent.agent.policy import PolicyEngine
from work_agent.vision import ScreenAnalysis, UIElement


class SlackTriagePolicyEngine(PolicyEngine):
    """Keep triage from opening a conversation, without second-guessing ordinary navigation.

    The rule is positional rather than label-based: input is permitted only while Slack is not
    yet the foreground application, and once Slack is in front there is nothing left to click.
    Matching launcher labels was tried and rejected - a Dock icon routinely carries an unread
    badge, so requiring an exact "Slack" label with no other visible text denied the very click
    that brings Slack forward.

    A leading "#" is still refused, because that unambiguously names a channel and no launcher
    looks like one. Everything else defers to the generic engine.
    """

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        decision = super().evaluate(proposal, screen)
        action = proposal.action
        if not is_hid_action(action):
            return decision
        if decision.decision is PolicyDecisionKind.DENY:
            return decision

        if _slack_is_foreground(screen):
            return PolicyDecision(
                decision=PolicyDecisionKind.DENY,
                reason=(
                    "Slack is already in front, so triage has nothing left to click. Selecting a "
                    "conversation would mark it read."
                ),
                inferred_risk=RiskCategory.LOCAL_EDIT,
            )

        if isinstance(action, (ClickElementAction, DoubleClickElementAction)):
            element = _visible_element(action.element_id, screen)
            if element is not None and element.label.strip().startswith("#"):
                return PolicyDecision(
                    decision=PolicyDecisionKind.DENY,
                    reason="That target names a Slack channel; opening it would mark it read.",
                    inferred_risk=RiskCategory.LOCAL_EDIT,
                )

        if decision.decision is not PolicyDecisionKind.REQUIRE_APPROVAL:
            return decision

        # Same relaxation the availability workflow uses: opening an application is navigation.
        navigation = proposal.model_copy(update={"risk": RiskCategory.NAVIGATION})
        reconsidered = super().evaluate(navigation, screen)
        if reconsidered.decision is not PolicyDecisionKind.ALLOW:
            return decision
        return PolicyDecision(
            decision=PolicyDecisionKind.ALLOW,
            reason="Bringing Slack to the foreground is ordinary navigation.",
            inferred_risk=RiskCategory.NAVIGATION,
        )


def _slack_is_foreground(screen: ScreenAnalysis) -> bool:
    return "slack" in screen.application.casefold()


def _visible_element(element_id: str, screen: ScreenAnalysis) -> UIElement | None:
    if screen.target is not None and screen.target.id == element_id:
        return screen.target
    return next(
        (element for element in screen.relevant_elements if element.id == element_id),
        None,
    )
