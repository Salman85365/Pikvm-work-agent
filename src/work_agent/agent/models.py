from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from work_agent.vision.models import (
    ActionVerification,
    AnalysisUsage,
    ScreenState,
    VerificationStatus,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextPurpose(StrEnum):
    NAVIGATION_SEARCH = "navigation_search"
    LOCAL_INPUT = "local_input"
    CONTENT_EDIT = "content_edit"
    EXTERNAL_COMMUNICATION = "external_communication"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


class RiskCategory(StrEnum):
    NAVIGATION = "navigation"
    READ_ONLY = "read_only"
    LOCAL_EDIT = "local_edit"
    EXTERNAL_COMMUNICATION = "external_communication"
    AUTHENTICATION = "authentication"
    DESTRUCTIVE = "destructive"
    SYSTEM_CHANGE = "system_change"
    UNKNOWN = "unknown"


class ScrollDirection(StrEnum):
    UP = "up"
    DOWN = "down"


class ApprovalMode(StrEnum):
    SAFE = "safe"
    EVERY = "every"


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ExecutionTransportStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ControllerState(StrEnum):
    INITIALIZING = "initializing"
    OBSERVING = "observing"
    PLANNING = "planning"
    POLICY_CHECK = "policy_check"
    WAITING_APPROVAL = "waiting_approval"
    PRE_ACTION_CHECK = "pre_action_check"
    EXECUTING = "executing"
    WAITING_FOR_SCREEN = "waiting_for_screen"
    VERIFYING = "verifying"
    FINISHED = "finished"
    PAUSED = "paused"
    FAILED = "failed"


class AgentFinalStatus(StrEnum):
    SUCCESS = "success"
    DRY_RUN = "dry_run"
    PAUSED = "paused"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StopCode(StrEnum):
    """Why a controller session ended, as data rather than prose.

    Skills and telemetry must classify outcomes from this, never by matching the
    human-readable summary, so a wording change cannot silently reclassify failures.
    """

    COMPLETED = "completed"
    DRY_RUN = "dry_run"
    RUNTIME_LIMIT = "runtime_limit"
    STEP_LIMIT = "step_limit"
    SCREEN_UNSAFE = "screen_unsafe"
    SCREEN_LOW_CONFIDENCE = "screen_low_confidence"
    PLANNER_LOW_CONFIDENCE = "planner_low_confidence"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_MISSING = "verification_missing"
    COMPLETION_UNVERIFIED = "completion_unverified"
    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    STEP_CANCELLED = "step_cancelled"
    USER_ASSISTANCE_REQUESTED = "user_assistance_requested"
    TRANSPORT_FAILED = "transport_failed"
    STUCK_REPEATED_ACTION = "stuck_repeated_action"
    STUCK_NO_SCREEN_CHANGE = "stuck_no_screen_change"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class WaitAction(_StrictModel):
    type: Literal["wait"]
    seconds: float = Field(ge=0.1, le=10.0)


class PressKeyAction(_StrictModel):
    type: Literal["press_key"]
    key: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _validate_key(self) -> PressKeyAction:
        _validate_key_name(self.key)
        return self


class HotkeyAction(_StrictModel):
    type: Literal["hotkey"]
    keys: list[str] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def _validate_keys(self) -> HotkeyAction:
        for key in self.keys:
            _validate_key_name(key)
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("hotkey keys must not repeat")
        return self


class TypeTextAction(_StrictModel):
    type: Literal["type_text"]
    text: str = Field(min_length=1, max_length=500)
    purpose: TextPurpose


class MoveMouseAction(_StrictModel):
    type: Literal["move_mouse"]
    element_id: str = Field(min_length=1, max_length=200)


class ClickElementAction(_StrictModel):
    type: Literal["click_element"]
    element_id: str = Field(min_length=1, max_length=200)
    button: Literal["left"]


class DoubleClickElementAction(_StrictModel):
    type: Literal["double_click_element"]
    element_id: str = Field(min_length=1, max_length=200)


class ScrollAction(_StrictModel):
    type: Literal["scroll"]
    direction: ScrollDirection
    amount: int = Field(ge=1, le=5)


class FinishAction(_StrictModel):
    type: Literal["finish"]
    summary: str = Field(min_length=1, max_length=1000)


class RequestUserAction(_StrictModel):
    type: Literal["request_user"]
    reason: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=1000)


