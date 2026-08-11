from __future__ import annotations

from work_agent.slack.analyzer import SlackAvailabilityScreenAnalyzer
from work_agent.vision import (
    AnalysisOptions,
    AnalysisUsage,
    BoundingBox,
    ImageDetail,
    NormalizedPoint,
    ObservationContext,
    ReasoningEffort,
    SafetyWarning,
    ScreenAnalysis,
    ScreenObservation,
    ScreenState,
    ServiceTier,
    UIElement,
    UIElementRole,
    VerificationStatus,
    VisionSettings,
)
from work_agent.vision.models import ActionVerification


def _usage(tokens: int) -> AnalysisUsage:
    return AnalysisUsage(
        input_tokens=tokens,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_tokens=tokens,
    )


def _element(
    *,
    element_id: str,
    label: str,
    role: UIElementRole,
    point: NormalizedPoint | None,
    box: BoundingBox | None,
    visible_text: str = "",
) -> UIElement:
    return UIElement(
        id=element_id,
        label=label,
        role=role,
        visible_text=visible_text,
        bounding_box=box,
        click_point=point,
        confidence=0.98,
    )


def _analysis(
    *,
    target: UIElement | None,
    relevant: list[UIElement] | None = None,
    usage_tokens: int = 1,
) -> ScreenAnalysis:
    return ScreenAnalysis(
        objective="Set Slack availability",
        application="Slack",
        screen_state=ScreenState.APPLICATION,
        summary="Slack is foreground.",
        target_found=target is not None,
        target=target,
        relevant_elements=relevant or [],
        warnings=[],
        safe_to_continue=True,
        stop_reason=None,
        confidence=0.98,
        screenshot_width=1920,
        screenshot_height=1080,
        requested_model="vision",
        model="vision",
        requested_service_tier=ServiceTier.DEFAULT,
        service_tier="default",
        image_detail=ImageDetail.HIGH,
        reasoning_effort=ReasoningEffort.LOW,
        usage=_usage(usage_tokens),
        latency_seconds=0.1,
        retries=0,
        escalated=False,
        attempted_models=["vision"],
    )


class _Analyzer:
    def __init__(
        self,
        observation: ScreenObservation,
        focused: ScreenAnalysis | list[ScreenAnalysis],
        *,
        confirmation: ScreenObservation | None = None,
    ) -> None:
        self.observations = [observation, *([confirmation] if confirmation is not None else [])]
        self.focused = focused if isinstance(focused, list) else [focused]
        self.focused_objectives: list[str] = []
        self.analysis_options: list[AnalysisOptions | None] = []
        self.observation_options: list[AnalysisOptions | None] = []

    def analyze(
        self,
        screenshot: bytes,
        *,
        objective: str,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenAnalysis:
        self.focused_objectives.append(objective)
        self.analysis_options.append(options)
        index = min(len(self.analysis_options) - 1, len(self.focused) - 1)
        return self.focused[index]

    def observe(
        self,
        screenshot: bytes,
        *,
        context: ObservationContext,
        width: int,
        height: int,
        options: AnalysisOptions | None = None,
    ) -> ScreenObservation:
        self.observation_options.append(options)
        index = min(len(self.observation_options) - 1, len(self.observations) - 1)
        return self.observations[index]


def _settings() -> VisionSettings:
    return VisionSettings(
        api_key="test-key",
        model="vision",
        fallback_model="fallback-vision",
    )


def _wrapper(delegate: _Analyzer) -> SlackAvailabilityScreenAnalyzer:
    return SlackAvailabilityScreenAnalyzer(delegate, _settings())


def test_slack_foreground_profile_target_gets_focused_localization() -> None:
    generic_target = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=940, x2=31, y2=970),
    )
    focused_target = _element(
        element_id="account-profile-avatar",
        label="Account profile/avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=912),
        box=BoundingBox(x1=10, y1=893, x2=31, y2=932),
    )
    verification = ActionVerification(
        status=VerificationStatus.SUCCESS,
        confidence=0.99,
        evidence="Slack became foreground.",
        expected_outcome_observed=True,
    )
    delegate = _Analyzer(
        ScreenObservation(
            analysis=_analysis(target=generic_target, usage_tokens=2),
            previous_action_verification=verification,
        ),
        _analysis(target=focused_target, usage_tokens=3),
    )

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(
            objective="Set Slack active",
            previous_action="click Slack taskbar icon",
            expected_outcome="Slack becomes foreground.",
        ),
        width=1920,
        height=1080,
    )

    assert result.analysis.target == focused_target
    assert result.analysis.objective == "Set Slack active"
    assert result.analysis.usage.total_tokens == 5
    assert result.previous_action_verification == verification
    assert result.vision_calls == 2
    assert "center" in delegate.focused_objectives[0]


def test_visible_availability_toggle_skips_profile_refinement() -> None:
    toggle = _element(
        element_id="manual_availability_toggle",
        label="Set yourself as active",
        role=UIElementRole.MENU_ITEM,
        point=NormalizedPoint(x=95, y=709),
        box=BoundingBox(x1=50, y1=694, x2=139, y2=725),
        visible_text="Set yourself as active",
    )
    observation = ScreenObservation(
        analysis=_analysis(target=toggle, relevant=[toggle]),
        previous_action_verification=None,
    )
    delegate = _Analyzer(observation, _analysis(target=toggle))

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(objective="Get Slack availability"),
        width=1920,
        height=1080,
    )

    assert result == observation
    assert delegate.focused_objectives == []


