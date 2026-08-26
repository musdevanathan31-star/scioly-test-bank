"""
Coverage for scrape_scioly._normalize()'s handling of scio.ly's `difficulty`
field: a raw record's `difficulty` (already a 0.0-1.0 float on scio.ly's own
scale) is promoted directly onto the real `difficulty` key with no
conversion, and a raw record with no `difficulty` leaves the question
unrated (key absent, not None/0).

Same fixture pattern as tests/test_heuristics.py: bqb.set_event() once at
import time against a real bundled event, no server/Flask app needed since
_normalize() only touches bqb.classify_topic()/bqb.EVENT/bqb._strip_points().

Run with: `python -m pytest tests/test_scrape_scioly_difficulty.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402

bqb.set_event("circuit_lab")

import scrape_scioly  # noqa: E402


def _raw(**overrides):
    base = {
        "id": "abc123",
        "question": "What is Ohm's Law?",
        "options": ["V=IR", "V=I/R", "V=I+R", "V=IR^2"],
        "answers": [0],
        "subtopics": [],
        "tournament": "Test Invitational 2026",
        "division": "C",
        "base52": "tXnNS",
    }
    base.update(overrides)
    return base


def test_scraped_difficulty_promoted_onto_real_field():
    raw = _raw(difficulty=0.6)
    q = scrape_scioly._normalize(raw)
    assert q is not None
    assert q["difficulty"] == 0.6
    assert "_scioly_difficulty" not in q


def test_scraped_record_without_difficulty_leaves_question_unrated():
    raw = _raw()
    assert "difficulty" not in raw
    q = scrape_scioly._normalize(raw)
    assert q is not None
    assert "difficulty" not in q


def test_scraped_difficulty_none_leaves_question_unrated():
    raw = _raw(difficulty=None)
    q = scrape_scioly._normalize(raw)
    assert q is not None
    assert "difficulty" not in q


def test_scraped_difficulty_is_lossless_for_every_observed_value():
    # Observed on the live scio.ly API across two events/both question
    # types (see scrape_scioly.py's module docstring) -- confirm every one
    # round-trips through _normalize with no rounding/conversion drift.
    for value in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        q = scrape_scioly._normalize(_raw(difficulty=value))
        assert q["difficulty"] == value


def test_unparseable_difficulty_is_dropped_not_stored():
    q = scrape_scioly._normalize(_raw(difficulty="not-a-number"))
    assert "difficulty" not in q
