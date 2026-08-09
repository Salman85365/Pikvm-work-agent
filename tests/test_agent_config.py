from __future__ import annotations

import pytest

from work_agent.agent.config import AgentSettings
from work_agent.agent.controller import ControllerOptions
from work_agent.agent.errors import AgentConfigurationError
from work_agent.vision import ReasoningEffort, ServiceTier, VisionSettings


def test_agent_settings_load_typed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_PLANNER_MODEL", "planner-model")
    monkeypatch.setenv("OPENAI_PLANNER_SERVICE_TIER", "flex")
    monkeypatch.setenv("OPENAI_PLANNER_REASONING_EFFORT", "none")
    monkeypatch.setenv("OPENAI_PLANNER_REQUEST_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("OPENAI_PLANNER_MAX_RETRIES", "1")
    monkeypatch.setenv("AGENT_MAX_STEPS", "8")
    monkeypatch.setenv("AGENT_MAX_RUNTIME_SECONDS", "90")
    monkeypatch.setenv("AGENT_MIN_ACTION_CONFIDENCE", "0.9")
    monkeypatch.setenv("AGENT_SCREEN_POLL_INTERVAL_MS", "250")

    settings = AgentSettings.from_env()

    assert settings.planner_model == "planner-model"
    assert settings.planner_service_tier is ServiceTier.FLEX
    assert settings.planner_reasoning_effort is ReasoningEffort.NONE
    assert settings.planner_request_timeout_seconds == 12
    assert settings.planner_max_retries == 1
    assert settings.max_steps == 8
    assert settings.max_runtime_seconds == 90
    assert settings.min_action_confidence == 0.9
    assert settings.screen_poll_interval_seconds == 0.25
    assert "test-secret" not in repr(settings)


def test_vision_specific_environment_overrides_legacy_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "legacy-model")
    monkeypatch.setenv("OPENAI_SERVICE_TIER", "flex")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "vision-model")
    monkeypatch.setenv("OPENAI_VISION_SERVICE_TIER", "default")
    monkeypatch.setenv("OPENAI_VISION_REASONING_EFFORT", "low")

    settings = VisionSettings.from_env()

    assert settings.model == "vision-model"
    assert settings.service_tier is ServiceTier.DEFAULT
    assert settings.reasoning_effort is ReasoningEffort.LOW


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENAI_PLANNER_SERVICE_TIER", "priority"),
        ("OPENAI_PLANNER_REASONING_EFFORT", "high"),
        ("OPENAI_PLANNER_MAX_RETRIES", "3"),
        ("AGENT_MAX_STEPS", "26"),
        ("AGENT_MAX_RUNTIME_SECONDS", "601"),
        ("AGENT_MIN_ACTION_CONFIDENCE", "1.1"),
        ("AGENT_SCREEN_POLL_INTERVAL_MS", "10"),
    ],
)
def test_invalid_agent_configuration_is_rejected(
    name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv(name, value)

    with pytest.raises(AgentConfigurationError):
        AgentSettings.from_env()


def test_missing_planner_api_key_error_does_not_echo_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")

    with pytest.raises(AgentConfigurationError, match="OPENAI_API_KEY is required") as caught:
        AgentSettings.from_env()

    assert "test-secret" not in str(caught.value)


def test_controller_options_enforce_hard_caps_even_when_constructed_directly() -> None:
    with pytest.raises(ValueError, match="hard cap"):
        ControllerOptions(max_steps=26, timeout_seconds=30)
    with pytest.raises(ValueError, match="600"):
        ControllerOptions(max_steps=1, timeout_seconds=601)
