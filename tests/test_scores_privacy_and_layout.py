"""The Scores page: student isolation, and the season-scale aggregates.

Two things are pinned here.

1. PRIVACY. A student sees only their own scores. This is treated as a
   security property, not a display detail -- the assertions check that
   another student's name and score are absent from the RESPONSE BODY,
   not merely unrendered, and that a student cannot widen their view by
   passing `student=` or `view=matrix` by hand. (Same standard as
   test_build_student_view.py's release gate.)

2. SCALE. A season is ~26 weekly windows, and a column is per
   (window x event), so two events a week is ~52 columns. The aggregates
   that make that survivable have edge cases worth pinning: "expected"
   must count only the events a student is actually rostered in, the
   per-assessment average must be computed over everyone rather than the
   filtered rows, and None (never attempted) must stay distinguishable
   from 0.0 (attempted, scored nothing).

Run with: `python -m pytest tests/test_scores_privacy_and_layout.py -q`
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SEASON = "2099-2100"


def _snapshot(n_questions: int, points: float = 1.0):
    return [{"number": str(i), "qtype": "mcq", "text": f"Q{i}",
             "max_points": points, "choices": [{"letter": "A", "text": "a"},
                                               {"letter": "B", "text": "b"}],
             "correct_answer": "A"}
            for i in range(1, n_questions + 1)]


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="scores-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    events.add_custom_event("alpha", "Alpha")
    events.add_custom_event("beta", "Beta")
    auth.create_user("coach1", "password123", "coach")
    for u in ("stu_a", "stu_b", "stu_c"):
        auth.create_user(u, "password123", "student")
    seasons.create_season(SEASON, event_slugs=["alpha", "beta"], created_by="coach1")
    # stu_c is rostered ONLY in beta -- the "expected" edge case.
    seasons.set_roster(SEASON, "alpha", ["stu_a", "stu_b"])
    seasons.set_roster(SEASON, "beta", ["stu_a", "stu_b", "stu_c"])
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    def client(who):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        return c

    yield review_app, assessments, seasons, client

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _graded_window(assessments, *, label, opens, slugs, scores, n_q=10):
    """Create a window and write a released, graded response per entry in
    `scores` ({username: n_correct}). Returns {slug: assessment_id}."""
    snap = _snapshot(n_q)
    w = assessments.create_window(SEASON, opens, opens.replace("T09", "T10"),
                                  slugs, label=label, created_by="coach1")
    out = {}
    for slug in slugs:
        t = assessments.get_assessment_for(w.window_id, slug)
        with assessments._assessments_transaction() as store:
            store[t.assessment_id] = dataclasses.replace(
                store[t.assessment_id], snapshot=snap, status="released")
        for username, n_correct in (scores.get(slug) or {}).items():
            auto = {q["number"]: {"points_earned": 1 if i < n_correct else 0,
                                  "points_possible": 1}
                    for i, q in enumerate(snap)}
            r = assessments.Response(
                student_username=username, assessment_id=t.assessment_id,
                question_order=list(range(len(snap))), answers={},
                auto_grade=auto, manual_grade={}, status="submitted",
                started_at=opens, last_saved_at=opens, submitted_at=opens,
                released=True, released_at=opens, released_by="coach1")
            p = assessments._response_path(t.assessment_id, username)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(assessments._response_to_dict(r), indent=2),
                         encoding="utf-8")
        out[slug] = t.assessment_id
    return out


def _row_tag(body: str, username: str) -> str:
    """The full opening <tr ...> tag for a student's summary row. The
    attributes are wrapped across lines, so a line-wise search finds only
    the first of them."""
    m = re.search(r'<tr data-name="' + re.escape(username) + r'"[^>]*>', body)
    assert m, f"no summary row for {username}"
    return m.group(0)


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def test_a_student_sees_only_their_own_scores(env):
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 9, "stu_b": 3}})

    body = client("stu_a").get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert "stu_a" in body, "a student must still see their own row"
    assert "stu_b" not in body, "another student's name leaked into the page"


def test_another_students_score_is_absent_from_the_body_not_just_hidden(env):
    """stu_b scored 3/10; that number must not be anywhere in what stu_a
    is served -- filtering happens while building the grid, not in CSS."""
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 9, "stu_b": 3}})

    body = client("stu_a").get(f"/scores?season={SEASON}").get_data(as_text=True)
    # stu_a's own 9/10 should be present; stu_b's 3/10 must not be.
    assert "stu_b" not in body
    assert "3 / 10" not in body and "3/10" not in body


def test_a_student_cannot_widen_the_view_with_query_parameters(env):
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 9, "stu_b": 3}})
    c = client("stu_a")

    forced = c.get(f"/scores?season={SEASON}&view=matrix&student=stu_b")
    assert forced.status_code == 200
    body = forced.get_data(as_text=True)
    assert "stu_b" not in body, "student= let a student pivot to someone else"

    plain = c.get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert body == plain, "view/student params must be inert for a student"


def test_a_student_does_not_see_events_they_are_not_rostered_in(env):
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha", "beta"],
                   scores={"alpha": {"stu_a": 9}, "beta": {"stu_c": 5}})
    body = client("stu_c").get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert "beta" in body, "stu_c is rostered in beta and should see it"
    assert "alpha" not in body, "stu_c is not rostered in alpha"


def test_the_coach_still_sees_every_student(env):
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 9, "stu_b": 3}})
    body = client("coach1").get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert "stu_a" in body and "stu_b" in body


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

def test_expected_counts_only_the_events_a_student_is_rostered_in(env):
    """stu_c is in beta only. Given a window covering both events, stu_c
    must be 'expected' for one assessment, not two -- otherwise every
    student shows as missing every other event's tests."""
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha", "beta"],
                   scores={"alpha": {"stu_a": 8, "stu_b": 8}, "beta": {}})

    body = client("coach1").get(f"/scores?season={SEASON}").get_data(as_text=True)
    # The <tr> attributes are spread over several lines, so match the tag.
    row_c = _row_tag(body, "stu_c")
    assert 'data-taken="0"' in row_c, row_c
    # Expected is 1 (beta only), so exactly 1 missing -- not 2.
    assert 'data-missing="1"' in row_c, row_c

    # The fully-rostered student is expected for both events: took alpha,
    # missed beta.
    row_a = _row_tag(body, "stu_a")
    assert 'data-taken="1"' in row_a and 'data-missing="1"' in row_a, row_a


