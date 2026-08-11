from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationKind(StrEnum):
    DIRECT_MESSAGE = "direct_message"
    GROUP_MESSAGE = "group_message"
    CHANNEL = "channel"
    THREAD = "thread"
    UNKNOWN = "unknown"


class AttentionLevel(StrEnum):
    """How much a conversation appears to want, judged only from visible sidebar state."""

    MENTIONED = "mentioned"
    DIRECT = "direct"
    UNREAD = "unread"


class UnreadConversation(_Strict):
    """One unread entry as it appears in Slack's sidebar.

    Deliberately carries no message text: triage runs without opening conversations, so only
    the name, kind, and badge state are observable.
    """

    name: str = Field(min_length=1, max_length=120)
    kind: ConversationKind
    unread_count: int = Field(ge=0, le=9999)
    has_mention: bool
    muted: bool


class SlackTriagePerception(_Strict):
    """Strict model-generated view of Slack's unread state, from the sidebar alone."""

    slack_foreground: bool
    sidebar_visible: bool
    conversations: list[UnreadConversation]
    total_unread_badge: int | None = Field(default=None, ge=0, le=9999)
    sidebar_truncated: bool
    summary: str = Field(min_length=1, max_length=600)
    safe_to_read: bool
    stop_reason: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_consistency(self) -> SlackTriagePerception:
        if self.safe_to_read and self.stop_reason is not None:
            raise ValueError("a readable screen cannot carry a stop reason")
        if not self.safe_to_read and not self.stop_reason:
            raise ValueError("an unreadable screen must explain why")
        if not self.sidebar_visible and self.conversations:
            raise ValueError("conversations cannot be reported when the sidebar is not visible")
        return self


@dataclass(frozen=True, slots=True)
class TriageItem:
    name: str
    kind: ConversationKind
    unread_count: int
    has_mention: bool
    attention: AttentionLevel


@dataclass(frozen=True, slots=True)
class TriageReport:
    kvm: str
    success: bool
    items: tuple[TriageItem, ...] = ()
    total_unread_badge: int | None = None
    sidebar_truncated: bool = False
    confidence: float = 0.0
    error: str | None = None
    stop_code: str | None = None
    log_error: str | None = None

    @property
    def needs_attention(self) -> tuple[TriageItem, ...]:
        """Mentions and direct messages — the entries a human would look at first."""

        return tuple(
            item
            for item in self.items
            if item.attention in {AttentionLevel.MENTIONED, AttentionLevel.DIRECT}
        )

    @property
    def informational(self) -> tuple[TriageItem, ...]:
        return tuple(item for item in self.items if item.attention is AttentionLevel.UNREAD)


@dataclass(frozen=True, slots=True)
class TriageBatchResult:
    reports: tuple[TriageReport, ...]

    @property
    def success(self) -> bool:
        return bool(self.reports) and all(report.success for report in self.reports)
