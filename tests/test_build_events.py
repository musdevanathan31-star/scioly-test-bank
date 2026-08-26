"""
Coverage for build events (phase 1): Event.has_build, Assessment.kind, the
(window, event, kind) assessment key, the no-formula rubric (scored vs.
measured lines, override precedence), the __build__ manual-grade key, the
build completeness/status flow, and that publish/go-live/take all refuse a
build assessment. Also proves an ordinary exam assessment is completely
unaffected by any of this.

Each test drives a temp DATA_ROOT via the module-reload fixture below (same
pattern as tests/test_deletion.py) so nothing here touches a real instance's
files or leaks event registrations into other test files.

Run with: `python -m pytest tests/test_build_events.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAST = "2020-01-01T00:00:00"
FUTURE = "2099-01-01T00:00:00"


@pytest.fixture()
def mods(monkeypatch):
    """Fresh auth/events/seasons/assessments/build_question_bank modules
    bound to a throwaway DATA_ROOT -- these all resolve DATA_ROOT at import
    time, so the env var has to be set before the reload rather than
    patched onto an already-imported module."""
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="build-events-test-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    import auth, events, seasons, assessments
    for mod in (auth, events, seasons, bqb, assessments):
        importlib.reload(mod)

    yield {"auth": auth, "events": events, "seasons": seasons,
           "assessments": assessments, "bqb": bqb, "root": Path(tmp)}

    monkeypatch.undo()
    for mod in (auth, events, seasons, bqb, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


# ---------------------------------------------------------------------------
# Event.has_build
# ---------------------------------------------------------------------------

def test_has_build_round_trips_through_events_custom_json(mods):
    events = mods["events"]
    events.add_custom_event("bridge", "Bridge Building", has_build=True)
    importlib.reload(events)
    assert events.EVENTS["bridge"].has_build is True


def test_event_without_has_build_behaves_as_today(mods):
    events = mods["events"]
    events.add_custom_event("plain_ev", "Plain Event")
    importlib.reload(events)
    assert events.EVENTS["plain_ev"].has_build is False


# ---------------------------------------------------------------------------
# (window, event, kind) — both an exam and a build assessment can exist
# ---------------------------------------------------------------------------

def test_window_creates_both_exam_and_build_assessments_for_a_build_event(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("bridge", "Bridge", has_build=True)
    seasons.create_season("2027", event_slugs=["bridge"])
    window = assessments.create_window("2027", PAST, FUTURE, ["bridge"])

    exam = assessments.get_assessment_for(window.window_id, "bridge")
    build = assessments.get_assessment_for(window.window_id, "bridge", kind="build")
    assert exam is not None and exam.kind == "exam"
    assert build is not None and build.kind == "build"
    assert exam.assessment_id != build.assessment_id
    assert exam.window_id == build.window_id == window.window_id


def test_window_creates_no_build_assessment_for_a_study_only_event(mods):
    seasons, assessments = mods["seasons"], mods["assessments"]
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    window = assessments.create_window("2027", PAST, FUTURE, ["circuit_lab"])
    assert assessments.get_assessment_for(window.window_id, "circuit_lab", kind="build") is None
    assert assessments.get_assessment_for(window.window_id, "circuit_lab") is not None


# ---------------------------------------------------------------------------
# `kind` vs. the legacy "building" status migration -- must never collide
# ---------------------------------------------------------------------------

def test_legacy_building_status_migrates_without_setting_kind_build(mods):
    assessments = mods["assessments"]
    d = {"assessment_id": "a1", "window_id": "w1", "season_id": "2027",
         "event_slug": "circuit_lab", "status": "building"}
    a = assessments._dict_to_assessment(d)
    assert a.status == "preparing"
    assert a.kind == "exam"


def test_dict_to_assessment_reads_kind_independently_of_status(mods):
    assessments = mods["assessments"]
    d = {"assessment_id": "a1", "window_id": "w1", "season_id": "2027",
         "event_slug": "bridge", "status": "building", "kind": "build"}
    a = assessments._dict_to_assessment(d)
    # The status migration still runs...
    assert a.status == "preparing"
    # ...completely independently of kind, which is read straight through.
    assert a.kind == "build"


def test_dict_to_assessment_defaults_kind_to_exam(mods):
    assessments = mods["assessments"]
    d = {"assessment_id": "a1", "window_id": "w1", "season_id": "2027", "event_slug": "circuit_lab"}
    assert assessments._dict_to_assessment(d).kind == "exam"


# ---------------------------------------------------------------------------
# The rubric: no formula, ever
# ---------------------------------------------------------------------------

def test_compute_build_total_sums_scored_lines_ignores_measured(mods):
    assessments = mods["assessments"]
    rubric = [
        {"id": "l1", "kind": "scored", "label": "Efficiency", "max_points": 50},
        {"id": "l2", "kind": "scored", "label": "Style", "max_points": 10},
        {"id": "l3", "kind": "measured", "label": "Mass", "unit": "g"},
    ]
    values = {"l1": 42, "l2": 8, "l3": 350}
    assert assessments.compute_build_total(rubric, values, None) == 50


def test_override_wins_and_clearing_reverts_to_the_sum(mods):
    assessments = mods["assessments"]
    rubric = [{"id": "l1", "kind": "scored", "label": "x", "max_points": 10}]
    values = {"l1": 7}
    assert assessments.compute_build_total(rubric, values, 99) == 99
    assert assessments.compute_build_total(rubric, values, None) == 7


def test_compute_build_total_none_with_no_scored_lines_and_no_override(mods):
    assessments = mods["assessments"]
    rubric = [{"id": "l1", "kind": "measured", "label": "Mass", "unit": "g"}]
    assert assessments.compute_build_total(rubric, {"l1": 350}, None) is None


def test_set_assessment_rubric_rejects_bad_lines(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("bridge", "Bridge", has_build=True)
    seasons.create_season("2027", event_slugs=["bridge"])
    window = assessments.create_window("2027", PAST, FUTURE, ["bridge"])
    build = assessments.get_assessment_for(window.window_id, "bridge", kind="build")

    with pytest.raises(ValueError):
        assessments.set_assessment_rubric(build.assessment_id, [{"kind": "scored", "label": ""}])
    with pytest.raises(ValueError):
        assessments.set_assessment_rubric(
            build.assessment_id, [{"kind": "scored", "label": "x", "max_points": -1}])
    with pytest.raises(ValueError):
        assessments.set_assessment_rubric(build.assessment_id, [{"kind": "bogus", "label": "x"}])

    ok = assessments.set_assessment_rubric(build.assessment_id, [
        {"kind": "scored", "label": "Efficiency", "max_points": 50},
        {"kind": "measured", "label": "Mass", "unit": "g"},
    ])
    assert len(ok.rubric) == 2
    assert ok.rubric[0]["kind"] == "scored" and ok.rubric[0]["max_points"] == 50
    assert ok.rubric[1]["kind"] == "measured" and ok.rubric[1]["unit"] == "g"
    # ids were assigned even though the caller didn't supply any
    assert all(line["id"] for line in ok.rubric)


def test_copy_rubric_from_populates_a_new_build_assessment(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("bridge", "Bridge", has_build=True)
    seasons.create_season("2026", event_slugs=["bridge"])
    seasons.create_season("2027", event_slugs=["bridge"])
    w26 = assessments.create_window("2026", PAST, FUTURE, ["bridge"])
    w27 = assessments.create_window("2027", PAST, FUTURE, ["bridge"])
    old = assessments.get_assessment_for(w26.window_id, "bridge", kind="build")
    new = assessments.get_assessment_for(w27.window_id, "bridge", kind="build")
    assessments.set_assessment_rubric(old.assessment_id, [
        {"kind": "scored", "label": "Efficiency", "max_points": 50},
    ])
    updated = assessments.copy_rubric_from(new.assessment_id, old.assessment_id)
    assert len(updated.rubric) == 1
    assert updated.rubric[0]["label"] == "Efficiency"


# ---------------------------------------------------------------------------
# No-rubric build assessment records a single score
# ---------------------------------------------------------------------------

def test_no_rubric_build_assessment_records_a_single_score(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    assert build.rubric == []

    assessments.set_build_grade(build.assessment_id, "stu1", override=88, override_max=100,
                                graded_by="coach1", comment="nice flight")
    resp = assessments.get_response(build.assessment_id, "stu1")
    grade = resp.manual_grade[assessments.BUILD_GRADE_KEY]
    assert grade["points_earned"] == 88
    assert grade["points_possible"] == 100
    assert grade["graded_by"] == "coach1"
    assert resp.status == "submitted"


def test_no_rubric_build_assessment_requires_an_override(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    with pytest.raises(ValueError):
        assessments.set_build_grade(build.assessment_id, "stu1", graded_by="coach1")


# ---------------------------------------------------------------------------
# assessment_grading_complete / status flow for build
# ---------------------------------------------------------------------------

def test_build_grading_complete_requires_every_rostered_student(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    seasons.set_roster("2027", "rocket", ["stu1", "stu2"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")

    def complete():
        return assessments.assessment_grading_complete(
            build.assessment_id, [], kind="build", season_id="2027", event_slug="rocket")

    assert complete() is False
    assessments.set_build_grade(build.assessment_id, "stu1", override=10, override_max=10, graded_by="c")
    assert complete() is False
    assessments.set_build_grade(build.assessment_id, "stu2", override=9, override_max=10, graded_by="c")
    assert complete() is True

    # The "graded" leg of the status flow flips automatically once every
    # rostered student has a recorded grade.
    assert assessments.get_assessment(build.assessment_id).status == "graded"


def test_build_grading_complete_vacuously_true_for_empty_roster(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    assert assessments.assessment_grading_complete(
        build.assessment_id, [], kind="build", season_id="2027", event_slug="rocket") is True


def test_release_grades_flips_build_status_to_released(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    seasons.set_roster("2027", "rocket", ["stu1"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    assessments.set_build_grade(build.assessment_id, "stu1", override=10, override_max=10, graded_by="c")
    assert assessments.get_assessment(build.assessment_id).status == "graded"

    count = assessments.release_grades(build.assessment_id, [], released_by="coach1",
                                       kind="build", season_id="2027", event_slug="rocket")
    assert count == 1
    updated = assessments.get_assessment(build.assessment_id)
    assert updated.status == "released"
    resp = assessments.get_response(build.assessment_id, "stu1")
    assert resp.released is True


def test_release_grades_refuses_incomplete_build_assessment(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    seasons.set_roster("2027", "rocket", ["stu1", "stu2"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")
    assessments.set_build_grade(build.assessment_id, "stu1", override=10, override_max=10, graded_by="c")
    with pytest.raises(ValueError):
        assessments.release_grades(build.assessment_id, [], released_by="coach1",
                                   kind="build", season_id="2027", event_slug="rocket")


# ---------------------------------------------------------------------------
# publish / go-live / take refuse a build assessment
# ---------------------------------------------------------------------------

def test_publish_and_go_live_refuse_a_build_assessment(mods):
    events, seasons, assessments = mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    seasons.create_season("2027", event_slugs=["rocket"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")

    with pytest.raises(ValueError, match="build"):
        assessments.publish_assessment(build.assessment_id)
    with pytest.raises(ValueError, match="build"):
        assessments.go_live_assessment(build.assessment_id)


def test_take_routes_refuse_a_build_assessment(mods):
    auth, events, seasons, assessments = mods["auth"], mods["events"], mods["seasons"], mods["assessments"]
    events.add_custom_event("rocket", "Rocket", has_build=True)
    auth.create_user("stu1", "password123", role="student", events=[])
    seasons.create_season("2027", event_slugs=["rocket"])
    seasons.set_roster("2027", "rocket", ["stu1"])
    window = assessments.create_window("2027", PAST, FUTURE, ["rocket"])
    build = assessments.get_assessment_for(window.window_id, "rocket", kind="build")

    import review_app
    importlib.reload(review_app)
    review_app.app.testing = True
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "stu1", "password": "password123"})

    r = c.get(f"/my-assessments/{build.assessment_id}/take")
    assert r.status_code == 400
    r = c.get(f"/api/my-assessments/{build.assessment_id}/take")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# An exam assessment is completely unaffected: kind defaults, grade,
# release, and Scores all behave exactly as before.
# ---------------------------------------------------------------------------

def test_exam_assessment_end_to_end_unaffected_by_kind(mods):
    auth, events, seasons, assessments, bqb = (
        mods["auth"], mods["events"], mods["seasons"], mods["assessments"], mods["bqb"])
    slug = sorted(events.EVENTS)[0]
    auth.create_user("coach1", "password123", role="coach")
    auth.create_user("stu1", "password123", role="student", events=[])
    seasons.create_season("2027", event_slugs=[slug], created_by="coach1")
    seasons.set_roster("2027", slug, ["stu1"])

    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["s_test.pdf"] = [
            {"number": "1", "text": "Q", "answer": "A", "qtype": "frq", "choices": [], "images": []}]

    window = assessments.create_window("2027", PAST, FUTURE, [slug], label="W1")
    test = assessments.get_assessment_for(window.window_id, slug)
    assert test.kind == "exam"
    assert test.rubric == []

    assessments.update_assessment_kept(
        test.assessment_id, [{"bucket": "s_test.pdf", "number": "1", "max_points": 1}])
    result = assessments.publish_assessment(test.assessment_id, "coach1")
    assessments.go_live_assessment(result["test"].assessment_id, "coach1")

    snapshot = result["test"].snapshot
    assessments.start_or_get_response(test.assessment_id, "stu1", snapshot)
    assessments.save_answer(test.assessment_id, "stu1", "1", {"qtype": "frq", "text": "my answer"})
    assessments.submit_response(test.assessment_id, "stu1", snapshot)

    assert assessments.assessment_grading_complete(test.assessment_id, snapshot) is False
    assessments.set_manual_grade(test.assessment_id, "stu1", "1", 1.0, 1.0, graded_by="coach1")
    assert assessments.assessment_grading_complete(test.assessment_id, snapshot) is True

    count = assessments.release_grades(test.assessment_id, snapshot, released_by="coach1")
    assert count == 1

    import review_app
    importlib.reload(review_app)
    review_app.app.testing = True
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    r = c.get("/scores?season=2027")
    assert r.status_code == 200
    assert b"stu1" in r.data
