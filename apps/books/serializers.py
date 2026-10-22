from pathlib import Path

from rest_framework import serializers

from .constants import ALLOWED_EXTENSIONS, MAX_BOOK_BYTES, MAX_HEARTBEAT_SECONDS
from .ingest import (
    IngestError,
    detect_format,
    extract_identity,
    ingest_book,
    read_upload_bytes,
)
from .models import Book


# -----------------------------------------------------------------------------
# Book serializer
# -----------------------------------------------------------------------------
class BookSerializer(serializers.ModelSerializer):
    file = serializers.FileField(write_only=True, required=False)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    pages_left = serializers.IntegerField(read_only=True)
    progress_percent = serializers.FloatField(read_only=True)
    has_cover = serializers.SerializerMethodField()

    class Meta:
        model = Book
        fields = [
            "id",
            "uuid",
            "title",
            "author",
            "format",
            "status",
            "page_count",
            "current_page",
            "pages_left",
            "progress_percent",
            "total_reading_seconds",
            "last_read_at",
            "has_cover",
            "file",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "uuid",
            "format",
            "page_count",
            "current_page",
            "pages_left",
            "progress_percent",
            "total_reading_seconds",
            "last_read_at",
            "has_cover",
            "created_at",
            "updated_at",
        ]

    def get_has_cover(self, obj):
        return bool(obj.cover)

    def validate_file(self, value):
        name = getattr(value, "name", "") or ""
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                "Only .pdf, .epub, and .txt files are supported."
            )
        size = getattr(value, "size", None)
        if size is not None and size > MAX_BOOK_BYTES:
            raise serializers.ValidationError("File must be 50 MB or smaller.")
        if size == 0:
            raise serializers.ValidationError("File is empty.")
        return value

    def validate(self, attrs):
        if self.instance is None and not attrs.get("file"):
            raise serializers.ValidationError({"file": "A book file is required."})
        return attrs

    def create(self, validated_data):
        upload = validated_data["file"]
        fmt = detect_format(upload.name)
        validated_data["format"] = fmt
        try:
            data = read_upload_bytes(upload)
        except Exception:
            raise serializers.ValidationError({"file": "Could not read this file."})

        title = (validated_data.get("title") or "").strip()
        author = (validated_data.get("author") or "").strip()
        extracted_title = ""
        extracted_author = ""
        if not title or not author:
            extracted_title, extracted_author = extract_identity(
                fmt, data, filename=upload.name
            )
        if title:
            validated_data["title"] = title
        else:
            validated_data["title"] = (
                extracted_title or Path(upload.name).stem or "Untitled"
            )
        if author:
            validated_data["author"] = author
        elif extracted_author:
            validated_data["author"] = extracted_author

        book = super().create(validated_data)
        try:
            ingest_book(book, data=data)
        except IngestError as exc:
            book.delete()
            raise serializers.ValidationError({"file": str(exc)}) from exc
        except Exception:
            book.delete()
            raise serializers.ValidationError({"file": "Could not read this file."})
        return book


class HeartbeatSerializer(serializers.Serializer):
    current_page = serializers.IntegerField(min_value=1)
    seconds = serializers.IntegerField(min_value=0, max_value=MAX_HEARTBEAT_SECONDS)
