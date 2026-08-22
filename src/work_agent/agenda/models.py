from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarSurface(StrEnum):
    TEAMS = "teams"
    BROWSER = "browser"
    UNKNOWN = "unknown"


class MeetingStatus(StrEnum):
    """Where a meeting sits relative to the clock read from the same screenshot."""

    IN_PROGRESS = "in_progress"
    UPCOMING = "upcoming"
    ENDED = "ended"
    UNKNOWN = "unknown"


class VisibleMeeting(_Strict):
    """One meeting exactly as the calendar grid already draws it.

    Times stay as displayed text rather than parsed instants: the remote machine's clock and
    timezone are not this Mac's, so the only trustworthy comparison is against the clock
    visible in the same screenshot.
    """

    title: str = Field(min_length=1, max_length=200)
    start_text: str | None = Field(default=None, max_length=20)
    end_text: str | None = Field(default=None, max_length=20)
    all_day: bool = False
    location: str | None = Field(default=None, max_length=120)
    organizer: str | None = Field(default=None, max_length=120)
    is_online: bool = False
    declined: bool = False


class AgendaPerception(_Strict):
    """Strict model-generated view of one day of a calendar, read without opening anything."""

    calendar_visible: bool
    surface: CalendarSurface
    showing_today: bool
    date_text: str | None = Field(default=None, max_length=60)
    # The remote machine's own clock, read from the menu bar, taskbar, or the calendar's
    # current-time line. Without it there is no honest way to say what is still ahead.
    current_time_text: str | None = Field(default=None, max_length=20)
    meetings: list[VisibleMeeting]
    later_truncated: bool
    earlier_truncated: bool
    obstructed: bool = False
    obstruction: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=600)
    safe_to_read: bool
    stop_reason: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_consistency(self) -> AgendaPerception:
        if self.safe_to_read and self.stop_reason is not None:
            raise ValueError("a readable screen cannot carry a stop reason")
        if not self.safe_to_read and not self.stop_reason:
            raise ValueError("an unreadable screen must explain why")
        if not self.calendar_visible and self.meetings:
            raise ValueError("meetings cannot be reported when no calendar is visible")
        return self


@dataclass(frozen=True, slots=True)
class MeetingItem:
    title: str
    start_text: str | None
    end_text: str | None
    start_minutes: int | None
    status: MeetingStatus
    all_day: bool
    location: str | None
    organizer: str | None
    is_online: bool
    declined: bool


@dataclass(frozen=True, slots=True)
class AgendaReport:
    kvm: str
    success: bool
    surface: CalendarSurface = CalendarSurface.UNKNOWN
    date_text: str | None = None
    current_time_text: str | None = None
    items: tuple[MeetingItem, ...] = ()
    later_truncated: bool = False
    earlier_truncated: bool = False
    obstructed: bool = False
    scrolled: bool = False
    confidence: float = 0.0
    error: str | None = None
    stop_code: str | None = None
    # Safety-warning category names from the last controller observation. Names only: never
    # what the screen showed.
    warnings: tuple[str, ...] = ()
    log_error: str | None = None

    @property
    def upcoming(self) -> tuple[MeetingItem, ...]:
        """Everything still ahead, including a meeting already running."""

        return tuple(item for item in self.items if item.status is not MeetingStatus.ENDED)

    @property
    def earlier(self) -> tuple[MeetingItem, ...]:
        return tuple(item for item in self.items if item.status is MeetingStatus.ENDED)

    @property
    def clock_read(self) -> bool:
        return self.current_time_text is not None


@dataclass(frozen=True, slots=True)
class AgendaBatchResult:
    reports: tuple[AgendaReport, ...]

    @property
    def success(self) -> bool:
        return bool(self.reports) and all(report.success for report in self.reports)
