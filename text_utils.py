"""
Lightweight text-normalisation helpers shared across the pipeline.

These were originally defined inside `build_question_bank.py`, which made
small consumers (scrape_scioly, qgen, texts) transitively import the entire
fitz/Anthropic-dependent pipeline just to call `_strip_points`. This module
breaks that dependency.

`build_question_bank.py` re-exports `_strip_points` for backwards compatibility
so existing imports (and tests using `bqb._strip_points`) keep working.
"""
from __future__ import annotations

import re
import unicodedata


# "(2 points)", "(1 point)", "(½ pt)", "[2pt]", "(3 pts each)" etc.
_POINTS_RE = re.compile(
    r"[\(\[]\s*[\d¼½¾⅛-⅞\./]+\s*"
    r"(?:point|pt|pts|points)s?\.?(?:\s+each)?\s*[\)\]]",
    re.IGNORECASE,
)

# Zero-width chars (ZWSP/ZWNJ/ZWJ/BOM) plus the Unicode bidi embedding/
# override (U+202A-U+202E) and isolate (U+2066-U+2069) control characters.
# Some PDF generators wrap every text run in bidi override marks (observed:
# scioly.org's disease_detectives bakingsoda 2026 test) — invisible in any
# viewer, but they break every "^"-anchored regex downstream since "^" no
# longer lines up with the first visible character. Built from chr() rather
# than embedding literal invisible characters in this source file.
_INVISIBLE_CODEPOINTS = (
    [0x200B, 0x200C, 0x200D, 0xFEFF]      # ZWSP, ZWNJ, ZWJ, BOM
    + list(range(0x202A, 0x202F))          # bidi embedding/override (LRE..RLO, PDF)
    + list(range(0x2066, 0x206A))          # bidi isolate (LRI, RLI, FSI, PDI)
)
_INVISIBLE_CHARS_RE = re.compile(
    "[" + "".join(chr(cp) for cp in _INVISIBLE_CODEPOINTS) + "]"
)


def normalize_unicode(s: str) -> str:
    """NFKC-normalise and strip invisible formatting characters (zero-width
    joiners, BOM, Unicode bidi embedding/override/isolate marks). Preserves
    whitespace/newlines, unlike strip_points() below — callers that need
    line structure (split_choices_by_lines, split_column_items) call this
    directly; strip_points() layers point-marker removal and whitespace
    collapsing on top for callers that don't care about line structure."""
    s = unicodedata.normalize("NFKC", s or "")
    return _INVISIBLE_CHARS_RE.sub("", s)


def strip_points(s: str) -> str:
    """Remove parenthetical point-value markers like '(2 points)' from text.

    Also normalises via normalize_unicode() so non-breaking spaces, fullwidth
    digits, zero-width/bidi-control chars, and other compatibility chars
    collapse or disappear before downstream regexes run. Without this,
    scanned-PDF text full of U+00A0/U+200B/U+2028 silently confuses
    split_choices and the Q_START anchor.
    """
    s = normalize_unicode(s)
    s = _POINTS_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()
