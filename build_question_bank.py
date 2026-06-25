"""
Build a topic-wise Sci-Oly question bank from downloaded PDFs, for any
registered event (see events.py for the EVENTS registry).

Cost-optimised two-phase pipeline:
  Phase 1  PyMuPDF text extraction (free) + spatial y-coord image matching.
  Phase 2  Claude Haiku vision — ONLY for pages that have embedded images
           (to fix association) or pages with zero extractable text (OCR).

Set ANTHROPIC_API_KEY in environment or in a .env file next to this script.
Vision calls are skipped gracefully when no key is found (text-only mode).

Usage:
  python build_question_bank.py --event <slug>              # process all test PDFs
  python build_question_bank.py --event <slug> --limit 3    # first N tests (for testing)
  python build_question_bank.py --event <slug> --rebuild    # ignore all caches
  python build_question_bank.py --event <slug> --text-only  # skip vision API entirely

Outputs (under <event_slug>/):
  <event_slug>/question_bank.md
  <event_slug>/images/<topic>_<src>_p<n>_i<n>_<hash>.<ext>
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF
import pdf_safety
import doc_convert

import jobs
import llm_providers

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# A module-level logger keyed by file. The default handler writes to stderr
# with a short timestamp+level prefix so production deployments can filter
# warnings/errors from informational chatter (which CLI tools still print()
# directly for live progress).
#
# Override the threshold via:  LOG_LEVEL=DEBUG python ...
#
# logger.warning("…") and logger.error("…") are recommended for anything that
# should survive log redirection; user-facing CLI progress (download counts,
# extraction summaries) stays on print() so the operator sees them in real time.
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("scioly.bqb")

# ---------------------------------------------------------------------------
# LLM usage tracking
# ---------------------------------------------------------------------------
# Running tally of Anthropic API consumption for this process. Surfaced to the
# UI via /api/usage so users can spot accidental burn from a stuck retry loop
# or a runaway scrape before the invoice arrives.
#
# Pricing per million tokens, overridable via env. Defaults match Haiku 4.5.
ANTHROPIC_INPUT_PRICE_PER_MTOK = float(
    os.environ.get("ANTHROPIC_INPUT_PRICE_PER_MTOK", "1.00"))
ANTHROPIC_OUTPUT_PRICE_PER_MTOK = float(
    os.environ.get("ANTHROPIC_OUTPUT_PRICE_PER_MTOK", "5.00"))

# Per-model pricing table (USD per million tokens), used for cost *previews*
# shown in the UI before a call is made (e.g. the diagram-chat cost estimate)
# where the generic ANTHROPIC_*_PRICE_PER_MTOK above (tuned for Haiku) would
# understate the cost of a Sonnet/Opus call. Falls back to the generic pair
# for any model not listed here.
_MODEL_PRICING_PER_MTOK = {
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4":   (3.00, 15.00),
    "claude-opus-4":     (15.00, 75.00),
}


def price_for_model(model: str) -> tuple[float, float]:
    """(input_price_per_mtok, output_price_per_mtok) for `model`."""
    for prefix, prices in _MODEL_PRICING_PER_MTOK.items():
        if model.startswith(prefix):
            return prices
    return (ANTHROPIC_INPUT_PRICE_PER_MTOK, ANTHROPIC_OUTPUT_PRICE_PER_MTOK)


import threading as _t_usage
_usage_lock = _t_usage.Lock()
_usage_stats: dict = {
    "calls":         0,
    "input_tokens":  0,
    "output_tokens": 0,
}


def _track_usage_tokens(input_tokens: int, output_tokens: int) -> None:
    """Accumulate a raw input/output token count into the running tally.
    This dashboard only ever tracked Anthropic spend (the server's own
    ANTHROPIC_API_KEY) -- call sites using a browser-supplied key for a
    different provider should NOT feed this, since the cost math here is
    Anthropic-pricing-specific."""
    try:
        with _usage_lock:
            _usage_stats["calls"] += 1
            _usage_stats["input_tokens"]  += int(input_tokens or 0)
            _usage_stats["output_tokens"] += int(output_tokens or 0)
    except Exception:
        # Tally is best-effort; never let a missing field break the call site.
        pass


def _track_usage(response) -> None:
    """Accumulate the input/output tokens from an Anthropic SDK response."""
    u = getattr(response, "usage", None)
    if not u:
        return
    _track_usage_tokens(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0))


def get_usage_stats() -> dict:
    """Snapshot the usage tally plus an estimated USD cost."""
    with _usage_lock:
        s = dict(_usage_stats)
    cost = (s["input_tokens"]  / 1_000_000 * ANTHROPIC_INPUT_PRICE_PER_MTOK
          + s["output_tokens"] / 1_000_000 * ANTHROPIC_OUTPUT_PRICE_PER_MTOK)
    s["estimated_cost_usd"] = round(cost, 4)
    s["input_price_per_mtok"]  = ANTHROPIC_INPUT_PRICE_PER_MTOK
    s["output_price_per_mtok"] = ANTHROPIC_OUTPUT_PRICE_PER_MTOK
    return s

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

from events import Event, get_event, EVENTS  # noqa: E402

# Per-event scope via ContextVar — each request / worker thread gets its own
# active Event instead of mutating shared globals. PEP 562 module-level
# __getattr__ keeps the old `bqb.BASE_DIR` / `bqb.EVENT` / etc. read syntax
# working unchanged: every attribute access resolves through the ContextVar.
#
# No default: callers must explicitly set_event(slug) before touching
# bqb.EVENT/BASE_DIR/etc. — every entry point (review_app._select_event,
# this module's own main(), tests) already does so up front.
from contextvars import ContextVar

_current_event: ContextVar[Event] = ContextVar("scioly_current_event")


def set_event(slug: str) -> Event:
    """Bind the active event for the current context. Idempotent.

    Multiple concurrent requests/threads each get their own binding — the
    ContextVar mechanism is what makes that work without a serialising lock."""
    ev = get_event(slug)
    _current_event.set(ev)
    ev.base_dir.mkdir(exist_ok=True)
    ev.image_dir.mkdir(exist_ok=True)
    return ev


def current_event() -> Event | None:
    """Return the active event for this context, or None if set_event()
    hasn't been called yet. Unlike EVENT/_ev(), never raises — use this to
    check before reading EVENT when you're not sure one is bound yet."""
    return _current_event.get(None)


def _get_current() -> Event:
    """Resolve the active event, raising a clear error if set_event() was
    never called in this context (there is no implicit default event)."""
    try:
        return _current_event.get()
    except LookupError:
        raise RuntimeError(
            "No active event: call set_event(slug) before accessing "
            "EVENT/BASE_DIR/etc. (see events.py for registered slugs)."
        ) from None


def _ev() -> Event:
    """Short-hand for resolving the current active event.

    Used internally by this module since bare `EVENT.foo` references can't go
    through the module-level __getattr__ (PEP 562 only catches `bqb.EVENT`
    from OUTSIDE the module). External code keeps using `bqb.EVENT` etc.
    """
    return _get_current()


# Property-style attribute resolution at module level (PEP 562).
# Reads of `bqb.EVENT`, `bqb.BASE_DIR`, etc. now route through the ContextVar.
def __getattr__(name: str):
    if name == "EVENT":
        return _get_current()
    if name == "BASE_DIR":
        return _get_current().base_dir
    if name == "IMAGE_DIR":
        return _get_current().image_dir
    if name == "OUT_MD":
        return _get_current().out_md
    if name == "STATE_FILE":
        return _get_current().state_file
    if name == "TOPICS":
        return list(_get_current().topics)
    if name == "TOPIC_KEYWORDS":
        return _get_current().topic_keywords
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# .env loader (no extra dependency)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env = Path(__file__).parent / ".env"
    if not env.exists():
        env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()

# ---------------------------------------------------------------------------
# Anthropic client (lazy, optional)
# ---------------------------------------------------------------------------

_anthropic_client = None
# Vision model — cheapest with image support today. Override with the
# ANTHROPIC_VISION_MODEL env var when Anthropic ships a successor without
# requiring a code change.
VISION_MODEL = os.environ.get("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5-20251001")

# Diagram-generation model — Sonnet is markedly better than Haiku at producing
# clean, correct SVG markup for technical diagrams (circuits, anatomy, chem
# structures). Override with ANTHROPIC_DIAGRAM_MODEL if a stronger model
# ships. Used by the review-page "Generate diagram" chat panel.
DIAGRAM_MODEL = os.environ.get("ANTHROPIC_DIAGRAM_MODEL", "claude-sonnet-4-6")


def _get_client():
    global _anthropic_client
    if _anthropic_client is None:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            import anthropic
            # `max_retries=4` overrides the SDK default of 2; combined with the
            # SDK's built-in exponential backoff this gives us 5 attempts spread
            # over ~30s on rate-limits / 5xx / connection drops, which is the
            # right tradeoff for bulk operations (Validate-page, Generate, etc.)
            # without making single-call failures wait forever. 4xx errors
            # (bad key / bad request) still fail-fast — the SDK only retries
            # on 408/409/429 and 5xx.
            _anthropic_client = anthropic.Anthropic(
                api_key=key,
                max_retries=4,
                timeout=60.0,
            )
    return _anthropic_client


def _vision_available() -> bool:
    return _get_client() is not None

# ---------------------------------------------------------------------------
# Topic taxonomy + keyword scoring (per-event; see events.py)
# ---------------------------------------------------------------------------

# TOPICS / TOPIC_KEYWORDS are now resolved via __getattr__ above (PEP 562).
# Reads of `bqb.TOPICS` work but always reflect the active ContextVar event.


def classify_topic(text: str) -> str:
    t = text.lower()
    best, best_score = "Other / General", 0
    # Re-read through __getattr__ on every call so the active ContextVar event
    # is honoured. (Pulled into a local first to skip the descriptor on each
    # iteration of the inner loop.)
    topic_keywords = _get_current().topic_keywords
    for topic, kws in topic_keywords.items():
        score = sum(w for kw, w in kws if kw in t)
        if score > best_score:
            best_score, best = score, topic
    return best

# ---------------------------------------------------------------------------
# Question / answer parsing helpers
# ---------------------------------------------------------------------------

# Question start: "1.", "1)", "Q1.", "Question 1:"
Q_START = re.compile(
    # 1-3 digit number followed by . / ) / : — NOT whitespace alone, which
    # would false-match choice text like "100 N" or "300 mV" inside a question.
    r"^(?:question\s+)?(\d{1,3})[\.\)\:](?:\s|$)",
    re.IGNORECASE,
)

# Same anchor but searchable anywhere in a line. Requires whitespace before
# the number AND a sentence-like terminator after, so we don't accidentally
# split on "factor of 2." mid-sentence.
_INLINE_Q_START = re.compile(
    r"(?:(?<=\s)|(?<=\?)|(?<=\.))"      # preceded by whitespace, ?, or .
    r"(\d{1,3})[\.\)]\s+"               # number + . or ) + space
    r"(?=[A-Z(])"                       # next char looks like a question start
)

# Answer-key lines: "1. B", "1) 42 Ω"
ANS_LINE = re.compile(r"^(\d{1,3})[\.\)]\s*(.+)$")

KEY_INDICATORS = frozenset([
    "answer key", "answer sheet", "solutions", "key:", "answers:",
    "point value",
])

NOISE_PREFIXES = ("©", "science olympiad", "page ", "name:", "team:", "school:")


# Option marker on its own line (PyMuPDF often splits "a." off): "a.", "A)", etc.
_MC_MARKER_LINE = re.compile(r"^[A-Ea-e][\.\)]$")


# Text-normalisation primitives now live in text_utils.py so small consumers
# (scrape_scioly, qgen) don't need to import the entire pipeline just to call
# strip_points. The aliases below keep the long-standing _strip_points name
# available for backwards compatibility with existing tests + callers.
from text_utils import strip_points as _strip_points, _POINTS_RE  # noqa: E402


def _is_noise(line: str) -> bool:
    l = line.lower()
    if _MC_MARKER_LINE.match(line):
        return False         # keep MC option markers
    return len(line) < 3 or any(l.startswith(p) for p in NOISE_PREFIXES)


# Match an MC option marker: " a. ", " B) ", or " (a) " — three common formats.
# Letter captured by whichever group matched.
#
# Three alternatives, in order:
#   1. Parenthesized form "(a)" or "(B)" — unambiguous, always match.
#   2. Lowercase "a." or "a)" preceded by whitespace — always match. Lowercase
#      single letters are very rarely used as unit suffixes after a digit, so
#      we don't need the digit guard here (and we'd otherwise break choices
#      ending in digits like "1:2 b. 2:1").
#   3. Uppercase "A." or "A)" preceded by whitespace — guarded against matching
#      unit suffixes like " C." after "9 C." (Coulombs), " A." after "5 A."
#      (amperes), etc. Without this, Q#9-style choices "a. 6 C b. 3 C c. 12 C"
#      false-match a phantom "C." and break the ascending-letter check.
_MC_OPTION = re.compile(
    r"(?:\(([A-Ea-e])\))"                     # group 1: (a), (B)
    r"|(?:(?:^|\s)([a-e])[\.\)]\s+)"          # group 2: a., a)  (no digit guard)
    r"|(?:(?:^|(?<!\d)\s)([A-E])[\.\)]\s+)"   # group 3: A., A)  (digit-guarded)
)


