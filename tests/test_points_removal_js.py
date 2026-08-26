"""
Unit tests for the pure JS helpers behind extract.html's "Remove point
markers" tool (Tools ▾ · Remove point markers…).

The feature scans the current PDF's questions (stem + every choice) for
leftover point-value markers from the source paper -- the worded form
("(2 points)", "[3 pts]"), a bare bracketed number ("[1]"), and a trailing
bare parenthesised number ("(6)") -- previews what would change, and removes
only what the user ticks. Three pure pieces live in extract.html and are
exercised here under Node, same as test_split_question_group_js.py:

  - pointsPresetRegex(presetId, wordedSource, customSource) -- builds the
    RegExp for a preset, or null on an invalid custom pattern.
  - tidyPointsWhitespace(s) -- collapses the double-space a removal leaves
    behind, then trims.
  - findPointsMatchesInQuestion(q, regex) -- scans one question's stem and
    every choice's text, returning what would change.

The worded preset's pattern text is passed in from the server
(POINTS_RE_SOURCE, rendered from text_utils._POINTS_RE.pattern) rather than
duplicated in the template, so a parity test here confirms the JS preset
and the real text_utils.strip_points() regex are exactly the same pattern
string -- they can't drift apart because there's only one copy of the text.

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

import text_utils  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node not installed -- JS helper execution is skipped",
)

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "extract.html"

WORDED_SOURCE = text_utils._POINTS_RE.pattern


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
    fns = ["pointsPresetRegex", "tidyPointsWhitespace", "findPointsMatchesInQuestion"]
    return "\n".join(_extract_function(html, fn) for fn in fns)


def _run_node(js_source: str, expr: str):
    """Run `js_source` followed by `console.log(JSON.stringify(<expr>))` under node."""
    script = js_source + f"\nconsole.log(JSON.stringify({expr}));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# pointsPresetRegex
# ---------------------------------------------------------------------------

def test_worded_preset_matches_text_utils_points_re_exactly(js_source):
    # Parity check: the JS preset is built from the same pattern *text* as
    # text_utils._POINTS_RE, not a hand-copied duplicate. Confirm the two
    # regexes agree on a representative set of strings, python-side vs
    # node-side, so a future edit to _POINTS_RE can't silently drift.
    cases = [
        "(2 points)", "(1 point)", "[3 pts]", "(3 pts each)",
        "no marker here", "(parenthetical) but not points",
    ]
    py_re = text_utils._POINTS_RE
    for text in cases:
        py_match = bool(py_re.search(text))
        out = _run_node(
            js_source,
            f'(function(){{ const re = pointsPresetRegex("worded", {json.dumps(WORDED_SOURCE)}, ""); '
            f'return re.test({json.dumps(text)}); }})()',
        )
        assert out == py_match, f"mismatch for {text!r}: python={py_match} js={out}"


def test_worded_preset_matches_fraction_point_values(js_source):
    text = "(½ point)"
    assert text_utils._POINTS_RE.search(text)  # sanity: python side matches too
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("worded", {json.dumps(WORDED_SOURCE)}, ""); '
        f'return re.test({json.dumps(text)}); }})()',
    )
    assert out is True


@pytest.mark.parametrize("text,expected", [
    ("[1]", True),
    ("[10]", True),
    ("[2.5]", True),
    ("[abc]", False),
    ("no brackets", False),
])
def test_bare_bracket_preset(js_source, text, expected):
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("bare_bracket", "", ""); '
        f'return re.test({json.dumps(text)}); }})()',
    )
    assert out == expected


def test_bare_paren_trailing_matches_end_only(js_source):
    # A trailing bare "(6)" is a real marker; the same text mid-string
    # (e.g. inside a formula) must survive untouched -- an unanchored
    # pattern would eat parts of a formula or answer value.
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("bare_paren_trailing", "", ""); '
        f'return re.test("What is F = ma(6) given these values"); }})()',
    )
    assert out is False

    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("bare_paren_trailing", "", ""); '
        f'return re.test("What is the value of R? (6)"); }})()',
    )
    assert out is True


def test_custom_regex_invalid_pattern_returns_null(js_source):
    out = _run_node(
        js_source,
        '(function(){ const re = pointsPresetRegex("custom", "", "[unterminated"); '
        'return re === null; })()',
    )
    assert out is True


def test_custom_regex_valid_pattern_compiles(js_source):
    out = _run_node(
        js_source,
        '(function(){ const re = pointsPresetRegex("custom", "", "foo\\\\d+"); '
        'return re.test("foo123"); })()',
    )
    assert out is True


# ---------------------------------------------------------------------------
# tidyPointsWhitespace
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("a  b", "a b"),
    ("a    b   c", "a b c"),
    ("  leading and trailing  ", "leading and trailing"),
    ("no extra space here", "no extra space here"),
    ("", ""),
])
def test_tidy_points_whitespace(js_source, text, expected):
    out = _run_node(js_source, f"tidyPointsWhitespace({json.dumps(text)})")
    assert out == expected


# ---------------------------------------------------------------------------
# findPointsMatchesInQuestion
# ---------------------------------------------------------------------------

def test_find_matches_stem_only(js_source):
    q = {"number": "5", "text": "What is the answer? (2 points)", "choices": []}
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("worded", {json.dumps(WORDED_SOURCE)}, ""); '
        f'return findPointsMatchesInQuestion({json.dumps(q)}, re); }})()',
    )
    assert len(out) == 1
    assert out[0]["field"] == "text"
    assert out[0]["idx"] == -1
    assert out[0]["after"] == "What is the answer?"


def test_find_matches_choice_only_not_stem(js_source):
    # Real corpus shape: a grouped sub-question's point marker sits inside a
    # choice's text, not the stem.
    q = {
        "number": "12",
        "text": "What is the potential difference between nodes 1 and 2? (1 point)",
        "choices": [
            {"letter": "A", "text": "no marker here"},
            {"letter": "B", "text": "also nothing (1 point)"},
        ],
    }
    re_stem_only = {
        "number": "12",
        "text": "no marker on the stem",
        "choices": [
            {"letter": "A", "text": "clean"},
            {"letter": "B", "text": "has a marker (3 points)"},
        ],
    }
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("worded", {json.dumps(WORDED_SOURCE)}, ""); '
        f'return findPointsMatchesInQuestion({json.dumps(re_stem_only)}, re); }})()',
    )
    assert len(out) == 1
    assert out[0]["field"] == "choices"
    assert out[0]["idx"] == 1
    assert out[0]["label"] == "Choice B"
    assert out[0]["after"] == "has a marker"


def test_find_matches_none_when_no_marker(js_source):
    q = {"number": "1", "text": "Clean stem", "choices": [{"letter": "A", "text": "clean choice"}]}
    out = _run_node(
        js_source,
        f'(function(){{ const re = pointsPresetRegex("worded", {json.dumps(WORDED_SOURCE)}, ""); '
        f'return findPointsMatchesInQuestion({json.dumps(q)}, re); }})()',
    )
    assert out == []


def test_find_matches_whitespace_collapsed_after_removal(js_source):
    q = {"number": "9", "text": "Value here [1] in the middle of a sentence", "choices": []}
    out = _run_node(
        js_source,
        '(function(){ const re = pointsPresetRegex("bare_bracket", "", ""); '
        f'return findPointsMatchesInQuestion({json.dumps(q)}, re); }})()',
    )
    assert len(out) == 1
    assert out[0]["after"] == "Value here in the middle of a sentence"
