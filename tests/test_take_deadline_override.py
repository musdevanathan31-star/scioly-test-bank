"""The deadline the take page counts down to must be the student's own.

`assessment_take.html`'s timer calls `submitTest(true)` the moment it
reaches zero. So the `closes_at` this endpoint returns is not a display
detail -- it decides when a student's test is taken away from them. If it
carries the class-wide window while the server is honouring a personal
override, the browser force-submits mid-answer at the old deadline and the
extension the coach granted is silently undone.

Everything else that resolves a deadline goes through
`assessments.effective_window()`; these tests pin that this endpoint does
too, in both directions (override present and absent).

Run with: `python -m pytest tests/test_take_deadline_override.py -q`
"""
from __future__ import annotations

import dataclasses
import importlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SEASON = "2099-2100"


def _iso(dt):
    return dt.isoformat()


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="deadline-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    events.add_custom_event("alpha", "Alpha")
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("stu_a", "password123", "student")
    seasons.create_season(SEASON, event_slugs=["alpha"], created_by="coach1")
    seasons.set_roster(SEASON, "alpha", ["stu_a"])
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    now = datetime.now(timezone.utc)
    opens = now - timedelta(hours=1)
    closes = now + timedelta(minutes=30)          # class-wide
    extended = now + timedelta(hours=6)            # this student's override

    snap = [{"number": "1", "qtype": "mcq", "text": "Q1", "max_points": 1,
             "choices": [{"letter": "A", "text": "a"}, {"letter": "B", "text": "b"}],
             "correct_answer": "A"}]
    w = assessments.create_window(SEASON, _iso(opens), _iso(closes), ["alpha"],
                                  label="W1", created_by="coach1")
    t = assessments.get_assessment_for(w.window_id, "alpha")
    with assessments._assessments_transaction() as store:
        store[t.assessment_id] = dataclasses.replace(
            store[t.assessment_id], snapshot=snap, status="live")

    def client(who):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        return c

    yield (review_app, assessments, t.assessment_id, client,
           _iso(closes), _iso(extended), _iso(opens))

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _grant_override(assessments, assessment_id, username, opens_at, closes_at):
    with assessments._assessments_transaction() as store:
        t = store[assessment_id]
        overrides = dict(t.overrides)
        overrides[username] = {"opens_at": opens_at, "closes_at": closes_at,
                               "granted_by": "coach1", "granted_at": opens_at,
                               "reason": "extended"}
        store[assessment_id] = dataclasses.replace(t, overrides=overrides)


def test_without_an_override_the_class_window_is_sent(env):
    review_app, assessments, aid, client, class_closes, _extended, _opens = env
    j = client("stu_a").get(f"/api/my-assessments/{aid}/take").get_json()
    assert j["closes_at"] == class_closes


def test_an_extended_student_gets_their_own_deadline(env):
    """The regression this file exists for: the countdown auto-submits at
    whatever time it is handed, so this must be the override."""
    review_app, assessments, aid, client, class_closes, extended, opens = env
    _grant_override(assessments, aid, "stu_a", opens, extended)

    j = client("stu_a").get(f"/api/my-assessments/{aid}/take").get_json()
    assert j is not None, "take request was refused; the override window is not open"
    assert j["closes_at"] == extended, (
        "take payload carried the class deadline; the browser would have "
        "force-submitted this student at the original time")
    assert j["closes_at"] != class_closes


def test_the_endpoint_agrees_with_effective_window(env):
    """Rather than re-deriving the rule, pin that this endpoint returns
    exactly what every other deadline consumer would compute."""
    review_app, assessments, aid, client, class_closes, extended, opens = env
    _grant_override(assessments, aid, "stu_a", opens, extended)

    test = assessments.get_assessment(aid)
    window = assessments.get_window(test.window_id)
    _, expected = assessments.effective_window(test, window, "stu_a")

    j = client("stu_a").get(f"/api/my-assessments/{aid}/take").get_json()
    assert j["closes_at"] == expected


def test_an_override_for_someone_else_does_not_affect_this_student(env):
    review_app, assessments, aid, client, class_closes, extended, opens = env
    import auth
    auth.create_user("stu_b", "password123", "student")
    _grant_override(assessments, aid, "stu_b", opens, extended)

    j = client("stu_a").get(f"/api/my-assessments/{aid}/take").get_json()
    assert j["closes_at"] == class_closes, "another student's override leaked"
