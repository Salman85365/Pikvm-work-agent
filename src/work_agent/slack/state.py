from __future__ import annotations

from work_agent.slack.models import Availability
from work_agent.vision import ActionVerification, ScreenAnalysis, VerificationStatus

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


def is_availability_toggle_reference(value: str) -> bool:
    normalized = value.strip().casefold().replace("-", "_")
    return any(marker in normalized for marker in _AVAILABILITY_TOGGLE_ID_MARKERS)


def infer_slack_availability(analysis: ScreenAnalysis) -> Availability | None:
    elements = list(analysis.relevant_elements)
    if analysis.target is not None and all(
        element.id != analysis.target.id for element in elements
    ):
        elements.append(analysis.target)
    text = " ".join(
        f"{element.label} {element.visible_text}".strip().lower() for element in elements
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

    @property
    def first(self) -> Availability | None:
        return self._observed[0] if self._observed else None

    @property
    def final(self) -> Availability | None:
        return self._observed[-1] if self._observed else None

    def observe(self, analysis: ScreenAnalysis) -> None:
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
