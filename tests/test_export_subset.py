"""
Coverage for subset export from the Browse page.

CSV/JSON/markdown subsets are built client-side, but PDF and Anki can only
be rendered server-side, so exporting a filtered or selected set means
POSTing the chosen questions. What matters here: the right questions reach
the renderer, in the order the page sent them (that order is the coach's
current sort, and a printout that ignores it is a different document from
the one on screen), and a hostile or stale payload can't do damage.

Run with: `python -m pytest tests/test_export_subset.py -q`
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(monkeypatch):
    """Logged-in coach against a throwaway DATA_ROOT, with _export_pdf
    replaced by a spy — the assertions are about which questions reach the
    renderer, not about reportlab's output bytes."""
    import importlib
    import build_question_bank as bqb
    # tests/test_heuristics.py binds circuit_lab at import time; reloading
    # bqb/events here would otherwise leave the active-event ContextVar
    # pointing into this test's temp DATA_ROOT for the rest of the session.
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="exportsub-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    from flask import Response

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["src_test.pdf"] = [
            {"number": str(i), "text": f"Q{i}", "answer": "A", "qtype": "mcq",
             "choices": [{"letter": "A", "text": "yes"}], "images": []}
            for i in range(1, 6)
        ]

    seen = {}

    def spy(all_qs, ctx=None, filename_stem="", layout="key"):
        seen["numbers"] = [q["number"] for q in all_qs]
        seen["stem"] = filename_stem
        seen["layout"] = layout
        return Response(b"%PDF-fake", mimetype="application/pdf",
                        headers={"Content-Disposition":
                                 f"attachment; filename={filename_stem}.pdf"})

    monkeypatch.setattr(review_app, "_export_pdf", spy)
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    headers = {"X-CSRF-Token": c.get_cookie("csrf_token").value}
    yield c, headers, slug, seen

    monkeypatch.undo()
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _keys(*numbers):
    return [{"bucket": "src_test.pdf", "number": str(n)} for n in numbers]


def test_get_still_exports_the_whole_bank(client):
    c, _h, slug, seen = client
    assert c.get(f"/event/{slug}/api/export.pdf").status_code == 200
    assert seen["numbers"] == ["1", "2", "3", "4", "5"]


def test_post_exports_only_the_questions_named(client):
    c, h, slug, seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
               json={"keys": _keys(2, 4), "label": "selected"})
    assert r.status_code == 200
    assert seen["numbers"] == ["2", "4"]
    assert "selected" in r.headers["Content-Disposition"]


def test_the_order_sent_is_the_order_rendered(client):
    # The page sends its current sort order; a printed paper that silently
    # reorders is not the document the coach was looking at.
    c, h, slug, seen = client
    c.post(f"/event/{slug}/api/export.pdf", headers=h,
           json={"keys": _keys(5, 1, 3), "label": "filtered"})
    assert seen["numbers"] == ["5", "1", "3"]


def test_a_stale_key_is_skipped_rather_than_failing_the_export(client):
    # The page's copy of the bank can lag a delete by another coach. Losing
    # one question from a printout beats losing the printout.
    c, h, slug, seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
               json={"keys": _keys(2, 99, 4), "label": "sel"})
    assert r.status_code == 200
    assert seen["numbers"] == ["2", "4"]


def test_empty_and_fully_stale_payloads_are_refused(client):
    c, h, slug, _seen = client
    assert c.post(f"/event/{slug}/api/export.pdf", headers=h,
                  json={"keys": [], "label": "x"}).status_code == 400
    assert c.post(f"/event/{slug}/api/export.pdf", headers=h,
                  json={"keys": _keys(99), "label": "x"}).status_code == 400
    assert c.post(f"/event/{slug}/api/export.pdf", headers=h,
                  json={"label": "x"}).status_code == 400


def test_label_cannot_escape_the_filename(client):
    # The label goes straight into a Content-Disposition header.
    c, h, slug, _seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
               json={"keys": _keys(1), "label": "../../etc/passwd"})
    disposition = r.headers["Content-Disposition"]
    assert "../" not in disposition
    assert "etc_passwd" in disposition


def test_subset_export_still_requires_csrf(client):
    # It's a POST, so it goes through _check_csrf like every other mutating
    # request; without the header it must not run.
    c, _h, slug, _seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", json={"keys": _keys(1)})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Answer layout (shared vocabulary with the markdown export)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["none", "inline", "key"])
def test_each_layout_reaches_the_renderer(client, layout):
    c, h, slug, seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
               json={"keys": _keys(1), "label": "sel", "layout": layout})
    assert r.status_code == 200
    assert seen["layout"] == layout


def test_layout_defaults_to_key_when_unspecified(client):
    c, h, slug, seen = client
    c.post(f"/event/{slug}/api/export.pdf", headers=h,
           json={"keys": _keys(1), "label": "sel"})
    assert seen["layout"] == "key"


def test_an_unknown_layout_is_refused_rather_than_defaulted(client):
    # Quietly falling back to "key" would hand back a copy showing every
    # answer to someone who asked for a clean question sheet.
    c, h, slug, _seen = client
    r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
               json={"keys": _keys(1), "layout": "answers-please"})
    assert r.status_code == 400
    assert "unknown layout" in r.get_json()["error"]


def test_layout_is_accepted_from_the_query_string_for_whole_bank_export(client):
    c, _h, slug, seen = client
    assert c.get(f"/event/{slug}/api/export.pdf?layout=none").status_code == 200
    assert seen["layout"] == "none"
    assert c.get(f"/event/{slug}/api/export.pdf?layout=nope").status_code == 400


def test_filenames_distinguish_the_layouts(client):
    # Three layouts landing on one filename is how the copy with the
    # answers gets handed out by mistake.
    c, h, slug, _seen = client
    names = {}
    for layout in ("none", "inline", "key"):
        r = c.post(f"/event/{slug}/api/export.pdf", headers=h,
                   json={"keys": _keys(1), "label": "sel", "layout": layout})
        names[layout] = r.headers["Content-Disposition"]
    assert len(set(names.values())) == 3, names
    assert "questions" in names["none"]
    assert "answers" in names["inline"]
