from __future__ import annotations

from collections.abc import Callable

from work_agent.vision import (
    AnalysisOptions,
    ObservationContext,
    SafetyWarning,
    ScreenAnalysis,
    ScreenAnalyzer,
    ScreenObservation,
    ScreenState,
)

# Everything that must still stop the session. Only UNEXPECTED_DIALOG is ever cleared: a login
# prompt, a lock screen, a destructive confirmation, a dead feed, or an unreadable screen are not
# things a window focus can push out of the way.
_HARD_WARNINGS = frozenset(
    {
        SafetyWarning.AUTHENTICATION_PROMPT,
        SafetyWarning.LOCK_SCREEN,
        SafetyWarning.DESTRUCTIVE_CONFIRMATION,
        SafetyWarning.REMOTE_DISCONNECT,
        SafetyWarning.LOW_CONFIDENCE,
        SafetyWarning.UNKNOWN_STATE,
    }
)

_HARD_STATES = frozenset({ScreenState.AUTHENTICATION, ScreenState.LOCK_SCREEN})


class AgendaScreenAnalyzer:
    """Let a system dialog sit on screen without ending the walk to the calendar.

    A corporate update prompt can own the middle of a work machine's screen for days, and a rule
    of stopping on any unexpected dialog would make reading the calendar impossible on exactly
    the machines that have one. The dialog itself is never touched: clicking the calendar
    application's own Dock icon puts its window in front, which is what a person does. The
    warning itself is kept on the analysis so the policy engine still sees the dialog and refuses
    to answer it; only the stop it would otherwise cause is lifted, and only when the dialog is
    the single problem.

    Nothing else is relaxed. An authentication prompt, lock screen, destructive confirmation,
    disconnected feed, or low-confidence read still stops the session exactly as before.

    Every observation's warning categories are also reported to ``warning_sink`` as names, so a
    stopped session can be diagnosed from the log without persisting anything the screen showed.
    """

    def __init__(
        self,
        delegate: ScreenAnalyzer,
        *,
        event_sink: Callable[[str], None] | None = None,
        warning_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self._delegate = delegate
        self._event_sink = event_sink or (lambda _: None)
        self._warning_sink = warning_sink or (lambda _: None)
        self._announced = False

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
        self._warning_sink(tuple(warning.value for warning in observation.analysis.warnings))
        if not blocked_only_by_a_dialog(observation.analysis):
            return observation

        if not self._announced:
            self._announced = True
            self._event_sink(
                "A system dialog is covering the screen. It will be left untouched; the calendar "
                "window will be brought in front of it instead."
            )
        if observation.analysis.safe_to_continue:
            return observation
        return observation.model_copy(update={"analysis": _walk_around(observation.analysis)})


def blocked_only_by_a_dialog(analysis: ScreenAnalysis) -> bool:
    """True when an unexpected dialog is the single reason this screen is unusable."""

    if SafetyWarning.UNEXPECTED_DIALOG not in analysis.warnings:
        return False
    if _HARD_WARNINGS.intersection(analysis.warnings):
        return False
    return analysis.screen_state not in _HARD_STATES


def _walk_around(analysis: ScreenAnalysis) -> ScreenAnalysis:
    """Lift the stop but keep the warning: policy still needs to know the dialog is there."""

    return analysis.model_copy(update={"safe_to_continue": True, "stop_reason": None})