def _opt_letter(m: re.Match) -> str:
    return (m.group(1) or m.group(2) or m.group(3)).upper()


# A real MC stem is interrogative — it asks something. Without one of these
# markers it's likely we sliced into the middle of a question (lost-A case).
# Vocabulary covers interrogatives + imperatives + declarative "given/suppose"
# style stems common in physics/chemistry/bio questions.
_STEM_QUESTION_HINT = re.compile(
    r"[\?\:]|_{3,}|"
    r"\b("
    # interrogative
    r"which|what|who|how|when|where|why|"
    # imperative — "do X"
    r"describe|identify|calculate|find|name|list|draw|circle|select|choose|"
    r"explain|determine|compute|state|define|evaluate|estimate|approximate|"
    r"derive|prove|show|verify|sketch|graph|plot|label|match|order|rank|"
    r"convert|express|simplify|solve|deduce|infer|conclude|"
    # declarative setup — "given/suppose/let X, ..."
    r"given|suppose|assume|consider|let|imagine|recall|note that|"
    # quantifier setup — "the X of Y is"
    r"the (?:value|ratio|magnitude|sign|direction|sum|product|difference"
        r"|integral|derivative|limit|maximum|minimum|average|mean|median"
        r"|probability|fraction|percentage|coefficient) of|"
    # T/F shorthand
    r"true or false|true/false|t/f|"
    # imperative-question prefix common in Sci-Oly
    r"which of the (?:following|above)|all of the following"
    r")\b",
    re.IGNORECASE,
)


def split_choices_by_lines(raw: str) -> tuple[str, list[dict]]:
    """
    Fallback splitter that uses newline structure preserved by PyMuPDF.
    Useful when a user drag-captures a choices region; the on-page layout
    typically has each choice on its own line, but space-collapsed text loses
    that signal.

    Returns ("", choices) when each non-empty line looks like a choice,
    or (text_minus_choices, choices) when a stem precedes the first marker.
    Returns (raw, []) if it can't find a confident split.
    """
    if not raw:
        return raw, []
    lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) < 2:
        return raw, []
    # Strategy 1: each line starts with a marker (the common multi-line case)
    marker_re = re.compile(r"^[\(]?([A-Ea-e])[\)\.]\s*(.*)$")
    parsed: list[dict] = []
    pre_stem: list[str] = []
    seen_marker = False
    for ln in lines:
        m = marker_re.match(ln)
        if m:
            seen_marker = True
            parsed.append({"letter": m.group(1).upper(), "text": (m.group(2) or "").strip()})
        else:
            if seen_marker and parsed:
                # Continuation of the previous choice (text wrapped)
                parsed[-1]["text"] = (parsed[-1]["text"] + " " + ln).strip()
            else:
                pre_stem.append(ln)
    # Accept if 2-5 choices, letters ascending from A
    if 2 <= len(parsed) <= 5:
        letters = [c["letter"] for c in parsed]
        expected = [chr(ord("A") + i) for i in range(len(parsed))]
        if letters == expected and all(c["text"] for c in parsed):
            stem = " ".join(pre_stem).strip()
            return stem, parsed
    # Strategy 2: no markers at all, but 2-5 non-empty lines -- treat each
    # line as a positional choice A/B/C/D/E (last resort)
    if not seen_marker and 2 <= len(lines) <= 5:
        out = [{"letter": chr(ord("A") + i), "text": ln} for i, ln in enumerate(lines)]
        return "", out
    return raw, []


def split_choices(text: str) -> tuple[str, list[dict]]:
    """
    Split a question into (stem, [choices]).
    Returns (text, []) if it doesn't look like an MC question.

    Handles two cases:
      1. Explicit letters present for all options: "A. X B. Y C. Z"
      2. PDF stripped some letters (e.g. only "b." and "d." survived) — the
         missing options are inferred from the surviving order.
    """
    matches = list(_MC_OPTION.finditer(text))
    if len(matches) < 2:
        return text, []

    letters = [_opt_letter(m) for m in matches]
    # Must be strictly ascending within A–E
    if letters != sorted(set(letters)) or letters[-1] not in "BCDE":
        return text, []
    # Last letter implies the full set: e.g. last="D" -> A,B,C,D
    last_idx = ord(letters[-1]) - ord("A")
    expected = [chr(ord("A") + i) for i in range(last_idx + 1)]
    if len(expected) < 2 or len(expected) > 5:
        return text, []

    # Stem = text before the first observed marker, possibly minus implicit A.
    first_marker_start = matches[0].start()
    stem = text[:first_marker_start].strip()
    choices: list[dict] = []

    if letters[0] == "A":
        # Clean case: walk through expected letters, all explicit
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            choices.append({"letter": _opt_letter(m), "text": text[start:end].strip()})
    else:
        # Implicit A (and possibly C, etc.) — infer using stem-end heuristics
        # Find a stem boundary inside the pre-marker text
        pre = text[:first_marker_start]
        qmark   = pre.rfind("?")
        underbl = pre.rfind("____")
        # Take whichever comes last; period-based split is too noisy for stems
        boundary = max(qmark, underbl)
        if boundary == -1:
            return text, []
        # Advance past the punctuation/underscores
        if boundary == qmark:
            boundary += 1
        else:
            # skip the run of underscores and trailing space
            while boundary < len(pre) and pre[boundary] == "_":
                boundary += 1
        stem = text[:boundary].strip()

        # Now walk expected letters and slice
        j = 0
        prev_end = boundary
        for letter in expected:
            if j < len(matches) and letters[j] == letter:
                start = matches[j].end()
                end = matches[j + 1].start() if j + 1 < len(matches) else len(text)
                choices.append({"letter": letter, "text": text[start:end].strip()})
                prev_end = end
                j += 1
            else:
                # Implicit letter — its text spans from prev_end to the next observed marker
                next_start = matches[j].start() if j < len(matches) else len(text)
                seg = text[prev_end:next_start].strip()
                choices.append({"letter": letter, "text": seg})
                prev_end = next_start

    # ---- sanity checks ----
    # Every choice must have non-empty text
    if any(not c["text"] for c in choices):
        return text, []
    # Reject if any choice's text is itself just a marker like "B." or starts
    # with one (catches the "C." -> "D." mis-split case)
    for c in choices:
        if _MC_MARKER_LINE.match(c["text"]):
            return text, []
        if _MC_OPTION.match(" " + c["text"]):
            # text immediately begins with another marker
            head = c["text"][:5]
            if re.match(r"^[A-Ea-e][\.\)]\s", head):
                return text, []
    # MC options are usually short; sub-question parts (a)..b)..c)) tend to be
    # very long. Reject only at the extreme upper end so we don't drop long
    # MC stems (some Sci-Oly stems are full sentences).
    avg_len = sum(len(c["text"]) for c in choices) / len(choices)
    if avg_len > 150:
        return text, []
    # Stem must be meaningful (not just leftover noise)
    if len(stem) < 8:
        return text, []
    # Stem must look interrogative — but ONLY when we had to guess the boundary
    # (implicit-A case). In the clean case where the first marker is "A.", we
    # already know exactly where the stem ends and don't need this protection.
    # (Many MC stems are completion-style: "...the potential of the conductor is".)
    if letters[0] != "A" and not _STEM_QUESTION_HINT.search(stem):
        return text, []
    return stem, choices


# ---------------------------------------------------------------------------
# Matching-question detection ("match the entries" — a left column matched
# 1:1 to a right column). split_choices() above already rejects these (too
# many items, uneven lengths) and they'd otherwise fall through as one
# opaque free-response question with the whole table glommed into `text`.
# ---------------------------------------------------------------------------

# Cue that the stem is asking for a matching exercise, independent of
# whether the table itself uses the word "match" anywhere in its rows.
_MATCHING_CUE = re.compile(
    r"\bmatch\b[^.?]{0,60}\b(following|each|column|item|term|choice|description)\b"
    r"|\bcolumn\s+[ab]\b",
    re.IGNORECASE,
)
_DIGIT_MARKER = re.compile(r"(?:^|\s)(\d{1,2})[\.\)]\s")
_ALPHA_MARKER = re.compile(r"(?:^|\s)([A-Za-z])[\.\)]\s")

# A leading answer-blank placeholder (e.g. "____ Eradication") left for the
# student to write their match into by hand — stripped unconditionally,
# before marker detection, since real PDFs commonly have no marker at all
# after the blank (see _split_column_items/_group_continuation_rows).
_PLACEHOLDER_BLANK_RE = re.compile(r"^_{2,}\s*")

# One marker pattern for either a digit ("1.", "1)") or a single letter
# ("A.", "A)") label, used everywhere a matching-column row's leading marker
# needs to be detected or stripped — regardless of which side ("left"/
# "right") is being parsed, since real PDFs don't reliably keep one charset
# per side. `\s*(.*)$` already tolerates a marker with nothing after it on
# the same line (e.g. a bare "f." whose text starts on the next line).
_MATCHING_MARKER_RE = re.compile(r"^\(?([A-Za-z]|\d{1,2})[\.\)]\s*(.*)$")


def _looks_like_matching(body: str) -> bool:
    """Cheap, vision-free signal that a question body is a matching table
    rather than a free-response question. Only called after split_choices()
    has already rejected `body` as MC. Gates the expensive positional-
    clustering pass (detect_matching_questions) so it only runs on real
    candidates.

    Requires the matching-instruction cue AND either:
      - many more numbered/lettered markers than split_choices's 5-item
        ceiling allows (a two-column table collapsed into one line reads
        as a long run of markers), or
      - both a digit-marker run and a letter-marker run present — the
        clearest plain-text signature of two interleaved columns.
    """
    if not _MATCHING_CUE.search(body):
        return False
    digit_markers = _DIGIT_MARKER.findall(body)
    alpha_markers = _ALPHA_MARKER.findall(body)
    if len(digit_markers) + len(alpha_markers) > 6:
        return True
    return bool(digit_markers) and bool(alpha_markers)


def _group_continuation_rows(rows: list[str]) -> list[list[int]]:
    """Group row indices so an unmarked row merges onto the previous
    *marked* row (a wrapped continuation line) — shared by both the manual
    capture path and automatic detection. A row before any marker has been
    seen stays its own group, so a column with no printed labels at all
    (every row genuinely separate) is left alone rather than guessed at."""
    groups: list[list[int]] = []
    seen_marker = False
    for i, row in enumerate(rows):
        if _MATCHING_MARKER_RE.match(row):
            seen_marker = True
            groups.append([i])
        elif seen_marker and groups:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _split_column_items(rows: list[str], label_charset: str) -> list[dict]:
    """Extract a {label, text} dict per already-segmented row (continuation
    lines already merged by the caller). Strips a leading numeric ("1.",
    "1)") or alpha ("A.", "A)") marker — either charset is accepted on
    either side, since real PDFs don't reliably keep one charset per
    column; `label_charset` only decides the positional-fallback numbering
    (1,2,3.../A,B,C...) for any row with no marker at all. Generalizes
    split_choices_by_lines()'s marker-per-line strategy without its 5-item
    ceiling or ascending-from-A requirement — matching columns commonly run
    6-10+ rows and the right column need not start at A."""
    items: list[dict] = []
    for row in rows:
        row = row.strip()
        if not row:
            continue
        m = _MATCHING_MARKER_RE.match(row)
        if m:
            raw_label = m.group(1)
            label = raw_label.upper() if raw_label.isalpha() else raw_label
            items.append({"label": label, "text": (m.group(2) or "").strip(), "image": None})
        else:
            items.append({"label": None, "text": row, "image": None})
    for i, it in enumerate(items):
        if it["label"] is None:
            it["label"] = str(i + 1) if label_charset == "numeric" else chr(ord("A") + i)
    return items


def split_column_items(raw: str, label_charset: str) -> list[dict]:
    """Split a drag-captured column region's raw (newline-preserving) text
    into one item per row: strips a leading answer-blank placeholder (e.g.
    "____") from every line, then merges wrapped continuation lines onto
    the previous marked row via _group_continuation_rows(). Used by the
    manual "add matching question" capture flow (review_app.py's
    /extract-region-column). `label_charset` is "numeric" for a left-column
    capture, "alpha" for a right-column one — see _split_column_items for
    what it actually affects (positional fallback only)."""
    if not raw:
        return []
    lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n")]
    lines = [_PLACEHOLDER_BLANK_RE.sub("", ln) for ln in lines]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    groups = _group_continuation_rows(lines)
    rows = [" ".join(lines[i] for i in g) for g in groups]
    return _split_column_items(rows, label_charset)


def _parse_matching_key_line(text: str, left_labels: list[str]) -> dict | None:
    """Recognize a key-page line listing right-column letters for a
    matching question, e.g. "5. A,C,B,D" or "5) A B C D" — only fires when
    the number of letters found exactly equals len(left_labels), zipped
    positionally into a {left_label: right_label} pairs dict. Deliberately
    conservative (per product decision): anything ambiguous returns None
    rather than guessing, leaving `pairs` empty for manual fill-in."""
    letters = re.findall(r"[A-Za-z]", text)
    if len(letters) != len(left_labels) or not letters:
        return None
    return {left: letter.upper() for left, letter in zip(left_labels, letters)}


