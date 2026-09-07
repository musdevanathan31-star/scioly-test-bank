"""Unit tests for the pure JS helpers behind the Browse page's sidebar
refresh fix: validating a question or changing its topic/focus used to
leave the sidebar's counts and filter-option labels stale until a manual
page reload -- neither setValidation() nor flushAutosave() touched
DATA.stats or re-ran the sidebar population at all.

Two pieces make the fix testable without a browser:

  - recomputeStats(questions) -- derives DATA.stats fresh from the
    in-memory question list, mirroring api_all_questions's Python logic
    (review_app.py) exactly (same fallback labels for a missing
    topic/source/validation). Refactored to take `questions` as a
    parameter rather than reading the DATA global, the same way
    nextGroupQuestionNumbers/nextQuestionNumber were parameterized this
    session for the same reason: it's now a pure function extractable and
    runnable under Node.
  - fillSelect(sel, items, allLabel) -- already existed; already captured
    and restored a <select>'s current value across a full option rebuild.
    That's the property the user asked to be pinned explicitly: a filter
    set to an existing topic must stay selected even as a brand-new topic
    is added to the option list by the same rebuild.

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

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "browse.html"


def _extract_function(src: str, name: str) -> str:
    """Pull one top-level `function name(...){ ... }` block out by brace-matching."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"could not find function {name} in browse.html"
    start = m.end() - 1
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


# A minimal stand-in for a real <select>, just enough to exercise
# fillSelect's actual logic (it reads sel.value, assigns sel.innerHTML,
# then reads [...sel.options]). Real <select>.innerHTML assignment resets
# the selection to the first option when nothing marks one "selected" --
# fillSelect's markup never does -- so this mimics that native behavior;
# without it, the "previously selected value disappeared" case couldn't be
# told apart from "value survives by accident of being untouched".
_FAKE_SELECT_JS = """
class FakeSelect {
  constructor(){ this._value = ""; this._options = []; }
  get options(){ return this._options; }
  get value(){ return this._value; }
  set value(v){ this._value = v; }
  set innerHTML(html){
    this._options = Array.from(html.matchAll(/<option value="([^"]*)"/g)).map(m => ({value: m[1]}));
    this._value = this._options.length ? this._options[0].value : "";
  }
}
"""


@pytest.fixture(scope="module")
def js_source():
    html = TEMPLATE.read_text(encoding="utf-8")
    esc_fn = _extract_function(html, "esc")
    fill_select_fn = _extract_function(html, "fillSelect")
    recompute_fn = _extract_function(html, "recomputeStats")
    return esc_fn + "\n" + fill_select_fn + "\n" + recompute_fn + "\n" + _FAKE_SELECT_JS


def _run_node(js_source: str, script_tail: str):
    script = js_source + "\n" + script_tail
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# recomputeStats
# ---------------------------------------------------------------------------

def _q(**kw):
    base = {"topic": None, "source": None, "focus": None, "validation": None, "_bucket": "bucketA"}
    base.update(kw)
    return base


def test_recompute_stats_counts_by_topic_source_bucket(js_source):
    questions = [
        _q(topic="Circuits", source="2019 Regional", _bucket="b1"),
        _q(topic="Circuits", source="2020 Regional", _bucket="b1"),
        _q(topic="Magnetism", source="2019 Regional", _bucket="b2"),
    ]
    out = _run_node(js_source, f"console.log(JSON.stringify(recomputeStats({json.dumps(questions)})));")
    assert out["total"] == 3
    assert out["by_topic"] == {"Circuits": 2, "Magnetism": 1}
    assert out["by_source"] == {"2019 Regional": 2, "2020 Regional": 1}
    assert out["by_bucket"] == {"b1": 2, "b2": 1}


def test_recompute_stats_fallback_labels_match_the_backend(js_source):
    """No topic/source/validation set at all -- must use the exact same
    fallback strings api_all_questions does, or the two would disagree
    about what a count means the moment one recompute happens client-side
    and the other happens on the next full page load."""
    questions = [_q()]
    out = _run_node(js_source, f"console.log(JSON.stringify(recomputeStats({json.dumps(questions)})));")
    assert out["by_topic"] == {"Other / General": 1}
    assert out["by_source"] == {"(no source)": 1}
    assert out["by_validation"] == {"unvalidated": 1}


def test_recompute_stats_reads_validation_status_from_the_nested_object(js_source):
    questions = [
        _q(validation={"status": "correct"}),
        _q(validation={"status": "incorrect"}),
        _q(validation=None),
    ]
    out = _run_node(js_source, f"console.log(JSON.stringify(recomputeStats({json.dumps(questions)})));")
    assert out["by_validation"] == {"correct": 1, "incorrect": 1, "unvalidated": 1}


def test_recompute_stats_focus_only_counted_when_truthy(js_source):
    questions = [_q(focus="Ohm's law"), _q(focus=""), _q()]
    out = _run_node(js_source, f"console.log(JSON.stringify(recomputeStats({json.dumps(questions)})));")
    assert out["by_focus"] == {"Ohm's law": 1}


# ---------------------------------------------------------------------------
# fillSelect -- the exact scenario the user described
# ---------------------------------------------------------------------------

def test_fill_select_keeps_the_current_selection_when_a_new_topic_appears(js_source):
    script = """
    const sel = new FakeSelect();
    sel.value = "general";
    fillSelect(sel, [["general", 10]], "All topics");   // simulate the select already showing "general"
    fillSelect(sel, [["general", 9], ["Kirchoff's laws", 1]], "All topics");  // a question got retagged
    console.log(JSON.stringify({
      value: sel.value,
      optionValues: sel.options.map(o => o.value),
    }));
    """
    out = _run_node(js_source, script)
    assert "Kirchoff's laws" in out["optionValues"], "new topic should appear in the option list"
    assert out["value"] == "general", "previously selected topic must stay selected"


def test_fill_select_falls_back_to_all_when_the_selected_value_disappears(js_source):
    """If the only question with the selected topic gets retagged away
    entirely, there is nothing valid left to restore -- it must fall back
    to the blank "All ..." option, not silently land on some other topic
    that happens to be first alphabetically/by count."""
    script = """
    const sel = new FakeSelect();
    sel.value = "";
    fillSelect(sel, [["OldTopic", 1]], "All topics");
    sel.value = "OldTopic";
    fillSelect(sel, [["NewTopic", 1]], "All topics");   // OldTopic no longer exists anywhere
    console.log(JSON.stringify({ value: sel.value }));
    """
    out = _run_node(js_source, script)
    assert out["value"] == "", "should fall back to the blank All-option, not a stale/wrong topic"


def test_fill_select_options_carry_the_updated_count(js_source):
    script = """
    const sel = new FakeSelect();
    fillSelect(sel, [["Circuits", 5]], "All topics");
    const before = sel.options.find(o => o.value === "Circuits");
    console.log(JSON.stringify({beforeExists: !!before}));
    """
    out = _run_node(js_source, script)
    assert out["beforeExists"]
