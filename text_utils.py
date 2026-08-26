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


def parse_answer_letters(answer, choices) -> set[str]:
    """Parse an MCQ `answer` string into the set of choice letters it names,
    e.g. "A" -> {"A"}, "A, D, E" -> {"A", "D", "E"}, "a,b" -> {"A", "B"}.

    Returns an EMPTY set when `answer` doesn't parse as a set of choice
    letters — most commonly a prose answer with units ("12 volts") rather
    than a lettered pick. That empty-set return is the fallback signal: 23
    real MCQs in the bank have prose answers instead of letters, and every
    caller of this helper must treat an empty result as "not letter-shaped"
    rather than "student picked nothing."

    A letter with no matching entry in `choices` is dropped (a typo/stale
    letter in `answer` shouldn't silently count as a valid pick). This is
    intentionally the single place this parsing rule lives — every grading
    and rendering path (Python and JS) must import/port this exact logic
    rather than re-deriving it, so "A, D, E" means the same thing everywhere.
    """
    raw = (answer or "").strip()
    if not raw:
        return set()
    # Letter-shaped: one or more single A-Z letters separated by commas
    # (optional surrounding whitespace). Anything else — units, sentences,
    # numbers — is prose and falls back to empty.
    parts = [p.strip() for p in raw.split(",")]
    if not all(re.fullmatch(r"[A-Za-z]", p) for p in parts):
        return set()
    valid_letters = {(c.get("letter") or "").strip().upper() for c in (choices or [])}
    return {p.upper() for p in parts if p.upper() in valid_letters}


# Difficulty band mapping. `difficulty` is stored as a float in 0.0-1.0
# (scio.ly's own scale -- see spec.md for why a float rather than named
# tiers), but the UI works with named bands. Thresholds are deliberately
# skewed rather than even quartiles: scio.ly's observed distribution
# clusters at 0.2-0.5, so an even split would label most real questions
# "Easy". Mirrored exactly in common_ui.py's COMMON_JS (DIFFICULTY_BANDS /
# difficultyBand() / difficultyValueForBand()) -- keep both in lockstep;
# tests/test_difficulty_band_js.py checks they agree.
#   Easy       value <= 0.3        represented as 0.3
#   Medium     0.3  < value <= 0.5 represented as 0.5
#   Hard       0.5  < value <= 0.7 represented as 0.7
#   Very Hard  value >  0.7        represented as 0.9
DIFFICULTY_BANDS = (
    ("Easy", 0.3, 0.3),
    ("Medium", 0.5, 0.5),
    ("Hard", 0.7, 0.7),
    ("Very Hard", None, 0.9),
)


def difficulty_band(value) -> str | None:
    """Map a stored `difficulty` float onto its named band. `None` (or any
    unparseable value) means unrated and returns `None` -- never "Easy".
    Unrated is a distinct state, not a low value."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for name, upper, _rep in DIFFICULTY_BANDS:
        if upper is None or v <= upper:
            return name
    return "Very Hard"  # unreachable (last band's upper is None)


def difficulty_value_for_band(band: str | None) -> float | None:
    """Band name -> the representative float a picker writes. Unknown/None
    band -> None (unrated)."""
    for name, _upper, rep in DIFFICULTY_BANDS:
        if name == band:
            return rep
    return None


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