def _is_cover_page(text: str) -> bool:
    """True if the page looks like a cover/instructions page rather than
    a real test page.

    Two signals must both fire to classify as cover:
      1. At least one Event.cover_markers token appears. ("Score:", "Names:"
         etc — necessary but not sufficient, since real test pages can carry
         the same words in question text.)
      2. Structural confirmation: fewer than 3 substantive numbered items,
         AND either short overall text OR a high ratio of underscore "fill in
         the blank" lines (>= 3 _____ runs) that strongly indicate a form."""
    t = text.lower()
    if not any(m in t for m in _ev().cover_markers):
        return False
    # Count substantive numbered items: number + at least 20 chars of text
    real = 0
    for ln in text.split("\n"):
        ln = ln.strip()
        m = Q_START.match(ln)
        if m and len(ln[m.end():].strip()) > 20:
            real += 1
    if real >= 3:
        # Plenty of real-looking questions — not a cover page, even with markers.
        return False
    # Structural confirmation: short page OR form-fill pattern.
    nonblank = [ln for ln in text.split("\n") if ln.strip()]
    short_page = len(nonblank) < 25
    fillin_runs = sum(1 for ln in text.split("\n")
                      if re.search(r"_{5,}", ln))
    form_like = fillin_runs >= 3
    return short_page or form_like


def _is_key_page(text: str) -> bool:
    """True if the block of text looks like an answer key page."""
    t = text.lower()
    if any(ind in t for ind in KEY_INDICATORS):
        return True
    # Key pages have many SHORT numbered lines; question pages have long ones.
    # Only count lines where the answer portion is brief (< 60 chars).
    short_ans = sum(
        1 for ln in text.split("\n")
        if (m := ANS_LINE.match(ln.strip())) and len(m.group(2).strip()) < 60
    )
    return short_ans >= 5


def _section_suffix(n: int) -> str:
    """Return a unique alpha suffix for the n-th duplicate question number.
    n=1 → 'b', n=2 → 'c', ... n=25 → 'z', n=26 → 'aa', n=27 → 'ab', ...
    Skips 'a' since the first occurrence has no suffix."""
    # Bijective base-26 with the first occurrence ('a') skipped.
    n += 1
    out = ""
    while n > 0:
        n -= 1
        out = chr(ord("a") + (n % 26)) + out
        n //= 26
    return out


def extract_questions(pages: list[str], source: str, year: str, division: str) -> list[dict]:
    """
    Extract questions from test-PDF text, page-by-page.

    - Cover/instructions pages are skipped (see _is_cover_page).
    - Once "Answer Key" / "Solutions" markers appear, the rest is skipped.
    - Duplicate numbers within the same source get suffixed: "1", "1b", "1c"...
      (Tests with multiple sections restart numbering — the suffix marks them.)
    """
    questions: list[dict] = []
    seen_nums: dict[str, int] = {}

    cur_num: str | None = None
    cur_lines: list[str] = []
    in_key = False
    # Tracks the highest left-column row number absorbed into the current
    # question's body since a matching-instruction cue appeared in it — see
    # the Q_START handling below for why this exists.
    matching_col_max = 0

    def flush():
        nonlocal cur_num, cur_lines, matching_col_max
        matching_col_max = 0
        if cur_num and cur_lines and not in_key:
            body = " ".join(cur_lines).strip()
            if len(body) > 8:
                count = seen_nums.get(cur_num, 0)
                seen_nums[cur_num] = count + 1
                display = cur_num if count == 0 else f"{cur_num}{_section_suffix(count)}"
                stem, choices = split_choices(body)
                is_matching_candidate = (not choices) and _looks_like_matching(body)
                stem = _strip_points(stem)
                choices = [{"letter": c["letter"], "text": _strip_points(c["text"])}
                           for c in choices]
                q = {
                    "number":   display,
                    "topic":    classify_topic(body),
                    "text":     stem,
                    "choices":  choices,
                    "images":   [],
                    "answer":   "",
                    "source":   source,
                    "year":     year,
                    "division": division,
                }
                if is_matching_candidate:
                    # Flagged for the positional-clustering pass
                    # (detect_matching_questions, called from process_pair
                    # before associate_images). Stripped before output if
                    # that pass can't confirm/restructure it.
                    q["_matching_candidate"] = True
                questions.append(q)
        cur_num = None
        cur_lines.clear()

    for page_text in pages:
        if in_key:
            break
        if not page_text.strip():
            continue
        if _is_cover_page(page_text):
            continue

        for raw in page_text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if any(ind in low for ind in KEY_INDICATORS):
                flush()
                in_key = True
                break
            if _is_noise(line):
                continue
            # Some PDFs (notably 2-column layouts) collapse multiple questions
            # onto a single text line because PyMuPDF reads cells in a flat
            # order. Detect inline Q_START anchors and split the line into
            # synthetic sub-lines so the outer loop's logic still works.
            inline_anchors = list(_INLINE_Q_START.finditer(line))
            if len(inline_anchors) >= 2:
                # Strip leading non-Q text into the previous question, then
                # treat each anchor as the start of a new sub-line.
                first = inline_anchors[0]
                if first.start() > 0:
                    pre = line[:first.start()].strip()
                    if pre and cur_num is not None:
                        cur_lines.append(pre)
                for j, am in enumerate(inline_anchors):
                    end = inline_anchors[j + 1].start() if j + 1 < len(inline_anchors) else len(line)
                    seg = line[am.start():end].strip()
                    if not seg:
                        continue
                    flush()
                    sm = Q_START.match(seg)
                    if sm:
                        cur_num = sm.group(1)
                        rest = seg[sm.end():].strip()
                        if rest:
                            cur_lines.append(rest)
                continue
            m = Q_START.match(line)
            if m:
                n = int(m.group(1))
                # A matching table's left column is itself numbered "1.",
                # "2.", ... — indistinguishable from a new top-level
                # question to Q_START on its own. Once the currently-open
                # question's body already contains a matching-instruction
                # cue, absorb a STRICTLY ascending continuation (1, 2, 3,
                # ...) as body lines instead of flushing a spurious new
                # "question" for each row. A real next top-level question
                # breaks the ascending run (it's essentially never exactly
                # matching_col_max+1) and falls through to the normal flush
                # below, so this only ever suppresses genuine column rows.
                if (cur_num is not None and n == matching_col_max + 1
                        and n <= 25 and _MATCHING_CUE.search(" ".join(cur_lines))):
                    matching_col_max = n
                    cur_lines.append(line)
                    continue
                flush()
                cur_num = m.group(1)
                rest = line[m.end():].strip()
                if rest:
                    cur_lines.append(rest)
            elif cur_num is not None:
                cur_lines.append(line)

    flush()
    return questions


# Matches point-value prefixes that appear in questions, not answers:
# "(1 pt) ...", "(1.5 pts) ...", "(2 pts) ..."
_POINT_PREFIX = re.compile(r"^\(\d+(\.\d+)?\s*pts?\b", re.IGNORECASE)


def extract_answers(pages: list[str]) -> dict[str, str]:
    """
    Extract question→answer mapping from a list of page texts.

    Only processes pages that look like answer key pages. Sanity filters:
      - answer portion < 150 chars
      - must not itself look like a new numbered question (Q_START)
      - must not end with "?" (reprinted questions end with "?")
      - must not start with a point-value marker "(N pts)" (reprinted questions)
    """
    answers: dict[str, str] = {}
    for page_text in pages:
        if not page_text.strip():
            continue
        if not _is_key_page(page_text):
            continue
        for line in page_text.split("\n"):
            line = line.strip()
            m = ANS_LINE.match(line)
            if not m:
                continue
            num, ans_text = m.group(1), m.group(2).strip()
            if not ans_text:
                continue
            if len(ans_text) > 150:
                continue
            # Reprinted question starts with "(N pt)" point-value marker
            if _POINT_PREFIX.match(ans_text):
                continue
            # Reprinted question ends with "?"
            if ans_text.endswith("?"):
                continue
            # Reprinted question is itself a numbered item
            if Q_START.match(ans_text):
                continue
            answers[num] = ans_text
    return answers


def extract_matching_answers(pages: list[str], questions: list[dict]) -> None:
    """Conservative answer-key population for matching questions (mutates
    q["matching"]["pairs"] in place for any question with qtype=="matching").
    Kept separate from extract_answers() since that function's flat
    dict[str,str] return shape doesn't fit a nested pairs mapping.

    Only fires when a key-page line for the question's number contains
    exactly as many letters as the question has left-column items — see
    _parse_matching_key_line. Anything ambiguous is left as pairs={} for
    manual fill-in via the review UI, per product decision (false-positive
    risk of a silently-wrong auto-filled answer key outweighs the
    convenience of guessing)."""
    matching_qs = {q["number"]: q for q in questions if q.get("qtype") == "matching"}
    if not matching_qs:
        return
    for page_text in pages:
        if not page_text.strip() or not _is_key_page(page_text):
            continue
        for line in page_text.split("\n"):
            line = line.strip()
            m = ANS_LINE.match(line)
            if not m:
                continue
            num, ans_text = m.group(1), m.group(2).strip()
            q = matching_qs.get(num)
            if not q or (q.get("matching") or {}).get("pairs"):
                continue
            left_labels = [it["label"] for it in (q.get("matching") or {}).get("left") or []]
            if not left_labels:
                continue
            pairs = _parse_matching_key_line(ans_text, left_labels)
            if pairs:
                q["matching"]["pairs"] = pairs

# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------

def _slug(text: str, n: int = 35) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:n]


def _save_image(doc: fitz.Document, xref: int, src_slug: str, page_num: int, idx: int) -> str | None:
    """Save an embedded image; return filename or None if too small."""
    try:
        base = doc.extract_image(xref)
    except Exception:
        return None
    data = base["image"]
    if len(data) < 2000:
        return None          # logo / bullet / tiny decoration
    ext  = base.get("ext", "png")
    h    = hashlib.md5(data).hexdigest()[:8]
    name = f"{src_slug}_p{page_num}_i{idx}_{h}.{ext}"
    path = _ev().image_dir / name
    if not path.exists():
        path.write_bytes(data)
    return name


def page_image_b64(page: fitz.Page, dpi: int = 96) -> str:
    """Render a PDF page to PNG at `dpi` and return as base64."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("png")).decode()


def region_image_b64(page: fitz.Page, rect: fitz.Rect, dpi: int = 200) -> str:
    """Render a rectangular region of a page at high DPI for vision OCR."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=rect, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("png")).decode()

# ---------------------------------------------------------------------------
# Vision helpers  (Claude Haiku, pay-per-use)
# ---------------------------------------------------------------------------

def vision_extract_region(b64: str) -> dict:
    """OCR a single region image and split into stem + choices.

    Returns {"stem": str, "choices": [{"letter","text"}, ...], "raw": str}.
    Used as a Haiku fallback when pure-Python region capture under-extracts
    (e.g. complex layouts, multi-column choices, or PDFs whose text layer is
    rasterized).
    """
    prompt = (
        "This image is a region from a Science Olympiad test PDF. "
        "It contains one question — possibly with multiple-choice options.\n\n"
        "Extract the text. Return a JSON object with this exact schema:\n"
        '{"stem": string, "choices": [{"letter": "A"|"B"|"C"|"D"|"E", "text": string}, ...], '
        '"answer": string|null}\n\n'
        "Rules:\n"
        "- stem: the question text only (no choice text, no option letters)\n"
        "- choices: empty list [] for free-response. For MC, the options A-E with their text.\n"
        "- answer: leave null unless the answer is explicitly written in the region\n"
        "- Reply with ONLY the JSON object, no markdown fences, no prose."
    )
    try:
        raw = _vision_call(b64, prompt, max_tokens=1200)
    except Exception as e:
        return {"stem": "", "choices": [], "answer": None, "error": str(e)}
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return {"stem": "", "choices": [], "answer": None,
                "error": "Could not parse vision response", "raw": (raw or "")[:300]}
    choices = []
    for c in (parsed.get("choices") or []):
        if isinstance(c, dict) and c.get("letter") and c.get("text"):
            choices.append({"letter": str(c["letter"]).upper(), "text": str(c["text"]).strip()})
    return {
        "stem":   (parsed.get("stem") or "").strip(),
        "choices": choices,
        "answer": parsed.get("answer"),
        "raw":    (raw or "")[:300],
    }


