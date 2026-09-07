"""
Unit tests for the pure JS helpers behind extract.html's multi-part-question
tooling: "Split into group" (an existing MCQ whose choices are really
sub-parts) and "Add multi-part question from region" (the same split,
performed directly on a fresh region capture instead of a repair pass).

Both entry points share the same three pure pieces, all tested here:
  - stripTrailingPointMarker(text) -- drops a trailing "(2 points)" etc.
  - nextGroupQuestionNumbers(base, count, existing) -- "21, 21b, 21c..."
    sibling numbering, skipping any number already in use.
  - nextQuestionNumber(existing) -- the next available bare integer number;
    also shared with the plain "Add question from region"/matching-question
    capture paths, which don't otherwise appear in this file.

The DOM-wiring halves (splitQuestionIntoGroup, the applyCapture "newGroup"
branch, the data-act dispatch) can't run outside a browser, but these pure
pieces are extracted here verbatim from the template and executed under Node
so their behaviour is actually checked, not just parsed.

Skipped when Node isn't installed, matching test_page_js_syntax.py's policy.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node not installed -- JS helper execution is skipped",
)

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "extract.html"


def _extract_function(src: str, name: str) -> str:
    """Pull one top-level `function name(...){ ... }` block out by brace-matching."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"could not find function {name} in extract.html"
    start = m.end() - 1  # position of the opening "{"
    depth = 0
    i = start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


@pytest.fixture(scope="module")
def js_source():
    html = TEMPLATE.read_text(encoding="utf-8")
    strip_fn = _extract_function(html, "stripTrailingPointMarker")
    next_num_fn = _extract_function(html, "nextQuestionNumber")
    group_numbers_fn = _extract_function(html, "nextGroupQuestionNumbers")
    return strip_fn + "\n" + next_num_fn + "\n" + group_numbers_fn


def _run_node(js_source: str, expr: str):
    """Run `js_source` followed by `console.log(JSON.stringify(<expr>))` under node."""
    script = js_source + f"\nconsole.log(JSON.stringify({expr}));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# stripTrailingPointMarker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("What is the value of R4? (1 point)", "What is the value of R4?"),
    ("What is the potential difference between nodes 1 and 2? (1 point)",
     "What is the potential difference between nodes 1 and 2?"),
    ("Explain your reasoning (2 points)", "Explain your reasoning"),
    ("Explain your reasoning (2 pts)", "Explain your reasoning"),
    ("Explain your reasoning (2pt)", "Explain your reasoning"),
    ("No marker here", "No marker here"),
    ("Value in (parentheses) mid-sentence, no point marker",
     "Value in (parentheses) mid-sentence, no point marker"),
    ("  leading/trailing space (1 point)   ", "leading/trailing space"),
    ("", ""),
])
def test_strip_trailing_point_marker(js_source, text, expected):
    out = _run_node(js_source, f"stripTrailingPointMarker({json.dumps(text)})")
    assert out == expected


def test_strip_trailing_point_marker_only_strips_trailing(js_source):
    # A point marker that isn't at the end must survive -- conservative by
    # design, per the feature spec ("only strip a trailing parenthesised
    # point marker").
    text = "(3 points) is not how this question starts, so leave it alone"
    out = _run_node(js_source, f"stripTrailingPointMarker({json.dumps(text)})")
    assert out == text


# ---------------------------------------------------------------------------
# nextGroupQuestionNumbers
# ---------------------------------------------------------------------------

def test_next_group_numbers_basic_sequence(js_source):
    # First sub-question keeps the bare original number; never "21a".
    out = _run_node(js_source, 'nextGroupQuestionNumbers("21", 3, ["21"])')
    assert out == ["21", "21b", "21c"]


def test_next_group_numbers_skips_collision(js_source):
    # "21b" is already taken by an unrelated real question -- skip to "21c".
    out = _run_node(
        js_source,
        'nextGroupQuestionNumbers("21", 3, ["21", "21b", "40"])',
    )
    assert out == ["21", "21c", "21d"]


def test_next_group_numbers_count_one_is_just_base(js_source):
    out = _run_node(js_source, 'nextGroupQuestionNumbers("5", 1, ["5"])')
    assert out == ["5"]


def test_next_group_numbers_many_collisions(js_source):
    existing = ["9", "9b", "9c", "9d", "9e"]
    out = _run_node(
        js_source,
        f'nextGroupQuestionNumbers("9", 3, {json.dumps(existing)})',
    )
    assert out == ["9", "9f", "9g"]


# ---------------------------------------------------------------------------
# nextQuestionNumber -- shared by "Add question from region", the matching-
# question capture, and the new "Add multi-part question from region" (both
# its group-built success path and its single-question fallback).
# ---------------------------------------------------------------------------

def test_next_question_number_empty_bank_starts_at_one(js_source):
    out = _run_node(js_source, "nextQuestionNumber([])")
    assert out == "1"


def test_next_question_number_is_max_plus_one(js_source):
    out = _run_node(js_source, 'nextQuestionNumber(["1", "2", "5"])')
    assert out == "6"


def test_next_question_number_ignores_letter_suffixed_siblings(js_source):
    # "14b"/"14c" (group siblings) must not be read as 14 -- and must not
    # push the next bare number past what the numeric part implies.
    out = _run_node(js_source, 'nextQuestionNumber(["14", "14b", "14c"])')
    assert out == "15"


def test_next_question_number_order_independent(js_source):
    out = _run_node(js_source, 'nextQuestionNumber(["9", "3", "21", "14b"])')
    assert out == "22"


def test_next_question_number_ignores_junk_entries(js_source):
    # A number field that's missing/blank/non-numeric (possible on a
    # hand-added question before the user fills it in) must not crash the
    # scan or be treated as 0 pushing the result down.
    out = _run_node(js_source, 'nextQuestionNumber(["", null, "abc", "7"])')
    assert out == "8"
