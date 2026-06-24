"""
Normalize .docx/.doc test/key submissions to PDF via headless LibreOffice,
so the rest of the pipeline (process_pair, pdf_safety, review.html's
page-based rendering/capture) only ever has to deal with PDFs.

scioly.org test submissions are mostly PDF, but a meaningful slice (~7% of
all linked files, by direct count of scioly_tests.json) are .docx/.doc.
Word documents have no native stored page-boundary concept, while every
extraction/review mechanism in this codebase is fundamentally page-based —
so rather than building a second, page-less extraction path, a .docx/.doc
is converted to a real PDF once at ingestion time and flows through the
existing pipeline completely unchanged. The original file is kept as the
source of record; only a sibling PDF is generated alongside it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CONVERT_TIMEOUT_SECONDS = 60


class DocConvertError(Exception):
    """Raised when a .docx/.doc can't be turned into a PDF — missing
    `soffice`, a malformed input file, or a hung/failed conversion. Callers
    should surface this loudly (e.g. as a failed job) rather than silently
    skipping the file, since a silent skip is exactly how these files went
    unnoticed for so long in the first place."""


def looks_like_docx(path: Path) -> bool:
    """.docx is a ZIP archive — magic-byte check, mirroring
    pdf_safety.looks_like_pdf's `%PDF-` check. Legacy .doc (OLE2 binary
    format) has its own different magic bytes, but since both formats are
    handed to the same `soffice` conversion regardless, callers should just
    trust the file extension for .doc rather than sniffing for it here."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def looks_like_doc(path: Path) -> bool:
    """Legacy .doc is an OLE2 compound file — magic-byte check. Only 11 of
    ~2000 scraped links are .doc (vs. 144 .docx), so this rides the same
    convert_to_pdf() path rather than getting bespoke handling."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def _soffice_path() -> str:
    exe = shutil.which("soffice") or shutil.which("soffice.bin")
    if not exe:
        raise DocConvertError(
            "LibreOffice ('soffice') is not installed or not on PATH. "
            "Install it to enable .docx/.doc ingestion, e.g.:\n"
            "  RHEL/Fedora:   sudo dnf install libreoffice-headless\n"
            "  Debian/Ubuntu: sudo apt install libreoffice"
        )
    return exe


def convert_to_pdf(src: Path, dest_dir: Path,
                    timeout_s: int = CONVERT_TIMEOUT_SECONDS) -> Path:
    """Convert `src` (.docx or .doc) to a PDF in `dest_dir`, returning the
    resulting PDF's path. Raises DocConvertError on any failure — missing
    `soffice`, a timeout, or a non-zero exit — rather than returning None,
    so a bad file can't silently vanish from a discovery loop.

    `HOME` is overridden to a fresh scratch directory for the subprocess:
    LibreOffice headless creates a user-profile directory on first run, and
    a writable `$HOME` isn't guaranteed for the account this app runs as
    (e.g. a systemd service user) — without this, conversion can hang
    waiting on a profile lock instead of failing fast."""
    src = Path(src)
    soffice = _soffice_path()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir:
        env = dict(os.environ)
        env["HOME"] = profile_dir
        try:
            result = subprocess.run(
                [soffice, "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", str(dest_dir), str(src)],
                capture_output=True, text=True, timeout=timeout_s, env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise DocConvertError(
                f"{src.name}: conversion timed out after {timeout_s}s") from e

    if result.returncode != 0:
        raise DocConvertError(
            f"{src.name}: soffice exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}")

    out = dest_dir / (src.stem + ".pdf")
    if not out.exists():
        raise DocConvertError(
            f"{src.name}: conversion reported success but {out.name} was not produced")
    return out