def vision_extract_column(b64: str, label_charset: str) -> dict:
    """OCR a single drag-captured column (one side of a matching table) and
    split it into labeled items. Haiku fallback for the manual matching-
    capture flow (review_app.py's /extract-region-column-vision), used when
    split_column_items()'s text-based heuristic struggles (merged cells,
    image-only rows, unusual spacing). Returns {"items": [{"label","text"}, ...]}.
    `label_charset` is accepted for call-site compatibility (the caller
    still knows which side it's capturing) but no longer narrows the
    prompt — real PDFs don't reliably keep one charset per column, so the
    model is asked to report whichever convention is actually printed."""
    prompt = (
        "This image is one column from a Science Olympiad matching-type "
        "question — a list of items, top to bottom.\n"
        "Items may be numbered (1, 2, 3, ...) or lettered (A, B, C, ...) — "
        "report whichever convention is actually printed, do not assume one.\n"
        "Extract each item's label and text. If an item's text wraps across "
        "multiple lines, join it into a single text value. If an item is "
        "prefixed with a blank/underscore placeholder (e.g. '____') meant "
        "for the student to write their answer in by hand, omit that "
        "placeholder from both the label and the text — it is not part of "
        "the item itself. If an item is a figure/diagram rather than text, "
        "set its text to ''.\n"
        'Return JSON only: {"items": [{"label": "1", "text": "..."}]}\n'
        "Reply with ONLY the JSON object, no markdown fences, no prose."
    )
    try:
        raw = _vision_call(b64, prompt, max_tokens=800)
    except Exception as e:
        return {"items": [], "error": str(e)}
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return {"items": [], "error": "Could not parse vision response", "raw": (raw or "")[:300]}
    items = [{"label": str(it.get("label", "")), "text": (it.get("text") or "").strip(), "image": None}
             for it in (parsed.get("items") or []) if isinstance(it, dict)]
    return {"items": items}


def _vision_call(b64: str, prompt: str, max_tokens: int = 512) -> str:
    """Single Haiku vision call; returns response text."""
    client = _get_client()
    if client is None:
        return ""
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    _track_usage(resp)
    return resp.content[0].text.strip()


# LaTeX commands whose first letter collides with a single-char JSON escape
# (\b \f \n \r \t). json.loads() doesn't raise on these -- it silently
# swallows the colliding letter as a control character instead -- so they
# can't be caught by the exception-driven loop in repair_json_text() and
# must be matched by name up front.
_LATEX_AMBIGUOUS_RE = re.compile(
    r"\\(theta|tan(?:gent)?|tau|text(?:bf|it|rm|sf|tt|normal)?|times|to|tilde|top|"
    r"triangleq?|nu|nabla|neq?|notin|nonumber|ni|newcommand|"
    r"rho|rightarrow|rceil|rfloor|rangle|rbrace|"
    r"beta|bar|binom|boxed|big[lgr]{0,2}|bmod|bullet|backslash|bot|breve|"
    r"frac|forall|flat)\b"
)


# json.JSONDecodeError messages that mean "a string was closed early by an
# unescaped literal quote inside it" -- the decoder happily parsed the
# truncated string, then choked on whatever came after expecting a
# delimiter instead of more text.
_QUOTE_FIX_MSGS = (
    "Expecting ',' delimiter",
    "Expecting property name enclosed in double quotes",
    "Extra data",
)


def repair_json_text(raw: str, max_iters: int = 2000) -> str:
    """
    Repair raw JSON text containing unescaped LaTeX backslashes and stray
    unescaped quotes (the two most common mistakes in hand-typed or
    LLM-exported question banks) so json.loads() can parse it.

    JSON only recognises \\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t and \\uXXXX
    as escapes inside a string. A literal LaTeX command is a backslash
    followed by letters, which falls into one of two failure modes:
      - Most commands (\\Delta, \\sqrt, \\alpha, \\partial, ...) start with a
        letter that ISN'T a valid escape, so json.loads() raises a hard
        JSONDecodeError pointing exactly at the backslash. We patch that by
        doubling the backslash (making it a literal one) and retry, in a
        loop, until parsing succeeds or stops making progress.
      - A handful of commands (\\theta, \\frac, \\nu, \\rho, \\beta, ...)
        start with a letter that IS a valid single-char escape (t/f/n/r/b),
        so json.loads() "succeeds" by silently consuming that letter as a
        control character (tab/newline/etc.) instead of raising -- those are
        matched by name via `_LATEX_AMBIGUOUS_RE` before parsing is attempted.

    Separately, a literal `"` inside a string value (units like 5", quoted
    text, ...) closes the JSON string early; the decoder then expects a `,`
    or closing bracket and instead finds more text, raising one of
    `_QUOTE_FIX_MSGS`. We patch that by escaping the nearest preceding
    unescaped quote and retrying -- this and the backslash fixes are
    interleaved in the same loop since a single document can have both
    error types at different points, in either order.

    Also escapes raw control characters (literal newlines/tabs pasted
    directly into a string value) using the same position-from-exception
    technique. Returns the best-effort repaired text; caller still needs to
    json.loads() it and handle failure.
    """
    if not raw:
        return raw
    s = _LATEX_AMBIGUOUS_RE.sub(lambda m: "\\\\" + m.group(1), raw)
    for _ in range(max_iters):
        try:
            json.loads(s)
            return s
        except json.JSONDecodeError as e:
            pos = e.pos
            if pos < len(s) and s[pos] == "\\":
                s = s[:pos] + "\\" + s[pos:]
                continue
            if pos < len(s) and ord(s[pos]) < 0x20:
                ch = s[pos]
                esc = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, f"\\u{ord(ch):04x}")
                s = s[:pos] + esc + s[pos + 1:]
                continue
            if any(e.msg.startswith(m) for m in _QUOTE_FIX_MSGS):
                j = pos - 1
                while j >= 0 and not (s[j] == '"' and (j == 0 or s[j - 1] != "\\")):
                    j -= 1
                if j >= 0:
                    s = s[:j] + '\\"' + s[j + 1:]
                    continue
            return s
    return s


def _parse_json(raw: str) -> dict | list | None:
    """
    Parse JSON from an LLM response, with a small repair ladder for the most
    common formatting mistakes the model makes.

    Repair steps (each attempted only if the previous failed):
      1. Strip markdown code fences (```json ... ```).
      2. Fix unescaped LaTeX/backslash sequences and raw control characters
         (see `repair_json_text`).
      3. Strip leading/trailing prose around the outermost {...} or [...].
      4. Remove JSON trailing commas (`,]` and `,}`).
      5. Replace JavaScript-style single-quoted strings with double-quoted.

    Returns the parsed object/list or None if nothing usable was recovered.
    """
    if not raw:
        return None
    s = raw.strip()
    # 0. normalize curly/smart quotes that Haiku sometimes slips in
    s = (s.replace("“", '"').replace("”", '"')
           .replace("‘", "'").replace("’", "'"))
    # 1. fences
    s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s).strip()
    if not s:
        return None
    # 1.5 LaTeX/backslash + control-character repair
    s = repair_json_text(s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 2. trim to outermost braces / brackets
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    ends   = [i for i in (s.rfind("}"), s.rfind("]")) if i >= 0]
    if starts and ends:
        cand = s[min(starts):max(ends) + 1]
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            s = cand   # carry forward to the next repair step
    # 3. trailing commas
    try:
        repaired = re.sub(r",(\s*[}\]])", r"\1", s)
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # 4. single-quoted strings (Haiku occasionally slips into JS style)
    try:
        # Replace 'foo' with "foo" outside of any embedded "..."
        # This is conservative: only flip quotes when the char isn't already
        # inside a double-quoted span.
        def _swap(m):
            return '"' + m.group(1).replace('"', r'\"') + '"'
        repaired = re.sub(r"'([^']*)'", _swap, s)
        repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def vision_assign_images(page: fitz.Page, img_fnames: list[str],
                         q_nums_on_page: list[str]) -> dict[int, str]:
    """
    Ask Haiku: for each image (0-indexed, top→bottom) on this page, which
    question number does it illustrate?

    Returns {img_idx: question_number_str}.
    "PREV" means the image belongs to the last question on the previous page.
    """
    if not img_fnames or not _vision_available():
        return {}

    q_list = ", ".join(q_nums_on_page) if q_nums_on_page else "none visible"
    prompt = (
        f"This is a Science Olympiad {_ev().name} test page.\n"
        f"Question numbers that START on this page: [{q_list}].\n"
        f"There are {len(img_fnames)} figure(s)/diagram(s) on the page, "
        f"indexed 0–{len(img_fnames)-1} from top to bottom.\n"
        f"For each figure index, which question number does it illustrate?\n"
        f'Reply with JSON only, e.g. {{"0":"5","1":"5","2":"6"}} — '
        f'use "PREV" if the figure belongs to a question that started on a previous page.'
    )
    raw = _vision_call(page_image_b64(page), prompt, max_tokens=256)
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return {}
    result: dict[int, str] = {}
    for k, v in parsed.items():
        try:
            result[int(k)] = str(v)
        except ValueError:
            pass
    return result


def vision_extract_text_page(page: fitz.Page) -> list[dict]:
    """
    Full OCR for a page with no extractable text.
    Returns list of {number, text, has_figure} dicts.
    """
    if not _vision_available():
        return []
    prompt = (
        f"This is a page from a Science Olympiad {_ev().name} test or answer key.\n"
        "Extract every question or answer visible.\n"
        "Return JSON:\n"
        '{"questions": [{"number":"1","text":"...","has_figure":false}], '
        '"answers": [{"number":"1","answer":"B"}]}\n'
        "Use an empty list for whichever category has none. "
        "Return {} if this is a cover/instruction/formula page."
    )
    raw = _vision_call(page_image_b64(page), prompt, max_tokens=1024)
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return []
    return parsed.get("questions", [])


def vision_extract_key_page(page: fitz.Page) -> dict[str, str]:
    """OCR an image-based key page → {question_number: answer_text}."""
    if not _vision_available():
        return {}
    prompt = (
        f"This is an answer key page from a Science Olympiad {_ev().name} test.\n"
        "Extract every answer.\n"
        'Return JSON only: {"1":"B","2":"14 Ω","3":"See work"}\n'
        "Return {} if no answers visible."
    )
    raw = _vision_call(page_image_b64(page), prompt, max_tokens=512)
    parsed = _parse_json(raw)
    return parsed if isinstance(parsed, dict) else {}


def vision_extract_matching(page: fitz.Page, q_num: str) -> dict | None:
    """Ask Haiku to identify and structure a matching-table question (#q_num)
    visible on this page: left-column items, right-column items, and the
    correct pairing if visibly indicated. Used as the high-confidence path
    in detect_matching_questions() — preferred over the x-position heuristic
    when available, since vision handles layouts (merged cells, non-
    rectangular columns) the heuristic can't. Does not attempt image-cell
    association — filenames still come from the caller's own embedded-image
    extraction, intersected against row/column bboxes; this keeps the
    prompt simple and avoids asking the LLM to invent filenames."""
    if not _vision_available():
        return None
    prompt = (
        f"This is a Science Olympiad {_ev().name} test page containing a "
        f"matching-type question (#{q_num}): a left column of items matched "
        "1:1 to a right column.\n"
        "Identify the left-column items and right-column items (their visible "
        "label and text), and the correct pairing ONLY if an answer is "
        "visibly indicated on this page (leave pairs empty otherwise).\n"
        "Items in either column may be numbered, lettered, or have no visible "
        "label at all — report whichever is actually printed, do not assume "
        "one column is always numbers and the other always letters.\n"
        "If an item's text wraps across multiple lines, join it into a "
        "single text value. If an item is prefixed with a blank/underscore "
        "placeholder (e.g. '____') meant for the student to write their "
        "answer in by hand, omit that placeholder from both the label and "
        "the text — it is not part of the item itself.\n"
        'Return JSON only: {"left":[{"label":"1","text":"..."}], '
        '"right":[{"label":"A","text":"..."}], "pairs":{}}\n'
        "If a cell is a figure/diagram rather than text, set its text to ''.\n"
        "Return null if no matching question is visible on this page."
    )
    raw = _vision_call(page_image_b64(page), prompt, max_tokens=768)
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict) or not parsed.get("left") or not parsed.get("right"):
        return None
    left = [{"label": str(it.get("label", "")), "text": (it.get("text") or "").strip(), "image": None}
            for it in parsed.get("left") or []]
    right = [{"label": str(it.get("label", "")), "text": (it.get("text") or "").strip(), "image": None}
             for it in parsed.get("right") or []]
    pairs = {str(k): str(v) for k, v in (parsed.get("pairs") or {}).items()}
    return {"left": left, "right": right, "pairs": pairs}


