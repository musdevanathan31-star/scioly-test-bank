"""
Assessment/club page details: True/False printed as circle-able words on
the offline PDF, collapsible window cards (closed windows start collapsed),
the server-clock badge, and the narrow rotated-header roster whose Save
reads slugs from data-slug.

Run with: `python -m pytest tests/test_dashboard_ui.py -q`
"""
from __future__ import annotations

import importlib
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402


def test_tf_prints_true_and_false_on_the_assessment_pdf(tmp_path):
    import fitz
    import review_app
    snap = [{"number": "1", "qtype": "tf", "text": "Capacitors in series add like resistors in parallel.",
             "max_points": 1, "choices": [], "correct_answer": "True"}]
    for layout in ("none", "key"):
        pdf = review_app._assessment_pdf(snap, "Week 1", "", layout, image_dir=tmp_path)
        text = " ".join("".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf")).split())
        assert "True False (circle one)" in text, layout


def test_tf_markdown_wording():
    import assessments
    entry = {"number": "1", "qtype": "tf", "text": "S", "correct_answer": "True", "max_points": 1}
    assert assessments.TF_PAPER_TEXT in "\n".join(assessments._render_question(entry, 1, include_answers=False))


@pytest.fixture()
def pages(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="dashui-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    previous_event = bqb.current_event()
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("stu1", "password123", "student")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    seasons.create_season("2027", event_slugs=[slug])
    seasons.set_roster("2027", slug, ["stu1"])
    assessments.create_window("2027", "2020-01-01T10:00:00+00:00", "2020-01-01T11:00:00+00:00",
                              [slug], label="Old week")
    assessments.create_window("2027", "2099-01-01T10:00:00+00:00", "2099-01-01T11:00:00+00:00",
                              [slug], label="Future week")

    def get(user, path):
        c = review_app.app.test_client()
        c.post("/login", data={"username": user, "password": "password123"})
        return c.get(path).get_data(as_text=True)

    yield get, slug, review_app
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_window_cards_are_collapsible(pages):
    get, slug, _ = pages
    html = get("coach1", "/assessments")
    cards = re.findall(r'<div class="window-card" data-window-id="[^"]+" data-closes="([^"]+)">', html)
    assert sorted(cards) == ["2020-01-01T11:00:00+00:00", "2099-01-01T11:00:00+00:00"]
    assert html.count('class="ghost win-toggle"') == 2
    assert html.count('<div class="win-body">') == 2
    assert re.search(r'1 test:\s*1 scheduled', html)          # one-line summary
    assert "assessmentWindowCollapsed" in html                 # remembered per browser


def test_server_clock_on_both_assessment_pages(pages):
    get, slug, _ = pages
    for user, path, warn in (("coach1", "/assessments", True), ("stu1", "/my-assessments", False)):
        html = get(user, path)
        m = re.search(r'<span class="server-clock" data-server-now="([^"]+)"( data-warn="1")?></span>', html)
        assert m, path
        assert m.group(1).endswith("+00:00")                   # an absolute UTC instant
        assert bool(m.group(2)) is warn                         # clock-skew warning: coaches only


def test_roster_headers_carry_slugs_and_names(pages):
    get, slug, review_app = pages
    html = get("coach1", "/club?season=2027")
    name = review_app.EVENTS[slug].name
    assert f'<th class="ev" data-slug="{slug}" title="{slug}"><span>{name}</span></th>' in html
    assert 'th[data-slug]' in html and "th.textContent" not in html
