from __future__ import annotations

import re

from work_agent.slack.models import Availability
from work_agent.vision import (
    ActionVerification,
    ScreenAnalysis,
    UIElementRole,
    VerificationStatus,
)

_ACTIVE_TOGGLE_PHRASES = (
    "set yourself as away",
    "set yourself away",
    "set as away",
)
_AWAY_TOGGLE_PHRASES = (
    "set yourself as active",
    "set yourself active",
    "set as active",
)
_AVAILABILITY_TOGGLE_ID_MARKERS = (
    "availability_toggle",
    "manual_availability",
    "set_yourself_as_active",
    "set_yourself_as_away",
    "active_toggle",
    "away_toggle",
)
_PROFILE_NAVIGATION_ID_MARKERS = (
    "profile",
    "avatar",
    "account_menu",
    "account_button",
)
_MANUAL_TOGGLE_PHRASES = _ACTIVE_TOGGLE_PHRASES + _AWAY_TOGGLE_PHRASES
# Words that would make a "set yourself as ..." match something other than the bare toggle.
_TOGGLE_FORBIDDEN_WORDS = ("send", "message", "status", "edit", "notification", "pause")
_LABEL_NOISE = re.compile(r"[^a-z0-9 ]+")


def normalize_label(text: str) -> str:
    """Case-, punctuation-, and whitespace-insensitive form of a model-reported label.

    Vision models decorate labels ("Set yourself as away (menu item)", "quoted Set yourself as
    active", "Set yourself as away..."); a boundary that demanded exact equality denied the very
    toggle the workflow exists to press.
    """

    return " ".join(_LABEL_NOISE.sub(" ", text.casefold()).split())


def manual_toggle_target(text: str) -> Availability | None:
    """Which availability a manual-toggle label would *select*, or None if it is not one.

    "Set yourself as away" selects AWAY (and therefore proves the current state is ACTIVE).
    """

    normalized = normalize_label(text)
    if not normalized or len(normalized) > 60:
        return None
    if any(word in normalized.split() for word in _TOGGLE_FORBIDDEN_WORDS):
        return None
    selects_away = any(phrase in normalized for phrase in _ACTIVE_TOGGLE_PHRASES)
    selects_active = any(phrase in normalized for phrase in _AWAY_TOGGLE_PHRASES)
    if selects_away == selects_active:
        return None
    return Availability.AWAY if selects_away else Availability.ACTIVE


def is_manual_toggle_label(text: str) -> bool:
    return manual_toggle_target(text) is not None


def is_availability_toggle_reference(value: str) -> bool:
    normalized = value.strip().casefold().replace("-", "_")
    return any(marker in normalized for marker in _AVAILABILITY_TOGGLE_ID_MARKERS)


def is_profile_navigation_reference(value: str) -> bool:
    normalized = value.strip().casefold().replace("-", "_")
    return not is_availability_toggle_reference(normalized) and any(
        marker in normalized for marker in _PROFILE_NAVIGATION_ID_MARKERS
    )


def is_profile_menu_outcome_reference(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    return any(phrase in normalized for phrase in ("profile menu", "account menu"))


def has_visible_manual_availability_control(analysis: ScreenAnalysis) -> bool:
    elements = list(analysis.relevant_elements)
    if analysis.target is not None and all(
        element.id != analysis.target.id for element in elements
    ):
        elements.append(analysis.target)
    for element in elements:
        if element.role not in {
            UIElementRole.MENU_ITEM,
            UIElementRole.BUTTON,
            UIElementRole.UNKNOWN,
        }:
            continue
        if element.confidence < 0.7:
            continue
        if is_manual_toggle_label(element.label) or is_manual_toggle_label(element.visible_text):
            return True
    return False


def infer_slack_availability(analysis: ScreenAnalysis) -> Availability | None:
    elements = list(analysis.relevant_elements)
    if analysis.target is not None and all(
        element.id != analysis.target.id for element in elements
    ):
        elements.append(analysis.target)
    text = " ".join(
        normalize_label(f"{element.label} {element.visible_text}") for element in elements
    )
    active_evidence = any(phrase in text for phrase in _ACTIVE_TOGGLE_PHRASES)
    away_evidence = any(phrase in text for phrase in _AWAY_TOGGLE_PHRASES)
    if active_evidence == away_evidence:
        return None
    return Availability.ACTIVE if active_evidence else Availability.AWAY


class AvailabilityTracker:
    def __init__(self, desired: Availability | None) -> None:
        self._desired = desired
        self._observed: list[Availability] = []
        self.menu_open_at_last_observation = False

    @property
    def first(self) -> Availability | None:
        return self._observed[0] if self._observed else None

    @property
    def final(self) -> Availability | None:
        return self._observed[-1] if self._observed else None

    def observe(self, analysis: ScreenAnalysis) -> None:
        self.menu_open_at_last_observation = has_visible_manual_availability_control(analysis)
        availability = infer_slack_availability(analysis)
        if availability is not None and (
            not self._observed or self._observed[-1] is not availability
        ):
            self._observed.append(availability)

    def validate_completion(self, analysis: ScreenAnalysis) -> str | None:
        availability = infer_slack_availability(analysis)
        if availability is None:
            return (
                "Slack availability completion lacked visible Active/Away toggle evidence; "
                "no success was assumed."
            )
        if self._desired is not None and availability is not self._desired:
            return (
                "Slack availability completion visibly disagreed with the requested state; "
                "no success was assumed."
            )
        return None

    def override_uncertain_verification(
        self,
        analysis: ScreenAnalysis,
        verification: ActionVerification,
    ) -> ActionVerification | None:
        if verification.status is not VerificationStatus.UNCERTAIN:
            return None
        availability = infer_slack_availability(analysis)
        if availability is None:
            return None
        if self._desired is not None and availability is not self._desired:
            return None
        return ActionVerification(
            status=VerificationStatus.SUCCESS,
            confidence=analysis.confidence,
            evidence="Fresh screen visibly shows Slack's manual availability control.",
            expected_outcome_observed=True,
        )