def vision_to_latex(b64: str) -> str:
    """
    Send a cropped equation image to Haiku, get back LaTeX.
    Returns the raw LaTeX (no $ delimiters) or "" if no math detected.
    """
    if not _vision_available():
        return ""
    prompt = (
        "Convert any mathematical expression in this image to LaTeX.\n"
        "Reply with ONLY the LaTeX code — no $ delimiters, no markdown fences, "
        "no explanation, no surrounding text. Preserve symbols like Ω, μ, π. "
        "If the image contains no math expression, reply with exactly: NONE"
    )
    out = _vision_call(b64, prompt, max_tokens=300).strip()
    if not out or out.upper() == "NONE":
        return ""
    # Strip wrappers Haiku may add anyway
    out = re.sub(r"^```(?:latex|tex)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    out = out.strip()
    out = re.sub(r"^\$+|\$+$", "", out).strip()
    return out


def validate_answer(q: dict, keys: dict | None = None) -> dict:
    """
    Ask an LLM to check whether the recorded answer to `q` is correct.
    Returns a dict suitable to store on q['validation']:
      {status, correct_answer, rationale, source, validated_at, model,
       text_at_validation, answer_at_validation}

    `keys`: optional per-provider API keys (browser-supplied, see
    review_app._request_llm_keys); defaults to the server's own
    ANTHROPIC_API_KEY when omitted, so CLI/batch callers are unaffected.
    """
    from datetime import datetime as _dt
    keys = keys or llm_providers.default_keys()
    if not llm_providers.available_providers(keys):
        return {"status": "unavailable", "rationale": "No LLM API key configured"}

    if q.get("qtype") == "matching":
        return _validate_matching_answer(q, keys)

    text = (q.get("text") or "").strip()
    answer = (q.get("answer") or "").strip()
    choices = q.get("choices") or []

    if not text:
        return {"status": "unavailable", "rationale": "question text is empty"}

    choices_str = ""
    if choices:
        lines = [f"  {c.get('letter','?')}. {c.get('text','')}" for c in choices]
        choices_str = "\nChoices:\n" + "\n".join(lines)

    prompt = (
        f"You are checking a Science Olympiad {_ev().name} answer for correctness.\n"
        "Be careful: this is a physics test for grades 6-9.\n\n"
        f"Question: {text}{choices_str}\n"
        f"Recorded answer: {answer or '(blank)'}\n\n"
        "Decide:\n"
        '1. status: "correct", "incorrect", or "uncertain" '
        '(use "uncertain" only if the question is genuinely ambiguous or '
        'lacks information needed to evaluate).\n'
        "2. correct_answer: the actual correct answer (letter for MC, value "
        "with units otherwise). Null if status==correct OR genuinely unknown.\n"
        "3. rationale: 1-3 sentences. Include the relevant equation or "
        "physical principle so a student would learn from it.\n"
        "4. source: a primary source — equation name (e.g. \"Ohm's Law: V=IR\"), "
        "physical law, or well-established fact. Avoid vague references.\n\n"
        "Reply with ONLY valid JSON, no markdown, no prose. Schema:\n"
        '{"status":"correct"|"incorrect"|"uncertain",'
        '"correct_answer":string|null,'
        '"rationale":string,'
        '"source":string}'
    )

    model_overrides = {"anthropic": VISION_MODEL}
    try:
        result = llm_providers.chat(
            keys=keys, system=None,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500, model_overrides=model_overrides,
        )
        if result["provider"] == "anthropic":
            _track_usage_tokens(result["input_tokens"], result["output_tokens"])
        raw = result["text"]
        used_model = result["model"]
    except llm_providers.LLMError as e:
        return {"status": "unavailable", "rationale": f"LLM error: {e}"}

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        # One retry with a stricter follow-up: feed the bad response back and
        # ask only for the JSON. Many failures are "Here is the JSON:\n{...}"
        # with extra prose the ladder didn't catch, or a refusal we can ignore.
        try:
            result2 = llm_providers.chat(
                keys=keys, system=None,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw or "(empty)"},
                    {"role": "user", "content":
                        "Your previous reply was not valid JSON. Reply NOW with "
                        "ONLY the JSON object matching the schema, no prose, "
                        "no markdown fences, no explanation. Single line is fine."},
                ],
                max_tokens=500, model_overrides=model_overrides,
            )
            if result2["provider"] == "anthropic":
                _track_usage_tokens(result2["input_tokens"], result2["output_tokens"])
            raw2 = result2["text"]
            parsed = _parse_json(raw2)
            if isinstance(parsed, dict):
                raw = raw2  # use the retry text for downstream fields
                used_model = result2["model"]
        except llm_providers.LLMError:
            pass
    if not isinstance(parsed, dict):
        return {
            "status": "uncertain",
            "rationale": "Could not parse validator response (LLM replied with non-JSON; click the raw response to inspect).",
            "raw": (raw or "")[:400],
            "validated_at":       _dt.now().isoformat(timespec="seconds"),
            "model":              used_model,
            "text_at_validation": text[:300],
            "answer_at_validation": answer,
        }

    status = parsed.get("status", "uncertain").lower()
    if status not in ("correct", "incorrect", "uncertain"):
        status = "uncertain"

    return {
        "status":             status,
        "correct_answer":     parsed.get("correct_answer"),
        "rationale":          (parsed.get("rationale") or "").strip(),
        "source":             (parsed.get("source") or "").strip(),
        "validated_at":       _dt.now().isoformat(timespec="seconds"),
        "model":              used_model,
        "text_at_validation": text[:300],
        "answer_at_validation": answer,
    }


def _validate_matching_answer(q: dict, keys: dict) -> dict:
    """Matching-question branch of validate_answer(): presents both columns
    plus the recorded pairs mapping, asks the LLM to verify each pairing,
    and returns a per-pair breakdown nested under the same top-level shape
    every other question type uses (status/rationale/source/validated_at/
    model) so review.html's existing validate-bar rendering needs only one
    additional conditional to also show `per_pair` when present — the
    aggregate status/badge logic is unchanged.

    Known limitation: this is a text-based check, not OCR — an image-only
    cell's content is invisible to the validator (no vision call here).
    Documented in spec.md rather than silently overclaiming accuracy."""
    from datetime import datetime as _dt
    m = q.get("matching") or {}
    left, right, pairs = m.get("left") or [], m.get("right") or [], m.get("pairs") or {}
    if not left or not right:
        return {"status": "unavailable", "rationale": "matching question has no items"}
    if not pairs:
        return {"status": "unavailable", "rationale": "no recorded matches to check"}

    def _cell(item):
        return item.get("text") or ("[figure, no text — not visible to this text-based check]"
                                     if item.get("image") else "(empty)")

    left_str = "\n".join(f"  {it['label']}. {_cell(it)}" for it in left)
    right_str = "\n".join(f"  {it['label']}. {_cell(it)}" for it in right)
    pairs_str = ", ".join(f"{l}→{r}" for l, r in pairs.items())

    prompt = (
        f"You are checking a Science Olympiad {_ev().name} matching question for correctness.\n"
        "Be careful: this is a physics test for grades 6-9.\n\n"
        f"Question: {q.get('text','')}\n"
        f"Column A:\n{left_str}\n"
        f"Column B:\n{right_str}\n\n"
        f"Recorded matches (A→B): {pairs_str}\n\n"
        "For each recorded match, decide if it's correct. "
        "Reply with ONLY valid JSON, no markdown, no prose. Schema:\n"
        '{"per_pair": [{"left":"1","ok":true|false,"correct_answer":"B"|null}], '
        '"rationale":string, "source":string}\n'
        "correct_answer is the right-column label that SHOULD be matched to this "
        "left item — null when ok is true or genuinely unknown."
    )
    model_overrides = {"anthropic": VISION_MODEL}
    try:
        result = llm_providers.chat(
            keys=keys, system=None,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700, model_overrides=model_overrides,
        )
        if result["provider"] == "anthropic":
            _track_usage_tokens(result["input_tokens"], result["output_tokens"])
        raw = result["text"]
        used_model = result["model"]
    except llm_providers.LLMError as e:
        return {"status": "unavailable", "rationale": f"LLM error: {e}"}

    parsed = _parse_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("per_pair"), list):
        return {
            "status": "uncertain",
            "rationale": "Could not parse validator response (LLM replied with non-JSON; click the raw response to inspect).",
            "raw": (raw or "")[:400],
            "validated_at": _dt.now().isoformat(timespec="seconds"),
            "model": used_model,
        }

    per_pair = []
    for p in parsed["per_pair"]:
        if not isinstance(p, dict) or not p.get("left"):
            continue
        per_pair.append({
            "left": str(p["left"]),
            "given": pairs.get(str(p["left"])),
            "ok": bool(p.get("ok")),
            "correct_answer": p.get("correct_answer"),
        })
    status = "correct" if per_pair and all(p["ok"] for p in per_pair) else "incorrect"
    if not per_pair:
        status = "uncertain"

    return {
        "status":       status,
        "per_pair":     per_pair,
        "rationale":    (parsed.get("rationale") or "").strip(),
        "source":       (parsed.get("source") or "").strip(),
        "validated_at": _dt.now().isoformat(timespec="seconds"),
        "model":        used_model,
        "text_at_validation": q.get("text", "")[:300],
    }

# ---------------------------------------------------------------------------
# Matching-question structuring (positional clustering + vision-assist)
# ---------------------------------------------------------------------------

def detect_matching_questions(doc: fitz.Document, questions: list[dict],
                              src_slug: str, use_vision: bool,
                              vision_cache: dict) -> None:
    """
    For every question flagged `_matching_candidate` by extract_questions(),
    replace it in-place with a structured matching question: locate the
    page it starts on (re-walking the same Q_START anchors, this time via
    bbox-aware "blocks" instead of flat text), then either ask Haiku vision
    (preferred, when `use_vision`) or fall back to x-position column
    clustering to split it into left/right items.

    MUST run before associate_images() — see that function's docstring for
    why (matching-table images need to land in the correct cell, not the
    question's top-level images[]).

    Mutates `questions` in-place; strips the transient `_matching_candidate`
    marker from every question regardless of outcome.
    """
    candidates: dict[str, dict] = {}
    for q in questions:
        if q.pop("_matching_candidate", None):
            candidates[q["number"]] = q
    if not candidates:
        return

    # Re-walk pages to find (a) which page each candidate starts on, and
    # (b) the y-extent of its block on that page, bounded by the next
    # Q_START anchor on the same page (or the page bottom). Applies the same
    # ascending-continuation suppression extract_questions() uses (see its
    # comment) — without it, a matching table's own "1.", "2." column rows
    # would each look like a fresh anchor and chop the window down to just
    # the instruction line, excluding every row from clustering.
    for pno, page in enumerate(doc, 1):
        anchors: list[tuple[str, float, float]] = []   # (qnum, anchor_y0, anchor_block_bottom)
        cur_anchor_text = ""
        matching_col_max = 0
        for block in page.get_text("blocks"):
            if block[6] != 0:
                continue
            for ln in block[4].split("\n"):
                ln = ln.strip()
                if not ln:
                    continue
                m = Q_START.match(ln)
                if not m:
                    continue
                n = int(m.group(1))
                if (n == matching_col_max + 1 and n <= 25
                        and _MATCHING_CUE.search(cur_anchor_text)):
                    matching_col_max = n
                    continue   # column row, not a new anchor
                anchors.append((m.group(1), block[1], block[3]))
                cur_anchor_text = ln
                matching_col_max = 0
        if not anchors:
            continue
        anchors.sort(key=lambda a: a[1])
        page_bottom = float(page.rect.height)
        for i, (qnum, y0, anchor_bottom) in enumerate(anchors):
            q = candidates.get(qnum)
            if q is None or q.get("matching"):
                continue  # not a candidate, or already resolved on an earlier page
            y1 = anchors[i + 1][1] if i + 1 < len(anchors) else page_bottom
            # Cluster strictly below the anchor's own stem block, so the
            # instruction line ("Match each item...") doesn't get swept into
            # whichever column its x0 happens to land in.
            cluster_y0 = anchor_bottom

            result = None
            from_vision = False
            if use_vision and _vision_available():
                cache_key = f"match_p{pno}_q{qnum}"
                if cache_key in vision_cache:
                    result = vision_cache[cache_key] or None
                else:
                    result = vision_extract_matching(page, qnum)
                    vision_cache[cache_key] = result
                    time.sleep(0.05)
                from_vision = result is not None
            if not result:
                result = _cluster_matching_columns(doc, page, pno, src_slug, cluster_y0, y1)

            q["qtype"] = "matching"
            if result:
                if from_vision:
                    # Known limitation: vision returns items with no bbox,
                    # so precise per-cell image assignment (only possible
                    # when we have the heuristic pass's block<->item
                    # correspondence) is skipped here. Cell images stay
                    # unset for vision-sourced matching questions until a
                    # reviewer attaches them manually.
                    pass
                q["matching"] = result
                # q["text"] still holds extract_questions()'s flattened blob
                # (instruction sentence + every column row run together,
                # since split_choices found no choices and left it as-is) —
                # take just the leading instructional portion, up to the
                # first absorbed column-row marker, as the clean stem.
                lead = re.split(r"\s\d{1,2}[\.\)]\s", q.get("text") or "", maxsplit=1)[0].strip()
                q["text"] = lead if len(lead) >= 8 else "Match each item in Column A to its match in Column B."
            else:
                # Couldn't confidently split — still flip the type (strictly
                # better than the opaque-FRQ status quo) but leave empty
                # columns and preserve the original text as a manual-entry
                # aid in the review UI.
                q["matching"] = {"left": [], "right": [], "pairs": {},
                                 "raw_text": q.get("text", "")}
            q["choices"] = []


