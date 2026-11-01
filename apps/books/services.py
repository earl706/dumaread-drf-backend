from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Book, BookReadingDay, ReadingDay


def _days_param(raw, default=365, maximum=500):
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = default
    return max(1, min(maximum, days))


def owner_tz(user):
    """Resolve a user's IANA timezone, falling back to UTC."""
    name = getattr(user, "timezone", None) or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def owner_localdate(user, when=None):
    """Calendar date for ``when`` (default now) in the owner's timezone."""
    instant = when or timezone.now()
    if timezone.is_naive(instant):
        instant = timezone.make_aware(instant, timezone.get_current_timezone())
    return instant.astimezone(owner_tz(user)).date()


def period_bounds(user, when=None):
    """
    Return (today, week_start Mon, week_end Sun, month_start, month_end)
    for the owner's calendar in their timezone.
    """
    today = owner_localdate(user, when)
    week_start = today - timedelta(days=today.weekday())  # Monday=0
    week_end = week_start + timedelta(days=6)
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month - timedelta(days=1)
    return today, week_start, week_end, month_start, month_end


def _minutes_from_seconds(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return 0
    return (seconds + 59) // 60


def _period_entry(goal_minutes, seconds):
    minutes = _minutes_from_seconds(seconds)
    if goal_minutes is None:
        return {"goal_minutes": None, "minutes": minutes, "percent": None}
    goal = max(0, int(goal_minutes))
    if goal <= 0:
        return {"goal_minutes": goal, "minutes": minutes, "percent": 100.0}
    percent = round(min(100.0, 100.0 * minutes / goal), 1)
    return {"goal_minutes": goal, "minutes": minutes, "percent": percent}


def _sum_seconds(qs, start, end):
    agg = qs.filter(date__gte=start, date__lte=end).aggregate(total=Sum("seconds"))
    return int(agg["total"] or 0)


def user_goal_progress(user, when=None):
    """Daily / weekly / monthly goal progress for all of the user's reading."""
    today, week_start, week_end, month_start, month_end = period_bounds(user, when)
    qs = ReadingDay.objects.filter(owner=user)
    return {
        "daily": _period_entry(user.goal_daily_minutes, _sum_seconds(qs, today, today)),
        "weekly": _period_entry(
            user.goal_weekly_minutes, _sum_seconds(qs, week_start, week_end)
        ),
        "monthly": _period_entry(
            user.goal_monthly_minutes, _sum_seconds(qs, month_start, month_end)
        ),
    }


def book_goal_progress(book, when=None):
    """Daily / weekly / monthly goal progress for a single book."""
    owner = book.owner
    today, week_start, week_end, month_start, month_end = period_bounds(owner, when)
    qs = BookReadingDay.objects.filter(book=book)
    return {
        "daily": _period_entry(book.goal_daily_minutes, _sum_seconds(qs, today, today)),
        "weekly": _period_entry(
            book.goal_weekly_minutes, _sum_seconds(qs, week_start, week_end)
        ),
        "monthly": _period_entry(
            book.goal_monthly_minutes, _sum_seconds(qs, month_start, month_end)
        ),
    }


def apply_heartbeat(book, current_page, seconds):
    """
    Apply a reader heartbeat: page position, elapsed seconds, and status.

    ``seconds`` is clamped to a non-negative int by the serializer; this
    function upserts today's ReadingDay and BookReadingDay (owner timezone)
    and returns a fresh Book instance.
    """
    page_count = max(1, int(book.page_count or 1))
    page = max(1, min(int(current_page), page_count))
    delta = max(0, int(seconds or 0))
    now = timezone.now()
    today = owner_localdate(book.owner, now)

    if page >= page_count:
        status = Book.STATUS_COMPLETED
    else:
        status = Book.STATUS_READING

    update = {
        "current_page": page,
        "status": status,
        "last_read_at": now,
        "updated_at": now,
    }
    if delta:
        update["total_reading_seconds"] = F("total_reading_seconds") + delta

    Book.objects.filter(pk=book.pk).update(**update)

    if delta:
        day, created = ReadingDay.objects.get_or_create(
            owner=book.owner,
            date=today,
            defaults={"seconds": delta},
        )
        if not created:
            ReadingDay.objects.filter(pk=day.pk).update(
                seconds=F("seconds") + delta,
                updated_at=now,
            )

        book_day, book_created = BookReadingDay.objects.get_or_create(
            owner=book.owner,
            book=book,
            date=today,
            defaults={"seconds": delta},
        )
        if not book_created:
            BookReadingDay.objects.filter(pk=book_day.pk).update(
                seconds=F("seconds") + delta,
                updated_at=now,
            )

    return Book.objects.select_related("owner").get(pk=book.pk)


def reading_activity_timeline(user, days=365):
    """
    Daily reading-time intensity for the owner.

    Intensity is minutes read (ceil of seconds / 60, minimum 1 if any time
    was logged). Returns a contiguous ``{ date, intensity, label? }`` list
    for ActivityHeatmap.
    """
    days = _days_param(days)
    end = owner_localdate(user)
    start = end - timedelta(days=days - 1)

    rows = (
        ReadingDay.objects.filter(owner=user, date__gte=start, date__lte=end)
        .annotate(day=TruncDate("date"))
        .values("day", "seconds")
        .order_by("day")
    )
    by_day = {}
    for row in rows:
        day = row["day"]
        if day is None:
            continue
        seconds = int(row["seconds"] or 0)
        minutes = (seconds + 59) // 60 if seconds else 0
        by_day[day] = minutes

    timeline = []
    cursor = start
    while cursor <= end:
        intensity = int(by_day.get(cursor, 0) or 0)
        entry = {"date": cursor.isoformat(), "intensity": intensity}
        if intensity:
            noun = "min" if intensity == 1 else "min"
            entry["label"] = f"{intensity} {noun} read"
        timeline.append(entry)
        cursor += timedelta(days=1)
    return timeline
