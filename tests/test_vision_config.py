from __future__ import annotations

import pytest

from work_agent.vision import (
    ImageDetail,
    ReasoningEffort,
    ServiceTier,
    VisionConfigurationError,
    VisionSettings,
)


def test_vision_settings_load_typed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "model-primary")
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "model-fallback")
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "flex")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    monkeypatch.setenv("OPENAI_IMAGE_DETAIL", "high")
    monkeypatch.setenv("OPENAI_STORE", "false")
    monkeypatch.setenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "1")
    monkeypatch.setenv("OPENAI_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("OPENAI_CONFIDENCE_THRESHOLD", "0.91")

    settings = VisionSettings.from_env()

    assert settings.api_key == "test-secret"
    assert settings.model == "model-primary"
    assert settings.fallback_model == "model-fallback"
    assert settings.service_tier is ServiceTier.FLEX
    assert settings.reasoning_effort is ReasoningEffort.NONE
    assert settings.image_detail is ImageDetail.HIGH
    assert settings.store is False
    assert settings.request_timeout_seconds == 12.5
    assert settings.max_retries == 1
    assert settings.escalation_enabled is True
    assert settings.confidence_threshold == 0.91
    assert "test-secret" not in repr(settings)


def test_missing_api_key_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")

    with pytest.raises(VisionConfigurationError, match="OPENAI_API_KEY is required"):
        VisionSettings.from_env()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENAI_SERVICE_TIER", "priority"),
        ("OPENAI_REASONING_EFFORT", "maximum"),
        ("OPENAI_IMAGE_DETAIL", "original"),
        ("OPENAI_STORE", "true"),
        ("OPENAI_MAX_RETRIES", "6"),
        ("OPENAI_CONFIDENCE_THRESHOLD", "1.1"),
    ],
)
def test_invalid_vision_configuration_is_rejected(
    name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv(name, value)

    with pytest.raises(VisionConfigurationError):
        VisionSettings.from_env()
