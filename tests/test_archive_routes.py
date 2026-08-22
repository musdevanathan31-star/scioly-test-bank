"""
The archive HTTP surface, exercised through the app rather than the module.

archive_map.py's own tests prove the scoping logic; these prove the routes
actually apply it. That gap is where access-control bugs live: a correct
predicate that some handler forgot to call.

Run with: `python -m pytest tests/test_archive_routes.py -q`
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
def app_ctx(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="aroutes-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import tournament_archive as ta
    importlib.reload(ta)
    import archive_map
    importlib.reload(archive_map)
    # Before review_app, and as a pair: see the note in test_archive_ops.py.
    import archive_ops, archive_import
    importlib.reload(archive_ops)
    importlib.reload(archive_import)
    import review_app
    importlib.reload(review_app)

    root = ta.archive_root()
    for rel in ("Division B/Circuit Lab/2019/UF/test.pdf",
                "Division B/Anatomy/2020/Regionals/a.pdf",
                "Division C/_UnknownEvent/2021/States/b.pdf"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
    ta.save_index(ta.build_index())

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("vol1", "password123", "volunteer", events=[slug])
    auth.create_user("stu1", "password123", "student")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    def client(who):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        # Mutating routes require the double-submit CSRF token the real
        # frontend attaches automatically. Without it every POST 403s, which
        # would make the role checks below pass for the wrong reason.
        token = next((ck.value for ck in c._cookies.values()
                      if getattr(ck, "key", getattr(ck, "name", "")) == "csrf_token"),
                     None) if hasattr(c, "_cookies") else None
        if token is None:
            token = c.get_cookie("csrf_token").value
        c.environ_base["HTTP_X_CSRF_TOKEN"] = token
        return c

    yield client, archive_map, slug

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_a_student_cannot_reach_the_archive_at_all(app_ctx):
    client, _am, _slug = app_ctx
    c = client("stu1")
    assert c.get("/archive").status_code == 403
    assert c.get("/api/archive/list").status_code == 403


def test_a_volunteer_can_open_the_page(app_ctx):
    client, _am, _slug = app_ctx
    assert client("vol1").get("/archive").status_code == 200


def test_a_volunteer_sees_no_divisions_until_something_is_mapped(app_ctx):
    client, _am, _slug = app_ctx
    body = client("vol1").get("/api/archive/list").get_json()
    assert body["subdirs"] == []


def test_a_volunteer_sees_only_their_mapped_branch(app_ctx):
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    body = client("vol1").get("/api/archive/list").get_json()
    assert [d["name"] for d in body["subdirs"]] == ["Division B"]
    inner = client("vol1").get("/api/archive/list?path=Division+B").get_json()
    assert [d["name"] for d in inner["subdirs"]] == ["Circuit Lab"]


def test_an_unmapped_path_is_404_not_403_for_a_volunteer(app_ctx):
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    # 403 would confirm the path exists, which is itself the disclosure.
    r = client("vol1").get("/api/archive/list?path=Division+C/_UnknownEvent")
    assert r.status_code == 404


def test_a_volunteer_cannot_read_or_write_the_mapping(app_ctx):
    client, _am, slug = app_ctx
    c = client("vol1")
    assert c.get("/archive/map").status_code == 403
    assert c.get("/api/archive/map").status_code == 403
    assert c.post("/api/archive/map", json={"pairs": {"x/y": slug}}).status_code == 403


def test_a_volunteer_cannot_reindex_or_read_duplicates(app_ctx):
    client, _am, _slug = app_ctx
    c = client("vol1")
    # Both are whole-archive operations by definition.
    assert c.post("/api/archive/reindex").status_code == 403
    assert c.post("/api/archive/cancel").status_code == 403
    assert c.get("/api/archive/duplicates").status_code == 403


def test_a_volunteers_status_omits_whole_archive_totals(app_ctx):
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    body = client("vol1").get("/api/archive/status").get_json()
    for key in ("total_files", "total_bytes", "duplicates", "archive_dir"):
        assert key not in body, f"{key} describes the whole archive"


def test_a_coach_sees_the_unmapped_backlog(app_ctx):
    client, _am, _slug = app_ctx
    body = client("coach1").get("/api/archive/list").get_json()
    assert {d["name"] for d in body["subdirs"]} == {"Division B", "Division C"}
    assert client("coach1").get(
        "/api/archive/list?path=Division+C/_UnknownEvent").status_code == 200


def test_path_traversal_is_refused(app_ctx):
    client, _am, _slug = app_ctx
    for bad in ("..", "../..", "Division B/../../outside"):
        r = client("coach1").get("/api/archive/list", query_string={"path": bad})
        assert r.status_code in (400, 404), bad


def test_saving_a_mapping_takes_effect_immediately(app_ctx):
    client, _am, slug = app_ctx
    c = client("coach1")
    r = c.post("/api/archive/map",
               json={"pairs": {"Division B/Circuit Lab": slug}})
    assert r.get_json()["ok"] is True
    body = client("vol1").get("/api/archive/list").get_json()
    assert [d["name"] for d in body["subdirs"]] == ["Division B"]


def test_a_bad_slug_is_rejected_with_a_reason(app_ctx):
    client, _am, _slug = app_ctx
    r = client("coach1").post("/api/archive/map",
                              json={"pairs": {"Division B/Circuit Lab": "nope"}})
    assert r.status_code == 400
    assert "no such event" in r.get_json()["error"]


# ---------------------------------------------------------------------------
# Phase 3 mutations
# ---------------------------------------------------------------------------

def test_a_volunteer_cannot_mutate_the_archive(app_ctx):
    client, am, slug = app_ctx
    # Even inside a subtree they legitimately hold: organising is a coach
    # job, and a volunteer moving a folder changes what everyone else sees.
    am.set_many({"Division B/Circuit Lab": slug})
    c = client("vol1")
    for body in ({"action": "rename", "path": "Division B/Circuit Lab", "name": "X"},
                 {"action": "move", "path": "Division B/Circuit Lab", "dest": ""},
                 {"action": "delete", "path": "Division B/Circuit Lab"},
                 {"action": "create", "path": "Division B/Circuit Lab", "name": "X"}):
        assert c.post("/api/archive/apply", json=body).status_code == 403, body
        assert c.post("/api/archive/preview", json=body).status_code == 403
    assert c.post("/api/archive/prune-empty", json={"path": ""}).status_code == 403
    assert c.get("/api/archive/ops").status_code == 403


def test_preview_does_not_touch_the_filesystem(app_ctx):
    import tournament_archive as ta
    client, _am, _slug = app_ctx
    before = sorted(p.as_posix() for p in ta.archive_root().rglob("*"))
    r = client("coach1").post("/api/archive/preview",
                              json={"action": "delete", "path": "Division B/Anatomy"})
    assert r.get_json()["preview"]["files"] == 1
    assert sorted(p.as_posix() for p in ta.archive_root().rglob("*")) == before


def test_a_coach_can_rename_through_the_api(app_ctx):
    import tournament_archive as ta
    client, _am, _slug = app_ctx
    r = client("coach1").post("/api/archive/apply",
                              json={"action": "rename",
                                    "path": "Division B/Anatomy", "name": "Anatomy B"})
    assert r.get_json()["ok"] is True
    assert (ta.archive_root() / "Division B/Anatomy B").is_dir()


def test_an_unknown_action_is_refused(app_ctx):
    client, _am, _slug = app_ctx
    r = client("coach1").post("/api/archive/apply",
                              json={"action": "chmod", "path": "Division B"})
    assert r.status_code == 400
    assert "unknown action" in r.get_json()["error"]


def test_traversal_is_refused_by_the_mutation_routes_too(app_ctx):
    client, _am, _slug = app_ctx
    c = client("coach1")
    # The browse route's containment check protects reads; these are writes.
    for body in ({"action": "delete", "path": "../outside"},
                 {"action": "rename", "path": "..", "name": "x"},
                 {"action": "create", "path": "../..", "name": "x"}):
        assert c.post("/api/archive/apply", json=body).status_code == 400, body


def test_the_ops_log_is_readable_after_a_change(app_ctx):
    client, _am, _slug = app_ctx
    c = client("coach1")
    c.post("/api/archive/apply", json={"action": "create",
                                       "path": "Division B", "name": "Fresh"})
    ops = c.get("/api/archive/ops").get_json()["ops"]
    assert ops[0]["action"] == "create"
    assert ops[0]["dest"] == "Division B/Fresh"
    assert ops[0]["by"] == "coach1"


# ---------------------------------------------------------------------------
# Phase 4 import
# ---------------------------------------------------------------------------

def test_a_coach_can_read_the_mapping_payload(app_ctx):
    client, _am, _slug = app_ctx
    # Not just the 403 path: the volunteer tests above never executed this
    # handler's body, which is how a NameError in it survived.
    body = client("coach1").get("/api/archive/map").get_json()
    assert body["indexed"] is True
    assert any(r["key"] == "Division B/Circuit Lab" for r in body["rows"])
    assert body["events"], "the event list drives the dropdown"


def test_import_targets_are_scoped_to_the_user(app_ctx):
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    path = "Division B/Circuit Lab/2019/UF"
    coach = client("coach1").get(
        "/api/archive/import/targets", query_string={"path": path}).get_json()
    vol = client("vol1").get(
        "/api/archive/import/targets", query_string={"path": path}).get_json()
    assert [e["slug"] for e in vol["events"]] == [slug]
    assert len(coach["events"]) >= len(vol["events"])
    # The mapping already says which event this subtree is, so the
    # destination is a confirmation rather than a choice.
    assert vol["suggested"] == slug
    assert vol["meta"]["year"] == "2019"


def test_a_volunteer_cannot_import_into_an_event_they_lack(app_ctx):
    client, am, slug = app_ctx
    import review_app
    other = next(s for s in sorted(review_app.EVENTS) if s != slug)
    am.set_many({"Division B/Circuit Lab": slug})
    body = {"slug": other,
            "items": [{"path": "Division B/Circuit Lab/2019/UF/test.pdf",
                       "role": "test"}]}
    r = client("vol1").post("/api/archive/import", json=body)
    assert r.status_code == 400
    assert "access to that event" in r.get_json()["error"]


def test_a_volunteer_cannot_import_a_file_they_cannot_see(app_ctx):
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    # Holds the destination event, but the source is outside their scope --
    # both ends have to be checked, not just one.
    body = {"slug": slug,
            "items": [{"path": "Division C/_UnknownEvent/2021/States/b.pdf",
                       "role": "test"}]}
    r = client("vol1").post("/api/archive/import", json=body)
    assert r.status_code == 400
    assert "access to" in r.get_json()["error"]


def test_a_volunteer_can_import_within_their_own_event(app_ctx):
    import tournament_archive as ta
    client, am, slug = app_ctx
    am.set_many({"Division B/Circuit Lab": slug})
    body = {"slug": slug,
            "items": [{"path": "Division B/Circuit Lab/2019/UF/test.pdf",
                       "role": "test"}]}
    r = client("vol1").post("/api/archive/import", json=body)
    assert r.status_code == 200, r.get_json()
    assert not (ta.archive_root() /
                "Division B/Circuit Lab/2019/UF/test.pdf").exists()


def test_import_preview_moves_nothing(app_ctx):
    import tournament_archive as ta
    client, _am, slug = app_ctx
    body = {"slug": slug,
            "items": [{"path": "Division B/Circuit Lab/2019/UF/test.pdf",
                       "role": "test"}]}
    r = client("coach1").post("/api/archive/import/preview", json=body)
    assert r.get_json()["plan"]["files"][0]["dest_name"].endswith("_test.pdf")
    assert (ta.archive_root() /
            "Division B/Circuit Lab/2019/UF/test.pdf").is_file()


def test_a_student_cannot_import(app_ctx):
    client, _am, slug = app_ctx
    r = client("stu1").post("/api/archive/import", json={"slug": slug, "items": []})
    assert r.status_code == 403
