"""
Roster withdrawals: a student who has SUBMITTED a test (or has a recorded
build score) in an event can't simply be removed from its roster --
unticking withdraws them. Withdrawn students keep their results from
windows that opened before they left (visible to them, on Scores, on build
grading), get no later tests, aren't counted missing, and don't block
grading. Ticking them again reinstates them.

Run with: `python -m pytest tests/test_roster_withdrawal.py -q`
"""
from __future__ import annotations

import dataclasses
import importlib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402

SEASON = "2027"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="withdraw-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    previous_event = bqb.current_event()
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    events.add_custom_event("alpha", "Alpha")
    for u in ("ann", "bob", "cal"):
        auth.create_user(u, "password123", "student")
    auth.create_user("coach1", "password123", "coach")
    seasons.create_season(SEASON, event_slugs=["alpha"])
    seasons.set_roster(SEASON, "alpha", ["ann", "bob", "cal"])

    now = datetime.now(timezone.utc)
    w1 = assessments.create_window(SEASON, _iso(now - timedelta(days=2)), _iso(now - timedelta(days=1)),
                                   ["alpha"], label="Week 1")
    t1 = assessments.get_assessment_for(w1.window_id, "alpha")
    snap = [{"number": "1", "qtype": "tf", "text": "S", "choices": [], "correct_answer": "True",
             "max_points": 1, "source_question_ref": {"bucket": "b", "number": "1"}}]
    with assessments._assessments_transaction() as store:
        store[t1.assessment_id] = dataclasses.replace(store[t1.assessment_id], snapshot=snap,
                                                      status="released")
    # ann submitted week 1; bob only started it; cal never opened it.
    for who in ("ann", "bob"):
        assessments.start_or_get_response(t1.assessment_id, who, snap)
    assessments.save_answer(t1.assessment_id, "ann", "1", {"qtype": "tf", "picked": "True"})
    assessments.submit_response(t1.assessment_id, "ann", snap)
    assessments.release_grades(t1.assessment_id, snap, released_by="coach1")

    def client(user):
        c = review_app.app.test_client()
        c.post("/login", data={"username": user, "password": "password123"})
        return c, c.get_cookie("csrf_token").value

    yield {"seasons": seasons, "assessments": assessments, "t1": t1, "w1": w1, "snap": snap,
           "client": client, "now": now, "review_app": review_app}
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_only_submitted_results_count(env):
    a = env["assessments"]
    assert a.students_with_results(SEASON, "alpha") == {("ann", "alpha")}   # bob only started


def test_unticking_withdraws_only_students_with_results(env):
    s, a = env["seasons"], env["assessments"]
    out = a.update_roster(SEASON, "alpha", [], by="coach1")
    assert out == {"removed": ["bob", "cal"], "withdrawn": ["ann"], "reinstated": []}
    assert s.get_roster(SEASON, "alpha") == []
    rec = s.get_withdrawals(SEASON, "alpha")["ann"]
    assert rec["by"] == "coach1" and rec["at"].endswith("+00:00")
    assert s.student_withdrawals(SEASON, "ann") == {"alpha": rec}
    # Saving again without her keeps her withdrawn (not erased).
    a.update_roster(SEASON, "alpha", [], by="coach1")
    assert "ann" in s.get_withdrawals(SEASON, "alpha")
    # Ticking her again reinstates her.
    out = a.update_roster(SEASON, "alpha", ["ann"], by="coach1")
    assert out["reinstated"] == ["ann"] and s.get_withdrawals(SEASON, "alpha") == {}
    assert s.get_roster(SEASON, "alpha") == ["ann"]


def test_build_score_counts_as_a_result(env):
    s, a = env["seasons"], env["assessments"]
    w = a.create_window(SEASON, _iso(env["now"]), _iso(env["now"] + timedelta(hours=1)), ["alpha"])
    t = a.get_assessment_for(w.window_id, "alpha")
    with a._assessments_transaction() as store:
        store[t.assessment_id] = dataclasses.replace(store[t.assessment_id], kind="build",
                                                     rubric=[{"id": "s", "kind": "scored", "label": "S",
                                                              "max_points": 10}])
    a.set_build_grade(t.assessment_id, "cal", rubric_values={"s": 7}, graded_by="coach1")
    assert ("cal", "alpha") in a.students_with_results(SEASON, "alpha")


