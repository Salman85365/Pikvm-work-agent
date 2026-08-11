from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from work_agent.schedule.errors import ScheduleError
from work_agent.slack.models import Availability

KARACHI_TIMEZONE = ZoneInfo("Asia/Karachi")
_ACTIVE_START = time(18, 0)
_AWAY_START = time(2, 0)


def desired_availability(moment: datetime) -> Availability:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ScheduleError("Availability reconciliation requires a timezone-aware timestamp.")
    local = moment.astimezone(KARACHI_TIMEZONE)
    weekday = local.weekday()
    local_time = local.time().replace(tzinfo=None)
    if weekday <= 4 and local_time >= _ACTIVE_START:
        return Availability.ACTIVE
    if 1 <= weekday <= 5 and local_time < _AWAY_START:
        return Availability.ACTIVE
    return Availability.AWAY
