"""
Coverage for deletion.py — the one place in this app where data really
goes away, so the things worth pinning down are: the gate is closed by
default, cascades reach everything they claim to, previews agree with what
deletion actually does, and nothing reaches sideways into a neighbour's
data.

Each test drives a temp DATA_ROOT via the module-reload fixture below, so
none of it can touch a real instance's files.

Run with: `python -m pytest tests/test_deletion.py -q`
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
def app_modules(monkeypatch):
    """Fresh modules bound to a throwaway DATA_ROOT.

    auth/seasons/events/testing all resolve DATA_ROOT at import time, so
    the env var has to be set before the reload rather than patched after.
    """
    import build_question_bank as bqb
    # tests/test_heuristics.py calls bqb.set_event("circuit_lab") at import
    # time, which pytest runs before any fixture here. Capture whatever is
    # active so teardown can put it back rather than guessing a slug.
    previous_event = bqb.current_event()

    tmp = tempfile.mkdtemp(prefix="deletion-test-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("ALLOW_HARD_DELETE", "true")
    import auth, events, seasons, testing, deletion
    for mod in (auth, events, seasons, testing, deletion):
        importlib.reload(mod)
    yield {"auth": auth, "events": events, "seasons": seasons,
           "testing": testing, "deletion": deletion, "root": Path(tmp)}

    # Teardown matters here in a way it doesn't for most fixtures: these
    # modules resolve DATA_ROOT at import time and hold it in module
    # globals, so a reload under the temp root leaks into every later test
    # in the session. Some of these tests also call bqb.set_event() on
    # scratch events, which leaves build_question_bank's active-event
    # ContextVar pointing at an event that has no topic keywords -- that
    # alone was enough to make tests/test_heuristics.py's classify_topic
    # cases fail when run after this file, and pass when run alone.
    monkeypatch.undo()
    for mod in (auth, events, seasons, testing, deletion):
        importlib.reload(mod)
    if previous_event is not None:
        # Re-resolve by slug, not by re-binding the old object: `events` was
        # just reloaded, so the registry holds new Event instances and the
        # stale one would point at the temp DATA_ROOT.
        bqb.set_event(previous_event.slug)


def _season_with_test(mods, season_id="2027"):
    seasons, testing = mods["seasons"], mods["testing"]
    slug = sorted(mods["events"].EVENTS)[0]
    seasons.create_season(season_id, event_slugs=[slug], created_by="coach")
    window = testing.create_window(season_id, "2027-01-01T09:00", "2027-01-01T11:00",
                                   [slug], label="Week 1", created_by="coach")
    test = testing.get_test_for(window.window_id, slug)
    return slug, window, test


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_disabled_by_default(app_modules, monkeypatch):
    # The whole safety story rests on this being off unless switched on, so
    # it is the first thing worth asserting.
    monkeypatch.delenv("ALLOW_HARD_DELETE", raising=False)
    assert app_modules["deletion"].enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
    ("false", False), ("0", False), ("", False), ("maybe", False),
])
def test_gate_parses_values(app_modules, monkeypatch, value, expected):
    monkeypatch.setenv("ALLOW_HARD_DELETE", value)
    assert app_modules["deletion"].enabled() is expected


# ---------------------------------------------------------------------------
# Cascades
# ---------------------------------------------------------------------------

def test_deleting_a_test_takes_its_responses(app_modules):
    testing, deletion = app_modules["testing"], app_modules["deletion"]
    _slug, _window, test = _season_with_test(app_modules)
    testing.start_or_get_response(test.test_id, "stu1", 3)
    testing.start_or_get_response(test.test_id, "stu2", 3)

    assert deletion.preview_test(test.test_id)["responses"] == 2
    result = deletion.delete_test(test.test_id)

    assert result == {"kind": "test", "tests": 1, "responses": 2}
    assert testing.get_test(test.test_id) is None
    assert testing.get_responses_for_test(test.test_id) == {}


def test_deleting_a_window_cascades_to_tests_and_responses(app_modules):
    testing, deletion = app_modules["testing"], app_modules["deletion"]
    _slug, window, test = _season_with_test(app_modules)
    testing.start_or_get_response(test.test_id, "stu1", 3)

    preview = deletion.preview_window(window.window_id)
    assert (preview["tests"], preview["responses"]) == (1, 1)

    result = deletion.delete_window(window.window_id)
    assert (result["windows"], result["tests"], result["responses"]) == (1, 1, 1)
    assert testing.get_window(window.window_id) is None
    assert testing.get_test(test.test_id) is None


def test_deleting_a_season_cascades_all_the_way_down(app_modules):
    seasons, testing, deletion = (app_modules["seasons"], app_modules["testing"],
                                  app_modules["deletion"])
    slug, window, test = _season_with_test(app_modules)
    seasons.set_roster("2027", slug, ["stu1", "stu2"])
    testing.start_or_get_response(test.test_id, "stu1", 3)

    preview = deletion.preview_season("2027")
    assert (preview["windows"], preview["tests"], preview["responses"]) == (1, 1, 1)
    assert preview["roster_entries"] == 2

    result = deletion.delete_season("2027")
    assert (result["windows"], result["tests"], result["responses"]) == (1, 1, 1)
    assert seasons.get_season("2027") is None
    assert testing.get_window(window.window_id) is None
    assert testing.get_test(test.test_id) is None


def test_preview_matches_what_deletion_reports(app_modules):
    # A confirmation dialog that overstates or understates the damage is
    # worse than none, because it will be believed.
    testing, deletion = app_modules["testing"], app_modules["deletion"]
    _slug, window, test = _season_with_test(app_modules)
    for name in ("a", "b", "c"):
        testing.start_or_get_response(test.test_id, name, 3)
    preview = deletion.preview_window(window.window_id)
    result = deletion.delete_window(window.window_id)
    for key in ("windows", "tests", "responses"):
        assert preview[key] == result[key], key


# ---------------------------------------------------------------------------
# Isolation — a delete must not reach into a neighbour
# ---------------------------------------------------------------------------

def test_deleting_one_test_leaves_another_tests_responses_alone(app_modules):
    testing, deletion = app_modules["testing"], app_modules["deletion"]
    seasons = app_modules["seasons"]
    slug, _w1, test1 = _season_with_test(app_modules, "2027")
    seasons.create_season("2028", event_slugs=[slug], created_by="coach")
    w2 = testing.create_window("2028", "2028-01-01T09:00", "2028-01-01T11:00", [slug])
    test2 = testing.get_test_for(w2.window_id, slug)

    testing.start_or_get_response(test1.test_id, "stu1", 3)
    testing.start_or_get_response(test2.test_id, "stu1", 3)

    deletion.delete_test(test1.test_id)

    assert testing.get_response(test2.test_id, "stu1") is not None
    assert testing.get_test(test2.test_id) is not None


def test_deleting_a_user_removes_their_responses_everywhere_only(app_modules):
    auth, testing, deletion = (app_modules["auth"], app_modules["testing"],
                               app_modules["deletion"])
    _slug, _window, test = _season_with_test(app_modules)
    auth.create_user("stu1", "password123", "student")
    testing.start_or_get_response(test.test_id, "stu1", 3)
    testing.start_or_get_response(test.test_id, "stu2", 3)

    result = deletion.delete_user("stu1")

    assert result["responses"] == 1
    assert auth.get_user("stu1") is None
    assert testing.get_response(test.test_id, "stu1") is None
    # The other student's answers are untouched.
    assert testing.get_response(test.test_id, "stu2") is not None


def test_deleting_a_response_leaves_the_test_intact(app_modules):
    testing, deletion = app_modules["testing"], app_modules["deletion"]
    _slug, _window, test = _season_with_test(app_modules)
    testing.start_or_get_response(test.test_id, "stu1", 3)

    deletion.delete_response(test.test_id, "stu1")

    assert testing.get_response(test.test_id, "stu1") is None
    assert testing.get_test(test.test_id) is not None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_builtin_events_are_refused_not_silently_recreated(app_modules, monkeypatch):
    # NOTE: events._BUILTIN_SLUGS is deliberately empty today — the shipped
    # defaults (circuit_lab, thermodynamics) are seeded by
    # _seed_default_events() specifically so they stay editable and
    # deletable like any user event (see events.py's comment above
    # _BUILTIN_SLUGS). So this injects one to exercise the guard itself,
    # rather than asserting a built-in exists and silently passing
    # vacuously the day one is added.
    events, deletion = app_modules["events"], app_modules["deletion"]
    slug = sorted(events.EVENTS)[0]
    monkeypatch.setattr(events, "_BUILTIN_SLUGS", frozenset({slug}))

    with pytest.raises(deletion.DeletionError, match="built-in"):
        deletion.preview_event(slug)
    with pytest.raises(deletion.DeletionError):
        deletion.delete_event(slug)
    assert slug in events.EVENTS, "a refused delete must change nothing"


def test_seeded_default_events_are_deletable(app_modules):
    # The flip side, pinned so the above can't be misread as "the shipped
    # events are protected". They are not, by design.
    events, deletion = app_modules["events"], app_modules["deletion"]
    slug = sorted(events.EVENTS)[0]
    assert events.is_builtin(slug) is False
    deletion.delete_event(slug)
    assert slug not in events.EVENTS


def test_deleting_an_event_moves_its_files_to_trash_rather_than_erasing(app_modules):
    events, deletion = app_modules["events"], app_modules["deletion"]
    events.add_custom_event("scratch_ev", "Scratch Event")
    ev = events.EVENTS["scratch_ev"]
    ev.base_dir.mkdir(parents=True, exist_ok=True)
    (ev.base_dir / "important.pdf").write_text("not really a pdf", encoding="utf-8")

    preview = deletion.preview_event("scratch_ev")
    assert preview["files"] == 1

    result = deletion.delete_event("scratch_ev")

    assert "scratch_ev" not in events.EVENTS
    assert not ev.base_dir.exists()
    moved = Path(result["moved_to"])
    assert moved.is_dir(), "event directory should have been moved, not deleted"
    assert (moved / "important.pdf").read_text(encoding="utf-8") == "not really a pdf"
    assert moved.parent.name == deletion.TRASH_DIRNAME


# ---------------------------------------------------------------------------
# Dispatch / errors
# ---------------------------------------------------------------------------

def test_unknown_kind_is_refused(app_modules):
    deletion = app_modules["deletion"]
    with pytest.raises(deletion.DeletionError, match="unknown kind"):
        deletion.delete("everything", "x")


def test_missing_entities_raise_rather_than_silently_succeeding(app_modules):
    deletion = app_modules["deletion"]
    for kind, ident in [("season", ("nope",)), ("window", ("nope",)),
                        ("test", ("nope",)), ("user", ("nope",))]:
        with pytest.raises(deletion.DeletionError):
            deletion.preview(kind, *ident)


def test_dispatch_tables_cover_exactly_the_declared_kinds(app_modules):
    deletion = app_modules["deletion"]
    assert set(deletion._PREVIEW) == set(deletion.KINDS)
    assert set(deletion._DELETE) == set(deletion.KINDS)


# ---------------------------------------------------------------------------
# Uploaded files: PDFs, generation sources, shared textbooks
# ---------------------------------------------------------------------------

def _make_event_with_pdf(mods, slug="scratch_ev", fname="scratch_2024_b_x_test.pdf"):
    events = mods["events"]
    events.add_custom_event(slug, "Scratch")
    ev = events.EVENTS[slug]
    ev.base_dir.mkdir(parents=True, exist_ok=True)
    (ev.base_dir / fname).write_bytes(b"%PDF-1.4 fake")
    return ev, fname


def test_deleting_a_pdf_reports_and_removes_its_extracted_questions(app_modules):
    deletion = app_modules["deletion"]
    ev, fname = _make_event_with_pdf(app_modules)

    import build_question_bank as bqb
    bqb.set_event(ev.slug)
    with bqb._state_transaction() as state:
        state.setdefault("questions", {})[fname] = [{"number": "1"}, {"number": "2"}]

    preview = deletion.preview_pdf(ev.slug, fname)
    assert preview["questions"] == 2, "the cascade must be stated up front"

    result = deletion.delete_pdf(ev.slug, fname)
    assert result["questions"] == 2
    assert not (ev.base_dir / fname).exists()
    assert Path(result["moved_to"]).is_file(), "PDFs are moved, not erased"

    bqb.set_event(ev.slug)
    assert fname not in bqb._load_state().get("questions", {})


def test_deleting_a_source_and_a_textbook_moves_them_to_trash(app_modules):
    deletion, events = app_modules["deletion"], app_modules["events"]
    events.add_custom_event("srcev", "Src")
    ev = events.EVENTS["srcev"]
    ev.texts_dir.mkdir(parents=True, exist_ok=True)
    (ev.texts_dir / "wiki.md").write_text("notes", encoding="utf-8")

    res = deletion.delete_source("srcev", "wiki.md")
    assert not (ev.texts_dir / "wiki.md").exists()
    assert Path(res["moved_to"]).read_text(encoding="utf-8") == "notes"

    tb = deletion.textbooks_dir()
    tb.mkdir(parents=True, exist_ok=True)
    (tb / "physics.pdf").write_bytes(b"book")
    res = deletion.delete_textbook("physics.pdf")
    assert not (tb / "physics.pdf").exists()
    assert Path(res["moved_to"]).read_bytes() == b"book"


@pytest.mark.parametrize("bad", ["../../secret.json", "..", "sub/../../out.pdf"])
def test_file_deletes_refuse_paths_that_escape_their_directory(app_modules, bad):
    # These routes take a filename straight from a URL, so containment is
    # the whole security story — a traversal here would move arbitrary
    # files out of the instance.
    deletion, events = app_modules["deletion"], app_modules["events"]
    events.add_custom_event("travev", "Trav")
    with pytest.raises(deletion.DeletionError):
        deletion.preview_pdf("travev", bad)
    with pytest.raises(deletion.DeletionError):
        deletion.preview_textbook(bad)


def test_missing_files_raise_rather_than_reporting_success(app_modules):
    deletion, events = app_modules["deletion"], app_modules["events"]
    events.add_custom_event("emptyev", "Empty")
    with pytest.raises(deletion.DeletionError, match="no such file"):
        deletion.preview_pdf("emptyev", "nope.pdf")
    with pytest.raises(deletion.DeletionError, match="no such event"):
        deletion.preview_pdf("no_such_event", "x.pdf")