Action = (
    WaitAction
    | PressKeyAction
    | HotkeyAction
    | TypeTextAction
    | MoveMouseAction
    | ClickElementAction
    | DoubleClickElementAction
    | ScrollAction
    | FinishAction
    | RequestUserAction
)


class ActionProposal(_StrictModel):
    action: Action
    expected_outcome: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    risk: RiskCategory
    reason_summary: str = Field(min_length=1, max_length=500)


class PolicyDecision(_StrictModel):
    decision: PolicyDecisionKind
    reason: str = Field(min_length=1, max_length=1000)
    inferred_risk: RiskCategory


class ExecutionResult(_StrictModel):
    action: Action
    started_at: datetime
    finished_at: datetime
    transport_status: ExecutionTransportStatus
    hid_action: bool
    error_code: str | None
    sanitized_error: str | None


class PlanningResult(_StrictModel):
    proposal: ActionProposal
    requested_model: str
    model: str
    requested_service_tier: str
    service_tier: str | None
    reasoning_effort: str
    usage: AnalysisUsage
    latency_seconds: float = Field(ge=0.0)
    retries: int = Field(ge=0)


class AgentStepSummary(_StrictModel):
    step_number: int = Field(ge=1)
    screen_fingerprint: str = Field(min_length=1)
    application: str
    screen_state: ScreenState
    action_summary: str
    policy_decision: PolicyDecisionKind
    transport_status: ExecutionTransportStatus | None
    verification_status: VerificationStatus | None


class AgentStep(_StrictModel):
    step_number: int = Field(ge=1)
    observed_at: datetime
    screen_fingerprint: str = Field(min_length=1)
    application: str
    screen_state: ScreenState
    proposal: ActionProposal
    policy_decision: PolicyDecision
    execution_result: ExecutionResult | None
    verification: ActionVerification | None


class SessionTelemetry(_StrictModel):
    session_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    final_status: AgentFinalStatus
    steps: int = Field(ge=0)
    hid_actions: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    vision_calls: int = Field(ge=0)
    planner_calls: int = Field(ge=0)
    vision_usage: AnalysisUsage
    planner_usage: AnalysisUsage
    retries: int = Field(ge=0)
    approval_requests: int = Field(ge=0)
    stale_action_cancellations: int = Field(ge=0)
    verification_failures: int = Field(ge=0)
    screen_settle_seconds: float = Field(ge=0.0)
    total_model_latency_seconds: float = Field(ge=0.0)
    runtime_seconds: float = Field(ge=0.0)


class AgentSessionResult(_StrictModel):
    status: AgentFinalStatus
    stop_code: StopCode
    summary: str = Field(min_length=1)
    telemetry: SessionTelemetry
    history: list[AgentStep]
    final_state: ControllerState


def action_summary(action: Action) -> str:
    if isinstance(action, WaitAction):
        return f"wait {action.seconds:.1f}s"
    if isinstance(action, PressKeyAction):
        return f"press_key {action.key}"
    if isinstance(action, HotkeyAction):
        return "hotkey " + "+".join(action.keys)
    if isinstance(action, TypeTextAction):
        return f"type_text purpose={action.purpose.value} characters={len(action.text)}"
    if isinstance(action, (MoveMouseAction, ClickElementAction, DoubleClickElementAction)):
        return f"{action.type} element={action.element_id}"
    if isinstance(action, ScrollAction):
        return f"scroll {action.direction.value} amount={action.amount}"
    if isinstance(action, FinishAction):
        return "finish"
    return "request_user"


def action_fingerprint(action: Action) -> str:
    summary = action_summary(action)
    if isinstance(action, TypeTextAction):
        text_digest = hashlib.sha256(action.text.encode("utf-8")).hexdigest()[:16]
        summary += f" text_sha256={text_digest}"
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def is_hid_action(action: Action) -> bool:
    return isinstance(
        action,
        (
            PressKeyAction,
            HotkeyAction,
            TypeTextAction,
            MoveMouseAction,
            ClickElementAction,
            DoubleClickElementAction,
            ScrollAction,
        ),
    )


def zero_usage() -> AnalysisUsage:
    return AnalysisUsage(
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_tokens=0,
    )


def _validate_key_name(key: str) -> None:
    if not key or key != key.strip() or "," in key or any(character.isspace() for character in key):
        raise ValueError("key names must not contain commas or whitespace")
