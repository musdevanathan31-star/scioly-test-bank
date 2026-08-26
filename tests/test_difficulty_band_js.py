"""
Unit tests for the JS mirror of text_utils.difficulty_band()/
difficulty_value_for_band() -- DIFFICULTY_BANDS / difficultyBand() /
difficultyValueForBand() in common_ui.py's COMMON_JS, injected into every
page (extract.html, browse.html, assessment_builder.html) via
`{{ common_js|safe }}`.

Extracted out of common_ui.py's COMMON_JS source string (not a template
file) by brace-matching, same technique as test_split_question_group_js.py /
test_points_removal_js.py use on templates/extract.html, and executed under
Node. The main point of this file is the parity check: the JS and Python
implementations must agree on every value in a shared table, so the two
copies of the band thresholds can't silently drift apart.

Skipped when Node isn't installed, matching the other JS test files' policy.
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
import common_ui  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node not installed -- JS helper execution is skipped",
)


def _extract_function(src: str, name: str) -> str:
    """Pull one top-level `function name(...){ ... }` block out by brace-matching."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"could not find function {name} in COMMON_JS"
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
    src = common_ui.COMMON_JS
    # DIFFICULTY_BANDS is a top-level const the two functions close over --
    # grab it by matching from its declaration to the closing "];".
    m = re.search(r"const DIFFICULTY_BANDS = \[", src)
    assert m, "could not find DIFFICULTY_BANDS in COMMON_JS"
    end = src.index("];", m.start()) + 2
    const_src = src[m.start():end]
    band_fn = _extract_function(src, "difficultyBand")
    value_fn = _extract_function(src, "difficultyValueForBand")
    return const_src + "\n" + band_fn + "\n" + value_fn


def _run_node(js_source: str, expr: str):
    script = js_source + f"\nconsole.log(JSON.stringify({expr}));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


PARITY_VALUES = [0.0, 0.1, 0.2, 0.3, 0.31, 0.4, 0.5, 0.55, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0]


@pytest.mark.parametrize("value", PARITY_VALUES)
def test_python_and_js_agree_on_band(js_source, value):
    py_band = text_utils.difficulty_band(value)
    js_band = _run_node(js_source, f"difficultyBand({json.dumps(value)})")
    assert js_band == py_band, f"mismatch for {value}: python={py_band} js={js_band}"


def test_python_and_js_agree_on_missing_value(js_source):
    py_band = text_utils.difficulty_band(None)
    js_band = _run_node(js_source, "difficultyBand(null)")
    assert py_band is None
    assert js_band is None


@pytest.mark.parametrize("band", ["Easy", "Medium", "Hard", "Very Hard"])
def test_python_and_js_agree_on_value_for_band(js_source, band):
    py_value = text_utils.difficulty_value_for_band(band)
    js_value = _run_node(js_source, f"difficultyValueForBand({json.dumps(band)})")
    assert js_value == py_value


def test_js_unknown_band_is_null(js_source):
    js_value = _run_node(js_source, 'difficultyValueForBand("nonsense")')
    assert js_value is None


@pytest.mark.parametrize("value", PARITY_VALUES)
def test_js_round_trip_is_stable(js_source, value):
    # Band -> value -> band must be a no-op for a *representative* value
    # (i.e. the four canonical outputs of difficultyValueForBand), which is
    # exactly what the parametrized bands below check; this variant checks
    # the JS band() call itself is deterministic/idempotent for arbitrary
    # scraped values too.
    once = _run_node(js_source, f"difficultyBand({json.dumps(value)})")
    twice = _run_node(
        js_source,
        f"difficultyBand(difficultyValueForBand(difficultyBand({json.dumps(value)})))",
    )
    assert once == twice
