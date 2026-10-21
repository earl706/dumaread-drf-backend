import math
import os
import tempfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from ebooklib import ITEM_DOCUMENT, epub
from pypdf import PdfReader

from .constants import FORMAT_BY_EXT, WORDS_PER_PAGE
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


def epub_text(data):
    # ebooklib needs a filesystem path; BytesIO raises TypeError on stat().
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        try:
            book = epub.read_epub(path)
        except Exception as exc:
            raise IngestError("Could not read this EPUB.") from exc
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
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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


def ingest_book(book):
    """Populate page_count from the uploaded file. Mutates and saves the book."""
    data = read_file_bytes(book.file)
    if book.format == Book.FORMAT_PDF:
        book.page_count = pdf_page_count(data)
    else:
        text = extract_text_from_bytes(book.format, data)
        book.page_count = pages_from_text(text)
    book.save(update_fields=["page_count", "updated_at"])
    return book
