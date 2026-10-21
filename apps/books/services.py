from datetime import timedelta

from django.db.models import F
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Book, ReadingDay


def _days_param(raw, default=365, maximum=366):
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = default
    return max(1, min(maximum, days))


def apply_heartbeat(book, current_page, seconds):
    """
    Apply a reader heartbeat: page position, elapsed seconds, and status.

    ``seconds`` is clamped to a non-negative int by the serializer; this
    function upserts today's ReadingDay and returns a fresh Book instance.
    """
    page_count = max(1, int(book.page_count or 1))
    page = max(1, min(int(current_page), page_count))
    delta = max(0, int(seconds or 0))
    now = timezone.now()

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
            date=timezone.localdate(),
            defaults={"seconds": delta},
        )
        if not created:
            ReadingDay.objects.filter(pk=day.pk).update(
                seconds=F("seconds") + delta,
                updated_at=now,
            )

    return Book.objects.get(pk=book.pk)


def reading_activity_timeline(user, days=365):
    """
    Daily reading-time intensity for the owner.

    Intensity is minutes read (ceil of seconds / 60, minimum 1 if any time
    was logged). Returns a contiguous ``{ date, intensity, label? }`` list
    for ActivityHeatmap.
    """
    days = _days_param(days)
    end = timezone.localdate()
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
