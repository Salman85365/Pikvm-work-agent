from __future__ import annotations

import re

from work_agent.agenda.models import AgendaPerception, MeetingItem, MeetingStatus, VisibleMeeting

AGENDA_PROMPT = """You are reading a PiKVM screenshot of a desktop that may be showing a calendar.

The calendar is either Microsoft Teams' Calendar tab or a browser tab already open on a calendar
such as Outlook on the web or Google Calendar. Set surface to teams or browser accordingly, and
calendar_visible false when no calendar day is legible.

Report only the meetings the grid already draws. For each one give the title, its start and end
exactly as displayed (for example "9:30 AM" or "14:00"), the location or room when shown, the
organizer when shown, is_online true when it carries a Teams/Meet/Zoom join affordance, all_day true
for a banner entry with no time, and declined true when it is struck through or otherwise marked as
declined. Never open, hover into, or expand a meeting to learn more: report only what is already
legible, and leave a field null rather than guessing it.

Read current_time_text from the remote machine's own clock - the menu bar or taskbar clock, or the
calendar's current-time line - and copy it verbatim. This matters more than it looks: that clock is
the only trustworthy way to tell which meetings are still ahead, because the machine reading this
screenshot may be in a different timezone. Leave it null when no clock is legible.

A week view is fine and is the common case. Read only today's column: the one the calendar marks as
today, whose date header is highlighted or underlined and matches the machine's own clock. Report
meetings from that column alone and ignore every other day, however full those columns look.

Set showing_today false only when today cannot be read at all - a calendar scrolled to a different
week, or left on a single day that is not today. Copy the heading of the day you actually read into
date_text.

Set later_truncated true when the day continues below the visible area, and earlier_truncated true
when it continues above, so that meetings may exist outside the current viewport.

Set obstructed true, and describe it in obstruction, when a menu, popover, dialog, banner,
notification, or tooltip covers any part of the day. An obstructed calendar that looks empty is not
evidence that nothing is scheduled.

Set safe_to_read false with a stop_reason for an authentication prompt, a lock screen, a
disconnected or blank feed, or any state where the calendar cannot be trusted."""


AGENDA_CONTEXT = (
    "Report the meetings already visible on today's calendar, the clock shown on the remote "
    "machine, and whether the day is clipped or covered. Do not open any meeting."
)


FOREGROUND_OBJECTIVE = """Bring a calendar into view, then finish.

Try Microsoft Teams first. When Teams is in the Dock or taskbar, click its icon to bring it
forward, then open its Calendar from the left rail if a different Teams section is showing. Only
when Teams cannot be reached at all, fall back to a browser window whose calendar is already open
and switch to that tab.

A system dialog, update prompt, or notification may be covering the screen. Leave it exactly as it
is. Do not click it, do not dismiss it, do not press Escape at it, and never click Open Software
Update, Install, Restart, Later, or Remind me. Click the Dock or taskbar icon of the application you
need instead: bringing that window to the front puts the dialog behind it, which is all this
workflow requires.

Finish as soon as a day of the calendar is visible. If a calendar day is already visible, finish
immediately without any action.

Do not open a new tab or window, do not navigate to a URL, and do not sign in anywhere. If neither
Teams nor an already-open calendar tab can be reached, request user assistance.

Never click a meeting, and never click Join, Accept, Decline, or Tentative. Joining would place the
user into a live call, and answering an invitation would send a reply on their behalf. This
workflow only looks."""


SCROLL_OBJECTIVE = """The visible calendar day is clipped, so later meetings may sit below the fold.

Scroll down within the calendar day to reveal them, then finish. Use only scrolling, and give the
scroll action the element_id of the calendar day grid so the pointer is placed over the calendar
first: the wheel scrolls whatever is under the pointer, and a scroll aimed at nothing changes
nothing. Two or three small scrolls are enough; finish once the end of the day is visible or nothing
new appears.

Do not click anything, not even a Dock or taskbar icon: this phase only scrolls the calendar that
is already in front. If a dialog, another window, or anything else prevents scrolling, finish
rather than clicking around it. Clicking a meeting would open it, and clicking Join, Accept,
Decline, or Tentative would enter a live call or send a reply on the user's behalf. Do not type. Do
not change the visible date or switch views."""