def _cluster_matching_columns(doc: fitz.Document, page: fitz.Page, pno: int,
                              src_slug: str, y0: float, y1: float) -> dict | None:
    """Heuristic, vision-free column split: within the page's [y0, y1) band
    (this question's vertical extent), bucket text blocks into two columns
    by the largest gap among their x0 values, sort each bucket top-to-
    bottom, and extract labeled items via _split_column_items(). Returns
    None (caller falls back to an empty matching shell) when the split
    isn't confident — fewer than 2 confident column blocks is a much
    stronger signal of "not actually two clean columns" than a clustering
    error worth forcing.

    Image-to-cell association happens here (not in a separate pass) because
    this is the only place a column's text *blocks* and the resulting
    labeled *items* can still be tied back together — via `row_y0s`, the
    y0 of each merge-group's first block (see _group_continuation_rows),
    rather than a strict 1:1 block<->item correspondence (broken on
    purpose now, since a wrapped multi-line cell legitimately merges
    several blocks into one item)."""
    blocks = [b for b in page.get_text("blocks")
              if b[6] == 0 and y0 <= b[1] < y1 and b[4].strip()]
    if len(blocks) < 4:
        return None
    xs = sorted({round(b[0], 1) for b in blocks})
    if len(xs) < 2:
        return None
    # split_x is the *midpoint* of the largest gap, not its lower edge — a
    # block's real x0 (e.g. 77.25) can round differently than the value
    # used to find the gap (e.g. 77.2), so comparing against the boundary
    # value itself would wrongly bucket it on the gap's far side. The
    # midpoint has comfortable clearance on either side for any real block.
    gap, gap_lo = max((xs[i + 1] - xs[i], xs[i]) for i in range(len(xs) - 1))
    if gap < 20:   # no confident two-column signal
        return None
    split_x = gap_lo + gap / 2

    left_blocks  = sorted((b for b in blocks if b[0] <= split_x), key=lambda b: b[1])
    right_blocks = sorted((b for b in blocks if b[0] > split_x),  key=lambda b: b[1])
    if len(left_blocks) < 2 or len(right_blocks) < 2:
        return None

    def _rows_for(blocks_):
        return [" ".join(ln.strip() for ln in b[4].split("\n") if ln.strip())
                for b in blocks_]

    def _grouped_rows_and_y0s(blocks_):
        raw_rows = [_PLACEHOLDER_BLANK_RE.sub("", r) for r in _rows_for(blocks_)]
        groups = _group_continuation_rows(raw_rows)
        rows = [" ".join(raw_rows[i] for i in g) for g in groups]
        row_y0s = [blocks_[g[0]][1] for g in groups]
        return rows, row_y0s

    left_rows, left_row_y0s = _grouped_rows_and_y0s(left_blocks)
    right_rows, right_row_y0s = _grouped_rows_and_y0s(right_blocks)

    left_items  = _split_column_items(left_rows, "numeric")
    right_items = _split_column_items(right_rows, "alpha")
    if len(left_items) < 2 or len(right_items) < 2:
        return None

    _assign_column_images(doc, page, pno, src_slug, left_row_y0s, left_items, y0, y1)
    _assign_column_images(doc, page, pno, src_slug, right_row_y0s, right_items, y0, y1)
    return {"left": left_items, "right": right_items, "pairs": {}}


def _assign_column_images(doc: fitz.Document, page: fitz.Page, pno: int, src_slug: str,
                          row_y0s: list[float], items: list[dict], y0: float, y1: float) -> None:
    """Mutates `items[i]["image"]` in place for any embedded image whose
    render rect's y0 falls within `row_y0s[i]`'s row span (that row's start
    y0 up to either the next row's start y0 or this question's y1) — relies
    on `row_y0s` and `items` being the same length, in the same top-to-
    bottom order (true by construction: both are built in lockstep, one
    merge-group at a time, in _cluster_matching_columns)."""
    if not row_y0s or len(row_y0s) != len(items):
        return
    seen: set[int] = set()
    for idx, info in enumerate(page.get_images(full=True)):
        xref = info[0]
        if xref in seen:
            continue
        seen.add(xref)
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        if not rects:
            continue
        img_y0 = float(rects[0].y0)
        if not (y0 <= img_y0 < y1):
            continue
        # Which row does this image's top edge fall into?
        row_i = None
        for i, ry0 in enumerate(row_y0s):
            row_bottom = row_y0s[i + 1] if i + 1 < len(row_y0s) else y1
            if ry0 <= img_y0 < row_bottom:
                row_i = i
                break
        if row_i is None or items[row_i].get("image"):
            continue
        fname = _save_image(doc, xref, src_slug, pno, idx)
        if fname:
            items[row_i]["image"] = fname


# ---------------------------------------------------------------------------
# Spatial + vision image→question association
# ---------------------------------------------------------------------------

def associate_images(doc: fitz.Document, questions: list[dict],
                     src_slug: str, use_vision: bool,
                     vision_cache: dict) -> None:
    """
    Scan every page, collect embedded images with their y-positions, and
    assign each image to the correct question.

    Strategy (in order):
      1. For pages with images, ask Haiku vision for exact assignments.
      2. Fall back to y-coordinate matching if vision is unavailable/fails.

    Mutates `questions[*]["images"]` in-place.
    Also renames saved files to include the topic slug.

    Matching questions (qtype=="matching") are excluded from `q_by_num` —
    detect_matching_questions() (called earlier in process_pair, before this
    function) already files any images inside a matching table's bbox into
    the correct cell (matching.left[i]["image"]/right[i]["image"]); without
    this exclusion, the y-coordinate fallback below would additionally (and
    wrongly) claim those same images as the question's own top-level images.
    """
    q_by_num: dict[str, dict] = {q["number"]: q for q in questions
                                  if q.get("qtype") != "matching"}
    last_q_num: str | None = None   # persists across pages

    for pno, page in enumerate(doc, 1):
        # ---- collect image xrefs and save files ----
        page_imgs: list[tuple[int, int, str]] = []   # (xref, idx, fname)
        seen: set[int] = set()
        for idx, info in enumerate(page.get_images(full=True)):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            fname = _save_image(doc, xref, src_slug, pno, idx)
            if fname:
                page_imgs.append((xref, idx, fname))

        if not page_imgs:
            # No images on this page — just update last_q_num from text
            for block in page.get_text("blocks"):
                if block[6] == 0:
                    for ln in block[4].split("\n"):
                        m = Q_START.match(ln.strip())
                        if m:
                            last_q_num = m.group(1)
            continue

        # ---- build sorted item stream (text blocks + image rects) ----
        items: list[dict] = []
        for block in page.get_text("blocks"):
            if block[6] == 0:
                for ln in block[4].split("\n"):
                    ln = ln.strip()
                    if ln:
                        items.append({"kind": "text", "y": block[1], "text": ln})

        img_y: list[tuple[float, int, str]] = []   # (y0, list_idx, fname)
        for list_idx, (xref, _, fname) in enumerate(page_imgs):
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            y0 = float(rects[0].y0) if rects else float("inf")
            img_y.append((y0, list_idx, fname))
            items.append({"kind": "img", "y": y0, "list_idx": list_idx, "fname": fname})

        items.sort(key=lambda x: x["y"])

        # ---- determine q_nums that START on this page ----
        page_q_starts: list[str] = []
        for it in items:
            if it["kind"] == "text":
                m = Q_START.match(it["text"])
                if m:
                    page_q_starts.append(m.group(1))

        # ---- vision assignment (with per-page cache) ----
        # All vision cache keys are namespaced by call-type to prevent silent
        # collisions when a new caller is added. See the per-call prefixes:
        #   assoc_p<N>   - vision_assign_images (image→Q association)
        #   ocr_p<N>     - vision_extract_text_page (whole-page OCR)
        #   key_ocr_p<N> - vision_extract_key_page (key OCR)
        cache_key = f"assoc_p{pno}"
        vision_map: dict[int, str] = {}
        if use_vision:
            if cache_key in vision_cache:
                vision_map = {int(k): v for k, v in vision_cache[cache_key].items()}
            else:
                vision_map = vision_assign_images(
                    page,
                    [fname for _, _, fname in page_imgs],
                    page_q_starts,
                )
                vision_cache[cache_key] = {str(k): v for k, v in vision_map.items()}
                time.sleep(0.05)   # gentle rate-limit

        # ---- assign images to questions ----
        cur_q_for_text = last_q_num   # tracks last Q seen in text stream

        for it in items:
            if it["kind"] == "text":
                m = Q_START.match(it["text"])
                if m:
                    cur_q_for_text = m.group(1)
            elif it["kind"] == "img":
                li = it["list_idx"]
                # Prefer vision answer; fall back to y-coord
                if li in vision_map and vision_map[li] != "PREV":
                    assigned_num = vision_map[li]
                elif li in vision_map and vision_map[li] == "PREV":
                    assigned_num = last_q_num
                else:
                    assigned_num = cur_q_for_text   # y-coord fallback

                if assigned_num and assigned_num in q_by_num:
                    q = q_by_num[assigned_num]
                    fname = it["fname"]
                    # Prefix filename with topic slug
                    topic_pfx = _slug(q["topic"])
                    new_name = f"{topic_pfx}_{fname}"
                    old_p = _ev().image_dir / fname
                    new_p = _ev().image_dir / new_name
                    if old_p.exists() and not new_p.exists():
                        old_p.rename(new_p)
                        fname = new_name
                    elif new_p.exists():
                        fname = new_name
                    if fname not in q["images"]:
                        q["images"].append(fname)

        last_q_num = cur_q_for_text if cur_q_for_text else last_q_num

# ---------------------------------------------------------------------------
# PDF pair processor
# ---------------------------------------------------------------------------

def _pdf_name_meta(pdf: Path) -> tuple[str, str, str]:
    """Return (year, division, rest_label)."""
    parts = pdf.stem.split("_")
    year = parts[1] if len(parts) > 1 else "?"
    div  = parts[2].upper() if len(parts) > 2 else "?"
    rest = "_".join(parts[3:]).replace("_test", "").replace("_key", "")
    rest = rest.replace("_", " ").strip()
    return year, div, rest


def _effective_pdf_meta(pdf: Path, state: dict) -> dict:
    """Tournament/year for `pdf`: an explicit `state["pdf_meta"][pdf.name]`
    override wins per-field (set via the review page's Tournament/Year
    fields); otherwise both fall back to the exact filename guess
    _pdf_name_meta already produced — byte-for-byte identical to
    pre-pdf_meta behavior for any PDF nobody has ever saved an override
    for. Division is always filename-derived; there's no per-PDF division
    override (only Tournament name and Year were asked for)."""
    year_guess, division, rest_guess = _pdf_name_meta(pdf)
    override = (state.get("pdf_meta") or {}).get(pdf.name) or {}
    return {"year": override.get("year", year_guess),
            "tournament": override.get("tournament", rest_guess),
            "division": division}


