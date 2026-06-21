"""
Defensive PDF opening shared by build_question_bank.py and texts.py — basic
content validation before trusting an uploaded file with PyMuPDF (fitz).

Three checks, in order: (1) magic-byte header, cheap and catches a renamed
non-PDF before PyMuPDF ever touches it; (2) a parse timeout, since a
malformed/hostile PDF can make fitz.open() hang; (3) a page-count ceiling,
checked right after open (cheap metadata read) and before any caller loops
over every page — that loop is the actual memory/CPU cost, not the open()
call itself.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import fitz

MAX_PAGES = 2000
PARSE_TIMEOUT_SECONDS = 30


class UnsafePdfError(Exception):
    """Raised instead of letting a bad/hostile PDF reach PyMuPDF unchecked.
    Callers should catch this the same way they already catch fitz's own
    exceptions — it's just a clearer, earlier rejection."""


def looks_like_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def open_pdf_safely(path: Path) -> fitz.Document:
    path = Path(path)
    if not looks_like_pdf(path):
        raise UnsafePdfError(f"{path.name} does not look like a PDF (missing %PDF- header)")

    # Deliberately NOT a `with` block: ThreadPoolExecutor.__exit__ calls
    # shutdown(wait=True), which would block on a genuinely hung fitz.open()
    # — exactly the case this timeout exists to escape. shutdown(wait=False)
    # below lets the request return promptly; the orphaned worker thread (if
    # any) is leaked, which is an acceptable tradeoff for this app's low
    # request volume versus hanging the whole request indefinitely.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = ex.submit(fitz.open, str(path))
    try:
        doc = future.result(timeout=PARSE_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError as e:
        ex.shutdown(wait=False)
        raise UnsafePdfError(
            f"{path.name} took too long to open (possible malformed PDF)") from e
    ex.shutdown(wait=False)

    page_count = doc.page_count
    if page_count > MAX_PAGES:
        doc.close()
        raise UnsafePdfError(
            f"{path.name} has {page_count} pages, exceeding the {MAX_PAGES}-page limit")
    return doc
