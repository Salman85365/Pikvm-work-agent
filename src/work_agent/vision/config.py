from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from dotenv import load_dotenv

from work_agent.vision.errors import VisionConfigurationError
from work_agent.vision.models import ImageDetail, ReasoningEffort, ServiceTier

_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _parse_bool(name: str, raw_value: str) -> bool:
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise VisionConfigurationError(f"{name} must be one of true/false, yes/no, on/off, or 1/0.")


def _parse_float(name: str, raw_value: str) -> float:
    try:
        return float(raw_value)
    except ValueError as exc:
        raise VisionConfigurationError(f"{name} must be a number.") from exc


def _parse_int(name: str, raw_value: str) -> int:
    try:
        return int(raw_value)
    except ValueError as exc:
        raise VisionConfigurationError(f"{name} must be an integer.") from exc


def _parse_enum(name: str, raw_value: str, enum_type: type[_EnumT]) -> _EnumT:
    try:
        return enum_type(raw_value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise VisionConfigurationError(f"{name} must be one of: {allowed}.") from exc


def _normalize_model(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise VisionConfigurationError(
            f"{name} must be a non-empty model identifier without whitespace."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class VisionSettings:
    api_key: str = field(repr=False)
    model: str = "gpt-5.6-luna"
    fallback_model: str = "gpt-5.6-terra"
    service_tier: ServiceTier = ServiceTier.DEFAULT
    reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    image_detail: ImageDetail = ImageDetail.AUTO
    store: bool = False
    request_timeout_seconds: float = 30.0
    max_retries: int = 2
    escalation_enabled: bool = False
    confidence_threshold: float = 0.80

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise VisionConfigurationError("OPENAI_API_KEY is required for screen analysis.")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "model", _normalize_model("OPENAI_MODEL", self.model))
        object.__setattr__(
            self,
            "fallback_model",
            _normalize_model("OPENAI_FALLBACK_MODEL", self.fallback_model),
        )
        if self.store:
            raise VisionConfigurationError("OPENAI_STORE must remain false for screen analysis.")
        if self.request_timeout_seconds <= 0:
            raise VisionConfigurationError(
                "OPENAI_REQUEST_TIMEOUT_SECONDS must be greater than zero."
            )
        if not 0 <= self.max_retries <= 2:
            raise VisionConfigurationError("OPENAI_MAX_RETRIES must be between 0 and 2.")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise VisionConfigurationError(
                "OPENAI_CONFIDENCE_THRESHOLD must be between 0.0 and 1.0."
            )

    @classmethod
    def from_env(cls) -> VisionSettings:
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY", "")
        return cls(
            api_key=api_key,
            model=os.getenv(
                "OPENAI_VISION_MODEL",
                os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            ),
            fallback_model=os.getenv("OPENAI_FALLBACK_MODEL", "gpt-5.6-terra"),
            service_tier=_parse_enum(
                "OPENAI_VISION_SERVICE_TIER",
                os.getenv(
                    "OPENAI_VISION_SERVICE_TIER",
                    os.getenv("OPENAI_SERVICE_TIER", "default"),
                ),
                ServiceTier,
            ),
            reasoning_effort=_parse_enum(
                "OPENAI_VISION_REASONING_EFFORT",
                os.getenv(
                    "OPENAI_VISION_REASONING_EFFORT",
                    os.getenv("OPENAI_REASONING_EFFORT", "low"),
                ),
                ReasoningEffort,
            ),
            image_detail=_parse_enum(
                "OPENAI_IMAGE_DETAIL",
                os.getenv("OPENAI_IMAGE_DETAIL", "auto"),
                ImageDetail,
            ),
            store=_parse_bool("OPENAI_STORE", os.getenv("OPENAI_STORE", "false")),
            request_timeout_seconds=_parse_float(
                "OPENAI_REQUEST_TIMEOUT_SECONDS",
                os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "30"),
            ),
            max_retries=_parse_int(
                "OPENAI_MAX_RETRIES",
                os.getenv("OPENAI_MAX_RETRIES", "2"),
            ),
            escalation_enabled=_parse_bool(
                "OPENAI_ESCALATION_ENABLED",
                os.getenv("OPENAI_ESCALATION_ENABLED", "false"),
            ),
            confidence_threshold=_parse_float(
                "OPENAI_CONFIDENCE_THRESHOLD",
                os.getenv("OPENAI_CONFIDENCE_THRESHOLD", "0.80"),
            ),
        )
