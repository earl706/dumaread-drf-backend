from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Greatest
from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.common.viewsets import OwnedModelViewSet

from .constants import CONTENT_TYPE_BY_FORMAT
from .ingest import IngestError, extract_text_from_bytes, read_file_bytes
from .models import Book
from .serializers import BookSerializer, HeartbeatSerializer
from .services import _days_param, apply_heartbeat, reading_activity_timeline


# -----------------------------------------------------------------------------
# Book viewset
# -----------------------------------------------------------------------------
class BookViewSet(OwnedModelViewSet):
    serializer_class = BookSerializer
    queryset = Book.objects.all()
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["status", "format"]
    search_fields = ["title", "author"]
    ordering_fields = [
        "created_at",
        "updated_at",
        "title",
        "status",
        "format",
        "page_count",
        "current_page",
        "last_read_at",
        "total_reading_seconds",
    ]

    @extend_schema(responses={200: dict})
    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        qs = self.get_queryset()
        active = qs.exclude(status=Book.STATUS_ARCHIVED)
        agg = active.aggregate(
            total=Count("id"),
            reading=Count("id", filter=Q(status=Book.STATUS_READING)),
            completed=Count("id", filter=Q(status=Book.STATUS_COMPLETED)),
            unread=Count("id", filter=Q(status=Book.STATUS_UNREAD)),
            pages_left=Sum(Greatest(F("page_count") - F("current_page"), Value(0))),
            total_reading_seconds=Sum("total_reading_seconds"),
        )
        return Response(
            {
                "total": agg["total"] or 0,
                "reading": agg["reading"] or 0,
                "completed": agg["completed"] or 0,
                "unread": agg["unread"] or 0,
                "pages_left": agg["pages_left"] or 0,
                "total_reading_seconds": agg["total_reading_seconds"] or 0,
            }
        )

    @extend_schema(
        parameters=[
            OpenApiParameter("days", int, description="Lookback window (1–366)")
        ],
        responses={200: dict},
    )
    @action(detail=False, methods=["get"], url_path="activity")
    def activity(self, request):
        """Contribution-style timeline of minutes read per day."""
        days = _days_param(request.query_params.get("days"), default=365, maximum=366)
        return Response(
            {
                "days": days,
                "timeline": reading_activity_timeline(request.user, days=days),
            }
        )

    @extend_schema(responses={200: BookSerializer})
    @action(detail=True, methods=["post"], url_path="heartbeat")
    def heartbeat(self, request, pk=None):
        book = self.get_object()
        serializer = HeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = apply_heartbeat(
            book,
            serializer.validated_data["current_page"],
            serializer.validated_data["seconds"],
        )
        return Response(BookSerializer(updated).data)

    @action(detail=True, methods=["get"], url_path="file")
    def download_file(self, request, pk=None):
        book = self.get_object()
        if not book.file:
            raise Http404("No file attached.")
        content_type = CONTENT_TYPE_BY_FORMAT.get(
            book.format, "application/octet-stream"
        )
        return FileResponse(
            book.file.open("rb"),
            as_attachment=False,
            filename=_download_name(book),
            content_type=content_type,
        )

    @action(detail=True, methods=["get"], url_path="text")
    def extracted_text(self, request, pk=None):
        book = self.get_object()
        if book.format == Book.FORMAT_PDF:
            return Response(
                {"detail": "Plain text is only available for EPUB and TXT files."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            data = read_file_bytes(book.file)
            text = extract_text_from_bytes(book.format, data)
        except IngestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"detail": "Could not extract text from this file."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"text": text, "page_count": book.page_count})


def _download_name(book):
    title = (book.title or "book").replace("/", "-")
    return f"{title}.{book.format}"
