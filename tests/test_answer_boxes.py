"""
Answer-key bounding boxes: the on-the-fly /answer-boxes endpoint that draws a
box on the Key PDF for every answer line, so a coach can see where each
extracted answer came from -- the same job api_question_bboxes already does
for questions, mirrored onto the separate answer-key document.

Uses synthetic fitz-built PDFs (event PDFs are gitignored and absent from a
fresh clone) -- same approach as tests/test_page_js_syntax.py.

Run with: `python -m pytest tests/test_answer_boxes.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    root = tempfile.mkdtemp(prefix="ansbox-")
    monkeypatch.setenv("DATA_ROOT", root)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    bqb.set_event(slug)
    bqb.EVENT.base_dir.mkdir(parents=True, exist_ok=True)

    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})

    yield c, review_app, bqb, slug

    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _make_key_pdf(bqb, pdfname: str, pages: list[list[str]]):
    """Write a synthetic *_key.pdf sibling of `pdfname` with one page per
    entry in `pages`, each entry a list of lines placed top-to-bottom."""
    import fitz
    key_name = pdfname.replace("_test.pdf", "_key.pdf")
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line)
            y += 20
    doc.save(str(bqb.EVENT.base_dir / key_name))
    doc.close()
    return key_name


def _url(slug, pdfname, pno):
    return f"/event/{slug}/api/pdf/{pdfname}/page/{pno}/answer-boxes"


def test_answer_lines_produce_boxes_with_numbers_and_plausible_rects(env):
    c, _app, bqb, slug = env
    pdfname = "demo_test.pdf"
    # "Answer Key" trips _is_key_page's phrase check so this page qualifies
    # without needing 5+ short-answer lines.
    _make_key_pdf(bqb, pdfname, [["Answer Key", "1. A", "2. B"]])

    r = c.get(_url(slug, pdfname, 1))
    assert r.status_code == 200
    data = r.get_json()
    assert data["page_height_pt"] > 0
    nums = {b["number"]: b for b in data["boxes"]}
    assert set(nums) == {"1", "2"}
    assert nums["1"]["answer"] == "A"
    assert nums["2"]["answer"] == "B"
    for b in data["boxes"]:
        assert b["x1"] > b["x0"]
        assert b["y1"] > b["y0"]
        assert b["x0"] >= 0 and b["y0"] >= 0


def test_a_non_answer_line_produces_no_box(env):
    c, _app, bqb, slug = env
    pdfname = "noise_test.pdf"
    _make_key_pdf(bqb, pdfname, [
        ["Answer Key", "1. A", "Section II", "Page 3 of 7", "2. B"],
    ])

    r = c.get(_url(slug, pdfname, 1))
    assert r.status_code == 200
    nums = {b["number"] for b in r.get_json()["boxes"]}
    assert nums == {"1", "2"}


def test_applied_is_false_only_when_the_base_is_shared(env):
    c, app, bqb, slug = env
    pdfname = "ambig_test.pdf"
    _make_key_pdf(bqb, pdfname, [["Answer Key", "12. B", "5. A"]])

    with bqb._state_transaction() as st:
        st.setdefault("questions", {})[pdfname] = [
            {"number": "12"}, {"number": "12b"}, {"number": "5"},
        ]

    r = c.get(_url(slug, pdfname, 1))
    assert r.status_code == 200
    by_num = {b["number"]: b for b in r.get_json()["boxes"]}
    assert by_num["12"]["applied"] is False, "12 shares a base with 12b -- ambiguous"
    assert by_num["5"]["applied"] is True, "5's base is unique -- unambiguous"


def test_applied_true_when_no_other_question_shares_the_base(env):
    c, app, bqb, slug = env
    pdfname = "clean_test.pdf"
    _make_key_pdf(bqb, pdfname, [["Answer Key", "1. A", "2. B", "3. C"]])

    with bqb._state_transaction() as st:
        st.setdefault("questions", {})[pdfname] = [
            {"number": "1"}, {"number": "2"}, {"number": "3"},
        ]

    r = c.get(_url(slug, pdfname, 1))
    assert r.status_code == 200
    boxes = r.get_json()["boxes"]
    assert boxes and all(b["applied"] for b in boxes)


def test_a_pdf_with_no_key_404s_rather_than_500ing(env):
    c, _app, bqb, slug = env
    pdfname = "nokey_test.pdf"
    # No *_key.pdf sibling written at all.
    r = c.get(_url(slug, pdfname, 1))
    assert r.status_code == 404


def test_boxes_come_only_from_pages_is_key_page_accepts(env):
    c, _app, bqb, slug = env
    pdfname = "mixedpages_test.pdf"
    # Page 1 qualifies (phrase match). Page 2 has a numbered line but no key
    # phrase and too few short-answer lines to pass the structural check --
    # _is_key_page rejects it, so no boxes should come from it even though
    # ANS_LINE itself would match "1. C".
    _make_key_pdf(bqb, pdfname, [
        ["Answer Key", "1. A"],
        ["1. C"],
    ])
    import build_question_bank as bqb_mod
    key_path = bqb.EVENT.base_dir / pdfname.replace("_test.pdf", "_key.pdf")
    import fitz
    doc = fitz.open(str(key_path))
    assert not bqb_mod._is_key_page(doc[1].get_text("text"))
    doc.close()

    r1 = c.get(_url(slug, pdfname, 1))
    assert {b["number"] for b in r1.get_json()["boxes"]} == {"1"}

    r2 = c.get(_url(slug, pdfname, 2))
    assert r2.get_json()["boxes"] == []