def process_pair(test_pdf: Path, key_pdf: Path | None,
                 state: dict, use_vision: bool,
                 should_cancel: Callable[[], bool] = lambda: False,
                 on_progress: Callable[..., None] = lambda **kw: None) -> list[dict]:
    """`should_cancel`/`on_progress` are optional job-queue hooks (see
    jobs.py) — checked/called only inside the vision-OCR loops below, the
    only parts of this function slow enough to need them. Both default to
    no-ops so the CLI (main(), which calls this directly) is unaffected.

    Each vision-OCR'd page is checkpoint-saved into `state` immediately
    (instead of only once at the very end, as before) — interrupting a long
    extraction now only loses the in-flight page, not every page completed
    so far, since vision_cache already persisted is what makes re-running
    after an interruption cheap rather than starting over."""
    cache_key = test_pdf.name

    # Synthetic buckets ("_generated_<slug>.pdf", "_scioly_<slug>.pdf") aren't
    # real PDFs on disk — they're virtual question containers populated by the
    # scio.ly accept or generation flows. Never run the extraction pipeline
    # on them; that would clobber the curated questions inside.
    if cache_key.startswith("_"):
        return state.get("questions", {}).get(cache_key, []) or []

    if cache_key in state["questions"] and not state.get("_rebuild"):
        print(f"  [CACHE] {test_pdf.name}")
        return state["questions"][cache_key]

    meta = _effective_pdf_meta(test_pdf, state)
    year, division = meta["year"], meta["division"]
    source = f"{year} Div-{division}: {meta['tournament']}"
    src_slug = _slug(test_pdf.stem)
    print(f"  [PROC]  {test_pdf.name}")

    # Per-PDF vision cache (keyed by page number string)
    if "vision" not in state:
        state["vision"] = {}
    vision_cache: dict = state["vision"].setdefault(cache_key, {})

    # ---- open test PDF ----
    try:
        doc = pdf_safety.open_pdf_safely(test_pdf)
    except Exception as e:
        log.error("vision pipeline call failed: %s", e)
        print(f"    [ERR] {e}")  # also surface to operator
        state["questions"][cache_key] = []
        return []

    # ---- text extraction (one entry per page) ----
    page_texts = [page.get_text("text") for page in doc]
    has_text = any(p.strip() for p in page_texts)

    questions: list[dict] = []
    answers:   dict[str, str] = {}

    if has_text:
        questions = extract_questions(page_texts, source, year, division)
        # Inline answer key sometimes lives at the end of the test PDF
        answers = extract_answers(page_texts)
    elif use_vision:
        # Image-based PDF — OCR each page via vision
        print("    [OCR] image-based PDF, using vision...")
        n_pages = doc.page_count
        for pno, page in enumerate(doc, 1):
            if should_cancel():
                state["vision"][cache_key] = vision_cache
                _save_state(state)
                doc.close()
                raise jobs.JobCancelled()
            pkey = f"ocr_p{pno}"
            if pkey in vision_cache:
                items = vision_cache[pkey]
            else:
                items = vision_extract_text_page(page)
                vision_cache[pkey] = items
                # Checkpoint-save immediately — interrupting mid-loop only
                # ever loses the in-flight page, not every page OCR'd so far
                # (previously nothing was durable until the whole function
                # returned; see the docstring above).
                state["vision"][cache_key] = vision_cache
                _save_state(state)
                time.sleep(0.1)
            on_progress(phase=f"OCR test PDF page {pno}/{n_pages}", done=pno, total=n_pages)
            for item in items:
                if isinstance(item, dict) and item.get("number"):
                    text = item.get("text", "").strip()
                    if text:
                        stem, choices = split_choices(text)
                        stem = _strip_points(stem)
                        choices = [{"letter": c["letter"], "text": _strip_points(c["text"])}
                                   for c in choices]
                        questions.append({
                            "number":   str(item["number"]),
                            "topic":    classify_topic(text),
                            "text":     stem,
                            "choices":  choices,
                            "images":   [],
                            "answer":   "",
                            "source":   source,
                            "year":     year,
                            "division": division,
                        })
    else:
        print("    [SKIP] image-based PDF — re-run without --text-only to OCR")

    # ---- matching-question structuring (must run before image association
    # — see associate_images()'s docstring for why) ----
    detect_matching_questions(doc, questions, src_slug, use_vision, vision_cache)
    # Inline key, now that any matching candidates have been structured
    extract_matching_answers(page_texts, questions)

    # ---- image association ----
    associate_images(doc, questions, src_slug, use_vision, vision_cache)
    doc.close()

    # ---- key PDF — answer extraction only (never question extraction) ----
    if key_pdf and key_pdf.exists():
        try:
            kdoc = pdf_safety.open_pdf_safely(key_pdf)
            kpage_texts = [p.get_text("text") for p in kdoc]
            has_key_text = any(p.strip() for p in kpage_texts)
            if has_key_text:
                # Key text found — extract answers page-by-page
                answers.update(extract_answers(kpage_texts))
                extract_matching_answers(kpage_texts, questions)
            if (not has_key_text or os.environ.get("VISION_KEY_OCR", "").lower() in ("1", "true", "yes")) and use_vision:
                # Either image-only key, OR opt-in vision pass on text keys
                # (set VISION_KEY_OCR=1) — many keys have structured tables
                # that extract_answers misses entirely. Vision merges into
                # `answers` without overwriting text-extracted entries.
                if has_key_text:
                    print("    [OCR] running supplemental vision pass on key (VISION_KEY_OCR=1)…")
                else:
                    print("    [OCR] image-based key, using vision...")
                n_kpages = kdoc.page_count
                for pno, page in enumerate(kdoc, 1):
                    if should_cancel():
                        state["vision"][cache_key] = vision_cache
                        _save_state(state)
                        kdoc.close()
                        raise jobs.JobCancelled()
                    pkey = f"key_ocr_p{pno}"
                    if pkey in vision_cache:
                        page_ans = vision_cache[pkey]
                    else:
                        page_ans = vision_extract_key_page(page)
                        vision_cache[pkey] = page_ans
                        state["vision"][cache_key] = vision_cache
                        _save_state(state)
                        time.sleep(0.1)
                    on_progress(phase=f"OCR key PDF page {pno}/{n_kpages}", done=pno, total=n_kpages)
                    # Don't clobber what text extraction already got right.
                    for qnum, ans in page_ans.items():
                        answers.setdefault(qnum, ans)
            kdoc.close()
        except jobs.JobCancelled:
            raise
        except Exception as e:
            log.warning("answer-key extraction error: %s", e)
            print(f"    [WARN] key error: {e}")

    # If the test uses section-restarted numbering (any suffixed duplicates),
    # text-based answer matching is unreliable across sections — skip it.
    # Empty answer is better than wrong answer.
    has_dupes = any(re.search(r"[a-z]$", q["number"]) for q in questions)
    if has_dupes:
        print(f"    [INFO] multi-section numbering detected — skipping text key match")
    else:
        for q in questions:
            if q.get("qtype") == "matching":
                continue   # pairs already populated by extract_matching_answers; answer stays ""
            q["answer"] = answers.get(q["number"], "")

    # Apply any user annotations saved from the review UI
    ann = state.get("annotations", {}).get(cache_key)
    if ann:
        before = len(questions)
        questions = apply_annotations(questions, ann)
        delta = len(questions) - before
        sign = "+" if delta >= 0 else ""
        print(f"    [ANNO] applied user annotations ({sign}{delta} questions)")

    n_img = sum(1 for q in questions if q["images"])
    print(f"    => {len(questions)} questions, {n_img} with figures")

    state["questions"][cache_key] = questions
    state["vision"][cache_key]    = vision_cache
    _save_state(state)
    return questions


# ---------------------------------------------------------------------------
# Manual annotations (from the Flask review UI)
# ---------------------------------------------------------------------------

def apply_annotations(questions: list[dict], ann: dict) -> list[dict]:
    """
    Layer user annotations on top of automatically-extracted questions.

    Annotation shape:
      {
        "field_overrides": {qnum: {text?, choices?, answer?, topic?, page?,
                                    validation?, lastEditedBy?, lastEditedDateTime?,
                                    qtype?, matching?}},
        "added":           [full question dicts],
        "deleted":         [qnum, ...],
        "image_overrides": {
          "assignments": {fname: qnum_or_list_of_qnum, ...},
          "detached":    [fname, ...]
        },
      The same image filename may be attached to more than one question by
      using a list of qnums as the assignment value (e.g. {"foo.png": ["5","7"]}).
        "regions": [...]    # provenance only, not used here
      }
    """
    if not ann:
        # Even with no annotations, return a shallow copy so the caller can
        # mutate without affecting our input (consistency with the annotated path).
        return [dict(q) for q in questions]

    # Defensive copy: callers (notably the browse endpoint) iterate over each
    # bucket's question list and mutating in place produced spooky action at a
    # distance. Inner dicts are shallow-copied so field_overrides don't bleed.
    questions = [dict(q) for q in questions]

    # 1. Drop deleted
    deleted = set(ann.get("deleted") or [])
    questions = [q for q in questions if q.get("number") not in deleted]

    # 2. Add user-added questions (skip if number collision)
    existing = {q.get("number") for q in questions}
    for added in (ann.get("added") or []):
        num = added.get("number")
        if num and num not in existing:
            # Shallow copy + ensure required fields present
            q = {
                "number":   num,
                "topic":    added.get("topic") or "Other / General",
                "text":     added.get("text") or "",
                "choices":  list(added.get("choices") or []),
                "answer":   added.get("answer") or "",
                "images":   list(added.get("images") or []),
                "source":   added.get("source", ""),
                "year":     added.get("year", ""),
                "division": added.get("division", ""),
                "page":     added.get("page", 1),
                "qtype":    added.get("qtype") or "frq",
                "matching": added.get("matching"),
            }
            questions.append(q)
            existing.add(num)

    # 3. Apply field overrides (works on both pipeline-extracted and added)
    overrides = ann.get("field_overrides") or {}
    for q in questions:
        ov = overrides.get(q.get("number"))
        if ov:
            for k in ("text", "choices", "answer", "topic", "focus", "page",
                      "extra_pages", "context_id", "image_descriptions",
                      "lastEditedBy", "lastEditedDateTime", "validation",
                      "qtype", "matching"):
                if k in ov:
                    q[k] = ov[k]

    # 4. Image overrides
    img_ov = ann.get("image_overrides") or {}
    assigns = img_ov.get("assignments") or {}
    detached = set(img_ov.get("detached") or [])
    reassigned = set(assigns.keys())

    # Strip detached and reassigned images from wherever they currently sit
    for q in questions:
        cur = q.get("images") or []
        q["images"] = [i for i in cur if i not in detached and i not in reassigned]

    # Attach reassigned images to their target questions. An assignment value
    # may be a single qnum (legacy 1:1) or a list of qnums (shared across
    # multiple questions). Dedup per question.
    by_num = {q.get("number"): q for q in questions}
    for fn, val in assigns.items():
        targets = val if isinstance(val, list) else [val]
        for qnum in targets:
            q = by_num.get(qnum)
            if q is None:
                continue
            imgs = q.setdefault("images", [])
            if fn not in imgs:
                imgs.append(fn)

    # 5. LLM answer validations (replay so they survive reprocess)
    validations = ann.get("validations") or {}
    for q in questions:
        v = validations.get(q.get("number"))
        if v:
            # Mark stale if the current question text/answer has drifted
            tv = (v.get("text_at_validation") or "")[:200]
            av = v.get("answer_at_validation") or ""
            cur_t = (q.get("text") or "")[:200]
            cur_a = q.get("answer") or ""
            v = dict(v)
            v["stale"] = (tv and cur_t and tv != cur_t) or (av != cur_a)
            q["validation"] = v

    return questions

# ---------------------------------------------------------------------------
# Markdown generation — clean, no HTML
# ---------------------------------------------------------------------------

def _render_matching_block(lines: list[str], matching: dict) -> None:
    """Render a matching question's two columns as a markdown table, plus
    the correct-pairing answer key. Cell images render as a markdown image
    reference inline in the cell (no separate figure block, since a
    matching cell's image IS the cell content, not a question-level figure)."""
    left = matching.get("left") or []
    right = matching.get("right") or []
    if not left and not right:
        return

    def _cell(item: dict) -> str:
        text = (item.get("text") or "").strip()
        img = item.get("image")
        if img:
            text = (text + " " if text else "") + f"![Figure](images/{img})"
        return text or "*(empty)*"

    lines.append("| # | Column A | | Column B |")
    lines.append("|---|---|---|---|")
    for i in range(max(len(left), len(right))):
        l = left[i] if i < len(left) else None
        r = right[i] if i < len(right) else None
        l_label = l.get("label", "") if l else ""
        r_label = r.get("label", "") if r else ""
        l_cell = _cell(l) if l else ""
        r_cell = _cell(r) if r else ""
        lines.append(f"| {i + 1} | **{l_label}.** {l_cell} | | **{r_label}.** {r_cell} |")
    lines.append("")

    pairs = matching.get("pairs") or {}
    if pairs:
        pair_strs = [f"{l}→{r}" for l, r in pairs.items()]
        lines.append(f"**Correct matches:** {', '.join(pair_strs)}")
        lines.append("")


def _render_question_block(lines: list[str], q: dict, i: int) -> None:
    """Render one question's heading/meta/text/choices/images/answer/validation
    block. Shared by clustered (case-study) and standalone rendering — does
    NOT append a trailing divider, since a cluster of case-study questions
    renders as one visual block with a single divider after the whole group."""
    lines.append(f"### Q{i}")
    meta = f"*{q['source']} · #{q['number']}"
    if q.get("focus"):
        meta += f" · focus: {q['focus']}"
    meta += "*"
    lines.append(meta)
    lines.append("")
    lines.append(q["text"])
    lines.append("")
    for c in q.get("choices", []):
        lines.append(f"- **{c['letter']}.** {c['text']}")
    if q.get("choices"):
        lines.append("")
    if q.get("qtype") == "matching":
        _render_matching_block(lines, q.get("matching") or {})
    for img in q.get("images", []):
        lines.append(f"![Figure](images/{img})")
        lines.append("")
    ans = q.get("answer", "").strip()
    if ans:
        lines.append(f"**Answer:** {ans}")
        lines.append("")
    v = q.get("validation") or {}
    if v and v.get("status") in ("correct", "incorrect", "uncertain"):
        icon = {"correct": "✓", "incorrect": "⚠",
                "uncertain": "?"}[v["status"]]
        label = v["status"].capitalize()
        stale_suffix = " *(stale — text/answer changed since check)*" if v.get("stale") else ""
        parts = [f"> {icon} **Verified: {label}**{stale_suffix}"]
        if v["status"] == "incorrect" and v.get("correct_answer"):
            parts.append(f"Likely correct answer: **{v['correct_answer']}**.")
        if v.get("rationale"):
            parts.append(v["rationale"])
        if v.get("source"):
            parts.append(f"*Source: {v['source']}*")
        lines.append("  ".join(parts))
        lines.append("")


def _context_key(q: dict) -> str | None:
    cid = q.get("context_id")
    return f"{q.get('_bucket', '')}::{cid}" if cid else None


def _cluster_by_context(qs: list[dict], context_lookup: dict[str, dict]) -> list[list[dict]]:
    """Group questions sharing a (bucket, context_id) key into one cluster,
    anchored at the sort position of its earliest member; every other
    question is its own cluster of one. Used so a case study's shared
    passage/table/diagram renders once, immediately before all the questions
    that reference it, instead of being scattered across the topic listing
    wherever each question happens to sort to."""
    clusters: list[list[dict]] = []
    by_key: dict[str, list[dict]] = {}
    for q in qs:
        key = _context_key(q)
        if key and key in context_lookup:
            if key not in by_key:
                by_key[key] = []
                clusters.append(by_key[key])
            by_key[key].append(q)
        else:
            clusters.append([q])
    return clusters


