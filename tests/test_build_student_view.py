"""
The student side of a build event.

Every student-facing rule in this app keys off a *submission* — has the
student submitted, is the window open, is the response in_progress. A build
event has no submission and never will: the coach records the result. So
listing, bucketing and the "past" state all have to be derived from the
window dates and the release flag instead, and the places that assumed a
submission are exactly where this goes wrong.

The release gate is treated here as a security property, not a display
detail: a graded-but-unreleased score must be *absent from the response
body*, not merely hidden by the template.

Run with: `python -m pytest tests/test_build_student_view.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Deliberately a decimal: assessment ids are hex, so a plain integer
# sentinel like 37 matches inside an id such as "37f7eecb..." and makes
# the leak assertion below pass or fail for the wrong reason.
SECRET_SCORE = 37.5


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="buildstu-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    events.add_custom_event("rocket", "Rocket", has_build=True)
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("stu1", "password123", "student")
    seasons.create_season("S1", event_slugs=["rocket"], created_by="coach1")
    seasons.set_roster("S1", "rocket", ["stu1"])
    window = assessments.create_window("S1", "2027-01-01T09:00", "2099-01-01T11:00",
                                       ["rocket"], label="BW")
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    assessments.set_assessment_rubric(build.assessment_id, [
        {"kind": "scored", "label": "Distance", "max_points": 40},
        {"kind": "measured", "label": "Mass", "unit": "g"},
    ])
    build = assessments.get_assessment(build.assessment_id)
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    def client(who):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        return c

    yield review_app, assessments, seasons, build, client

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _grade(assessments, build, *, released: bool):
    scored_id = next(l["id"] for l in build.rubric if l["kind"] == "scored")
    measured_id = next(l["id"] for l in build.rubric if l["kind"] == "measured")
    assessments.set_build_grade(build.assessment_id, "stu1",
                                {scored_id: SECRET_SCORE, measured_id: 12.4},
                                graded_by="coach1", comment="clean flight")
    if released:
        assessments.release_grades(build.assessment_id, "coach1", kind="build")
    return scored_id, measured_id


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_a_scheduled_build_event_is_listed_to_the_student(env):
    review_app, assessments, seasons, build, client = env
    r = client("stu1").get("/api/my-assessments")
    assert r.status_code == 200
    entries = r.get_json()["assessments"]
    mine = [e for e in entries if e["assessment_id"] == build.assessment_id]
    assert mine, f"build event not listed while scheduled: {entries}"
    assert mine[0].get("kind") == "build"


def test_the_student_page_renders_without_a_take_button(env):
    review_app, assessments, seasons, build, client = env
    html = client("stu1").get("/my-assessments").get_data(as_text=True)
    assert "Build event" in html, "build row should be labelled as such"
    assert f"/my-assessments/{build.assessment_id}/take" not in html, (
        "a build event must not offer a take link")


def test_a_student_cannot_take_a_build_assessment(env):
    review_app, assessments, seasons, build, client = env
    c = client("stu1")
    assert c.get(f"/my-assessments/{build.assessment_id}/take").status_code == 400
    assert c.get(f"/api/my-assessments/{build.assessment_id}/take").status_code == 400


# ---------------------------------------------------------------------------
# The release gate — a security property, not a display detail
# ---------------------------------------------------------------------------

def test_an_unreleased_score_never_reaches_the_student(env):
    review_app, assessments, seasons, build, client = env
    _grade(assessments, build, released=False)
    c = client("stu1")

    listing = c.get("/api/my-assessments").get_data(as_text=True)
    assert str(SECRET_SCORE) not in listing, "score leaked into the listing payload"

    r = c.get(f"/my-assessments/{build.assessment_id}/results")
    assert r.status_code == 403, "results must be gated until released"
    assert str(SECRET_SCORE) not in r.get_data(as_text=True)


def test_after_release_the_student_sees_the_breakdown(env):
    review_app, assessments, seasons, build, client = env
    _grade(assessments, build, released=True)
    html = client("stu1").get(f"/my-assessments/{build.assessment_id}/results").get_data(as_text=True)
    assert str(SECRET_SCORE) in html, "released score should be visible"
    assert "Distance" in html, "scored line missing"
    assert "Mass" in html, "measured line missing"
    assert "clean flight" in html, "coach comment missing"


def test_measured_lines_are_shown_as_not_part_of_the_score(env):
    """The whole point of the scored/measured split: a student must not read
    a 12.4 g mass as 12.4 points."""
    review_app, assessments, seasons, build, client = env
    _grade(assessments, build, released=True)
    html = client("stu1").get(f"/my-assessments/{build.assessment_id}/results").get_data(as_text=True)
    assert "not part of the score" in html.lower()
    # The total is the scored line alone, never scored + measured.
    assert f"{float(SECRET_SCORE):.1f}" in html


def test_another_student_cannot_read_this_students_result(env):
    review_app, assessments, seasons, build, client = env
    import auth
    auth.create_user("stu2", "password123", "student")
    _grade(assessments, build, released=True)
    r = client("stu2").get(f"/my-assessments/{build.assessment_id}/results")
    assert r.status_code in (403, 404)
    assert str(SECRET_SCORE) not in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Exam behaviour must be untouched
# ---------------------------------------------------------------------------

def test_listing_an_exam_only_season_is_unchanged(env):
    """A season with no build event must produce exactly what it did before:
    the build work must not alter the exam path."""
    review_app, assessments, seasons, build, client = env
    import events as events_mod
    events_mod.add_custom_event("examonly", "Exam Only")
    seasons.create_season("S2", event_slugs=["examonly"], created_by="coach1")
    seasons.set_roster("S2", "examonly", ["stu1"])
    r = client("stu1").get("/api/my-assessments")
    assert r.status_code == 200
    for e in r.get_json()["assessments"]:
        if e["event_slug"] == "examonly":
            assert e.get("kind", "exam") == "exam"
