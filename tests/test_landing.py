"""
Where each role lands after logging in, and what its menu offers.

Coaches and volunteers go to the assessments dashboard: during a season the
recurring job is preparing, running and grading the week's assessments,
while curating the question bank is the off-season task. Students go
straight to their own page rather than bouncing through "/" only to be
redirected out of it.

Pinned because it's the kind of behaviour that gets quietly reverted by an
unrelated change to the login route, and nobody notices until a coach
mentions the app "goes to the wrong page now".

Run with: `python -m pytest tests/test_landing.py -q`
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
def app_with_users(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="landing-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    # A path prefix, because that's how it actually runs behind Caddy and
    # it's where a hand-built redirect URL would go wrong.
    monkeypatch.setenv("APPLICATION_ROOT", "/testbank/ncms")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("vol1", "password123", "volunteer", events=[slug])
    auth.create_user("stu1", "password123", "student")
    # A student carrying event slugs — what the Club Management CSV
    # bulk-add produces when its `events` column is filled in.
    auth.create_user("stu2", "password123", "student", events=[slug])
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    yield review_app, slug

    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _login(app, who):
    c = app.test_client()
    return c, c.post("/login", data={"username": who, "password": "password123"})


@pytest.mark.parametrize("who,expected", [
    ("coach1", "/testbank/ncms/assessments"),
    ("vol1", "/testbank/ncms/assessments"),
    ("stu1", "/testbank/ncms/my-assessments"),
])
def test_each_role_lands_on_its_own_home(app_with_users, who, expected):
    review_app, _slug = app_with_users
    _c, r = _login(review_app.app, who)
    assert r.status_code == 302
    assert r.headers["Location"] == expected


def test_a_deep_link_still_wins_over_the_default(app_with_users):
    # Someone following a link to a specific page, or bounced to /login by
    # an expired session, must end up where they were going.
    review_app, slug = app_with_users
    c = review_app.app.test_client()
    target = f"/testbank/ncms/event/{slug}/browse"
    r = c.post(f"/login?next={target}",
               data={"username": "coach1", "password": "password123"})
    assert r.headers["Location"] == target


def test_the_event_list_is_untouched_and_still_served_at_root(app_with_users):
    # Several templates use "/" as their "back to event list" target, so
    # moving the landing page must not have turned "/" into a redirect.
    review_app, _slug = app_with_users
    c, _r = _login(review_app.app, "coach1")
    assert c.get("/").status_code == 200


def test_students_are_still_kept_out_of_the_event_list(app_with_users):
    review_app, _slug = app_with_users
    c, _r = _login(review_app.app, "stu1")
    r = c.get("/")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/my-assessments")


# ---------------------------------------------------------------------------
# Menu scoping — the navicon's per-event accordions
# ---------------------------------------------------------------------------

import re  # noqa: E402

_GROUPS = re.compile(r'class="nav-group-toggle"[^>]*>([^<]+)<')


def _menu_groups(client):
    html = client.get("/settings").get_data(as_text=True)
    panel = html[html.index('id="navIconPanel"'):html.index("</form>")]
    return [g.strip() for g in _GROUPS.findall(panel)]


@pytest.mark.parametrize("who", ["stu1", "stu2"])
def test_students_get_no_event_groups_even_with_events_assigned(app_with_users, who):
    # stu2 is the regression: the nav used to infer "no bank access" from an
    # empty user.events, so a student with slugs set was shown Question bank
    # / Test bank / Primary sources. Every link 403s, but it listed event
    # names to students and offered destinations that only fail.
    review_app, _slug = app_with_users
    c, _r = _login(review_app.app, who)
    assert _menu_groups(c) == []


def test_students_are_still_blocked_from_the_pages_themselves(app_with_users):
    review_app, slug = app_with_users
    c, _r = _login(review_app.app, "stu2")
    for path in (f"/event/{slug}/", f"/event/{slug}/browse", f"/event/{slug}/sources"):
        assert c.get(path).status_code == 403, path


def test_coaches_and_volunteers_still_get_their_event_groups(app_with_users):
    review_app, _slug = app_with_users
    for who in ("coach1", "vol1"):
        c, _r = _login(review_app.app, who)
        groups = _menu_groups(c)
        assert any("Question bank" in g for g in groups), (who, groups)
        assert any("Test bank" in g for g in groups), (who, groups)
