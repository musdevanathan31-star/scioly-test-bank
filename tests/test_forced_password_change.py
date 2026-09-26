"""
Forced first-login password change (auth.User.must_change_password).

Accounts an operator creates with a formula starting password
(season_admin.py `accounts`, the Club page CSV import) carry the flag;
review_app's _require_login confines them to the change-password form
until change_own_password() clears it.

Run with: `python -m pytest tests/test_forced_password_change.py -q`
"""
from __future__ import annotations

import importlib
import io
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def env(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="forcepw-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    auth.create_user("coach1", "password123", "coach")
    auth.create_user("fresh", "startpass1", "student", display_name="Fresh Kid",
                     must_change_password=True)
    auth.create_user("settled", "password123", "student")

    def client(who, password):
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": password})
        c.csrf = c.get_cookie("csrf_token").value
        return c

    yield auth, seasons, client
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _change(c, current, new):
    return c.post("/api/account/password", json={"current_password": current, "new_password": new},
                  headers={"X-CSRF-Token": c.csrf})


def test_flagged_user_is_confined_to_the_password_form(env):
    auth, _seasons, client = env
    c = client("fresh", "startpass1")
    r = c.get("/my-assessments")
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings")
    r = c.get("/api/my-assessments")
    assert r.status_code == 403 and r.get_json()["must_change_password"]
    page = c.get("/settings")
    assert page.status_code == 200 and b"choose your own" in page.data


def test_changing_the_password_clears_the_flag(env):
    auth, _seasons, client = env
    c = client("fresh", "startpass1")
    assert _change(c, "startpass1", "startpass1").status_code == 400     # must actually change
    assert auth.get_user("fresh").must_change_password
    assert _change(c, "startpass1", "mynewpass9").get_json()["ok"]
    user = auth.get_user("fresh")
    assert not user.must_change_password and user.display_name == "Fresh Kid"
    assert c.get("/my-assessments").status_code == 200


def test_unflagged_users_are_unaffected(env):
    _auth, _seasons, client = env
    assert client("settled", "password123").get("/my-assessments").status_code == 200
    assert client("coach1", "password123").get("/").status_code == 200
    # Reusing the same password is only refused while the flag is set.
    c = client("settled", "password123")
    assert _change(c, "password123", "password123").get_json()["ok"]


def test_updates_keep_the_flag_and_operator_reset_sets_it(env):
    auth, _seasons, _client = env
    auth.update_user("fresh", disabled=False)
    auth.set_display_name("fresh", "Renamed")
    assert auth.get_user("fresh").must_change_password
    auth.set_password_by_operator("settled", "resetpass1")
    assert auth.get_user("settled").must_change_password
    assert auth.verify_login("settled", "resetpass1") is not None


def test_csv_import_saves_name_and_sets_flag(env):
    auth, seasons, client = env
    import events
    events.add_custom_event("alpha", "Alpha")
    seasons.create_season("2027", event_slugs=["alpha"], created_by="coach1")
    c = client("coach1", "password123")
    csv = "display_name,username,events\nJane Doe,,alpha\n"
    r = c.post("/api/seasons/2027/students/bulk-csv", headers={"X-CSRF-Token": c.csrf},
               data={"file": (io.BytesIO(csv.encode()), "s.csv")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    user = auth.get_user("janedoe")
    assert user.display_name == "Jane Doe" and user.must_change_password
    assert seasons.get_roster("2027", "alpha") == ["janedoe"]
