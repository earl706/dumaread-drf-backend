from pathlib import Path

from django.db import models

from apps.common.models import OwnedModel


def book_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or ".bin"
    return f"books/{instance.owner_id}/{instance.uuid}/original{ext}"


def cover_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or ".jpg"
    return f"books/{instance.owner_id}/{instance.uuid}/cover{ext}"


# -----------------------------------------------------------------------------
# Book
# -----------------------------------------------------------------------------
class Book(OwnedModel):
    FORMAT_PDF = "pdf"
    FORMAT_EPUB = "epub"
    FORMAT_TXT = "txt"
    FORMAT_CHOICES = [
        (FORMAT_PDF, "PDF"),
        (FORMAT_EPUB, "EPUB"),
        (FORMAT_TXT, "Text"),
    ]

    STATUS_UNREAD = "unread"
    STATUS_READING = "reading"
    STATUS_COMPLETED = "completed"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_UNREAD, "Unread"),
        (STATUS_READING, "Reading"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255, blank=True)
    format = models.CharField(max_length=8, choices=FORMAT_CHOICES)
    file = models.FileField(upload_to=book_upload_to)
    cover = models.ImageField(upload_to=cover_upload_to, blank=True)
    page_count = models.PositiveIntegerField(default=1)
    current_page = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_UNREAD
    )
    total_reading_seconds = models.PositiveIntegerField(default=0)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta(OwnedModel.Meta):
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["owner", "format"]),
        ]

    def __str__(self):
        return self.title

    @property
    def pages_left(self):
        return max(0, int(self.page_count or 0) - int(self.current_page or 0))

    @property
    def progress_percent(self):
        total = int(self.page_count or 0)
        if total <= 0:
            return 0
        current = min(int(self.current_page or 0), total)
        return round(100 * current / total, 1)


# -----------------------------------------------------------------------------
# Daily reading-time aggregate (heatmap)
# -----------------------------------------------------------------------------
class ReadingDay(OwnedModel):
    date = models.DateField()
    seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "date"],
                name="uniq_reading_day_owner_date",
            )
        ]

    def __str__(self):
        return f"{self.owner_id} {self.date} ({self.seconds}s)"
