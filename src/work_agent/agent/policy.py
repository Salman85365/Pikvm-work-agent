from __future__ import annotations

import re

from work_agent.agent.models import (
    ActionProposal,
    ClickElementAction,
    DoubleClickElementAction,
    FinishAction,
    HotkeyAction,
    MoveMouseAction,
    PolicyDecision,
    PolicyDecisionKind,
    PressKeyAction,
    RequestUserAction,
    RiskCategory,
    ScrollAction,
    TextPurpose,
    TypeTextAction,
    WaitAction,
)
from work_agent.vision.models import (
    SafetyWarning,
    ScreenAnalysis,
    ScreenState,
    UIElement,
    UIElementRole,
)

_APPROVAL_TERMS = frozenset(
    {
        "send",
        "submit",
        "delete",
        "remove",
        "discard",
        "merge",
        "push",
        "approve",
        "confirm",
        "purchase",
        "pay",
        "save",
    }
)
_DENY_TERMS = frozenset(
    {
        "shutdown",
        "shut down",
        "restart",
        "reboot",
        "format",
        "disable protection",
        "turn off protection",
    }
)
_SAFE_HOTKEYS = frozenset(
    {
        ("ControlLeft", "KeyL"),
        ("ControlLeft", "KeyF"),
        ("ControlLeft", "Tab"),
        ("ControlLeft", "ShiftLeft", "Tab"),
        ("AltLeft", "Tab"),
        ("MetaLeft", "Tab"),
        ("MetaLeft", "Space"),
    }
)
_SAFE_KEYS = frozenset(
    {
        "Escape",
        "Tab",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "MetaLeft",
    }
)
_DENIED_KEYS = frozenset({"Power", "Sleep", "WakeUp"})
_DENIED_HOTKEYS = frozenset(
    {
        ("ControlLeft", "AltLeft", "Delete"),
        ("ControlLeft", "AltLeft", "End"),
        ("MetaLeft", "KeyL"),
    }
)
_STOP_WARNINGS = frozenset(
    {
        SafetyWarning.AUTHENTICATION_PROMPT,
        SafetyWarning.LOCK_SCREEN,
        SafetyWarning.UNEXPECTED_DIALOG,
        SafetyWarning.DESTRUCTIVE_CONFIRMATION,
        SafetyWarning.REMOTE_DISCONNECT,
        SafetyWarning.LOW_CONFIDENCE,
        SafetyWarning.UNKNOWN_STATE,
    }
)


