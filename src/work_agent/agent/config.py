from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from work_agent.agent.errors import AgentConfigurationError
from work_agent.vision.models import ReasoningEffort, ServiceTier

HARD_MAX_STEPS = 25
HARD_MAX_RUNTIME_SECONDS = 600.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AgentConfigurationError(f"{name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise AgentConfigurationError(f"{name} must be a number.") from exc


def _model(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise AgentConfigurationError(
            f"{name} must be a non-empty model identifier without whitespace."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class AgentSettings:
    api_key: str = field(repr=False)
    planner_model: str = "gpt-5.6-terra"
    planner_service_tier: ServiceTier = ServiceTier.DEFAULT
    planner_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    planner_request_timeout_seconds: float = 30.0
    planner_max_retries: int = 2
    max_steps: int = 12
    max_runtime_seconds: float = 180.0
    min_action_confidence: float = 0.80
    max_no_change_steps: int = 2
    max_repeated_actions: int = 1
    screen_poll_interval_seconds: float = 0.30
    screen_change_timeout_seconds: float = 5.0
    screen_stable_frames: int = 2
    screen_stable_threshold: float = 0.015
    stale_screen_threshold: float = 0.06

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise AgentConfigurationError("OPENAI_API_KEY is required for agent planning.")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(
            self,
            "planner_model",
            _model("OPENAI_PLANNER_MODEL", self.planner_model),
        )
        if self.planner_request_timeout_seconds <= 0:
            raise AgentConfigurationError(
                "OPENAI_PLANNER_REQUEST_TIMEOUT_SECONDS must be greater than zero."
            )
        if not 0 <= self.planner_max_retries <= 2:
            raise AgentConfigurationError("OPENAI_PLANNER_MAX_RETRIES must be between 0 and 2.")
        if not 1 <= self.max_steps <= HARD_MAX_STEPS:
            raise AgentConfigurationError(
                f"AGENT_MAX_STEPS must be between 1 and {HARD_MAX_STEPS}."
            )
        if not 0 < self.max_runtime_seconds <= HARD_MAX_RUNTIME_SECONDS:
            raise AgentConfigurationError(
                "AGENT_MAX_RUNTIME_SECONDS must be greater than zero and no more than "
                f"{HARD_MAX_RUNTIME_SECONDS:g}."
            )
        if not 0.0 <= self.min_action_confidence <= 1.0:
            raise AgentConfigurationError(
                "AGENT_MIN_ACTION_CONFIDENCE must be between 0.0 and 1.0."
            )
        if not 0 <= self.max_no_change_steps <= 5:
            raise AgentConfigurationError("AGENT_MAX_NO_CHANGE_STEPS must be between 0 and 5.")
        if not 1 <= self.max_repeated_actions <= 3:
            raise AgentConfigurationError("AGENT_MAX_REPEATED_ACTIONS must be between 1 and 3.")
        if not 0.05 <= self.screen_poll_interval_seconds <= 2.0:
            raise AgentConfigurationError(
                "AGENT_SCREEN_POLL_INTERVAL_MS must be between 50 and 2000."
            )
        if not 0.1 <= self.screen_change_timeout_seconds <= 30.0:
            raise AgentConfigurationError(
                "AGENT_SCREEN_CHANGE_TIMEOUT_SECONDS must be between 0.1 and 30."
            )
        if not 1 <= self.screen_stable_frames <= 5:
            raise AgentConfigurationError("AGENT_SCREEN_STABLE_FRAMES must be between 1 and 5.")
        for name, value in (
            ("AGENT_SCREEN_STABLE_THRESHOLD", self.screen_stable_threshold),
            ("AGENT_STALE_SCREEN_THRESHOLD", self.stale_screen_threshold),
        ):
            if not 0.0 < value <= 1.0:
                raise AgentConfigurationError(f"{name} must be greater than 0.0 and at most 1.0.")

    @classmethod
    def from_env(cls) -> AgentSettings:
        load_dotenv()
        try:
            tier = ServiceTier(os.getenv("OPENAI_PLANNER_SERVICE_TIER", "default").lower())
        except ValueError as exc:
            raise AgentConfigurationError(
                "OPENAI_PLANNER_SERVICE_TIER must be one of: default, flex."
            ) from exc
        try:
            effort = ReasoningEffort(os.getenv("OPENAI_PLANNER_REASONING_EFFORT", "low").lower())
        except ValueError as exc:
            raise AgentConfigurationError(
                "OPENAI_PLANNER_REASONING_EFFORT must be one of: none, low, medium."
            ) from exc
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            planner_model=os.getenv("OPENAI_PLANNER_MODEL", "gpt-5.6-terra"),
            planner_service_tier=tier,
            planner_reasoning_effort=effort,
            planner_request_timeout_seconds=_env_float(
                "OPENAI_PLANNER_REQUEST_TIMEOUT_SECONDS",
                _env_float("OPENAI_REQUEST_TIMEOUT_SECONDS", 30.0),
            ),
            planner_max_retries=_env_int(
                "OPENAI_PLANNER_MAX_RETRIES",
                _env_int("OPENAI_MAX_RETRIES", 2),
            ),
            max_steps=_env_int("AGENT_MAX_STEPS", 12),
            max_runtime_seconds=_env_float("AGENT_MAX_RUNTIME_SECONDS", 180.0),
            min_action_confidence=_env_float("AGENT_MIN_ACTION_CONFIDENCE", 0.80),
            max_no_change_steps=_env_int("AGENT_MAX_NO_CHANGE_STEPS", 2),
            max_repeated_actions=_env_int("AGENT_MAX_REPEATED_ACTIONS", 1),
            screen_poll_interval_seconds=(
                _env_float("AGENT_SCREEN_POLL_INTERVAL_MS", 300.0) / 1000.0
            ),
            screen_change_timeout_seconds=_env_float("AGENT_SCREEN_CHANGE_TIMEOUT_SECONDS", 5.0),
            screen_stable_frames=_env_int("AGENT_SCREEN_STABLE_FRAMES", 2),
            screen_stable_threshold=_env_float("AGENT_SCREEN_STABLE_THRESHOLD", 0.015),
            stale_screen_threshold=_env_float("AGENT_STALE_SCREEN_THRESHOLD", 0.06),
        )
