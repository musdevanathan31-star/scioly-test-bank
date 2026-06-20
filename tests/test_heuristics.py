"""
Regression baseline for the heuristics that drive question extraction and dedup.

These tests pin down today's observable behaviour for:
  - split_choices              (MC option detection + stem split)
  - split_choices_by_lines     (multi-line fallback splitter)
  - _strip_points              (point-marker stripper + NFKC normalisation)
  - classify_topic             (keyword-weighted topic assignment)
  - _section_suffix            (duplicate-question alpha-suffix generator)
  - qgen.is_duplicate          (Jaccard 3-gram dedup threshold)

Run with: `python -m pytest tests/ -q` (no external deps).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402
import qgen                         # noqa: E402

bqb.set_event("circuit_lab")


# ---------------------------------------------------------------------------
# split_choices
# ---------------------------------------------------------------------------

def test_split_choices_clean_abcd():
    text = "What is V? a. 1 V b. 2 V c. 3 V d. 4 V"
    stem, choices = bqb.split_choices(text)
    assert stem.startswith("What is V?")
    assert [c["letter"] for c in choices] == ["A", "B", "C", "D"]
    assert choices[0]["text"] == "1 V"


def test_split_choices_no_question_hint_still_works_when_clean_A():
    # Completion-style stem (no "?" or "which/what") — the post-fix branch
    # should still accept when letters[0] == "A".
    text = ("If an solid conducting sphere is given a positive net charge, "
            "the electrostatic potential of the conductor is "
            "a. Constant b. Entirely zero c. Zero on the surface "
            "d. Largest on the surface e. Largest at the center")
    stem, choices = bqb.split_choices(text)
    assert len(choices) == 5
    assert choices[0]["letter"] == "A"
    assert choices[0]["text"] == "Constant"


def test_split_choices_rejects_unit_C_after_digit():
    # Pre-fix bug: "5 C." inside the question stem matched _MC_OPTION as a
    # phantom "C." option, which broke the strictly-ascending letter check.
    text = "Q has 5 C of charge. a. yes b. no"
    stem, choices = bqb.split_choices(text)
    assert [c["letter"] for c in choices] == ["A", "B"]


def test_split_choices_not_multiple_choice():
    text = "Compute the resistance of the circuit. Show your work."
    stem, choices = bqb.split_choices(text)
    assert choices == []
    assert stem == text


def test_split_choices_by_lines_multiline_fallback():
    raw = """\
What does this do?
a. choice one
b. choice two
c. choice three
d. choice four
"""
    stem, choices = bqb.split_choices_by_lines(raw)
    assert stem == "What does this do?"
    assert len(choices) == 4
    assert choices[2]["letter"] == "C" and choices[2]["text"] == "choice three"


def test_split_choices_by_lines_no_markers_positional():
    raw = "alpha\nbeta\ngamma\ndelta"
    stem, choices = bqb.split_choices_by_lines(raw)
    assert stem == ""
    assert [c["letter"] for c in choices] == ["A", "B", "C", "D"]
    assert choices[2]["text"] == "gamma"


# ---------------------------------------------------------------------------
# _strip_points + NFKC normalisation
# ---------------------------------------------------------------------------

def test_strip_points_removes_marker():
    assert bqb._strip_points("(2 points) Find R") == "Find R"
    assert bqb._strip_points("[3pts] each") == "each"


def test_strip_points_normalises_nbsp():
    # NBSP (U+00A0) and ZWS (U+200B) shouldn't survive normalisation.
    out = bqb._strip_points("a b​c")
    assert " " not in out
    assert "​" not in out
    assert "abc" in out.replace(" ", "")


# ---------------------------------------------------------------------------
# classify_topic (event-specific keyword scoring)
# ---------------------------------------------------------------------------

def test_classify_topic_capacitors():
    assert bqb.classify_topic("compute the RC time constant for the capacitor") == "Capacitors"


def test_classify_topic_kirchhoff():
    assert bqb.classify_topic("apply KVL around the closed loop") == "Kirchhoff's Laws"


def test_classify_topic_other_when_unknown():
    assert bqb.classify_topic("the quick brown fox") == "Other / General"


# ---------------------------------------------------------------------------
# _section_suffix
# ---------------------------------------------------------------------------

def test_section_suffix_basic():
    assert bqb._section_suffix(1) == "b"
    assert bqb._section_suffix(2) == "c"
    assert bqb._section_suffix(9) == "j"


def test_section_suffix_overflow():
    assert bqb._section_suffix(25) == "z"
    assert bqb._section_suffix(26) == "aa"
    assert bqb._section_suffix(27) == "ab"
    assert bqb._section_suffix(51) == "az"
    assert bqb._section_suffix(52) == "ba"


# ---------------------------------------------------------------------------
# qgen.is_duplicate (Jaccard 3-gram threshold 0.4)
# ---------------------------------------------------------------------------

def test_is_duplicate_identical():
    bank = [{"text": "Find the voltage across the capacitor.", "number": "1"}]
    is_dup, matched = qgen.is_duplicate(
        {"text": "Find the voltage across the capacitor."}, bank)
    assert is_dup
    assert matched == "1"


def test_is_duplicate_different():
    bank = [{"text": "Find the voltage across the capacitor.", "number": "1"}]
    is_dup, _ = qgen.is_duplicate(
        {"text": "Compute the magnetic flux through the coil."}, bank)
    assert not is_dup


def test_is_duplicate_paraphrase_threshold():
    bank = [{"text": "The capacitor stores charge.", "number": "1"}]
    is_dup, _ = qgen.is_duplicate(
        {"text": "A resistor dissipates heat."}, bank)
    assert not is_dup


# ---------------------------------------------------------------------------
# Event.filename_prefix derivation
# ---------------------------------------------------------------------------

def test_filename_prefix_derives_from_event_match():
    from events import Event
    e = Event(slug="disease_detectives", name="Disease Detectives",
              event_match=("disease detectives",),
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "diseasedetectives"


def test_filename_prefix_explicit_override_wins():
    from events import Event
    e = Event(slug="anatomy_physiology", name="Anatomy & Physiology",
              event_match=("anatomy",), filename_prefix="anatomy",
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "anatomy"


def test_filename_prefix_falls_back_to_slug_when_no_event_match():
    from events import Event
    e = Event(slug="my_local_event", name="My Local Event",
              event_match=(),
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "my_local_event"
