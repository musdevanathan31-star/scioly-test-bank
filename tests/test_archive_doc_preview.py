"""
Previewing Word documents in the tournament archive.

Most scioly.org tests are distributed as `.docx`, so an archive browser that
only previews `.pdf` makes a coach download a file just to find out whether
it is the test or the key. `.doc`/`.docx` are converted to PDF once and
cached, then rendered through the same page pipeline as a real PDF.

Two properties matter and are covered here:

- The conversion is cached, and the cache lives OUTSIDE the archive tree.
  The archive is frequently mounted read-only (the app surfaces that as a
  first-class state), so writing a sibling PDF next to the source is not an
  option, and re-running LibreOffice per page would make paging unusable.
- A missing converter is information, not a crash. LibreOffice is not
  installable from the default repos on every target distribution, so the
  "no converter" path has to stay a clean, explanatory error.

Run with: `python -m pytest tests/test_archive_doc_preview.py -q`
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import doc_convert          # noqa: E402
import review_app as ra     # noqa: E402


# ---------------------------------------------------------------------------
# _soffice_path — locating a converter
# ---------------------------------------------------------------------------

def test_soffice_bin_override_accepts_an_absolute_path(monkeypatch):
    """The override exists so a Flatpak/container/hand-built LibreOffice can
    be named directly, without a shell shim called `soffice` on PATH."""
    monkeypatch.setenv("SOFFICE_BIN", sys.executable)
    assert doc_convert._soffice_path() == sys.executable


def test_soffice_bin_override_accepts_a_name_on_path(monkeypatch):
    monkeypatch.setenv("SOFFICE_BIN", Path(sys.executable).name)
    assert doc_convert._soffice_path()


def test_soffice_bin_override_that_does_not_exist_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("SOFFICE_BIN", "/definitely/not/here/soffice")
    with pytest.raises(doc_convert.DocConvertError) as e:
        doc_convert._soffice_path()
    assert "SOFFICE_BIN" in str(e.value)


def test_missing_converter_names_the_env_override_as_a_way_out(monkeypatch):
    monkeypatch.delenv("SOFFICE_BIN", raising=False)
    monkeypatch.setattr(doc_convert.shutil, "which", lambda _n: None)
    with pytest.raises(doc_convert.DocConvertError) as e:
        doc_convert._soffice_path()
    msg = str(e.value)
    assert "SOFFICE_BIN" in msg, "the error must point at the escape hatch"


# ---------------------------------------------------------------------------
# _archive_renderable_pdf — what actually gets rasterised
# ---------------------------------------------------------------------------

def test_pdf_passes_through_untouched(tmp_path):
    """A real PDF must not go anywhere near the converter."""
    src = tmp_path / "test.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    assert ra._archive_renderable_pdf(src) == src


def test_unknown_extension_passes_through(tmp_path):
    """Anything else is handed on unchanged so the existing not-a-PDF
    branch reports it, rather than being mistaken for a document."""
    src = tmp_path / "notes.txt"
    src.write_text("hello")
    assert ra._archive_renderable_pdf(src) == src


@pytest.mark.parametrize("ext", [".docx", ".doc", ".DOCX"])
def test_word_document_without_a_converter_raises_not_crashes(tmp_path, monkeypatch, ext):
    monkeypatch.delenv("SOFFICE_BIN", raising=False)
    monkeypatch.setattr(doc_convert.shutil, "which", lambda _n: None)
    src = tmp_path / f"tournament_test{ext}"
    src.write_bytes(b"PK\x03\x04not-really-a-docx")
    with pytest.raises(doc_convert.DocConvertError):
        ra._archive_renderable_pdf(src)


def test_conversion_result_is_cached_outside_the_archive(tmp_path, monkeypatch):
    """Second call must not re-run the converter, and nothing may be written
    into the directory holding the source document."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    src = archive_dir / "sometest.docx"
    src.write_bytes(b"PK\x03\x04not-really-a-docx")

    cache_root = tmp_path / "render_cache"
    monkeypatch.setattr(ra, "_RENDER_CACHE_DIR", cache_root)

    calls = []

    def fake_convert(s, dest_dir, *a, **k):
        calls.append(Path(s))
        out = Path(dest_dir) / "sometest.pdf"
        out.write_bytes(b"%PDF-1.4\nconverted\n")
        return out

    monkeypatch.setattr(doc_convert, "convert_to_pdf", fake_convert)

    first = ra._archive_renderable_pdf(src)
    second = ra._archive_renderable_pdf(src)

    assert first == second, "the cached conversion must be reused"
    assert len(calls) == 1, f"converter ran {len(calls)} times; must be cached"
    assert first.read_bytes().startswith(b"%PDF")
    assert cache_root in first.parents, "conversion must live under the render cache"
    assert archive_dir not in first.parents, "must never write into the archive tree"
    assert list(archive_dir.iterdir()) == [src], "archive dir must be untouched"


def test_replacing_the_source_document_invalidates_the_conversion(tmp_path, monkeypatch):
    """Keyed on mtime+size, so a swapped-in document is not served as the
    previous one's pages."""
    src = tmp_path / "t.docx"
    src.write_bytes(b"PK\x03\x04one")
    monkeypatch.setattr(ra, "_RENDER_CACHE_DIR", tmp_path / "cache")

    def fake_convert(s, dest_dir, *a, **k):
        out = Path(dest_dir) / "t.pdf"
        out.write_bytes(b"%PDF-1.4\n" + Path(s).read_bytes())
        return out

    monkeypatch.setattr(doc_convert, "convert_to_pdf", fake_convert)

    first = ra._archive_renderable_pdf(src)
    os.utime(src, (1, 1))
    src.write_bytes(b"PK\x03\x04two-and-longer")
    second = ra._archive_renderable_pdf(src)
    assert first != second, "a different source must produce a different cache entry"