def test_per_assessment_average_is_over_everyone_not_the_filtered_row(env):
    """Narrowing to one student must not turn the class average into that
    student's own score."""
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 10, "stu_b": 0}})
    c = client("coach1")

    body = c.get(f"/scores?season={SEASON}&view=matrix&student=stu_a").get_data(as_text=True)
    # Class average across 10/10 and 0/10 is 50%, not stu_a's 100%.
    assert "50%" in body, "class average collapsed to the filtered student"


def test_never_attempted_is_distinct_from_scored_zero(env):
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_b": 0}})
    body = client("coach1").get(f"/scores?season={SEASON}&view=matrix").get_data(as_text=True)
    # stu_b attempted and scored 0; stu_a never attempted and gets a dash.
    assert "&#8212;" in body or "—" in body, "no-attempt marker missing"


def test_columns_are_in_chronological_order(env):
    """The trend sparkline is meaningless if columns aren't time-ordered,
    and window insertion order is not a guarantee."""
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="LATER", opens="2099-12-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 5}})
    _graded_window(assessments, label="EARLIER", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 5}})
    body = client("coach1").get(f"/scores?season={SEASON}&view=matrix").get_data(as_text=True)
    assert body.index("EARLIER") < body.index("LATER"), "columns not chronological"


def test_the_matrix_header_carries_the_max_and_cells_do_not(env):
    """The denominator is stated once per column instead of in every one
    of ~2000 cells."""
    review_app, assessments, seasons, client = env
    _graded_window(assessments, label="W1", opens="2099-10-01T09:00",
                   slugs=["alpha"], scores={"alpha": {"stu_a": 7}}, n_q=25)
    body = client("coach1").get(f"/scores?season={SEASON}&view=matrix").get_data(as_text=True)
    assert 'class="max">/ 25' in body, "column header should state the max once"
    assert "7 / 25" not in body, "cells should carry the raw score alone"


def test_sparkline_points_use_a_fixed_scale(env):
    """Self-scaling would make 61->63% look like 20->95% in a column being
    compared across 40 students."""
    review_app, assessments, seasons, client = env
    pts = review_app._sparkline_points([0.0, 100.0], width=84.0, height=18.0)
    assert pts == "0.0,18.0 84.0,0.0"
    assert review_app._sparkline_points([50.0]) == "", "one reading is not a trend"
    assert review_app._sparkline_points([]) == ""
    # A flat 60% line sits at the same height regardless of the other values.
    flat = review_app._sparkline_points([60.0, 60.0])
    assert flat.split()[0].split(",")[1] == flat.split()[1].split(",")[1]


def test_score_bands(env):
    review_app, assessments, seasons, client = env
    assert review_app._score_band(None) == ""
    assert review_app._score_band(95) == "band-ok"
    assert review_app._score_band(80) == "band-ok"
    assert review_app._score_band(79.9) == "band-warn"
    assert review_app._score_band(60) == "band-warn"
    assert review_app._score_band(59.9) == "band-bad"
    assert review_app._score_band(0) == "band-bad"


def test_pct_keeps_none_distinct_from_zero(env):
    review_app, assessments, seasons, client = env
    assert review_app._pct(0, 0) is None
    assert review_app._pct(0, 10) == 0.0
