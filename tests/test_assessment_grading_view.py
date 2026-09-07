"""A coach grading a submission must be able to see the WHOLE thing, not
only the free-response items that need a manual grade.

`api_get_grading` used to return `snapshot_frqs` (the FRQ-only subset) as
the page's only view of the assessment. That's the right list for what
needs manual grading -- assessment_grading_complete()'s release gate is
correctly FRQ-only -- but it meant a 5-question test with 4 auto-graded
MCQ/T-F/matching items and 1 FRQ showed the coach exactly 1 question, with
no indication the other 4 even existed. Fixed by adding a `snapshot` field
carrying every question, and `auto_grade` on each response (previously
omitted) so the page can show what was auto-awarded on those questions too.

The template's own JS rendering can't run outside a browser (same
limitation noted in test_split_question_group_js.py's docstring for
DOM-wiring code), so this pins the payload CONTRACT: the data a coach needs
to see every question is actually present in the API response.

Run with: `python -m pytest tests/test_assessment_grading_view.py -q`
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SEASON = "2099-2100"


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="gradingview-")
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

    # A mix of qtypes -- the actual shape of the reported bug (5 questions,
    # only 1 of them FRQ).
    snapshot = [
        {"number": "1", "qtype": "mcq", "text": "1+1?", "max_points": 1,
         "choices": [{"letter": "A", "text": "2"}, {"letter": "B", "text": "3"}],
         "correct_answer": "A"},
        {"number": "2", "qtype": "tf", "text": "Sky is blue.", "max_points": 1,
         "choices": [], "correct_answer": "True"},
        {"number": "3", "qtype": "matching", "text": "Match them.", "max_points": 2,
         "choices": [],
         "matching": {"left": [{"label": "1", "text": "A"}, {"label": "2", "text": "B"}],
                      "right": [{"label": "A", "text": "x"}, {"label": "B", "text": "y"}],
                      "pairs": {"1": "A", "2": "B"}}},
        {"number": "4", "qtype": "frq", "text": "Explain.", "max_points": 3,
         "choices": [], "correct_answer": "Because reasons."},
        {"number": "5", "qtype": "mcq", "text": "2+2?", "max_points": 1,
         "choices": [{"letter": "A", "text": "4"}, {"letter": "B", "text": "5"}],
         "correct_answer": "A"},
    ]
    w = assessments.create_window(SEASON, "2099-10-01T09:00", "2099-10-01T10:00",
                                  ["alpha"], label="W1", created_by="coach1")
    t = assessments.get_assessment_for(w.window_id, "alpha")
    with assessments._assessments_transaction() as store:
        store[t.assessment_id] = dataclasses.replace(
            store[t.assessment_id], snapshot=snapshot, status="live")

    answers = {
        "1": {"qtype": "mcq", "picked": "A"},
        "2": {"qtype": "tf", "picked": "False"},          # wrong, on purpose
        "3": {"qtype": "matching", "picks": {"1": "A", "2": "A"}},
        "4": {"qtype": "frq", "text": "My reasoning."},
        "5": {"qtype": "mcq", "picked": "A"},
    }
    auto_grade = {
        "1": {"correct": True, "points_earned": 1.0, "points_possible": 1.0},
        "2": {"correct": False, "points_earned": 0.0, "points_possible": 1.0},
        "3": {"per_pair": [{"label": "1", "given": "A", "expected": "A", "ok": True},
                           {"label": "2", "given": "A", "expected": "B", "ok": False}],
              "points_earned": 1.0, "points_possible": 2.0},
        "5": {"correct": True, "points_earned": 1.0, "points_possible": 1.0},
    }
    resp = assessments.Response(
        student_username="stu_a", assessment_id=t.assessment_id,
        question_order=list(range(len(snapshot))), answers=answers,
        auto_grade=auto_grade, manual_grade={}, status="submitted",
        started_at="2099-10-01T09:05", last_saved_at="2099-10-01T09:20",
        submitted_at="2099-10-01T09:20")
    p = assessments._response_path(t.assessment_id, "stu_a")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(assessments._response_to_dict(resp), indent=2), encoding="utf-8")

    def client(who):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        return c

    yield review_app, assessments, t.assessment_id, client

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_the_payload_carries_every_question_not_just_frq(env):
    review_app, assessments, aid, client = env
    j = client("coach1").get(f"/api/assessments/{aid}/grading").get_json()
    numbers = [q["number"] for q in j["snapshot"]]
    assert numbers == ["1", "2", "3", "4", "5"], (
        "the grading page's data must include every question -- this is "
        "the actual bug: a coach saw only the 1 FRQ out of 5 questions")


def test_snapshot_frqs_stays_frq_only_the_release_gate_depends_on_it(env):
    """Purely additive change -- the FRQ-only list that drives
    updateProgress()'s completeness count and the release gate must be
    completely unaffected."""
    review_app, assessments, aid, client = env
    j = client("coach1").get(f"/api/assessments/{aid}/grading").get_json()
    assert [q["number"] for q in j["snapshot_frqs"]] == ["4"]


def test_auto_grade_is_present_on_the_response_for_non_frq_questions(env):
    """Previously omitted entirely -- without this the page has no way to
    show what a student was auto-awarded on the questions it's now also
    displaying."""
    review_app, assessments, aid, client = env
    j = client("coach1").get(f"/api/assessments/{aid}/grading").get_json()
    ag = j["responses"]["stu_a"]["auto_grade"]
    assert ag["1"]["points_earned"] == 1.0
    assert ag["2"]["points_earned"] == 0.0
    assert ag["3"]["per_pair"][0]["ok"] is True
    assert ag["3"]["per_pair"][1]["ok"] is False


def test_answers_for_every_qtype_are_retrievable_from_the_payload(env):
    """What the coach actually needs to see per question -- confirms the
    answer data a render pass would need is really there, for every qtype,
    not only frq."""
    review_app, assessments, aid, client = env
    j = client("coach1").get(f"/api/assessments/{aid}/grading").get_json()
    answers = j["responses"]["stu_a"]["answers"]
    assert answers["1"]["picked"] == "A"
    assert answers["2"]["picked"] == "False"
    assert answers["3"]["picks"] == {"1": "A", "2": "A"}
    assert answers["5"]["picked"] == "A"


def test_a_student_cannot_reach_the_grading_endpoint(env):
    review_app, assessments, aid, client = env
    r = client("stu_a").get(f"/api/assessments/{aid}/grading")
    assert r.status_code == 403