def test_closed_menu_after_toggle_defers_to_read_only_verification_session() -> None:
    profile = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=940, x2=31, y2=970),
    )
    observation = ScreenObservation(
        analysis=_analysis(target=profile),
        previous_action_verification=ActionVerification(
            status=VerificationStatus.SUCCESS,
            confidence=0.98,
            evidence="The profile menu closed.",
            expected_outcome_observed=True,
        ),
    )
    delegate = _Analyzer(observation, _analysis(target=profile))

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(
            objective="Set Slack active",
            previous_action="click_element element=manual-availability-toggle",
            expected_outcome="Slack applies the availability change.",
        ),
        width=1920,
        height=1080,
    )

    assert result.previous_action_verification is not None
    assert result.previous_action_verification.status is VerificationStatus.UNCERTAIN
    assert result.previous_action_verification.expected_outcome_observed is False
    assert result.vision_calls == 1
    assert delegate.focused_objectives == []


def test_invalid_primary_profile_target_uses_stronger_focused_retry() -> None:
    generic = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=940, x2=31, y2=970),
    )
    invalid = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=893, x2=31, y2=932),
    )
    valid = _element(
        element_id="account_profile_avatar",
        label="Account profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=912),
        box=BoundingBox(x1=10, y1=893, x2=31, y2=932),
    )
    delegate = _Analyzer(
        ScreenObservation(
            analysis=_analysis(target=generic),
            previous_action_verification=None,
        ),
        [_analysis(target=invalid), _analysis(target=valid)],
    )

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(objective="Set Slack active"),
        width=1920,
        height=1080,
    )

    assert result.analysis.target == valid
    assert result.vision_calls == 3
    assert delegate.analysis_options[1] is not None
    assert delegate.analysis_options[1].model == "fallback-vision"
    assert delegate.analysis_options[1].reasoning_effort is ReasoningEffort.MEDIUM


def test_two_invalid_profile_targets_stop_safely_without_exception() -> None:
    generic = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=940, x2=31, y2=970),
    )
    invalid = _element(
        element_id="profile_avatar",
        label="Profile avatar",
        role=UIElementRole.BUTTON,
        point=NormalizedPoint(x=20, y=954),
        box=BoundingBox(x1=10, y1=893, x2=31, y2=932),
    )
    delegate = _Analyzer(
        ScreenObservation(
            analysis=_analysis(target=generic),
            previous_action_verification=None,
        ),
        [_analysis(target=invalid), _analysis(target=invalid)],
    )

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(objective="Set Slack active"),
        width=1920,
        height=1080,
    )

    assert result.analysis.safe_to_continue is False
    assert result.analysis.target is None
    assert SafetyWarning.UNKNOWN_STATE in result.analysis.warnings
    assert result.vision_calls == 3


def test_unsafe_observation_is_confirmed_once_with_fallback_model() -> None:
    toggle = _element(
        element_id="manual_availability_toggle",
        label="Set yourself as active",
        role=UIElementRole.MENU_ITEM,
        point=NormalizedPoint(x=95, y=709),
        box=BoundingBox(x1=50, y1=694, x2=139, y2=725),
        visible_text="Set yourself as active",
    )
    unsafe = _analysis(target=None, usage_tokens=2).model_copy(
        update={
            "safe_to_continue": False,
            "stop_reason": "The state may be unexpected.",
            "warnings": [SafetyWarning.UNKNOWN_STATE],
        }
    )
    confirmed = ScreenObservation(
        analysis=_analysis(target=toggle, relevant=[toggle], usage_tokens=3),
        previous_action_verification=None,
    )
    delegate = _Analyzer(
        ScreenObservation(analysis=unsafe, previous_action_verification=None),
        _analysis(target=toggle),
        confirmation=confirmed,
    )

    result = _wrapper(delegate).observe(
        b"image",
        context=ObservationContext(objective="Get Slack availability"),
        width=1920,
        height=1080,
    )

    assert result.analysis.safe_to_continue is True
    assert result.analysis.usage.total_tokens == 5
    assert result.vision_calls == 2
    assert delegate.observation_options[1] is not None
    assert delegate.observation_options[1].model == "fallback-vision"
    assert delegate.observation_options[1].reasoning_effort is ReasoningEffort.MEDIUM


def test_hard_safety_warning_is_not_overridden_or_retried() -> None:
    unsafe = _analysis(target=None).model_copy(
        update={
            "safe_to_continue": False,
            "stop_reason": "An authentication screen is visible.",
            "warnings": [SafetyWarning.AUTHENTICATION_PROMPT],
            "screen_state": ScreenState.AUTHENTICATION,
        }
    )
    delegate = _Analyzer(
        ScreenObservation(analysis=unsafe, previous_action_verification=None),
        _analysis(target=None),
        confirmation=ScreenObservation(
            analysis=_analysis(target=None),
            previous_action_verification=None,
        ),
    )
    events: list[str] = []

    result = SlackAvailabilityScreenAnalyzer(
        delegate,
        _settings(),
        event_sink=events.append,
    ).observe(
        b"image",
        context=ObservationContext(objective="Get Slack availability"),
        width=1920,
        height=1080,
    )

    assert result.analysis.safe_to_continue is False
    assert result.analysis.screen_state is ScreenState.AUTHENTICATION
    assert result.vision_calls == 1
    assert delegate.focused_objectives == []
    assert len(delegate.observation_options) == 1
    assert events == ["Vision safety stop: authentication_prompt."]
