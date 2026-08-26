"""
Unit tests for the pure JS geometry helpers behind extract.html's editable
question-bounding-box feature (drag a question's PDF box to resize it when a
section heading bled into the question).

Five pure pieces live in extract.html and are exercised here under Node,
same pattern as test_points_removal_js.py:

  - normalizeRect(rect) -- turns a possibly-inverted drag ({x0,y0,x1,y1}
    where x0>x1 and/or y0>y1, e.g. dragging the top edge below the bottom
    edge) into one with x0<=x1 and y0<=y1.
  - clampRectToPage(rect, pageW, pageH) -- clamps a rect's edges into
    [0,pageW] x [0,pageH].
  - ptsToPx(rect, dpi) / pxToPts(rect, dpi) -- PDF points <-> natural image
    pixels at a given render DPI (the same convention api_question_bboxes
    and extract-region already use server-side).
  - isDegenerateRect(rect, minSize) -- true when either dimension is under
    minSize, so a drag that never really moved (or collapsed a handle past
    its opposite edge) is rejected client-side instead of being POSTed to
    extract-region.
  - applyHandleDrag(rect, handle, dx, dy) -- moves the edge(s) a given
    resize handle (n/s/e/w/ne/nw/se/sw) controls by (dx,dy).

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
    fns = ["normalizeRect", "clampRectToPage", "ptsToPx", "pxToPts",
           "isDegenerateRect", "applyHandleDrag"]
    return "\n".join(_extract_function(html, fn) for fn in fns)


def _run_node(js_source: str, expr: str):
    """Run `js_source` followed by `console.log(JSON.stringify(<expr>))` under node."""
    script = js_source + f"\nconsole.log(JSON.stringify({expr}));"
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# normalizeRect
# ---------------------------------------------------------------------------

def test_normalize_already_ordered_rect_is_unchanged(js_source):
    rect = {"x0": 10, "y0": 20, "x1": 100, "y1": 200}
    out = _run_node(js_source, f"normalizeRect({json.dumps(rect)})")
    assert out == rect


def test_normalize_inverted_x_and_y(js_source):
    # Dragging the top edge past the bottom (and left past right) inverts
    # both axes -- normalizeRect must swap them back into order rather than
    # producing a rect with x1<x0/y1<y0 (which would compute as negative
    # width/height downstream).
    rect = {"x0": 100, "y0": 200, "x1": 10, "y1": 20}
    out = _run_node(js_source, f"normalizeRect({json.dumps(rect)})")
    assert out == {"x0": 10, "y0": 20, "x1": 100, "y1": 200}
    assert out["x1"] - out["x0"] >= 0
    assert out["y1"] - out["y0"] >= 0


def test_normalize_inverted_y_only(js_source):
    # Dragging just the top edge below the bottom edge: only y is inverted.
    rect = {"x0": 10, "y0": 200, "x1": 100, "y1": 20}
    out = _run_node(js_source, f"normalizeRect({json.dumps(rect)})")
    assert out == {"x0": 10, "y0": 20, "x1": 100, "y1": 200}


# ---------------------------------------------------------------------------
# clampRectToPage
# ---------------------------------------------------------------------------

def test_clamp_rect_fully_inside_page_is_unchanged(js_source):
    rect = {"x0": 10, "y0": 10, "x1": 100, "y1": 100}
    out = _run_node(js_source, f"clampRectToPage({json.dumps(rect)}, 612, 792)")
    assert out == rect


def test_clamp_rect_overflowing_page_bounds(js_source):
    # A drag that pushed a handle past the page edge on every side.
    rect = {"x0": -50, "y0": -20, "x1": 700, "y1": 900}
    out = _run_node(js_source, f"clampRectToPage({json.dumps(rect)}, 612, 792)")
    assert out == {"x0": 0, "y0": 0, "x1": 612, "y1": 792}


def test_clamp_negative_only_on_one_edge(js_source):
    rect = {"x0": -10, "y0": 50, "x1": 300, "y1": 400}
    out = _run_node(js_source, f"clampRectToPage({json.dumps(rect)}, 612, 792)")
    assert out == {"x0": 0, "y0": 50, "x1": 300, "y1": 400}


# ---------------------------------------------------------------------------
# ptsToPx / pxToPts -- round trip at several DPIs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dpi", [72, 96, 120, 150, 200, 300])
def test_points_pixels_round_trip(js_source, dpi):
    rect = {"x0": 12.5, "y0": 34.0, "x1": 456.75, "y1": 601.25}
    out = _run_node(
        js_source,
        f"pxToPts(ptsToPx({json.dumps(rect)}, {dpi}), {dpi})",
    )
    for k in ("x0", "y0", "x1", "y1"):
        assert out[k] == pytest.approx(rect[k], abs=1e-9)


def test_pts_to_px_at_120_dpi_matches_the_backends_factor(js_source):
    # f = 72/dpi is the exact factor api_extract_region uses server-side
    # (review_app.py) to go from image pixels back to PDF points -- ptsToPx
    # must be its precise inverse at the render DPI the page always uses.
    rect = {"x0": 0, "y0": 0, "x1": 72, "y1": 144}
    out = _run_node(js_source, f"ptsToPx({json.dumps(rect)}, 120)")
    assert out == {"x0": 0, "y0": 0, "x1": 120, "y1": 240}


# ---------------------------------------------------------------------------
# isDegenerateRect
# ---------------------------------------------------------------------------

def test_zero_size_rect_is_degenerate(js_source):
    rect = {"x0": 10, "y0": 10, "x1": 10, "y1": 10}
    out = _run_node(js_source, f"isDegenerateRect({json.dumps(rect)}, 6)")
    assert out is True


def test_tiny_rect_under_min_size_is_degenerate(js_source):
    rect = {"x0": 10, "y0": 10, "x1": 13, "y1": 40}   # width 3 < minSize 6
    out = _run_node(js_source, f"isDegenerateRect({json.dumps(rect)}, 6)")
    assert out is True


def test_rect_at_or_above_min_size_is_not_degenerate(js_source):
    rect = {"x0": 10, "y0": 10, "x1": 20, "y1": 40}   # 10x30, both >= 6
    out = _run_node(js_source, f"isDegenerateRect({json.dumps(rect)}, 6)")
    assert out is False


def test_degenerate_check_normalizes_an_inverted_rect_first(js_source):
    # An inverted-but-large rect must not read as degenerate just because
    # x1-x0 comes out negative before normalizing.
    rect = {"x0": 100, "y0": 100, "x1": 10, "y1": 10}
    out = _run_node(js_source, f"isDegenerateRect({json.dumps(rect)}, 6)")
    assert out is False


# ---------------------------------------------------------------------------
# applyHandleDrag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("handle,dx,dy,expected", [
    ("e",  20,   0, {"x0": 0, "y0": 0, "x1": 120, "y1": 100}),
    ("w", -20,   0, {"x0": -20, "y0": 0, "x1": 100, "y1": 100}),
    ("s",   0,  30, {"x0": 0, "y0": 0, "x1": 100, "y1": 130}),
    ("n",   0, -30, {"x0": 0, "y0": -30, "x1": 100, "y1": 100}),
    ("se", 20,  30, {"x0": 0, "y0": 0, "x1": 120, "y1": 130}),
    ("nw", -20, -30, {"x0": -20, "y0": -30, "x1": 100, "y1": 100}),
    ("ne", 20, -30, {"x0": 0, "y0": -30, "x1": 120, "y1": 100}),
    ("sw", -20, 30, {"x0": -20, "y0": 0, "x1": 100, "y1": 130}),
])
def test_apply_handle_drag_moves_only_its_own_edges(js_source, handle, dx, dy, expected):
    rect = {"x0": 0, "y0": 0, "x1": 100, "y1": 100}
    out = _run_node(js_source, f'applyHandleDrag({json.dumps(rect)}, "{handle}", {dx}, {dy})')
    assert out == expected


def test_apply_handle_drag_can_invert_the_rect(js_source):
    # Dragging the top edge (n) far enough down crosses the bottom edge --
    # applyHandleDrag itself does NOT normalize (callers do that once, on
    # release), so this is expected to come out with y0 > y1.
    rect = {"x0": 0, "y0": 0, "x1": 100, "y1": 100}
    out = _run_node(js_source, 'applyHandleDrag({"x0":0,"y0":0,"x1":100,"y1":100}, "n", 0, 150)')
    assert out["y0"] > out["y1"]
    normalized = _run_node(
        js_source,
        f'normalizeRect(applyHandleDrag({json.dumps(rect)}, "n", 0, 150))',
    )
    assert normalized["y0"] <= normalized["y1"]