class PolicyEngine:
    """Conservative local policy that never trusts planner-declared risk alone."""

    def evaluate(self, proposal: ActionProposal, screen: ScreenAnalysis) -> PolicyDecision:
        action = proposal.action
        if isinstance(action, (FinishAction, RequestUserAction)):
            return self._allow("No HID action is requested.", RiskCategory.READ_ONLY)
        if isinstance(action, WaitAction):
            return self._allow(
                "A bounded local wait has no remote side effect.", RiskCategory.READ_ONLY
            )

        if screen.screen_state in {ScreenState.AUTHENTICATION, ScreenState.LOCK_SCREEN}:
            return self._deny(
                "Remote authentication and lock screens require the user.",
                RiskCategory.AUTHENTICATION,
            )
        if not screen.safe_to_continue or any(
            warning in _STOP_WARNINGS for warning in screen.warnings
        ):
            return self._deny(
                "The current screen analysis requires a conservative stop.",
                RiskCategory.UNKNOWN,
            )

        if proposal.risk in {RiskCategory.DESTRUCTIVE, RiskCategory.SYSTEM_CHANGE}:
            return self._deny(
                "Destructive and system-changing actions are denied in the generic controller.",
                proposal.risk,
            )
        if proposal.risk is RiskCategory.AUTHENTICATION:
            return self._deny(
                "The generic controller cannot enter authentication information.",
                RiskCategory.AUTHENTICATION,
            )
        if isinstance(action, ClickElementAction) and self._slack_availability_toggle(
            action.element_id,
            screen,
        ):
            return self._allow(
                "The visible target is Slack's bounded manual Active/Away toggle.",
                RiskCategory.LOCAL_EDIT,
            )
        if proposal.risk in {
            RiskCategory.EXTERNAL_COMMUNICATION,
            RiskCategory.LOCAL_EDIT,
            RiskCategory.UNKNOWN,
        }:
            return self._approval(
                "The planner-declared effect is consequential or unknown.",
                proposal.risk,
            )

        if isinstance(action, TypeTextAction):
            return self._type_text(action, screen)
        if isinstance(action, PressKeyAction):
            return self._press_key(action, screen)
        if isinstance(action, HotkeyAction):
            if tuple(action.keys) in _DENIED_HOTKEYS:
                return self._deny(
                    "This hotkey can enter a system or lock-screen workflow.",
                    RiskCategory.SYSTEM_CHANGE,
                )
            if tuple(action.keys) not in _SAFE_HOTKEYS:
                return self._approval(
                    "This hotkey is not on the local navigation allowlist.",
                    RiskCategory.UNKNOWN,
                )
            return self._allow("The hotkey is on the local navigation allowlist.")
        if isinstance(action, (MoveMouseAction, ScrollAction)):
            return self._allow("Pointer movement and bounded scrolling are non-consequential.")
        if isinstance(action, (ClickElementAction, DoubleClickElementAction)):
            return self._element_action(action.element_id, screen)
        return self._deny("The action type is outside the policy vocabulary.", RiskCategory.UNKNOWN)

    def _type_text(self, action: TypeTextAction, screen: ScreenAnalysis) -> PolicyDecision:
        context = f"{screen.application} {screen.summary}".lower()
        if self._terminal_context(context):
            return self._deny(
                "AI-generated terminal or shell input is denied in Milestone 4.",
                RiskCategory.SYSTEM_CHANGE,
            )
        if action.purpose is TextPurpose.AUTHENTICATION or self._looks_sensitive(action.text):
            return self._deny(
                "Authentication or secret-like text cannot be typed by the generic controller.",
                RiskCategory.AUTHENTICATION,
            )
        if action.purpose is TextPurpose.NAVIGATION_SEARCH and self._known_search_field(screen):
            return self._allow("Text is limited to a visible navigation/search field.")
        if action.purpose is TextPurpose.EXTERNAL_COMMUNICATION:
            return self._approval(
                "Typing external communication requires explicit approval.",
                RiskCategory.EXTERNAL_COMMUNICATION,
            )
        if action.purpose in {
            TextPurpose.LOCAL_INPUT,
            TextPurpose.CONTENT_EDIT,
            TextPurpose.UNKNOWN,
        }:
            return self._approval(
                "The effect of this text entry is not limited to known navigation.",
                RiskCategory.LOCAL_EDIT,
            )
        return self._deny("The text purpose could not be classified.", RiskCategory.UNKNOWN)

    def _press_key(self, action: PressKeyAction, screen: ScreenAnalysis) -> PolicyDecision:
        if action.key in _DENIED_KEYS:
            return self._deny(
                "Power and system-state keys are denied in the generic controller.",
                RiskCategory.SYSTEM_CHANGE,
            )
        if action.key == "Enter":
            context = " ".join(
                [
                    screen.application,
                    screen.summary,
                    *(
                        f"{element.label} {element.visible_text}"
                        for element in screen.relevant_elements
                    ),
                ]
            ).lower()
            if self._contains_terms(context, _DENY_TERMS):
                return self._deny(
                    "Enter could confirm a denied system or destructive action.",
                    RiskCategory.DESTRUCTIVE,
                )
            if self._contains_terms(context, _APPROVAL_TERMS) or any(
                term in context for term in ("message composer", "message field", "reply")
            ):
                return self._approval(
                    "Enter may submit content in the current context.",
                    RiskCategory.EXTERNAL_COMMUNICATION,
                )
            if self._known_search_field(screen):
                return self._allow("Enter is limited to a visible navigation/search context.")
            return self._approval(
                "Enter may submit or confirm in an unclassified context.",
                RiskCategory.UNKNOWN,
            )
        if action.key in _SAFE_KEYS:
            return self._allow("The key is on the local navigation allowlist.")
        return self._approval(
            "This key is not on the local navigation allowlist.",
            RiskCategory.UNKNOWN,
        )

    def _element_action(self, element_id: str, screen: ScreenAnalysis) -> PolicyDecision:
        element = self._element(screen, element_id)
        if element is None:
            return self._deny(
                "The requested element is absent from the current analysis.",
                RiskCategory.UNKNOWN,
            )
        text = f"{element.label} {element.visible_text}".lower()
        if self._contains_terms(text, _DENY_TERMS):
            return self._deny(
                "The target label indicates a denied system or destructive action.",
                RiskCategory.DESTRUCTIVE,
            )
        if self._contains_terms(text, _APPROVAL_TERMS):
            return self._approval(
                "The target label may cause a consequential external action.",
                RiskCategory.UNKNOWN,
            )
        if element.role is UIElementRole.UNKNOWN:
            return self._approval(
                "The target role is unknown.",
                RiskCategory.UNKNOWN,
            )
        return self._allow("The current target is an ordinary navigation element.")

    @classmethod
    def _slack_availability_toggle(cls, element_id: str, screen: ScreenAnalysis) -> bool:
        if screen.application.strip().lower() != "slack":
            return False
        element = cls._element(screen, element_id)
        if element is None or element.role not in {UIElementRole.MENU_ITEM, UIElementRole.BUTTON}:
            return False
        allowed = {"set yourself as active", "set yourself as away"}
        visible_values = {
            value.strip().lower()
            for value in (element.label, element.visible_text)
            if value.strip()
        }
        return len(visible_values) == 1 and visible_values.issubset(allowed)

    @staticmethod
    def _element(screen: ScreenAnalysis, element_id: str) -> UIElement | None:
        if screen.target is not None and screen.target.id == element_id:
            return screen.target
        return next(
            (element for element in screen.relevant_elements if element.id == element_id),
            None,
        )

    @staticmethod
    def _known_search_field(screen: ScreenAnalysis) -> bool:
        for element in screen.relevant_elements:
            if element.role is not UIElementRole.TEXT_FIELD:
                continue
            text = f"{element.label} {element.visible_text}".lower()
            if any(term in text for term in ("search", "find", "address")):
                return True
        return any(term in screen.summary.lower() for term in ("search field", "search box"))

    @staticmethod
    def _terminal_context(context: str) -> bool:
        return any(
            term in context
            for term in ("terminal", "powershell", "command prompt", "cmd.exe", "shell")
        )

    @staticmethod
    def _looks_sensitive(text: str) -> bool:
        stripped = text.strip()
        if re.fullmatch(r"\d{6}", stripped):
            return True
        lowered = stripped.lower()
        return lowered.startswith(("sk-", "ghp_", "github_pat_", "xoxb-", "xoxp-"))

    @staticmethod
    def _contains_terms(text: str, terms: frozenset[str]) -> bool:
        return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)

    @staticmethod
    def _allow(
        reason: str,
        risk: RiskCategory = RiskCategory.NAVIGATION,
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyDecisionKind.ALLOW,
            reason=reason,
            inferred_risk=risk,
        )

    @staticmethod
    def _approval(reason: str, risk: RiskCategory) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyDecisionKind.REQUIRE_APPROVAL,
            reason=reason,
            inferred_risk=risk,
        )

    @staticmethod
    def _deny(reason: str, risk: RiskCategory) -> PolicyDecision:
        return PolicyDecision(
            decision=PolicyDecisionKind.DENY,
            reason=reason,
            inferred_risk=risk,
        )
