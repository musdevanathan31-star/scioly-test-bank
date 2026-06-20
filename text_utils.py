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


def strip_points(s: str) -> str:
    """Remove parenthetical point-value markers like '(2 points)' from text.

    Also NFKC-normalises so non-breaking spaces, fullwidth digits, and other
    compatibility chars collapse to ASCII equivalents before downstream regexes
    run. Without this, scanned-PDF text full of U+00A0/U+200B/U+2028 silently
    confuses split_choices and the Q_START anchor.
    """
    s = unicodedata.normalize("NFKC", s or "")
    # Strip zero-width characters that NFKC doesn't fold (these mostly come
    # from copy-pasted academic text and break regex anchors).
    s = s.replace("​", "").replace("‌", "").replace("‍", "").replace("﻿", "")
    s = _POINTS_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()
