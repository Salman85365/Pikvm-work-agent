from __future__ import annotations

from collections.abc import Callable

from work_agent.slack.state import (
    infer_slack_availability,
    is_availability_toggle_reference,
)
from work_agent.vision import (
    ActionVerification,
    AnalysisOptions,
    ImageDetail,
    ObservationContext,
    ReasoningEffort,
    SafetyWarning,
    ScreenAnalysis,
    ScreenAnalyzer,
    ScreenObservation,
    UIElement,
    UIElementRole,
    VerificationStatus,
    VisionSettings,
)

_PROFILE_LOCALIZATION_OBJECTIVE = """Slack is foreground. Locate only the full clickable account
profile/avatar button that opens the Slack profile menu. Put the click point well inside the center
of the full button, not on its presence badge or below the control. Do not inspect messages."""
_PROFILE_MARKERS = ("profile", "avatar", "account")
_PROFILE_ROLES = frozenset({UIElementRole.BUTTON, UIElementRole.ICON})
_NON_OVERRIDABLE_WARNINGS = frozenset(
    {
        SafetyWarning.AUTHENTICATION_PROMPT,
        SafetyWarning.LOCK_SCREEN,
        SafetyWarning.UNEXPECTED_DIALOG,
        SafetyWarning.DESTRUCTIVE_CONFIRMATION,
        SafetyWarning.REMOTE_DISCONNECT,
    }
)


class SlackAvailabilityScreenAnalyzer:
    """Confirm unsafe states and refine small Slack targets without fixed coordinates."""

    def __init__(
        self,
        delegate: ScreenAnalyzer,
        settings: VisionSettings,
        *,
        event_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._delegate = delegate
        self._fallback_model = settings.fallback_model
        self._event_sink = event_sink or (lambda _: None)

    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenAnalysis:
        return self._delegate.analyze(
            screenshot,
            objective=objective,
            width=width,
            height=height,
            options=options,
        )

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenObservation:
        observation = self._delegate.observe(
            screenshot,
            context=context,
            width=width,
            height=height,
            options=options,
        )
        hard_warnings = _NON_OVERRIDABLE_WARNINGS.intersection(observation.analysis.warnings)
        if hard_warnings:
            categories = ",".join(sorted(warning.value for warning in hard_warnings))
            self._event_sink(f"Vision safety stop: {categories}.")
        elif not observation.analysis.safe_to_continue:
            self._event_sink(
                "Vision safety confirmation: using one read-only fallback-model observation."
            )
            confirmation = self._delegate.observe(
                screenshot,
                context=context,
                width=width,
                height=height,
                options=self._fallback_options(options),
            )
            observation = _merge_observations(observation, confirmation)

        if (
            context.previous_action is not None
            and is_availability_toggle_reference(context.previous_action)
            and infer_slack_availability(observation.analysis) is None
        ):
            return observation.model_copy(
                update={
                    "previous_action_verification": ActionVerification(
                        status=VerificationStatus.UNCERTAIN,
                        confidence=observation.analysis.confidence,
                        evidence=(
                            "Slack closed the profile menu, so final manual availability is not "
                            "yet visible."
                        ),
                        expected_outcome_observed=False,
                    )
                }
            )
        if not _needs_profile_localization(observation.analysis):
            return observation

        focused_attempts = [
            self._delegate.analyze(
                screenshot,
                objective=_PROFILE_LOCALIZATION_OBJECTIVE,
                width=width,
                height=height,
                options=options,
            )
        ]
        focused = focused_attempts[-1]
        if not _valid_profile_target(focused):
            self._event_sink("Vision profile localization: using one fallback-model retry.")
            focused_attempts.append(
                self._delegate.analyze(
                    screenshot,
                    objective=_PROFILE_LOCALIZATION_OBJECTIVE,
                    width=width,
                    height=height,
                    options=self._fallback_options(options),
                )
            )
            focused = focused_attempts[-1]

        prior = [observation.analysis, *focused_attempts[:-1]]
        combined = _combined_analysis(focused, previous=prior, objective=context.objective)
        call_count = observation.vision_calls + len(focused_attempts)
        if not _valid_profile_target(focused):
            return ScreenObservation(
                analysis=combined.model_copy(
                    update={
                        "target_found": False,
                        "target": None,
                        "relevant_elements": [],
                        "warnings": [*focused.warnings, SafetyWarning.UNKNOWN_STATE],
                        "safe_to_continue": False,
                        "stop_reason": (
                            "Focused Slack profile localization could not establish a safe "
                            "clickable control."
                        ),
                    }
                ),
                previous_action_verification=observation.previous_action_verification,
                vision_calls=call_count,
            )
        return ScreenObservation(
            analysis=combined,
            previous_action_verification=observation.previous_action_verification,
            vision_calls=call_count,
        )

    def _fallback_options(self, options: AnalysisOptions | None) -> AnalysisOptions:
        selected = options or AnalysisOptions()
        return selected.model_copy(
            update={
                "model": self._fallback_model,
                "reasoning_effort": ReasoningEffort.MEDIUM,
                "image_detail": ImageDetail.HIGH,
            }
        )


def _merge_observations(
    first: ScreenObservation,
    confirmation: ScreenObservation,
) -> ScreenObservation:
    return ScreenObservation(
        analysis=_combined_analysis(
            confirmation.analysis,
            previous=[first.analysis],
            objective=confirmation.analysis.objective,
        ),
        previous_action_verification=confirmation.previous_action_verification,
        vision_calls=first.vision_calls + confirmation.vision_calls,
    )


def _combined_analysis(
    final: ScreenAnalysis,
    *,
    previous: list[ScreenAnalysis],
    objective: str,
) -> ScreenAnalysis:
    usage = final.usage
    latency = final.latency_seconds
    retries = final.retries
    escalated = final.escalated
    attempted_models: list[str] = []
    for analysis in previous:
        usage += analysis.usage
        latency += analysis.latency_seconds
        retries += analysis.retries
        escalated = escalated or analysis.escalated
        attempted_models.extend(analysis.attempted_models)
    attempted_models.extend(final.attempted_models)
    return final.model_copy(
        update={
            "objective": objective,
            "usage": usage,
            "latency_seconds": latency,
            "retries": retries,
            "escalated": escalated,
            "attempted_models": attempted_models,
        }
    )


def _needs_profile_localization(analysis: ScreenAnalysis) -> bool:
    return (
        analysis.safe_to_continue
        and "slack" in analysis.application.casefold()
        and infer_slack_availability(analysis) is None
    )


def _valid_profile_target(analysis: ScreenAnalysis) -> bool:
    target = analysis.target
    if (
        not analysis.safe_to_continue
        or "slack" not in analysis.application.casefold()
        or target is None
        or target.role not in _PROFILE_ROLES
        or target.bounding_box is None
        or target.click_point is None
    ):
        return False
    identity = f"{target.id} {target.label}".casefold()
    if not any(marker in identity for marker in _PROFILE_MARKERS):
        return False
    return _point_inside_box(target)


def _point_inside_box(element: UIElement) -> bool:
    assert element.bounding_box is not None
    assert element.click_point is not None
    box = element.bounding_box
    point = element.click_point
    return box.x1 <= point.x <= box.x2 and box.y1 <= point.y <= box.y2
