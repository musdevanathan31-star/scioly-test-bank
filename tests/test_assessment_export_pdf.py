"""
Downloading an assessment to administer on paper.

The Test/Key buttons used to produce markdown, which cannot carry an
image — a question asking about a labelled diagram exported as a bare
filename reference and was useless on the printed page. They now produce a
PDF with the figures embedded, falling back to markdown when reportlab
isn't installed rather than erroring about a dependency the coach can't fix.

Run with: `python -m pytest tests/test_assessment_export_pdf.py -q`
"""
from __future__ import annotations

import builtins
import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def exported(monkeypatch):
    """A published assessment with a real figure, an image-less question,
    and one referring to a figure that isn't there."""
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="apdf-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    monkeypatch.delenv("SCHOOL_LOGO", raising=False)   # isolate: any image is the figure
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    import fitz

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    bqb.set_event(slug)
    bqb.EVENT.image_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page().get_pixmap().save(str(bqb.EVENT.image_dir / "fig1.png"))
    doc.close()

    questions = [
        {"number": "1", "text": "Identify the component labelled X.",
         "answer": "Resistor", "qtype": "frq", "choices": [],
         "images": ["fig1.png"], "image_descriptions": {"fig1.png": "circuit"}},
        {"number": "2", "text": "What is R?", "answer": "B", "qtype": "mcq",
         "choices": [{"letter": "A", "text": "radius"},
                     {"letter": "B", "text": "resistance"}], "images": []},
        {"number": "3", "text": "Refers to a figure that is gone.",
         "answer": "x", "qtype": "frq", "choices": [], "images": ["gone.png"]},
    ]
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["s_test.pdf"] = questions
    seasons.create_season("2027", event_slugs=[slug], created_by="coach1")
    window = assessments.create_window("2027", "2020-01-01T09:00",
                                       "2099-01-01T11:00", [slug], label="W1")
    a = assessments.get_assessment_for(window.window_id, slug)
    assessments.update_assessment_kept(
        a.assessment_id,
        [{"bucket": "s_test.pdf", "number": q["number"], "max_points": 1}
         for q in questions])
    assessments.publish_assessment(a.assessment_id, "coach1")

    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    yield c, f"/assessments/{a.assessment_id}/export"

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


@pytest.mark.parametrize("which", ["test", "key"])
def test_the_default_download_is_a_pdf(exported, which):
    c, base = exported
    r = c.get(f"{base}/{which}")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.get_data()[:5] == b"%PDF-"
    assert r.headers["Content-Disposition"].endswith(f"-{which}.pdf")


def test_the_pdf_actually_embeds_the_figure(exported):
    # The whole reason for preferring PDF. With no logo configured, any
    # embedded image is the question's figure.
    c, base = exported
    pdf = c.get(f"{base}/test").get_data()
    assert b"/XObject" in pdf or b"/Image" in pdf


def test_a_missing_figure_does_not_break_the_export(exported):
    # Question 3 points at a file that isn't there. A note in the document
    # beats losing the download.
    c, base = exported
    r = c.get(f"{base}/test")
    assert r.status_code == 200
    assert r.get_data()[:5] == b"%PDF-"


def test_the_explicit_md_url_still_returns_markdown(exported):
    c, base = exported
    r = c.get(f"{base}/test.md")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert r.get_data(as_text=True).startswith("#")


def test_it_falls_back_to_markdown_without_reportlab(exported, monkeypatch):
    c, base = exported
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "reportlab" or name.startswith("reportlab."):
            raise ImportError("simulated: reportlab not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    r = c.get(f"{base}/test")
    assert r.status_code == 200, "a missing optional dep must not fail the download"
    assert r.headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert r.headers["Content-Disposition"].endswith(".md")


def test_the_key_contains_answers_and_the_test_does_not(exported):
    # Same guarantee the markdown export makes, now for the PDF path: the
    # student copy must not leak the answers. Checked on the markdown
    # rendering of the same snapshot, since PDF text is split across glyph
    # operators and not reliably greppable.
    c, base = exported
    student = c.get(f"{base}/test.md").get_data(as_text=True)
    key = c.get(f"{base}/key.md").get_data(as_text=True)
    assert "Resistor" not in student
    assert "Resistor" in key


def test_the_link_the_page_builds_is_not_the_markdown_one(exported):
    import review_app
    from flask import url_for
    # The endpoint carries two rules -- /export/<which> and /export/<which>.md
    # -- and url_for picks one of them. If it ever picks the .md rule, every
    # download silently becomes markdown no matter what is installed, because
    # the handler keys off the path ending.
    with review_app.app.test_request_context("/"):
        for which in ("test", "key"):
            built = url_for("api_export_assessment_markdown",
                            assessment_id="abc123", which=which)
            assert not built.endswith(".md"), built


def test_a_fallback_says_why_in_a_header(exported, monkeypatch):
    c, base = exported
    import review_app
    monkeypatch.setattr(review_app, "_optional_dep_error",
                        lambda mod: f"Could not import {mod!r} in this interpreter")
    r = c.get(f"{base}/test")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/markdown")
    # Silence here is what made a wrong-interpreter install look like a
    # deliberate choice of format.
    assert "reportlab" in r.headers.get("X-Export-Fallback", "")


def test_an_explicit_markdown_request_is_not_flagged_as_a_fallback(exported):
    c, base = exported
    r = c.get(f"{base}/test.md")
    assert r.status_code == 200
    # Asking for markdown and getting it is not a degraded result.
    assert "X-Export-Fallback" not in r.headers


def test_reportlab_is_a_declared_dependency():
    # It was absent from requirements.txt, so no deployment ever installed
    # it and the PDF path was dead on every server that had not been
    # hand-patched.
    reqs = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8")
    assert "reportlab" in reqs


def test_figures_resolve_against_the_assessments_own_event(exported, monkeypatch):
    """The reported bug: PDFs exported with every figure as "[figure not
    found]".

    _select_assessment is deliberately independent of _select_event(), so
    bqb.EVENT during an export request is whatever the context happened to
    carry — not this assessment's event. Resolving figures from it read the
    wrong directory. This forces that mismatch and requires the image to
    still be embedded.
    """
    c, base = exported
    import review_app, build_question_bank as bqb, events

    # Reproduce the real condition: a worker thread that last served some
    # other event still has that event in its context when the export runs.
    right = events.EVENTS[sorted(events.EVENTS)[0]]
    wrong = next(ev for ev in events.EVENTS.values() if ev.slug != right.slug)
    seen = {}
    real_figure_dir = review_app._assessment_pdf

    def spy(*a, **kw):
        bqb.set_event(wrong.slug)          # as a previous request would leave it
        seen["ambient"] = bqb.EVENT.slug
        seen["passed"] = kw.get("image_dir")
        return real_figure_dir(*a, **kw)

    monkeypatch.setattr(review_app, "_assessment_pdf", spy)
    pdf = c.get(f"{base}/test").get_data()
    assert seen["ambient"] == wrong.slug, "the mismatch was not reproduced"
    assert seen["passed"] == right.image_dir, (
        "the export must pass its own event's image dir, not rely on context")
    assert pdf[:5] == b"%PDF-"
    assert b"figure not found" not in pdf
    assert b"/XObject" in pdf or b"/Image" in pdf
