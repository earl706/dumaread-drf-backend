import math
import os
import re
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from django.core.files.base import ContentFile
from ebooklib import ITEM_COVER, ITEM_DOCUMENT, ITEM_IMAGE, epub
from PIL import Image
from pypdf import PdfReader

from .constants import (
    COVER_JPEG_QUALITY,
    COVER_MAX_WIDTH,
    FORMAT_BY_EXT,
    WORDS_PER_PAGE,
)
from .models import Book


class IngestError(ValueError):
    """Raised when a book file cannot be read or paginated."""


# -----------------------------------------------------------------------------
# Format + pagination helpers
# -----------------------------------------------------------------------------
def detect_format(filename):
    ext = Path(filename or "").suffix.lower()
    fmt = FORMAT_BY_EXT.get(ext)
    if not fmt:
        raise IngestError("Only .pdf, .epub, and .txt files are supported.")
    return fmt


def tokenize(text):
    return (text or "").split()


def pages_from_word_count(word_count):
    if word_count <= 0:
        return 1
    return max(1, math.ceil(word_count / WORDS_PER_PAGE))


def pages_from_text(text):
    return pages_from_word_count(len(tokenize(text)))


class _HTMLTextExtractor(HTMLParser):
    """Keep headings, paragraphs, and list items so the reader can paginate them."""

    SKIP = {"script", "style"}
    HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
    PARA = {"p", "div", "section", "article", "blockquote", "tr", "table"}

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._chunks.append("\n")
        elif tag == "li":
            self._chunks.append("\n• ")
        elif tag in self.HEADING:
            self._chunks.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in self.HEADING or tag in self.PARA:
            self._chunks.append("\n\n")

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)

    def text(self):
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        out = []
        blank = False
        for line in lines:
            if not line:
                blank = True
                continue
            if out and blank:
                out.append("")
            out.append(line)
            blank = False
        return "\n".join(out).strip()


class _HTMLHeadingExtractor(HTMLParser):
    """First h1–h3 in document order, used as an EPUB title fallback."""

    SKIP = {"script", "style"}
    HEADING = {"h1", "h2", "h3"}

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip = 0
        self._in_heading = False
        self.heading = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip or self.heading:
            return
        if tag in self.HEADING:
            self._in_heading = True
            self._chunks = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
            return
        if self._in_heading and tag in self.HEADING:
            self._in_heading = False
            self.heading = " ".join("".join(self._chunks).split())

    def handle_data(self, data):
        if self._in_heading and not self._skip:
            self._chunks.append(data)


def first_html_heading(html):
    parser = _HTMLHeadingExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return ""
    return parser.heading


def html_to_text(html):
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return " ".join((html or "").split())
    return parser.text()


def decode_text_bytes(data):
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# -----------------------------------------------------------------------------
# Per-format extractors
# -----------------------------------------------------------------------------
def pdf_page_count(data):
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise IngestError("Could not read this PDF.") from exc
    count = len(reader.pages)
    if count <= 0:
        raise IngestError("This PDF has no pages.")
    return count


@contextmanager
def _epub_book(data, *, required=True):
    # ebooklib needs a filesystem path; BytesIO raises TypeError on stat().
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        try:
            book = epub.read_epub(path)
        except Exception as exc:
            if required:
                raise IngestError("Could not read this EPUB.") from exc
            yield None
            return
        yield book
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def epub_text(data):
    with _epub_book(data) as book:
        chunks = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            raw = item.get_content() or b""
            html = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, bytes)
                else str(raw)
            )
            chunks.append(html_to_text(html))
        text = " ".join(chunk for chunk in chunks if chunk)
        if not text:
            raise IngestError("This EPUB has no readable text.")
        return text


def extract_text_from_bytes(fmt, data):
    if fmt == Book.FORMAT_TXT:
        return decode_text_bytes(data)
    if fmt == Book.FORMAT_EPUB:
        return epub_text(data)
    raise IngestError("Plain text is only available for EPUB and TXT files.")


def read_file_bytes(file_field):
    file_field.open("rb")
    try:
        file_field.seek(0)
        return file_field.read()
    finally:
        file_field.close()


def read_upload_bytes(upload):
    """Read an uploaded file and rewind so Django can still store it."""
    if hasattr(upload, "seek"):
        upload.seek(0)
    data = upload.read()
    if hasattr(upload, "seek"):
        upload.seek(0)
    return data


def ingest_book(book, data=None):
    """Populate page_count and cover from the uploaded file. Mutates and saves."""
    if data is None:
        data = read_file_bytes(book.file)
    if book.format == Book.FORMAT_PDF:
        book.page_count = pdf_page_count(data)
    else:
        text = extract_text_from_bytes(book.format, data)
        book.page_count = pages_from_text(text)
    update_fields = ["page_count", "updated_at"]
    if attach_cover(book, data=data):
        update_fields.append("cover")
    book.save(update_fields=update_fields)
    return book


