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
