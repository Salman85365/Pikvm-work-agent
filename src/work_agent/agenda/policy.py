from __future__ import annotations

import re

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    DoubleClickElementAction,
    PolicyDecision,
    PolicyDecisionKind,
    PressKeyAction,
    RiskCategory,
    ScrollAction,
    TextPurpose,
    TypeTextAction,
    is_hid_action,
)
from work_agent.agent.policy import PolicyEngine
from work_agent.vision import SafetyWarning, ScreenAnalysis, UIElement, UIElementRole

# The two controls that make a calendar different from a read-only surface. Joining places the
# user into a live call with their microphone and camera; answering an invitation sends a reply
# on their behalf. Both are named explicitly rather than covered by a broad allowlist, so that
# ordinary navigation - a Dock icon, Teams' Calendar rail, an already-open browser tab - keeps
# working. Word boundaries keep "Accepted" and "Joining" from matching a control that is only
# reporting state.
_FORBIDDEN_CONTROL = re.compile(
    r"\b(join|accept|decline|tentative|rsvp|propose new time)\b",
    re.IGNORECASE,
)

# A blocking system dialog is walked around, not answered: the calendar application's own window
# is brought in front of it. Nothing inside it may ever be clicked - "Open Software Update" would
# start an OS update, and even "Later" or "Remind me" is answering a prompt this workflow was not
# asked to touch. Reaching the calendar never requires any of these.
_SYSTEM_DIALOG_CONTROL = re.compile(
    r"\b(software update|update|upgrade|install|restart|reboot|shut down|"
    r"later|remind me|not now|postpone|defer)\b",
    re.IGNORECASE,
)


class AgendaPolicyEngine(PolicyEngine):
    """Let the controller reach a calendar, and stop it short of acting on a meeting.

    Reading an agenda is safe; the hazards are narrow and specific. Unlike Slack triage, merely
    opening a meeting destroys nothing, so clicks are not banned wholesale - that would also deny
    the Dock icon and the Calendar tab, removing the feature while looking careful. What is denied
    is joining a call, answering an invitation, and editing event content.
    """

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        decision = super().evaluate(proposal, screen)
        action = proposal.action
        if not is_hid_action(action):
            return decision
        if decision.decision is PolicyDecisionKind.DENY:
            return decision

        if (
            isinstance(action, PressKeyAction)
            and action.key == "Escape"
            and SafetyWarning.UNEXPECTED_DIALOG in screen.warnings
        ):
            # Escape is how an overlay inside the calendar is cleared, but with a system dialog
            # on screen it would answer the dialog. Owned here per the calendar rules, whatever
            # the generic engine does.
            return PolicyDecision(
                decision=PolicyDecisionKind.DENY,
                reason=(
                    "A system dialog is on screen and Escape would answer it. Bring the "
                    "calendar's own window to the front instead."
                ),
                inferred_risk=RiskCategory.SYSTEM_CHANGE,
            )

        if isinstance(action, (ClickElementAction, DoubleClickElementAction)):
            element = _visible_element(action.element_id, screen)
            if element is not None and _FORBIDDEN_CONTROL.search(_text_of(element)):
                return PolicyDecision(
                    decision=PolicyDecisionKind.DENY,
                    reason=(
                        "That target joins a call or answers an invitation. Reading the agenda "
                        "never does either."
                    ),
                    inferred_risk=RiskCategory.EXTERNAL_COMMUNICATION,
                )
            if element is not None and (
                element.role is UIElementRole.DIALOG
                or _SYSTEM_DIALOG_CONTROL.search(_text_of(element))
            ):
                return PolicyDecision(
                    decision=PolicyDecisionKind.DENY,
                    reason=(
                        "That target is part of a system dialog. Bring the calendar's own window "
                        "to the front instead of answering it."
                    ),
                    inferred_risk=RiskCategory.SYSTEM_CHANGE,
                )

        if isinstance(action, TypeTextAction) and action.purpose in {
            TextPurpose.CONTENT_EDIT,
            TextPurpose.EXTERNAL_COMMUNICATION,
        }:
            return PolicyDecision(
                decision=PolicyDecisionKind.DENY,
                reason="Reading the agenda never edits an event or sends anything.",
                inferred_risk=RiskCategory.EXTERNAL_COMMUNICATION,
            )

        if decision.decision is not PolicyDecisionKind.REQUIRE_APPROVAL:
            return decision

        # Same relaxation the Slack workflows use: bringing an already-running application or an
        # already-open tab into view is navigation, not a consequential act.
        navigation = proposal.model_copy(update={"risk": RiskCategory.NAVIGATION})
        reconsidered = super().evaluate(navigation, screen)
        if reconsidered.decision is not PolicyDecisionKind.ALLOW:
            return decision
        return PolicyDecision(
            decision=PolicyDecisionKind.ALLOW,
            reason="Reaching an already-open calendar is ordinary navigation.",
            inferred_risk=RiskCategory.NAVIGATION,
        )


def _text_of(element: UIElement) -> str:
    return f"{element.label} {element.visible_text}"


def _visible_element(element_id: str, screen: ScreenAnalysis) -> UIElement | None:
    if screen.target is not None and screen.target.id == element_id:
        return screen.target
    return next(
        (element for element in screen.relevant_elements if element.id == element_id),
        None,
    )


class AgendaScrollPolicyEngine(AgendaPolicyEngine):
    """The scroll phase may only scroll the calendar that is already in front.

    Every other input is denied by construction, so the planner's general instinct to click a
    Dock icon around a dialog cannot bring a different application forward mid-read.
    """

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        decision = super().evaluate(proposal, screen)
        action = proposal.action
        if not is_hid_action(action) or isinstance(action, ScrollAction):
            return decision
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason="The calendar scroll phase only scrolls; finish instead of clicking or typing.",
            inferred_risk=RiskCategory.NAVIGATION,
        )
