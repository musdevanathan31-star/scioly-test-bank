"""
A PDF filename containing `+` must stay reachable through a proxy that
decodes the request path twice.

Buckets are PDF filenames, and scraped ones legitimately contain `+` (this
repo has a real `circuitlab_2019_c_ssss-utf-8u+6211u+662f_test.pdf`). The
frontend percent-encodes it as `%2B`, but a proxy can decode the path twice
— `%2B` -> `+` -> ` ` — applying the query-string convention that `+` means
space to a path, where it does not hold. Every lookup for that one PDF then
404s with "question not found" while every other PDF on the instance works,
which is exactly what was reported from production.

Verified against the running app before writing this: the same pick-image
request returns 200 with `%2B` and with a literal `+`, and 404s only in the
space-substituted form.

Run with: `python -m pytest tests/test_bucket_plus_recovery.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_app as ra  # noqa: E402


PLUS_BUCKET = "circuitlab_2019_c_ssss-utf-8u+6211u+662f_test.pdf"
MANGLED = PLUS_BUCKET.replace("+", " ")


def _state():
    return {"questions": {
        PLUS_BUCKET: [{"number": "1", "text": "plus-named pdf"},
                      {"number": "3", "text": "another"}],
        "circuitlab_2019_b_uflorida_test.pdf": [{"number": "1", "text": "ordinary pdf"}],
    }}


def test_exact_bucket_still_matches():
    q, qs = ra._find_question(_state(), PLUS_BUCKET, "1")
    assert q is not None and q["text"] == "plus-named pdf"
    assert len(qs) == 2


def test_space_mangled_bucket_recovers():
    """The reported production failure."""
    q, _ = ra._find_question(_state(), MANGLED, "1")
    assert q is not None and q["text"] == "plus-named pdf"


def test_recovery_finds_the_right_question_not_just_the_bucket():
    q, _ = ra._find_question(_state(), MANGLED, "3")
    assert q is not None and q["text"] == "another"


def test_missing_number_in_recovered_bucket_still_misses():
    """Recovery must not invent a question that isn't there."""
    q, qs = ra._find_question(_state(), MANGLED, "99")
    assert q is None
    assert len(qs) == 2          # bucket resolved, number genuinely absent


def test_unrelated_unknown_bucket_still_misses():
    q, qs = ra._find_question(_state(), "no_such_test.pdf", "1")
    assert q is None and qs == []


def test_ordinary_buckets_are_untouched():
    q, _ = ra._find_question(_state(), "circuitlab_2019_b_uflorida_test.pdf", "1")
    assert q is not None and q["text"] == "ordinary pdf"


def test_recovery_is_one_directional():
    """A bucket whose real name contains a space is matched exactly and is
    not rewritten into a plus-named one."""
    state = {"questions": {"a real name.pdf": [{"number": "1", "text": "spaced"}],
                           "a+real+name.pdf": [{"number": "1", "text": "plussed"}]}}
    q, _ = ra._find_question(state, "a real name.pdf", "1")
    assert q["text"] == "spaced"          # exact match wins over the fallback
