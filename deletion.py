"""Hard deletion — the one place in this app where data really goes away.

Everything else the UI calls "delete" or "remove" is soft and reversible:
events and seasons get an `archived` flag, users get `disabled`, questions
get an annotation. That stance is deliberate and documented (README's
"Nothing is ever permanently deleted through the app", spec.md section 14),
and it stays the default. This module is the deliberate exception, added so
a pre-production instance can be cleared of test data without an operator
SSH-ing in to hand-edit JSON.

Two things keep it from undermining the stance it breaks:

1. **It is off unless switched on.** `review_app` gates every route here on
   `ALLOW_HARD_DELETE` in the instance's .env. Going to production is
   removing one line, not reverting code.
2. **Nothing is deleted without first being counted.** Every entity has a
   `preview_*` that returns exactly what would go, and the UI puts those
   numbers in front of the operator before anything happens. A cascade
   whose size is a surprise is the failure mode worth engineering against
   here -- deleting one season can reasonably mean deleting hundreds of
   student answers.

**Why cascade policy lives here rather than in each module**: the cascades
cross module boundaries (a season owns windows owns tests owns responses,
an event owns a directory on disk), and testing.py already imports
seasons.py -- so seasons.py cannot import testing.py to cascade downward
without a cycle. Each module owns record-level primitives that touch only
its own storage; this module composes them and is the only place that
knows the shape of the tree.

**Event files are moved, not erased.** `delete_event` relocates the event
directory to DATA_ROOT/.deleted/<slug>-<timestamp>/ rather than removing
it. The registry entry is gone and the app can no longer see it, but a PDF
library that took a season to assemble is not destroyed by one click in a
browser. Clearing that directory is an operator's decision, made over SSH,
with the backups still covering it in the meantime.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import auth
import events as events_mod
import seasons as seasons_mod
import testing as testing_mod

TRASH_DIRNAME = ".deleted"

#: Entity kinds this module can preview and delete, in the order a cascade
#: reaches them. Used by review_app to validate the :kind path segment
#: rather than maintaining a second list.
KINDS = ("user", "season", "event", "window", "test", "response",
         "pdf", "source", "textbook")


class DeletionError(Exception):
    """Refused — the caller asked for something this module won't do."""