def test_other_roster_writes_end_a_withdrawal(env):
    s, a = env["seasons"], env["assessments"]
    a.update_roster(SEASON, "alpha", [], by="coach1")
    s.add_to_roster(SEASON, "alpha", ["ann"])                 # e.g. CSV import
    assert s.get_withdrawals(SEASON, "alpha") == {} and "ann" in s.get_roster(SEASON, "alpha")


def test_account_and_season_deletion_clear_withdrawals(env):
    s, a = env["seasons"], env["assessments"]
    a.update_roster(SEASON, "alpha", [], by="coach1")
    assert s.remove_user_from_all_rosters("ann") == 1
    assert s.get_withdrawals(SEASON, "alpha") == {}
    a.update_roster(SEASON, "alpha", ["ann"])
    a.update_roster(SEASON, "alpha", [])
    s.delete_season_record(SEASON)
    assert s.get_all_withdrawals(SEASON) == {}


def test_window_ownership(env):
    s, a = env["seasons"], env["assessments"]
    a.update_roster(SEASON, "alpha", ["bob", "cal"], by="coach1")      # ann withdrawn now
    assert "ann" in s.roster_for_window(SEASON, "alpha", env["w1"].opens_at)   # before she left
    later = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    assert "ann" not in s.roster_for_window(SEASON, "alpha", later)


def test_withdrawn_student_keeps_old_results_but_gets_no_new_tests(env):
    s, a = env["seasons"], env["assessments"]
    a.update_roster(SEASON, "alpha", ["bob", "cal"], by="coach1")
    time.sleep(1.1)       # withdrawal timestamps have 1-second resolution
    w2 = a.create_window(SEASON, _iso(datetime.now(timezone.utc) + timedelta(seconds=1)),
                         _iso(datetime.now(timezone.utc) + timedelta(hours=1)), ["alpha"], label="Week 2")
    t2 = a.get_assessment_for(w2.window_id, "alpha")
    with a._assessments_transaction() as store:
        store[t2.assessment_id] = dataclasses.replace(store[t2.assessment_id], snapshot=env["snap"],
                                                      status="live")
    c, tok = env["client"]("ann")
    ids = {x["assessment_id"]: x for x in c.get("/api/my-assessments").get_json()["assessments"]}
    assert env["t1"].assessment_id in ids and ids[env["t1"].assessment_id]["bucket"] == "past"
    assert t2.assessment_id not in ids
    assert c.get(f"/my-assessments/{env['t1'].assessment_id}/results").status_code != 403
    assert c.get(f"/api/my-assessments/{t2.assessment_id}/take").status_code == 403
    assert c.get(f"/api/my-assessments/{env['t1'].assessment_id}/take").status_code == 403


def test_scores_keep_withdrawn_row_without_counting_missing(env):
    a = env["assessments"]
    a.update_roster(SEASON, "alpha", ["bob", "cal"], by="coach1")
    c, tok = env["client"]("coach1")
    html = c.get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert "ann" in html
    c, tok = env["client"]("ann")
    html = c.get(f"/scores?season={SEASON}").get_data(as_text=True)
    assert "Week 1" in html                                   # her own past column


def test_put_route_reports_withdrawals(env):
    c, tok = env["client"]("coach1")
    r = c.put(f"/api/seasons/{SEASON}/roster/alpha", json={"usernames": ["cal"]},
              headers={"X-CSRF-Token": tok})
    j = r.get_json()
    assert j["ok"] and j["withdrawn"] == ["ann"] and j["removed"] == ["bob"]
    html = c.get(f"/club?season={SEASON}").get_data(as_text=True)
    assert 'class="chk wd"' in html                            # ann's W cell


def test_build_completeness_ignores_withdrawn(env):
    s, a = env["seasons"], env["assessments"]
    w = a.create_window(SEASON, _iso(env["now"]), _iso(env["now"] + timedelta(hours=1)), ["alpha"])
    t = a.get_assessment_for(w.window_id, "alpha")
    with a._assessments_transaction() as store:
        store[t.assessment_id] = dataclasses.replace(store[t.assessment_id], kind="build",
                                                     rubric=[{"id": "s", "kind": "scored", "label": "S",
                                                              "max_points": 10}])
    a.update_roster(SEASON, "alpha", ["cal"], by="coach1")         # ann withdrawn, bob removed
    a.set_build_grade(t.assessment_id, "cal", rubric_values={"s": 7}, graded_by="coach1")
    assert a.assessment_grading_complete(t.assessment_id, [], kind="build",
                                         season_id=SEASON, event_slug="alpha")
