"""
Worked explanations: the `explanation` field (Markdown + LaTeX), its
sources (bundle import, qgen), the v5→v6 migration out of
validation.rationale, PATCH/annotation editing, the take-page strip, the
markdown exports, and the PDF keys (explanations.py's LaTeX-to-text).

Run with: `python -m pytest tests/test_explanations.py -q`
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402
import explanations  # noqa: E402

SOLUTION = (r"1. Speed is distance over time: $v = \frac{d}{t}$" "\n"
            r"2. Substitute:" "\n"
            r"$$v = \frac{12.6\ \text{m}}{3.00\ \text{s}}$$" "\n"
            r"3. **Result:** $v = 4.20\ \text{m/s}$")


# ---------------------------------------------------------------------------
# LaTeX → text and PDF markup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tex, text", [
    (r"v = \frac{d}{t}", "v = d/t"),
    (r"v = \frac{12.6\ \text{m}}{3.00\ \text{s}}", "v = (12.6 m)/(3.00 s)"),
    (r"E = mc^2", "E = mc²"),
    (r"x_0 + v_0 t", "x₀ + v₀ t"),
    (r"\sqrt{\frac{2h}{g}}", "√(2h/g)"),
    (r"3.0 \times 10^{8}", "3.0 × 10⁸"),
    (r"10^{-3}", "10⁻³"),
    (r"\Delta V = I R_{eq}", "ΔV = I R_eq"),
    (r"\theta \approx 30^\circ", "θ ≈ 30°"),
    (r"\lambda = \frac{h}{p}", "λ = h/p"),
])
def test_latex_to_text(tex, text):
    assert explanations.latex_to_text(tex) == text


def test_pdf_paragraphs_structure_and_escaping():
    out = explanations.to_pdf_paragraphs(SOLUTION + "\n- note <b>raw</b> & more")
    kinds = [k for k, _ in out]
    assert kinds == ["item", "item", "math", "item", "item"]
    assert out[0][1] == "1. Speed is distance over time: <i>v = d/t</i>"
    assert out[2][1] == "<i>v = (12.6 m)/(3.00 s)</i>"
    assert out[3][1].startswith("3. <b>Result:</b>")
    assert "&lt;b&gt;raw&lt;/b&gt; &amp; more" in out[4][1]        # source HTML escaped


def test_clean():
    assert explanations.clean("  a\r\nb  ") == "a\nb"
    assert explanations.clean(None) == ""
    assert len(explanations.clean("x" * 50_000)) == explanations.MAX_LEN


# ---------------------------------------------------------------------------
# Sources and migration
# ---------------------------------------------------------------------------

def test_bundle_import_puts_justification_in_explanation():
    import bundle_import
    q, _ = bundle_import.convert_question(
        {"id": "q1", "qtype": "frq", "text": "How fast?", "answer": "4.20 m/s",
         "justification": SOLUTION}, digest="d", topics=["T"], season="2027",
        source_label="x", image_names={}, classify=lambda t: "T")
    assert q["explanation"] == SOLUTION
    assert q["validation"]["rationale"] == ""


def test_qgen_rationale_becomes_explanation():
    import qgen
    q = qgen.candidate_to_question({"text": "t", "answer": "a", "rationale": "because"}, "1", "x")
    assert q["explanation"] == "because" and q["validation"]["rationale"] == ""


def test_migration_moves_only_import_rationales():
    state = {"_schema_version": 5, "questions": {"b": [
        {"number": "1", "validation": {"status": "correct", "model": "import", "rationale": SOLUTION}},
        {"number": "2", "validation": {"status": "uncertain", "generated": True, "rationale": "llm why"}},
        {"number": "3", "validation": {"status": "correct", "validated_by": "ai", "rationale": "AI verdict"}},
        {"number": "4", "validation": {"status": "correct", "model": "import",
                                       "rationale": "Marked validated on import."}},
        {"number": "5", "explanation": "keep me",
         "validation": {"model": "import", "rationale": "other"}},
    ]}}
    bqb._migrate_state(state)
    q1, q2, q3, q4, q5 = state["questions"]["b"]
    assert q1["explanation"] == SOLUTION and q1["validation"]["rationale"] == ""
    assert q2["explanation"] == "llm why"
    assert "explanation" not in q3 and q3["validation"]["rationale"] == "AI verdict"
    assert "explanation" not in q4
    assert q5["explanation"] == "keep me"
    assert state["_schema_version"] == bqb.STATE_SCHEMA_VERSION == 6


def test_annotation_override_carries_explanation():
    q = {"number": "1", "text": "t", "answer": "a", "choices": [], "images": []}
    [out] = bqb.apply_annotations([q], {"field_overrides": {"1": {"explanation": "step 1"}}})
    assert out["explanation"] == "step 1"


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def test_bank_markdown_has_solution_block():
    lines: list[str] = []
    bqb._render_question_block(lines, {"source": "s", "number": "1", "text": "How fast?",
                                       "answer": "4.20 m/s", "choices": [], "images": [],
                                       "explanation": SOLUTION}, 1)
    body = "\n".join(lines)
    assert "**Solution:**" in body and r"$v = \frac{d}{t}$" in body


def test_assessment_markdown_inline_and_section():
    import assessments
    entry = assessments._snapshot_one_question(
        {"number": "7", "text": "How fast?", "qtype": "frq", "answer": "4.20 m/s", "explanation": SOLUTION},
        "b.pdf", 1.0)
    assert entry["explanation"] == SOLUTION
    inline = assessments.render_questions_markdown([entry], title="T", answers="inline")
    assert "**Solution:**" in inline and "Substitute" in inline
    section = assessments.render_questions_markdown([entry], title="T", answers="section")
    assert "   2. Substitute:" in section                      # indented under its key line
    student = assessments.render_questions_markdown([entry], title="T", answers="none")
    assert "Substitute" not in student


def test_assessment_pdf_key_includes_solution(tmp_path):
    import fitz
    import review_app
    snap = [{"number": "1", "qtype": "frq", "text": "How fast is the cart?", "max_points": 1,
             "choices": [], "correct_answer": "4.20 m/s", "explanation": SOLUTION}]
    key = review_app._assessment_pdf(snap, "Week 1", "", "key", image_dir=tmp_path)
    text = "".join(p.get_text() for p in fitz.open(stream=key, filetype="pdf"))
    assert "v = d/t" in text and "(12.6 m)/(3.00 s)" in text and "Result:" in text
    student = review_app._assessment_pdf(snap, "Week 1", "", "none", image_dir=tmp_path)
    text = "".join(p.get_text() for p in fitz.open(stream=student, filetype="pdf"))
    assert "Substitute" not in text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="expl-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    previous_event = bqb.current_event()
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["t.pdf"] = [
            {"number": "1", "text": "Q", "qtype": "frq", "answer": "a", "choices": [], "images": []}]
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    yield c, slug, c.get_cookie("csrf_token").value
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_patch_sets_and_clears_explanation(client):
    c, slug, tok = client
    r = c.patch(f"/event/{slug}/api/q/t.pdf/1", json={"explanation": "  1. step\r\n2. step  "},
                headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    bqb.set_event(slug)
    assert bqb._load_state()["questions"]["t.pdf"][0]["explanation"] == "1. step\n2. step"
    c.patch(f"/event/{slug}/api/q/t.pdf/1", json={"explanation": ""}, headers={"X-CSRF-Token": tok})
    assert "explanation" not in bqb._load_state()["questions"]["t.pdf"][0]


def test_take_page_strips_explanation():
    src = Path(__file__).resolve().parent.parent.joinpath("review_app.py").read_text(encoding="utf-8")
    import re
    m = re.search(r'if k not in \(([^)]*)\)\}', src)
    assert m and '"explanation"' in m.group(1)


# ---------------------------------------------------------------------------
# Browser renderer (common_ui.py renderExplanationHTML)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node not installed")
def test_js_renderer():
    import common_ui
    src = common_ui.COMMON_JS
    fn = src[src.index("window.renderExplanationHTML = function(text){"):src.index("window.renderMathSafe")]
    text = SOLUTION + "\n\n- *a* `b` <script>x</script>\nplain $a*b*c$"
    js = "const window = {};\n" + fn + f"\nconsole.log(window.renderExplanationHTML({json.dumps(text)}));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout.strip()
    assert out.startswith('<ol start="1"><li>Speed is distance over time: $v = \\frac{d}{t}$</li>')
    # the display equation stays inside step 2, with both $$ intact
    assert "<li>Substitute:<div>$$v = \\frac{12.6\\ \\text{m}}{3.00\\ \\text{s}}$$</div></li>" in out
    assert "<b>Result:</b>" in out
    assert "<ul><li><i>a</i> <code>b</code> &lt;script&gt;x&lt;/script&gt;</li></ul>" in out
    assert "<p>plain $a*b*c$</p>" in out                         # markdown never touches math


def test_backfill_fills_old_snapshots_once(client):
    """Tests published before explanations existed get them from the bank,
    once, without touching anything else in the frozen snapshot."""
    import assessments, seasons, dataclasses
    c, slug, tok = client
    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st["questions"]["t.pdf"][0]["explanation"] = "1. step"
    seasons.create_season("s", event_slugs=[slug])
    w = assessments.create_window("s", "2030-01-01T10:00:00+00:00", "2030-01-01T11:00:00+00:00", [slug])
    t = assessments.get_assessment_for(w.window_id, slug)
    old_entry = {"number": "1", "qtype": "frq", "text": "Q", "correct_answer": "a", "max_points": 1,
                 "source_question_ref": {"bucket": "t.pdf", "number": "1"}}      # pre-feature shape
    with assessments._assessments_transaction() as store:
        store[t.assessment_id] = dataclasses.replace(store[t.assessment_id], snapshot=[old_entry],
                                                     status="live")
    assert assessments.backfill_snapshot_explanations() == 1
    snap = assessments.load_assessments()[t.assessment_id].snapshot
    assert snap[0]["explanation"] == "1. step" and snap[0]["correct_answer"] == "a"
    assert assessments.backfill_snapshot_explanations() == 0