# -----------------------------------------------------------------------------
# Title + author extraction (metadata first, then visible text)
# -----------------------------------------------------------------------------
_IDENTITY_MAX = 255
_AUTHOR_LINE_RE = re.compile(
    r"^(?:by|written by|authors?)\s*[:\-–—]?\s+(.+)$",
    re.IGNORECASE,
)
_BAD_TITLE_START_RE = re.compile(
    r"^(chapter|part|book|volume|contents|copyright|isbn|published|www\.|https?://)",
    re.IGNORECASE,
)
_NAME_PUNCT = set(".''’-")
_JUNK_TITLES = {
    "untitled",
    "unknown",
    "unknown title",
    "title",
    "book",
    "document",
    "new document",
    "microsoft word",
    "adobe acrobat",
    "adobe acrobat pdf",
    "scan",
    "scanned",
    "scanned document",
    "image",
    "photo",
    "pdf",
    "epub",
    "text",
    "none",
    "n/a",
    "na",
    "null",
    "layout",
    "cover",
    "cover page",
    "title page",
    "page",
    "page 1",
    "chapter",
    "chapter 1",
    "contents",
    "table of contents",
    "copyright",
    "copyright page",
}
_JUNK_TITLE_PREFIXES = ("microsoft word", "adobe acrobat", "untitled")
_JUNK_AUTHORS = {
    "unknown",
    "unknown author",
    "anonymous",
    "n/a",
    "na",
    "none",
    "null",
    "admin",
    "user",
    "author",
    "the author",
    "microsoft word",
    "adobe",
    "adobe acrobat",
    "writer",
    "google",
    "chrome",
    "mozilla",
    "preview",
}
_BAD_AUTHOR_WORDS = {
    "chapter",
    "contents",
    "copyright",
    "volume",
    "edition",
    "published",
    "publisher",
    "isbn",
    "introduction",
    "preface",
    "prologue",
    "foreword",
}
_SKIP_EPUB_PARTS = (
    "nav",
    "toc",
    "ncx",
    "copyright",
    "colophon",
    "dedication",
    "acknowledg",
)


def _clean_space(value):
    return " ".join(str(value or "").replace("\x00", " ").split())


