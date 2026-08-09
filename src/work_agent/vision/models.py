from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServiceTier(StrEnum):
    DEFAULT = "default"
    FLEX = "flex"


class ReasoningEffort(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"


class ImageDetail(StrEnum):
    LOW = "low"
    AUTO = "auto"
    HIGH = "high"


class ScreenState(StrEnum):
    DESKTOP = "desktop"
    APPLICATION = "application"
    DIALOG = "dialog"
    LOCK_SCREEN = "lock_screen"
    AUTHENTICATION = "authentication"
    LOADING = "loading"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class SafetyWarning(StrEnum):
    AUTHENTICATION_PROMPT = "authentication_prompt"
    LOCK_SCREEN = "lock_screen"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    DESTRUCTIVE_CONFIRMATION = "destructive_confirmation"
    REMOTE_DISCONNECT = "remote_disconnect"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN_STATE = "unknown_state"


class UIElementRole(StrEnum):
    BUTTON = "button"
    ICON = "icon"
    MENU = "menu"
    MENU_ITEM = "menu_item"
    TEXT_FIELD = "text_field"
    CHECKBOX = "checkbox"
    TAB = "tab"
    LINK = "link"
    LIST_ITEM = "list_item"
    WINDOW = "window"
    DIALOG = "dialog"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


class NormalizedPoint(_StrictModel):
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)


class BoundingBox(_StrictModel):
    x1: int = Field(ge=0, le=1000)
    y1: int = Field(ge=0, le=1000)
    x2: int = Field(ge=0, le=1000)
    y2: int = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def _validate_order(self) -> BoundingBox:
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError("bounding box maximums must not be smaller than minimums")
        return self


class UIElement(_StrictModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    role: UIElementRole
    visible_text: str
    bounding_box: BoundingBox | None
    click_point: NormalizedPoint | None
    confidence: float = Field(ge=0.0, le=1.0)


class ScreenPerception(_StrictModel):
    """Strict model-generated fields, excluding trusted local request metadata."""

    application: str = Field(min_length=1)
    screen_state: ScreenState
    summary: str = Field(min_length=1)
    target_found: bool
    target: UIElement | None
    relevant_elements: list[UIElement]
    warnings: list[SafetyWarning]
    safe_to_continue: bool
    stop_reason: str | None
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_consistency(self) -> ScreenPerception:
        if self.target_found != (self.target is not None):
            raise ValueError("target_found must match whether target is present")
        if self.safe_to_continue and self.stop_reason is not None:
            raise ValueError("safe analyses cannot include a stop reason")
        if not self.safe_to_continue and not self.stop_reason:
            raise ValueError("unsafe analyses must include a stop reason")
        return self


class ActionVerification(_StrictModel):
    status: VerificationStatus
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1, max_length=500)
    expected_outcome_observed: bool


class ObservationContext(_StrictModel):
    objective: str = Field(min_length=1)
    previous_action: str | None = Field(default=None, max_length=500)
    expected_outcome: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate_previous_action(self) -> ObservationContext:
        if (self.previous_action is None) != (self.expected_outcome is None):
            raise ValueError(
                "previous_action and expected_outcome must either both be present or both be absent"
            )
        return self


class ScreenObservationPerception(_StrictModel):
    analysis: ScreenPerception
    previous_action_verification: ActionVerification | None


class AnalysisUsage(_StrictModel):
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    def __add__(self, other: AnalysisUsage) -> AnalysisUsage:
        return AnalysisUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class ScreenAnalysis(_StrictModel):
    objective: str = Field(min_length=1)
    application: str
    screen_state: ScreenState
    summary: str
    target_found: bool
    target: UIElement | None
    relevant_elements: list[UIElement]
    warnings: list[SafetyWarning]
    safe_to_continue: bool
    stop_reason: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    screenshot_width: int = Field(gt=1)
    screenshot_height: int = Field(gt=1)
    requested_model: str
    model: str
    requested_service_tier: ServiceTier
    service_tier: str | None
    image_detail: ImageDetail
    reasoning_effort: ReasoningEffort
    usage: AnalysisUsage
    latency_seconds: float = Field(ge=0.0)
    retries: int = Field(ge=0)
    escalated: bool
    attempted_models: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_consistency(self) -> ScreenAnalysis:
        if self.target_found != (self.target is not None):
            raise ValueError("target_found must match whether target is present")
        if self.safe_to_continue and self.stop_reason is not None:
            raise ValueError("safe analyses cannot include a stop reason")
        if not self.safe_to_continue and not self.stop_reason:
            raise ValueError("unsafe analyses must include a stop reason")
        return self


class ScreenObservation(_StrictModel):
    analysis: ScreenAnalysis
    previous_action_verification: ActionVerification | None


class AnalysisOptions(_StrictModel):
    model: str | None = None
    service_tier: ServiceTier | None = None
    reasoning_effort: ReasoningEffort | None = None
    image_detail: ImageDetail | None = None

    @model_validator(mode="after")
    def _validate_model(self) -> AnalysisOptions:
        if self.model is not None:
            normalized = self.model.strip()
            if not normalized or any(character.isspace() for character in normalized):
                raise ValueError("model override must be a non-empty identifier without whitespace")
            object.__setattr__(self, "model", normalized)
        return self
