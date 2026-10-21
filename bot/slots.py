"""Чистые функции: сетка привязана к началу рабочего дня, интервалы [начало, конец)."""
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bot.config import Business, WEEKDAYS


@dataclass(frozen=True)
class Busy:
    master_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Slot:
    master_id: str
    start: datetime
    end: datetime


def overlaps(start, end, other_start, other_end):
    return start < other_end and end > other_start


def available_slots(business: Business, service_id: str, day: date, timezone: ZoneInfo,
                    now: datetime, busy=(), master_id=None):
    if now.tzinfo is None:
        raise ValueError('now должен содержать часовой пояс')
    today = now.astimezone(timezone).date()
    if day < today or day >= today + timedelta(days=business.booking_horizon_days) or day in business.days_off:
        return []
    service = business.services[service_id]
    weekday = WEEKDAYS[day.weekday()]
    cutoff = now.astimezone(UTC) + timedelta(minutes=business.min_lead_minutes)
    result = []
    busy = tuple(busy)
    for master in business.masters.values():
        if (master_id and master.id != master_id) or service_id not in master.services or weekday not in master.working_hours:
            continue
        begin, finish = master.working_hours[weekday]
        local = datetime.combine(day, begin, timezone)
        close = datetime.combine(day, finish, timezone).astimezone(UTC)
        pauses = [(datetime.combine(day, a, timezone).astimezone(UTC),
                   datetime.combine(day, b, timezone).astimezone(UTC))
                  for a, b in master.breaks.get(weekday, ())]
        while local.time() < finish:
            start = local.astimezone(UTC)
            end = start + timedelta(minutes=service.duration_minutes)
            # При переводе часов не предлагаем несуществующее местное время.
            valid = start.astimezone(timezone).replace(tzinfo=None) == local.replace(tzinfo=None)
            if (valid and start >= cutoff and end <= close
                    and not any(overlaps(start, end, a, b) for a, b in pauses)
                    and not any(b.master_id == master.id and overlaps(start, end, b.start, b.end) for b in busy)):
                result.append(Slot(master.id, start, end))
            local += timedelta(minutes=business.slot_step_minutes)
    return sorted(result, key=lambda s: (s.start, s.master_id))


def can_cancel(start: datetime, now: datetime, deadline_minutes: int):
    return start - now > timedelta(minutes=deadline_minutes)


def reminder_times(start: datetime, created: datetime):
    return {hours: start - timedelta(hours=hours) for hours in (24, 2)
            if start - timedelta(hours=hours) >= created}