DISMISS_OBJECTIVE = """Something is covering the calendar.

If it is a system dialog, update prompt, or notification, do not touch it. Click the Dock or taskbar
icon of the calendar's own application to bring the calendar back in front of it.

If instead it is a menu or popover belonging to the calendar application itself, press Escape.

Finish once the calendar day is visible and nothing overlaps it. Never click the overlay, never
click inside the calendar grid, and never click Join, Accept, Decline, or Tentative. Do not scroll.
If neither approach clears it, request user assistance."""


_CLOCK = re.compile(
    r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?:(?P<meridiem>[ap])\.?\s*m\.?)?$",
    re.IGNORECASE,
)


def parse_clock(text: str | None) -> int | None:
    """Convert a displayed time such as "9:30 AM" or "14:00" into minutes past midnight.

    Returns None for anything unrecognised: a wrong guess here would silently move a meeting
    between "still ahead" and "already over", so an unreadable time stays unknown instead.
    """

    if text is None:
        return None
    # macOS renders "9:30 AM" with a narrow no-break space and Outlook on the web uses a
    # plain one; both must normalize to an ordinary space before matching.
    cleaned = text.strip().replace("\u202f", " ").replace("\u00a0", " ")
    match = _CLOCK.match(cleaned)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if minute > 59:
        return None
    meridiem = match.group("meridiem")
    if meridiem is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.casefold() == "p" else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute


def classify(
    start_minutes: int | None,
    end_minutes: int | None,
    now_minutes: int | None,
    *,
    all_day: bool,
) -> MeetingStatus:
    """Place a meeting against the clock read from the same screenshot."""

    if all_day:
        return MeetingStatus.UPCOMING
    if now_minutes is None or start_minutes is None:
        return MeetingStatus.UNKNOWN
    if end_minutes is not None and end_minutes <= now_minutes:
        return MeetingStatus.ENDED
    if start_minutes <= now_minutes:
        return MeetingStatus.IN_PROGRESS
    return MeetingStatus.UPCOMING


_STATUS_ORDER = {
    MeetingStatus.IN_PROGRESS: 0,
    MeetingStatus.UPCOMING: 1,
    MeetingStatus.UNKNOWN: 2,
    MeetingStatus.ENDED: 3,
}

_END_OF_DAY = 24 * 60 + 1


def meeting_key(meeting: VisibleMeeting) -> tuple[str, str, str]:
    """Identity for a meeting across scrolled reads of the same day."""

    return (
        meeting.title.strip().casefold(),
        (meeting.start_text or "").strip().casefold(),
        (meeting.end_text or "").strip().casefold(),
    )


def build_items(perception: AgendaPerception) -> tuple[MeetingItem, ...]:
    now_minutes = parse_clock(perception.current_time_text)
    items = [
        _item(meeting, now_minutes) for meeting in perception.meetings if meeting.title.strip()
    ]
    items.sort(key=_order)
    return tuple(items)


def _order(item: MeetingItem) -> tuple[int, int, str]:
    if item.all_day:
        start = -1
    else:
        start = item.start_minutes if item.start_minutes is not None else _END_OF_DAY
    return (_STATUS_ORDER[item.status], start, item.title)


def _item(meeting: VisibleMeeting, now_minutes: int | None) -> MeetingItem:
    start_minutes = parse_clock(meeting.start_text)
    end_minutes = parse_clock(meeting.end_text)
    return MeetingItem(
        title=meeting.title.strip(),
        start_text=meeting.start_text,
        end_text=meeting.end_text,
        start_minutes=start_minutes,
        status=classify(start_minutes, end_minutes, now_minutes, all_day=meeting.all_day),
        all_day=meeting.all_day,
        location=meeting.location,
        organizer=meeting.organizer,
        is_online=meeting.is_online,
        declined=meeting.declined,
    )
