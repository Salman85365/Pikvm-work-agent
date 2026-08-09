from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import OpenAI
from PIL import Image

from work_agent.vision import (
    ActionVerification,
    AnalysisOptions,
    ImageDetail,
    ObservationContext,
    OpenAIScreenAnalyzer,
    ReasoningEffort,
    SafetyWarning,
    ScreenObservationPerception,
    ScreenPerception,
    ScreenState,
    ServiceTier,
    VerificationStatus,
    VisionAuthenticationError,
    VisionImageError,
    VisionStructuredOutputError,
    VisionTimeoutError,
)
from work_agent.vision.config import VisionSettings


class _FakeResponses:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeOpenAI:
    def __init__(self, outcomes: list[object]) -> None:
        self.responses = _FakeResponses(outcomes)


def _image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _perception(
    *,
    confidence: float = 0.95,
    state: ScreenState = ScreenState.APPLICATION,
    safe: bool = True,
) -> ScreenPerception:
    return ScreenPerception(
        application="Slack",
        screen_state=state,
        summary="Slack is visible.",
        target_found=False,
        target=None,
        relevant_elements=[],
        warnings=[],
        safe_to_continue=safe,
        stop_reason=None if safe else "Review required.",
        confidence=confidence,
    )


def _usage(
    *, input_tokens: int = 100, output_tokens: int = 20, reasoning_tokens: int = 4
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=7, cache_write_tokens=3),
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        total_tokens=input_tokens + output_tokens,
    )


def _response(
    perception: ScreenPerception,
    *,
    model: str = "gpt-5.6-luna-2026-07-01",
    tier: str = "default",
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        output_parsed=perception,
        model=model,
        service_tier=tier,
        usage=usage or _usage(),
    )


def _settings(**overrides: object) -> VisionSettings:
    values: dict[str, object] = {
        "api_key": "local-test-secret",
        "model": "gpt-5.6-luna",
        "fallback_model": "gpt-5.6-terra",
        "max_retries": 2,
        "confidence_threshold": 0.8,
    }
    values.update(overrides)
    return VisionSettings(**values)  # type: ignore[arg-type]


def _clock(values: list[float]) -> Iterator[float]:
    yield from values


def test_openai_request_is_stateless_private_and_typed() -> None:
    image = _image_bytes()
    fake = _FakeOpenAI([_response(_perception())])
    times = _clock([10.0, 11.25])
    analyzer = OpenAIScreenAnalyzer(
        _settings(),
        client=cast(OpenAI, fake),
        clock=lambda: next(times),
    )

    result = analyzer.analyze(
        image,
        objective="Identify the visible application",
        width=64,
        height=32,
        options=AnalysisOptions(
            model="gpt-5.6-luna",
            service_tier=ServiceTier.FLEX,
            reasoning_effort=ReasoningEffort.NONE,
            image_detail=ImageDetail.HIGH,
        ),
    )

    assert len(fake.responses.calls) == 1
    request = fake.responses.calls[0]
    assert request["store"] is False
    assert request["model"] == "gpt-5.6-luna"
    assert request["service_tier"] == "flex"
    assert request["reasoning"] == {"effort": "none"}
    assert request["text_format"] is ScreenPerception
    assert "previous_response_id" not in request
    content = request["input"][0]["content"]
    assert content[0] == {
        "type": "input_text",
        "text": "Objective: Identify the visible application",
    }
    assert content[1]["detail"] == "high"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert result.requested_service_tier is ServiceTier.FLEX
    assert result.service_tier == "default"
    assert result.model == "gpt-5.6-luna-2026-07-01"
    assert result.usage.input_tokens == 100
    assert result.usage.cached_input_tokens == 7
    assert result.usage.cache_write_tokens == 3
    assert result.usage.reasoning_tokens == 4
    assert result.latency_seconds == 1.25


