# Shared pagination + upload limits. Keep in sync with frontend/src/lib/reading.js.

WORDS_PER_PAGE = 300
MAX_BOOK_BYTES = 50 * 1024 * 1024
MAX_HEARTBEAT_SECONDS = 120

ALLOWED_EXTENSIONS = {".pdf", ".epub", ".txt"}
FORMAT_BY_EXT = {
    ".pdf": "pdf",
    ".epub": "epub",
    ".txt": "txt",
}
CONTENT_TYPE_BY_FORMAT = {
    "pdf": "application/pdf",
    "epub": "application/epub+zip",
    "txt": "text/plain; charset=utf-8",
}