def enabled() -> bool:
    """Whether hard deletion is switched on for this instance.

    Read on every call rather than cached at import: an operator flipping
    it in .env and restarting is the expected path, but a cached value
    would also make it untestable without reimporting the module.
    """
    return (os.environ.get("ALLOW_HARD_DELETE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def trash_dir() -> Path:
    return events_mod.DATA_ROOT / TRASH_DIRNAME


# ---------------------------------------------------------------------------
# Previews — what would go, without touching anything
# ---------------------------------------------------------------------------

def _test_counts(test_id: str) -> dict:
    return {"tests": 1, "responses": testing_mod.count_responses_for_test(test_id)}


def preview_test(test_id: str) -> dict:
    test = testing_mod.get_test(test_id)
    if test is None:
        raise DeletionError(f"no such test: {test_id}")
    return {
        "kind": "test", "label": f"{test.event_slug} test",
        **_test_counts(test_id),
    }


def preview_window(window_id: str) -> dict:
    window = testing_mod.get_window(window_id)
    if window is None:
        raise DeletionError(f"no such test window: {window_id}")
    tests = testing_mod.tests_for_window(window_id)
    return {
        "kind": "window",
        "label": window.label or f"window {window_id[:8]}",
        "windows": 1,
        "tests": len(tests),
        "responses": sum(testing_mod.count_responses_for_test(t.test_id) for t in tests),
    }


def preview_season(season_id: str) -> dict:
    season = seasons_mod.get_season(season_id)
    if season is None:
        raise DeletionError(f"no such season: {season_id}")
    windows = testing_mod.windows_for_season(season_id)
    tests = testing_mod.tests_for_season(season_id)
    return {
        "kind": "season",
        "label": season.display_label,
        "is_current": season.is_current,
        "seasons": 1,
        "windows": len(windows),
        "tests": len(tests),
        "responses": sum(testing_mod.count_responses_for_test(t.test_id) for t in tests),
        "roster_entries": seasons_mod.roster_entry_count(season_id),
    }


def preview_user(username: str) -> dict:
    user = auth.get_user(username)
    if user is None:
        raise DeletionError(f"no such user: {username}")
    responses = 0
    for test in testing_mod.load_tests().values():
        if testing_mod.get_response(test.test_id, username) is not None:
            responses += 1
    roster_entries = 0
    for season in seasons_mod.load_seasons().values():
        for names in seasons_mod.get_full_roster(season.season_id).values():
            if username in names:
                roster_entries += 1
    return {
        "kind": "user", "label": username, "role": user.role,
        "users": 1, "responses": responses, "roster_entries": roster_entries,
    }


def preview_event(slug: str) -> dict:
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        raise DeletionError(f"no such event: {slug}")
    if events_mod.is_builtin(slug):
        raise DeletionError(
            "built-in events can't be deleted — their definition lives in "
            "events.py, so the registry entry would be re-created on the "
            "next restart. Archive it instead."
        )
    n_files = n_bytes = 0
    if ev.base_dir.is_dir():
        for path in ev.base_dir.rglob("*"):
            if path.is_file():
                n_files += 1
                n_bytes += path.stat().st_size
    return {
        "kind": "event", "label": ev.name, "events": 1,
        "files": n_files, "bytes": n_bytes,
        "moves_to": str(trash_dir()),
    }


def preview_response(test_id: str, username: str) -> dict:
    if testing_mod.get_response(test_id, username) is None:
        raise DeletionError(f"no response from {username} for that test")
    return {"kind": "response", "label": username, "responses": 1}


# ---------------------------------------------------------------------------
# Deletions — each returns the same shape its preview did, filled with what
# actually went, so a caller can report the real outcome rather than
# echoing back the estimate it showed beforehand.
# ---------------------------------------------------------------------------

def delete_test(test_id: str) -> dict:
    preview_test(test_id)          # raises if it doesn't exist
    responses = testing_mod.delete_responses_for_test(test_id)
    testing_mod.delete_test_record(test_id)
    return {"kind": "test", "tests": 1, "responses": responses}


def delete_window(window_id: str) -> dict:
    preview_window(window_id)
    tests = testing_mod.tests_for_window(window_id)
    responses = 0
    for test in tests:
        responses += testing_mod.delete_responses_for_test(test.test_id)
        testing_mod.delete_test_record(test.test_id)
    testing_mod.delete_window_record(window_id)
    return {"kind": "window", "windows": 1, "tests": len(tests), "responses": responses}


def delete_season(season_id: str) -> dict:
    info = preview_season(season_id)
    windows = testing_mod.windows_for_season(season_id)
    tests = testing_mod.tests_for_season(season_id)
    responses = 0
    for test in tests:
        responses += testing_mod.delete_responses_for_test(test.test_id)
        testing_mod.delete_test_record(test.test_id)
    for window in windows:
        testing_mod.delete_window_record(window.window_id)
    seasons_mod.delete_season_record(season_id)
    return {
        "kind": "season", "seasons": 1, "windows": len(windows),
        "tests": len(tests), "responses": responses,
        "roster_entries": info["roster_entries"],
    }


def delete_user(username: str) -> dict:
    preview_user(username)
    responses = testing_mod.delete_responses_for_user(username)
    roster_entries = seasons_mod.remove_user_from_all_rosters(username)
    auth.delete_user(username)
    return {"kind": "user", "users": 1, "responses": responses,
            "roster_entries": roster_entries}


def delete_event(slug: str) -> dict:
    info = preview_event(slug)      # also refuses built-ins
    ev = events_mod.EVENTS[slug]
    moved_to = ""
    if ev.base_dir.is_dir():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = trash_dir() / f"{slug}-{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Move before deregistering: if the move fails (permissions, a file
        # held open) the event stays fully registered and usable rather
        # than vanishing from the UI with its data stranded on disk.
        shutil.move(str(ev.base_dir), str(dest))
        moved_to = str(dest)
    events_mod.delete_event_record(slug)
    return {"kind": "event", "events": 1, "files": info["files"],
            "bytes": info["bytes"], "moved_to": moved_to}


def delete_response(test_id: str, username: str) -> dict:
    preview_response(test_id, username)
    testing_mod.delete_response(test_id, username)
    return {"kind": "response", "responses": 1}


# ---------------------------------------------------------------------------
# Uploaded files: test/key PDFs, generation source material, shared textbooks
#
# README long said "there's still no route that deletes an uploaded file at
# all — by design", and that reasoning still holds for a live instance: a
# source PDF is the input the whole bank was derived from, and losing one
# silently is unrecoverable in a way losing a season record is not. These
# exist under the same ALLOW_HARD_DELETE gate as everything else here, and
# like events they MOVE the file to the trash directory rather than
# unlinking it, so a misclick costs an operator one `mv` rather than a
# re-download of something that may no longer be online.
#
# Deleting a test PDF also drops its extracted questions, because the bank
# keys questions by the PDF filename they came from ("bucket"). Leaving the
# bucket behind would strand questions whose source can never be reopened,
# re-cropped, or re-processed — so the preview states that count up front.
# ---------------------------------------------------------------------------

def _contained(base: Path, name: str) -> Path:
    """Resolve `name` directly under `base`, refusing anything that escapes.

    deletion.py deliberately doesn't import review_app (that would be a
    cycle), so this repeats _safe_join's containment check rather than
    reaching for it. Same rule: a bare ".." survives secure_filename, so
    containment is verified after resolving, not assumed from the string.
    """
    candidate = (base / name).resolve()
    base_resolved = base.resolve()
    if candidate == base_resolved or base_resolved not in candidate.parents:
        raise DeletionError(f"unsafe filename: {name!r}")
    return candidate


def _move_to_trash(path: Path, label: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = trash_dir() / f"{label}-{stamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.move(str(path), str(dest))
    return str(dest)


def _event_or_raise(slug: str):
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        raise DeletionError(f"no such event: {slug}")
    return ev


def _file_stats(path: Path) -> dict:
    return {"files": 1, "bytes": path.stat().st_size}


def preview_pdf(slug: str, filename: str) -> dict:
    ev = _event_or_raise(slug)
    path = _contained(ev.base_dir, filename)
    if not path.is_file():
        raise DeletionError(f"no such file: {filename}")
    # Questions are bucketed by source PDF filename, so the bucket key is
    # the filename itself.
    import build_question_bank as bqb
    bqb.set_event(slug)
    state = bqb._load_state()
    n_questions = len(state.get("questions", {}).get(filename, []) or [])
    return {"kind": "pdf", "label": filename, "questions": n_questions,
            "moves_to": str(trash_dir()), **_file_stats(path)}


def delete_pdf(slug: str, filename: str) -> dict:
    info = preview_pdf(slug, filename)
    ev = events_mod.EVENTS[slug]
    path = _contained(ev.base_dir, filename)

    import build_question_bank as bqb
    bqb.set_event(slug)
    with bqb._state_transaction() as state:
        state.get("questions", {}).pop(filename, None)
        state.get("annotations", {}).pop(filename, None)

    moved_to = _move_to_trash(path, f"{slug}-pdf")
    return {"kind": "pdf", "files": 1, "bytes": info["bytes"],
            "questions": info["questions"], "moved_to": moved_to}


def preview_source(slug: str, filename: str) -> dict:
    ev = _event_or_raise(slug)
    path = _contained(ev.texts_dir, filename)
    if not path.is_file():
        raise DeletionError(f"no such source: {filename}")
    return {"kind": "source", "label": filename,
            "moves_to": str(trash_dir()), **_file_stats(path)}


def delete_source(slug: str, filename: str) -> dict:
    info = preview_source(slug, filename)
    ev = events_mod.EVENTS[slug]
    path = _contained(ev.texts_dir, filename)
    return {"kind": "source", "files": 1, "bytes": info["bytes"],
            "moved_to": _move_to_trash(path, f"{slug}-source")}


def textbooks_dir() -> Path:
    return events_mod.DATA_ROOT / "textbooks"


def preview_textbook(filename: str) -> dict:
    path = _contained(textbooks_dir(), filename)
    if not path.is_file():
        raise DeletionError(f"no such textbook: {filename}")
    return {"kind": "textbook", "label": filename,
            "moves_to": str(trash_dir()), **_file_stats(path)}


def delete_textbook(filename: str) -> dict:
    info = preview_textbook(filename)
    path = _contained(textbooks_dir(), filename)
    return {"kind": "textbook", "files": 1, "bytes": info["bytes"],
            "moved_to": _move_to_trash(path, "textbook")}


# ---------------------------------------------------------------------------
# Dispatch — one table so review_app has a single route pair rather than
# twelve near-identical ones, and so KINDS can't drift from what's wired.
# ---------------------------------------------------------------------------

_PREVIEW = {
    "user": preview_user, "season": preview_season, "event": preview_event,
    "window": preview_window, "test": preview_test, "response": preview_response,
    "pdf": preview_pdf, "source": preview_source, "textbook": preview_textbook,
}
_DELETE = {
    "user": delete_user, "season": delete_season, "event": delete_event,
    "window": delete_window, "test": delete_test, "response": delete_response,
    "pdf": delete_pdf, "source": delete_source, "textbook": delete_textbook,
}


def preview(kind: str, *ident: str) -> dict:
    if kind not in _PREVIEW:
        raise DeletionError(f"unknown kind: {kind}")
    return _PREVIEW[kind](*ident)


def delete(kind: str, *ident: str) -> dict:
    if kind not in _DELETE:
        raise DeletionError(f"unknown kind: {kind}")
    return _DELETE[kind](*ident)