def test_transient_timeout_retries_are_bounded() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    timeouts = [openai.APITimeoutError(request=request) for _ in range(3)]
    fake = _FakeOpenAI(timeouts)
    sleeps: list[float] = []
    analyzer = OpenAIScreenAnalyzer(
        _settings(max_retries=2),
        client=cast(OpenAI, fake),
        sleeper=sleeps.append,
    )

    with pytest.raises(VisionTimeoutError):
        analyzer.analyze(
            _image_bytes(),
            objective="Identify the application",
            width=64,
            height=32,
        )

    assert len(fake.responses.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_authentication_failure_is_not_retried_or_leaked() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(401, request=request)
    error = openai.AuthenticationError(
        "local-test-secret was rejected",
        response=response,
        body=None,
    )
    fake = _FakeOpenAI([error])
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(VisionAuthenticationError) as caught:
        analyzer.analyze(
            _image_bytes(),
            objective="Identify the application",
            width=64,
            height=32,
        )

    assert len(fake.responses.calls) == 1
    assert "local-test-secret" not in str(caught.value)


def test_missing_structured_output_is_rejected() -> None:
    fake = _FakeOpenAI(
        [SimpleNamespace(output_parsed=None, model="model", service_tier=None, usage=None)]
    )
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(VisionStructuredOutputError):
        analyzer.analyze(
            _image_bytes(),
            objective="Identify the application",
            width=64,
            height=32,
        )


def test_low_confidence_is_locally_marked_unsafe() -> None:
    fake = _FakeOpenAI([_response(_perception(confidence=0.79))])
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    result = analyzer.analyze(
        _image_bytes(),
        objective="Identify the application",
        width=64,
        height=32,
    )

    assert SafetyWarning.LOW_CONFIDENCE in result.warnings
    assert result.safe_to_continue is False
    assert result.stop_reason is not None


def test_authentication_screen_is_locally_marked_unsafe() -> None:
    fake = _FakeOpenAI([_response(_perception(state=ScreenState.AUTHENTICATION))])
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    result = analyzer.analyze(
        _image_bytes(),
        objective="Identify the application",
        width=64,
        height=32,
    )

    assert SafetyWarning.AUTHENTICATION_PROMPT in result.warnings
    assert result.safe_to_continue is False


def test_optional_escalation_runs_at_most_once_and_aggregates_usage() -> None:
    fake = _FakeOpenAI(
        [
            _response(_perception(confidence=0.6), model="luna", usage=_usage()),
            _response(
                _perception(confidence=0.93),
                model="terra",
                usage=_usage(input_tokens=200, output_tokens=30, reasoning_tokens=8),
            ),
        ]
    )
    times = _clock([1.0, 2.0, 3.0, 5.0])
    analyzer = OpenAIScreenAnalyzer(
        _settings(escalation_enabled=True),
        client=cast(OpenAI, fake),
        clock=lambda: next(times),
    )

    result = analyzer.analyze(
        _image_bytes(),
        objective="Identify the application",
        width=64,
        height=32,
    )

    assert [call["model"] for call in fake.responses.calls] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
    ]
    assert result.escalated is True
    assert result.attempted_models == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert result.model == "terra"
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 50
    assert result.latency_seconds == 3.0


def test_analysis_rejects_mismatched_dimensions_before_api_call() -> None:
    fake = _FakeOpenAI([_response(_perception())])
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(VisionImageError, match="dimensions"):
        analyzer.analyze(
            _image_bytes(),
            objective="Identify the application",
            width=65,
            height=32,
        )

    assert fake.responses.calls == []


def test_observe_combines_current_analysis_and_previous_action_verification() -> None:
    perception = ScreenObservationPerception(
        analysis=_perception(),
        previous_action_verification=ActionVerification(
            status=VerificationStatus.SUCCESS,
            confidence=0.96,
            evidence="The profile menu is visibly open.",
            expected_outcome_observed=True,
        ),
    )
    fake = _FakeOpenAI(
        [
            SimpleNamespace(
                output_parsed=perception,
                model="gpt-5.6-luna",
                service_tier="default",
                usage=_usage(),
            )
        ]
    )
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    result = analyzer.observe(
        _image_bytes(),
        context=ObservationContext(
            objective="Open profile menu",
            previous_action="click_element element=profile",
            expected_outcome="Profile menu becomes visible.",
        ),
        width=64,
        height=32,
    )

    request = fake.responses.calls[0]
    assert request["store"] is False
    assert request["text_format"] is ScreenObservationPerception
    assert "previous_response_id" not in request
    assert result.analysis.application == "Slack"
    assert result.previous_action_verification is not None
    assert result.previous_action_verification.status is VerificationStatus.SUCCESS


def test_first_observation_rejects_fabricated_previous_action_verification() -> None:
    perception = ScreenObservationPerception(
        analysis=_perception(),
        previous_action_verification=ActionVerification(
            status=VerificationStatus.NOT_APPLICABLE,
            confidence=1.0,
            evidence="No action was supplied.",
            expected_outcome_observed=False,
        ),
    )
    fake = _FakeOpenAI(
        [
            SimpleNamespace(
                output_parsed=perception,
                model="model",
                service_tier="default",
                usage=_usage(),
            )
        ]
    )
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(VisionStructuredOutputError, match="first observation"):
        analyzer.observe(
            _image_bytes(),
            context=ObservationContext(objective="Identify the screen"),
            width=64,
            height=32,
        )


def test_post_action_observation_requires_verification() -> None:
    perception = ScreenObservationPerception(
        analysis=_perception(),
        previous_action_verification=None,
    )
    fake = _FakeOpenAI(
        [
            SimpleNamespace(
                output_parsed=perception,
                model="model",
                service_tier="default",
                usage=_usage(),
            )
        ]
    )
    analyzer = OpenAIScreenAnalyzer(_settings(), client=cast(OpenAI, fake))

    with pytest.raises(VisionStructuredOutputError, match="omitted"):
        analyzer.observe(
            _image_bytes(),
            context=ObservationContext(
                objective="Open menu",
                previous_action="press_key Escape",
                expected_outcome="Menu closes.",
            ),
            width=64,
            height=32,
        )