def _render_topic_section(lines: list[str], topic: str, qs: list[dict],
                          heading_level: int = 2, q_counter_start: int = 1,
                          context_lookup: dict[str, dict] | None = None) -> int:
    """Render one topic's worth of questions. Returns the next Q-counter value."""
    qs.sort(key=lambda q: (-int(q.get("year") or 0), q.get("number", "")))
    lines += ["", "---", "", f"{'#' * heading_level} {topic}", ""]
    i = q_counter_start
    for cluster in _cluster_by_context(qs, context_lookup or {}):
        key = _context_key(cluster[0])
        ctx = (context_lookup or {}).get(key) if key else None
        if ctx:
            heading = "**Case study"
            if ctx.get("title"):
                heading += f": {ctx['title']}"
            heading += "**"
            lines.append(heading)
            lines.append("")
            if ctx.get("text"):
                lines.append(ctx["text"])
                lines.append("")
            for img in ctx.get("images") or []:
                lines.append(f"![Context figure](images/{img})")
                lines.append("")
        for q in cluster:
            _render_question_block(lines, q, i)
            i += 1
        lines.append("---")
        lines.append("")
    return i


def _all_contexts() -> dict[str, dict]:
    """Shared context blocks (case-study passages/tables/diagrams) across
    every bucket of the current event, namespaced "bucket::id" — context ids
    are only unique within their own bucket (see review.html's nextContextId
    counter, which restarts per PDF)."""
    state = _load_state()
    out: dict[str, dict] = {}
    for bucket, ann in state.get("annotations", {}).items():
        for c in (ann.get("contexts") or []):
            cid = c.get("id")
            if cid:
                out[f"{bucket}::{cid}"] = c
    return out


def build_markdown(all_questions: list[dict]) -> str:
    ev = _ev()
    context_lookup = _all_contexts()
    topics = list(ev.topics)
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for q in all_questions:
        t = q.get("topic") or "Other / General"
        if t not in topics:
            t = "Other / General"
        by_topic[t].append(q)

    sources = sorted({q["source"] for q in all_questions})
    lines: list[str] = []

    lines += [
        f"# {ev.name} — Question Bank",
        "",
        f"*{len(sources)} source tests · {len(all_questions)} questions*",
        "",
        "## Sources",
        "",
    ]
    for s in sources:
        lines.append(f"- {s}")

    # If the event declares rotating foci AND we have any focus-tagged
    # questions, split the bank by focus first, then by topic within each
    # focus. Otherwise fall through to the flat topic-only layout.
    use_focus = bool(ev.foci or ()) and any(q.get("focus") for q in all_questions)

    if use_focus:
        ordered_foci = list(ev.foci) + ["(no focus)"]
        by_focus: dict[str, list[dict]] = defaultdict(list)
        for q in all_questions:
            f = q.get("focus") or "(no focus)"
            by_focus[f].append(q)

        lines += ["", "---", "", "## Table of Contents", ""]
        for focus in ordered_foci:
            if focus not in by_focus:
                continue
            anchor = re.sub(r"[^a-z0-9]+", "-", focus.lower()).strip("-")
            lines.append(f"- [{focus}](#{anchor}) — {len(by_focus[focus])} questions")

        counter = 1
        for focus in ordered_foci:
            if focus not in by_focus:
                continue
            lines += ["", "---", "", f"# {focus}", ""]
            sub: dict[str, list[dict]] = defaultdict(list)
            for q in by_focus[focus]:
                t = q.get("topic") or "Other / General"
                if t not in topics:
                    t = "Other / General"
                sub[t].append(q)
            for topic in topics:
                if topic not in sub:
                    continue
                counter = _render_topic_section(lines, topic, sub[topic],
                                                heading_level=2,
                                                q_counter_start=counter,
                                                context_lookup=context_lookup)
        return "\n".join(lines)

    lines += ["", "---", "", "## Table of Contents", ""]
    for topic in topics:
        if topic not in by_topic:
            continue
        anchor = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
        lines.append(f"- [{topic}](#{anchor}) — {len(by_topic[topic])} questions")

    counter = 1
    for topic in topics:
        if topic not in by_topic:
            continue
        counter = _render_topic_section(lines, topic, by_topic[topic],
                                        heading_level=2,
                                        q_counter_start=counter,
                                        context_lookup=context_lookup)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
# Per-event reentrant lock around the load → mutate → save cycle. Multi-threaded
# WSGI servers (or just two browser tabs) can hit /save and /reprocess
# simultaneously; without the lock + atomic write you get corrupt JSON.
import threading as _threading
_state_locks: dict[str, _threading.RLock] = {}


def _state_lock() -> _threading.RLock:
    key = _ev().slug if _current_event.get(None) else "_default_"
    lk = _state_locks.get(key)
    if lk is None:
        lk = _state_locks[key] = _threading.RLock()
    return lk


def _drain_state_writes(timeout_per_lock: float = 3.0) -> int:
    """Block until every known per-event state lock can be acquired, then
    release them. Used by the SIGINT handler to wait out any in-flight save
    before the process exits.

    Returns the number of locks successfully drained (locks that couldn't be
    acquired within `timeout_per_lock` seconds are skipped so we don't hang
    forever on a deadlocked writer)."""
    drained = 0
    for slug, lk in list(_state_locks.items()):
        if lk.acquire(timeout=timeout_per_lock):
            try:
                drained += 1
            finally:
                lk.release()
    return drained


def install_graceful_shutdown() -> None:
    """Install a SIGINT handler that drains in-flight state writes before
    re-raising the default KeyboardInterrupt. Idempotent.

    Importing this module doesn't install the handler — callers (the Flask
    review_app, the CLI build script) opt in. Library code that runs inside
    other applications shouldn't hijack signals."""
    import signal
    if getattr(install_graceful_shutdown, "_installed", False):
        return
    install_graceful_shutdown._installed = True
    prev = signal.getsignal(signal.SIGINT)

    def _handler(signum, frame):
        # Best-effort: drain, then chain to the previous handler so Werkzeug /
        # the default KeyboardInterrupt path still runs.
        try:
            n = _drain_state_writes()
            if n:
                print(f"\n[shutdown] drained {n} state lock(s); exiting.")
        except Exception:
            pass
        if callable(prev) and prev not in (signal.SIG_IGN, signal.SIG_DFL):
            prev(signum, frame)
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)


# Latest schema version. Older state files are migrated forward on load. Bump
# the constant and add a migrate_to_N entry when a breaking change ships.
STATE_SCHEMA_VERSION = 3


def _migrate_state(state: dict) -> dict:
    """Forward-migrate `state` to STATE_SCHEMA_VERSION in place."""
    v = state.get("_schema_version", 1)
    # v1 → v2: introduce annotations dict if missing (no field changes).
    if v < 2:
        state.setdefault("annotations", {})
        state.setdefault("manual", {})
    # v2 → v3: introduce pdf_meta dict (per-PDF Tournament name/Year
    # overrides, keyed by test filename) if missing (no field changes).
    if v < 3:
        state.setdefault("pdf_meta", {})
    state["_schema_version"] = STATE_SCHEMA_VERSION
    return state


def _load_state_unlocked() -> dict:
    # Caller must already hold _state_lock().
    state_file = _ev().state_file
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return _migrate_state(data)
        except Exception:
            pass
    return _migrate_state({"questions": {}, "vision": {}})


def _save_state_unlocked(state: dict) -> None:
    # Caller must already hold _state_lock().
    state["_schema_version"] = STATE_SCHEMA_VERSION
    state_file = _ev().state_file
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, state_file)


def _load_state() -> dict:
    with _state_lock():
        return _load_state_unlocked()


def _save_state(state: dict) -> None:
    """Atomically persist `state`. Writes to a sibling .tmp file then os.replace
    so a crash/interrupt mid-write never leaves a half-written JSON on disk."""
    with _state_lock():
        _save_state_unlocked(state)


@contextlib.contextmanager
def _state_transaction():
    """Hold the current event's _state_lock() across the full
    load -> mutate -> save cycle — see auth.py's _users_transaction() for the
    lost-update bug this avoids: a route handler that did `state =
    _load_state()` then `_save_state(state)` as two separate calls left the
    lock released in between, so two near-simultaneous edits to the same
    event's question bank (e.g. two annotation saves, or an autosave PATCH
    racing a reprocess-cancel) could each load the same pre-mutation
    snapshot and the later save would silently overwrite the earlier one.

    Deliberately NOT used by process_pair()'s OCR-job checkpoint saves —
    that function is handed a `state` snapshot once and holds it (and keeps
    re-saving it) for the entire duration of a potentially multi-minute
    vision pipeline run. Wrapping that in one lock acquisition would block
    every other request against the same event's state file for the whole
    job; it's a separate, harder problem than the short request-handler
    load/mutate/save pairs this transaction targets. Save only runs if the
    `with`-block body doesn't raise."""
    with _state_lock():
        state = _load_state_unlocked()
        yield state
        _save_state_unlocked(state)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Sci-Oly question bank")
    parser.add_argument("--event", required=True,
                        choices=sorted(EVENTS),
                        help="Which event to process (required)")
    parser.add_argument("--limit",     type=int, default=0,
                        help="Max test PDFs to process (0 = all)")
    parser.add_argument("--rebuild",   action="store_true",
                        help="Ignore all caches and reprocess everything")
    parser.add_argument("--text-only", action="store_true",
                        help="Skip vision API even if key is available")
    args = parser.parse_args()
    install_graceful_shutdown()

    set_event(args.event)
    ev = _ev()
    print(f"Event: {ev.name}  (slug={ev.slug})")
    print(f"  base dir: {ev.base_dir}")
    print()

    use_vision = not args.text_only and _vision_available()
    if use_vision:
        print(f"Vision mode: ON  (model={VISION_MODEL})")
    else:
        print("Vision mode: OFF (text-only extraction)")

    state = _load_state()
    if args.rebuild:
        state = {"questions": {}, "vision": {}}
        # Clean up old images so we don't get stale files
        for f in ev.image_dir.glob("*"):
            f.unlink(missing_ok=True)

    test_pdfs = sorted(ev.base_dir.glob(f"{ev.filename_prefix}_*_test.pdf"))

    # .docx/.doc test files: convert any without an already-converted PDF
    # sibling. The CLI runs as one long batch already, so (unlike the web
    # upload path) it's fine to convert inline rather than job-queueing it.
    doc_sources = sorted(ev.base_dir.glob(f"{ev.filename_prefix}_*_test.docx")) + \
                  sorted(ev.base_dir.glob(f"{ev.filename_prefix}_*_test.doc"))
    existing_pdf_names = {p.name for p in test_pdfs}
    for src in doc_sources:
        pdf_sibling = src.with_suffix(".pdf")
        if pdf_sibling.exists():
            if pdf_sibling.name not in existing_pdf_names:
                test_pdfs.append(pdf_sibling)
                existing_pdf_names.add(pdf_sibling.name)
            continue
        print(f"Converting {src.name} to PDF via LibreOffice...")
        try:
            converted = doc_convert.convert_to_pdf(src, src.parent)
        except doc_convert.DocConvertError as e:
            print(f"  [!] {e}")
            continue
        test_pdfs.append(converted)
        existing_pdf_names.add(converted.name)
    test_pdfs = sorted(test_pdfs)

    if args.limit:
        test_pdfs = test_pdfs[: args.limit]

    print(f"Found {len(test_pdfs)} test PDFs\n")

    all_questions: list[dict] = []

    # Include cached results for PDFs not in this run. Tag each with its
    # source bucket so build_markdown() can look up shared context blocks
    # (which are only unique within a bucket — see state["annotations"]).
    for ck, cqs in state["questions"].items():
        if not any(p.name == ck for p in test_pdfs):
            for q in cqs:
                q["_bucket"] = ck
            all_questions.extend(cqs)

    for test_pdf in test_pdfs:
        key_pdf = test_pdf.parent / test_pdf.name.replace("_test.pdf", "_key.pdf")
        if not key_pdf.exists():
            for ext in (".docx", ".doc"):
                key_src = test_pdf.parent / test_pdf.name.replace("_test.pdf", f"_key{ext}")
                if key_src.exists():
                    print(f"Converting {key_src.name} to PDF via LibreOffice...")
                    try:
                        key_pdf = doc_convert.convert_to_pdf(key_src, key_src.parent)
                    except doc_convert.DocConvertError as e:
                        print(f"  [!] {e}")
                    break
        qs = process_pair(
            test_pdf,
            key_pdf if key_pdf.exists() else None,
            state,
            use_vision,
        )
        for q in qs:
            q["_bucket"] = test_pdf.name
        all_questions.extend(qs)

    # Deduplicate (same source + number + first 80 chars)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for q in all_questions:
        key = (q["source"], q["number"], q["text"][:80])
        if key not in seen:
            seen.add(key)
            unique.append(q)
    all_questions = unique

    print(f"\nTotal unique questions: {len(all_questions)}")
    counts = defaultdict(int)
    for q in all_questions:
        counts[q.get("topic", "Other")] += 1
    for t in ev.topics:
        if t in counts:
            print(f"  {t}: {counts[t]}")

    if not all_questions:
        print("No questions found.")
        sys.exit(1)

    ev.out_md.write_text(build_markdown(all_questions), encoding="utf-8")
    print(f"\nWrote: {ev.out_md}")
    print(f"Images: {ev.image_dir}/")
    if not use_vision:
        print("\nTip: set ANTHROPIC_API_KEY and re-run (without --text-only) to enable")
        print("     vision-based image assignment and OCR for image-based PDFs.")


if __name__ == "__main__":
    main()
