"""
The PDF preview endpoints' contract with the browser.

These broke in the Test -> Assessment rename, which rewrote three string
literals that were never the renamed concept: the `?target=test` query
value, the "test" captured out of a `<...>_test.pdf` filename on disk, and
the `{"test": n}` key event_index.html reads as `counts.test`. All three
name an *external* Sci-Oly test PDF as opposed to its answer key.

Nothing caught it. The routes still existed, the templates still rendered,
the suite still passed, and the only symptom was every PDF page preview
404ing, which looked exactly like the extracted images having been lost.

Run with: `python -m pytest tests/test_pdf_wire_contract.py -q`
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def pdf_client(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="pdfwire-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    import fitz

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    bqb.set_event(slug)
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    pdfname = "circuitlab_2019_c_demo_test.pdf"
    bqb.EVENT.base_dir.mkdir(parents=True, exist_ok=True)
    doc.save(str(bqb.EVENT.base_dir / pdfname))
    doc.close()

    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    yield c, f"/event/{slug}/api/pdf/{pdfname}", review_app, bqb

    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_page_render_accepts_the_target_value_the_browser_sends(pdf_client):
    # templates/event_index.html hardcodes {target: "test"}; the server must
    # accept that exact string.
    c, base, _app, _bqb = pdf_client
    r = c.get(f"{base}/page/2.png?dpi=120&target=test")
    assert r.status_code == 200, r.get_data()[:200]
    assert r.headers["Content-Type"].startswith("image/png")


def test_page_render_defaults_to_the_test_pdf_when_target_is_absent(pdf_client):
    c, base, _app, _bqb = pdf_client
    assert c.get(f"{base}/page/1.png").status_code == 200


def test_page_counts_uses_the_key_names_the_frontend_reads(pdf_client):
    # event_index.html: PD_PAGE_COUNTS = {test: counts.test, key: counts.key}
    c, base, _app, _bqb = pdf_client
    payload = c.get(f"{base}/page-counts").get_json()
    assert payload["test"] == 2
    assert "key" in payload


def test_a_filename_ending_in_test_pdf_is_classified_as_a_test(pdf_client):
    # _FILENAME_ROLE_RE captures "test"/"key" from the name on disk, so the
    # comparison has to be against those literal tokens.
    _c, _base, review_app, bqb = pdf_client
    _explained, test_files = review_app._explained_filenames(bqb.EVENT.base_dir)
    assert [p.name for p in test_files] == ["circuitlab_2019_c_demo_test.pdf"]


@pytest.mark.parametrize("query,expected", [
    ("?target=bogus", 404),      # not a supplementary doc for this test
    ("?target=key", 404),        # no key PDF exists here
])
def test_unknown_targets_still_404(pdf_client, query, expected):
    c, base, _app, _bqb = pdf_client
    assert c.get(f"{base}/page/1.png{query}").status_code == expected


def test_out_of_range_page_404s(pdf_client):
    c, base, _app, _bqb = pdf_client
    assert c.get(f"{base}/page/9.png?target=test").status_code == 404
