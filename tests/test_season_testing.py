"""
Regression coverage for the "student sees no upcoming/current tests" bug:
my_tests_page()/api_my_tests() (review_app.py) used to gate everything behind
seasons.get_current_season() with no fallback, while every coach-facing
season-scoped page (club_management_page/tests_dashboard_page/scores_page)
silently fell back to "the only/most recent season" when none was marked
current. A coach could fully roster students and prepare/publish/go-live a
test without ever clicking "Mark as current" — every page they touched
looked correct — while a student saw three empty buckets with no error.

seasons.resolve_season_id() now centralizes that fallback for every
season-scoped route. These tests prove: (1) the helper's fallback behavior
in isolation, (2) the new auto-current-on-first-season bootstrap, and (3)
the exact end-to-end scenario reported — a rostered student sees a live
test even though no season was ever explicitly marked current.

Run with: `python -m pytest tests/test_season_testing.py -q`
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth                    # noqa: E402
import seasons                 # noqa: E402
import testing as testing_mod  # noqa: E402

PAST = "2020-01-01T00:00:00"
FUTURE = "2099-01-01T00:00:00"


def _patch_files(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "auth_users.json")
    monkeypatch.setattr(seasons, "SEASONS_FILE", tmp_path / "seasons.json")
    monkeypatch.setattr(seasons, "ROSTERS_FILE", tmp_path / "season_rosters.json")
    monkeypatch.setattr(testing_mod, "WINDOWS_FILE", tmp_path / "test_windows.json")
    monkeypatch.setattr(testing_mod, "TESTS_FILE", tmp_path / "tests.json")
    monkeypatch.setattr(testing_mod, "RESPONSES_FILE", tmp_path / "test_responses.json")


def _force_not_current(season_id: str) -> None:
    """Simulate "no season has ever been marked current" — bypasses
    create_season()'s new first-season-auto-current bootstrap so the
    fallback logic itself (not the bootstrap) is what's under test."""
    with seasons._seasons_transaction() as ss:
        ss[season_id] = replace(ss[season_id], is_current=False)


def _force_live(test_id: str) -> None:
    """Flip a Test straight to "live" without running the real
    prepare/publish pipeline — irrelevant to season-resolution, the thing
    actually under test here."""
    with testing_mod._tests_transaction() as ts:
        ts[test_id] = replace(ts[test_id], status="live")


# ---------------------------------------------------------------------------
# seasons.resolve_season_id()
# ---------------------------------------------------------------------------

def test_resolve_season_id_no_seasons_returns_empty(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    assert seasons.resolve_season_id() == ""


def test_resolve_season_id_falls_back_to_only_season_when_none_current(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    _force_not_current("2027")
    assert seasons.get_current_season() is None
    assert seasons.resolve_season_id() == "2027"


def test_resolve_season_id_prefers_current_over_most_recent(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    seasons.create_season("2026", event_slugs=["circuit_lab"])
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    seasons.set_current_season("2026")
    # "2027" sorts later/higher, but "2026" is explicitly current and wins.
    assert seasons.resolve_season_id() == "2026"


def test_resolve_season_id_explicit_request_always_wins(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    assert seasons.resolve_season_id("some-other-id") == "some-other-id"


# ---------------------------------------------------------------------------
# create_season() auto-current bootstrap
# ---------------------------------------------------------------------------

def test_create_season_first_ever_is_auto_current(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    s = seasons.create_season("2027", event_slugs=["circuit_lab"])
    assert s.is_current is True
    assert seasons.get_current_season().season_id == "2027"


def test_create_season_second_does_not_auto_flip_current(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    s2 = seasons.create_season("2028", event_slugs=["circuit_lab"])
    assert s2.is_current is False
    assert seasons.get_current_season().season_id == "2027"


# ---------------------------------------------------------------------------
# End-to-end: the exact reported bug
# ---------------------------------------------------------------------------

def test_student_sees_live_test_with_no_season_marked_current(tmp_path, monkeypatch):
    """Coach rosters a student and goes live on a test under a season that
    is NOT marked current (the literal reported scenario) — the student's
    My Tests must still show it, via the same fallback the coach pages use."""
    _patch_files(monkeypatch, tmp_path)
    auth.create_user("student1", "password123", role="student", events=[])

    seasons.create_season("2027", event_slugs=["circuit_lab"])
    _force_not_current("2027")
    assert seasons.get_current_season() is None

    seasons.set_roster("2027", "circuit_lab", ["student1"])

    window = testing_mod.create_window(
        season_id="2027", opens_at=PAST, closes_at=FUTURE,
        event_slugs=["circuit_lab"], label="Jul 1",
    )
    test = testing_mod.get_test_for(window.window_id, "circuit_lab")
    assert test.status == "preparing"
    _force_live(test.test_id)

    import review_app
    review_app.app.testing = True
    with review_app.app.test_client() as c:
        r = c.post("/login", data={"username": "student1", "password": "password123"})
        assert r.status_code == 302

        r = c.get("/api/my-tests")
        assert r.status_code == 200
        out = r.get_json()["tests"]
        assert len(out) == 1, f"expected the live circuit_lab test, got {out!r}"
        assert out[0]["event_slug"] == "circuit_lab"
        assert out[0]["bucket"] == "current"

        r = c.get("/my-tests")
        assert r.status_code == 200
        assert b"circuit_lab" in r.data
        assert b"Nothing open right now." not in r.data


def test_student_sees_nothing_for_still_preparing_event(tmp_path, monkeypatch):
    """Sibling case from the report: a coach prepared some events but not
    others — the still-"preparing" one must not show up anywhere."""
    _patch_files(monkeypatch, tmp_path)
    auth.create_user("student1", "password123", role="student", events=[])
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    _force_not_current("2027")
    seasons.set_roster("2027", "circuit_lab", ["student1"])
    testing_mod.create_window(
        season_id="2027", opens_at=PAST, closes_at=FUTURE,
        event_slugs=["circuit_lab"], label="Jul 1",
    )
    # Never flip to "live" — stays "preparing".

    import review_app
    review_app.app.testing = True
    with review_app.app.test_client() as c:
        c.post("/login", data={"username": "student1", "password": "password123"})
        r = c.get("/api/my-tests")
        assert r.get_json()["tests"] == []