def _clip_identity(value, max_len=_IDENTITY_MAX):
    text = _clean_space(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _strip_wrapping_quotes(text):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”‘’":
        return text[1:-1].strip()
    return text


def _is_junk_title(title, filename=None):
    text = _clean_space(title)
    if not text:
        return True
    lowered = text.lower().strip(" ._-")
    if lowered in _JUNK_TITLES:
        return True
    if any(lowered.startswith(prefix) for prefix in _JUNK_TITLE_PREFIXES):
        return True
    if "/" in text or "\\" in text:
        return True
    if lowered.endswith((".pdf", ".epub", ".txt", ".doc", ".docx")):
        return True
    if filename:
        stem = Path(filename).stem.lower()
        name = Path(filename).name.lower()
        if stem and lowered == stem:
            return True
        if name and lowered == name:
            return True
    return False


def _usable_title(value, filename=None):
    text = _strip_wrapping_quotes(_clip_identity(value))
    if _is_junk_title(text, filename):
        return ""
    return text


def _usable_author(value):
    text = _strip_wrapping_quotes(_clip_identity(value))
    if not text:
        return ""
    lowered = text.lower().strip(" .")
    if lowered in _JUNK_AUTHORS:
        return ""
    if lowered.startswith("microsoft ") or lowered.startswith("adobe "):
        return ""
    return text


def _join_authors(values):
    seen = set()
    names = []
    for raw in values:
        text = _usable_author(raw)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(text)
    return ", ".join(names)


def _looks_like_title(line):
    text = _clean_space(line)
    if not 2 <= len(text) <= 120:
        return False
    if _is_junk_title(text):
        return False
    if _BAD_TITLE_START_RE.match(text):
        return False
    if re.fullmatch(r"[\divxlc]+", text.lower()):
        return False
    if text.endswith(".") and len(text.split()) > 8:
        return False
    return True


def _looks_like_author_name(line):
    text = _clean_space(line).strip(" .")
    if not text or len(text) > 80:
        return False
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts or len(parts) > 6:
        return False
    tokens = []
    for part in parts:
        tokens.extend(part.split())
    if not 1 <= len(tokens) <= 12:
        return False
    lowered = {token.lower().strip(".") for token in tokens}
    if lowered & _BAD_AUTHOR_WORDS:
        return False
    if any(char.isdigit() for char in text):
        return False
    honorifics = {"jr", "sr", "ii", "iii", "iv"}
    ok = sum(
        1
        for token in tokens
        if _is_name_token(token) or token.lower().strip(".") in honorifics
    )
    return ok >= max(1, int(len(tokens) * 0.7))


def _is_name_token(token):
    if not token:
        return False
    letters = 0
    for char in token:
        if char.isalpha():
            letters += 1
        elif char not in _NAME_PUNCT:
            return False
    return letters >= 1


def _author_from_line(line):
    match = _AUTHOR_LINE_RE.match(_clean_space(line))
    if not match:
        return ""
    candidate = re.sub(r"\s+(?:and|&)\s+", ", ", match.group(1).strip(" ."))
    if not candidate or len(candidate) > 80:
        return ""
    if not _looks_like_author_name(candidate):
        return ""
    return _usable_author(candidate)


def _identity_from_lines(lines, *, force_first_title=False):
    cleaned = [_clean_space(line) for line in lines]
    cleaned = [line for line in cleaned if line][:24]
    title = ""
    author = ""
    if force_first_title and cleaned:
        first = _clip_identity(_strip_wrapping_quotes(cleaned[0]))
        if first and not _is_junk_title(first) and len(first) <= 120:
            title = first
            cleaned = cleaned[1:]
    for index, line in enumerate(cleaned):
        by_author = _author_from_line(line)
        if by_author:
            if not author:
                author = by_author
            if title:
                break
            continue
        if not title and _looks_like_title(line):
            title = _clip_identity(_strip_wrapping_quotes(line))
            continue
        if title and not author and _looks_like_author_name(line):
            author = _usable_author(line)
            break
        if title and index >= 2:
            break
    return title, author


def _xmp_title(xmp):
    value = getattr(xmp, "dc_title", None)
    if isinstance(value, dict):
        return value.get("x-default") or next(iter(value.values()), "")
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _xmp_creators(xmp):
    value = getattr(xmp, "dc_creator", None)
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _pdf_meta_from_reader(reader, filename=None):
    title = ""
    author = ""
    info = getattr(reader, "metadata", None)
    if info:
        title = _usable_title(getattr(info, "title", None), filename)
        author = _usable_author(getattr(info, "author", None))
    if title and author:
        return title, author
    try:
        xmp = reader.xmp_metadata
    except Exception:
        xmp = None
    if xmp:
        if not title:
            title = _usable_title(_xmp_title(xmp), filename)
        if not author:
            author = _join_authors(_xmp_creators(xmp))
    return title, author


def _pdf_text_from_reader(reader, filename=None):
    title = ""
    author = ""
    pages = getattr(reader, "pages", None) or []
    for page in pages[:3]:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        found_title, found_author = _identity_from_lines(text.splitlines())
        if found_title and not title:
            title = found_title
        if found_author and not author:
            author = found_author
        if title:
            break
    return _usable_title(title, filename) or title, author


def _pdf_identity(data, filename=None):
    try:
        reader = PdfReader(BytesIO(data))
    except Exception:
        return "", ""
    title, author = _pdf_meta_from_reader(reader, filename)
    if not title or not author:
        found_title, found_author = _pdf_text_from_reader(reader, filename)
        title = title or found_title
        author = author or found_author
    return title, author


def _epub_dc_values(book, name):
    values = []
    try:
        pairs = book.get_metadata("DC", name) or []
    except Exception:
        return values
    for value, _attrs in pairs:
        text = _clean_space(value)
        if text:
            values.append(text)
    return values


def _epub_meta_identity(book, filename=None):
    titles = _epub_dc_values(book, "title")
    creators = _epub_dc_values(book, "creator")
    title = _usable_title(titles[0] if titles else "", filename)
    return title, _join_authors(creators)


def _epub_documents(book):
    items = []
    seen = set()
    for idref, _linear in getattr(book, "spine", None) or []:
        item = book.get_item_with_id(idref)
        if item is not None:
            items.append(item)
            seen.add(item.get_id())
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        if item.get_id() not in seen:
            items.append(item)
    return items


def _epub_item_html(item):
    raw = item.get_content() or b""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _should_skip_epub_item(item):
    name = (item.get_name() or "").lower()
    return any(part in name for part in _SKIP_EPUB_PARTS)


def _epub_content_identity(book, filename=None):
    for item in _epub_documents(book):
        if _should_skip_epub_item(item):
            continue
        html = _epub_item_html(item)
        heading = first_html_heading(html)
        lines = html_to_text(html).splitlines()
        if heading:
            heading_norm = _clean_space(heading)
            lines = [heading] + [
                line for line in lines if _clean_space(line) != heading_norm
            ]
        found_title, found_author = _identity_from_lines(
            lines, force_first_title=bool(heading)
        )
        if found_title:
            return _usable_title(found_title, filename) or found_title, found_author
    return "", ""


def _epub_identity(data, filename=None):
    with _epub_book(data, required=False) as book:
        if book is None:
            return "", ""
        title, author = _epub_meta_identity(book, filename)
        if not title or not author:
            found_title, found_author = _epub_content_identity(book, filename)
            title = title or found_title
            author = author or found_author
        return title, author


def extract_identity(fmt, data, filename=""):
    """Best-effort title and author. Empty strings when nothing usable is found."""
    try:
        if fmt == Book.FORMAT_PDF:
            title, author = _pdf_identity(data, filename)
        elif fmt == Book.FORMAT_EPUB:
            title, author = _epub_identity(data, filename)
        elif fmt == Book.FORMAT_TXT:
            text = decode_text_bytes(data)
            title, author = _identity_from_lines(
                text.splitlines(), force_first_title=True
            )
        else:
            title, author = "", ""
    except Exception:
        return "", ""
    return title or "", author or ""


# -----------------------------------------------------------------------------
# Cover extraction (PDF first page, EPUB cover image)
# -----------------------------------------------------------------------------
def _to_jpeg(image):
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > COVER_MAX_WIDTH:
        ratio = COVER_MAX_WIDTH / image.width
        image = image.resize(
            (COVER_MAX_WIDTH, max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=COVER_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _bytes_to_jpeg(raw):
    if not raw:
        return None
    try:
        with Image.open(BytesIO(raw)) as image:
            image.load()
            return _to_jpeg(image)
    except Exception:
        return None


def _pdf_embedded_image(data):
    try:
        reader = PdfReader(BytesIO(data))
        if not reader.pages:
            return None
        images = getattr(reader.pages[0], "images", None) or []
        for image in images:
            jpeg = _bytes_to_jpeg(getattr(image, "data", None))
            if jpeg:
                return jpeg
    except Exception:
        return None
    return None


def _pdf_cover_bytes(data):
    pdf = None
    try:
        pdf = pdfium.PdfDocument(data)
        if len(pdf) <= 0:
            return None
        page = pdf[0]
        width = page.get_width() or 1
        scale = min(2.5, max(0.75, COVER_MAX_WIDTH / float(width)))
        image = page.render(scale=scale).to_pil()
        return _to_jpeg(image)
    except Exception:
        return _pdf_embedded_image(data)
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass


def _item_bytes(item):
    if not item:
        return None
    raw = item.get_content()
    return raw or None


def _epub_cover_bytes(book):
    for item in book.get_items_of_type(ITEM_COVER):
        raw = _item_bytes(item)
        if raw:
            return raw
    cover_id = None
    for _value, attrs in book.get_metadata("OPF", "cover") or []:
        cover_id = (attrs or {}).get("content") or (attrs or {}).get("id")
        if cover_id:
            break
    if cover_id:
        raw = _item_bytes(book.get_item_with_id(cover_id))
        if raw:
            return raw
    for candidate in ("cover", "cover-image", "cover_image"):
        raw = _item_bytes(book.get_item_with_id(candidate))
        if raw:
            return raw
    named = []
    for item in book.get_items_of_type(ITEM_IMAGE):
        raw = _item_bytes(item)
        if not raw:
            continue
        name = (item.get_name() or "").lower()
        if "cover" in name:
            return raw
        named.append(raw)
    return named[0] if named else None


def _epub_cover_from_bytes(data):
    with _epub_book(data, required=False) as book:
        if book is None:
            return None
        return _bytes_to_jpeg(_epub_cover_bytes(book))


def extract_cover_bytes(fmt, data):
    if fmt == Book.FORMAT_PDF:
        return _pdf_cover_bytes(data)
    if fmt == Book.FORMAT_EPUB:
        return _epub_cover_from_bytes(data)
    return None


def attach_cover(book, data=None):
    """Extract a JPEG cover if possible. Sets book.cover; does not save."""
    if not book.file:
        return False
    try:
        payload = data if data is not None else read_file_bytes(book.file)
        jpeg = extract_cover_bytes(book.format, payload)
    except Exception:
        return False
    if not jpeg:
        return False
    book.cover.save("cover.jpg", ContentFile(jpeg), save=False)
    return True


def ensure_cover(book):
    """True if the book has a cover, extracting from the file when missing."""
    if book.cover:
        return True
    if not attach_cover(book):
        return False
    book.save(update_fields=["cover", "updated_at"])
    return True
