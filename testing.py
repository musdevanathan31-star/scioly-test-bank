"""
Season-long test administration: TestWindow (a scheduled open/close span
covering one or more events), Test (one event's test within one window —
built by volunteers, published as a frozen snapshot, then administered),
and Response (one student's in-progress/submitted answers + grading for
one Test).

Stored as three separate flat JSON files at DATA_ROOT (not one combined
file, not per-event/per-window directories) — matches auth.py's/seasons.py's
one-concept-per-file convention, and critically keeps test_responses.json
(by far the highest-write-volume file — every student's autosave every few
seconds during a live window) on its own lock so a burst of concurrent
student autosaves never blocks a coach's read of tests.json for the
dashboard. All three share the same atomic tempfile+os.replace write helper
and a lock-registry keyed by filename, mirroring build_question_bank.py's
per-event _state_locks pattern.

A Test is prepared from one event's question bank but is NOT gated by
`auth.User.events` (a volunteer's bank-edit access) — test-preparation
assignment is a separate grant, keyed on TestWindow.assignments, enforced
by review_app.py's `_select_test()` guard, deliberately independent of
`_select_event()`. See seasons.py's module docstring for the parallel
reasoning on why a season's event lineup never touches bank-access either.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent
DATA_ROOT = Path(os.environ.get("DATA_ROOT") or REPO_ROOT)
WINDOWS_FILE = DATA_ROOT / "test_windows.json"
TESTS_FILE = DATA_ROOT / "tests.json"
RESPONSES_FILE = DATA_ROOT / "test_responses.json"

SCHEMA_VERSION = 1

_lock_registry: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _registry_lock:
        lk = _lock_registry.get(key)
        if lk is None:
            lk = _lock_registry[key] = threading.RLock()
        return lk


def _load_json_unlocked(path: Path, default):
    # Caller must already hold _lock_for(path).
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json_unlocked(path: Path, data) -> None:
    # Caller must already hold _lock_for(path).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path, default):
    with _lock_for(path):
        return _load_json_unlocked(path, default)


def _save_json(path: Path, data) -> None:
    with _lock_for(path):
        _save_json_unlocked(path, data)


@contextlib.contextmanager
def _windows_transaction():
    """Hold WINDOWS_FILE's lock across the full load -> mutate -> save cycle
    — see auth.py's _users_transaction() for the lost-update bug this avoids
    (two mutators loading the same pre-mutation snapshot, last save wins).
    Save only runs if the `with`-block body doesn't raise."""
    with _lock_for(WINDOWS_FILE):
        raw = _load_json_unlocked(WINDOWS_FILE, {})
        windows = {wid: _dict_to_window(d) for wid, d in raw.items()}
        yield windows
        _save_json_unlocked(WINDOWS_FILE, {wid: _window_to_dict(w) for wid, w in windows.items()})


@contextlib.contextmanager
def _tests_transaction():
    """Same as _windows_transaction(), for TESTS_FILE."""
    with _lock_for(TESTS_FILE):
        raw = _load_json_unlocked(TESTS_FILE, {})
        tests = {tid: _dict_to_test(d) for tid, d in raw.items()}
        yield tests
        _save_json_unlocked(TESTS_FILE, {tid: _test_to_dict(t) for tid, t in tests.items()})


@contextlib.contextmanager
def _responses_transaction():
    """Same as _windows_transaction(), for RESPONSES_FILE — by far the
    highest-write-volume file (every student's autosave every few seconds
    during a live window), so this is the most exposed instance of the
    lost-update race in this module. Mutators that need to read-then-merge
    (save_answer's answers dict, set_manual_grade's manual_grade dict) must
    do the read and the write inside the SAME transaction, not via a
    separate get_response() call followed by _save_response() — composing
    two transactions reopens the same race one level up (two answers saved
    around the same time would still drop one)."""
    with _lock_for(RESPONSES_FILE):
        raw = _load_json_unlocked(RESPONSES_FILE, {})
        data = {tid: {u: _dict_to_response(d) for u, d in by_user.items()} for tid, by_user in raw.items()}
        yield data
        out = {tid: {u: _response_to_dict(r) for u, r in by_user.items()} for tid, by_user in data.items()}
        _save_json_unlocked(RESPONSES_FILE, out)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# TestWindow — a scheduled open/close span (may run a few days, not
# necessarily one) covering one or more events for one season.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestWindow:
    window_id: str
    season_id: str
    label: str = ""               # free text, purely descriptive — display
                                   # always derives the actual date range
                                   # from opens_at/closes_at, never parses this
    opens_at: str = ""              # ISO8601 datetime, absolute
    closes_at: str = ""              # ISO8601 datetime, absolute — may be days after opens_at
    event_slugs: tuple[str, ...] = ()
    assignments: dict = field(default_factory=dict)   # {event_slug: [volunteer_usernames]}
    archived: bool = False
    created_at: str = ""
    created_by: str = ""


def _window_to_dict(w: TestWindow) -> dict:
    return {
        "window_id": w.window_id, "season_id": w.season_id, "label": w.label,
        "opens_at": w.opens_at, "closes_at": w.closes_at,
        "event_slugs": list(w.event_slugs), "assignments": dict(w.assignments),
        "archived": w.archived, "created_at": w.created_at, "created_by": w.created_by,
    }


def _dict_to_window(d: dict) -> TestWindow:
    return TestWindow(
        window_id=d["window_id"], season_id=d.get("season_id", ""), label=d.get("label", ""),
        opens_at=d.get("opens_at", ""), closes_at=d.get("closes_at", ""),
        event_slugs=tuple(d.get("event_slugs") or ()), assignments=dict(d.get("assignments") or {}),
        archived=bool(d.get("archived", False)), created_at=d.get("created_at", ""),
        created_by=d.get("created_by", ""),
    )


def load_windows() -> dict[str, TestWindow]:
    raw = _load_json(WINDOWS_FILE, {})
    return {wid: _dict_to_window(d) for wid, d in raw.items()}


def get_window(window_id: str) -> TestWindow | None:
    return load_windows().get(window_id)


def create_window(season_id: str, opens_at: str, closes_at: str,
                   event_slugs: list[str], label: str = "", created_by: str = "") -> TestWindow:
    """Validates event_slugs is a subset of the season's lineup and
    opens_at < closes_at, then creates the window and lazily creates one
    Test per event (status "preparing", kept=[])."""
    import seasons as seasons_mod

    season = seasons_mod.get_season(season_id)
    if season is None:
        raise ValueError(f"unknown season {season_id!r}")
    unknown = [s for s in event_slugs if s not in season.event_slugs]
    if unknown:
        raise ValueError(f"event slug(s) not in season {season_id!r}'s lineup: {', '.join(unknown)}")
    if not opens_at or not closes_at:
        raise ValueError("opens_at and closes_at are required")
    if opens_at >= closes_at:
        raise ValueError("opens_at must be before closes_at")

    window_id = uuid.uuid4().hex
    window = TestWindow(
        window_id=window_id, season_id=season_id, label=label,
        opens_at=opens_at, closes_at=closes_at, event_slugs=tuple(event_slugs),
        created_at=_now_iso(), created_by=created_by,
    )
    with _windows_transaction() as windows:
        windows[window_id] = window
    for slug in event_slugs:
        _ensure_test(window_id, season_id, slug, created_by)
    return window


def update_window(window_id: str, label: str | None = None, opens_at: str | None = None,
                   closes_at: str | None = None, event_slugs: list[str] | None = None) -> TestWindow:
    """Edit a window's fields. Adding an event_slug lazily creates its Test;
    removing one does NOT delete its Test record (it just stops appearing
    on the active dashboard view) — never destroys data."""
    import seasons as seasons_mod

    with _windows_transaction() as windows:
        existing = windows.get(window_id)
        if existing is None:
            raise ValueError(f"unknown window {window_id!r}")
        new_opens = opens_at if opens_at is not None else existing.opens_at
        new_closes = closes_at if closes_at is not None else existing.closes_at
        if new_opens >= new_closes:
            raise ValueError("opens_at must be before closes_at")
        new_slugs = existing.event_slugs
        if event_slugs is not None:
            season = seasons_mod.get_season(existing.season_id)
            unknown = [s for s in event_slugs if season and s not in season.event_slugs]
            if unknown:
                raise ValueError(f"event slug(s) not in season {existing.season_id!r}'s lineup: {', '.join(unknown)}")
            new_slugs = tuple(event_slugs)
        updated = replace(
            existing,
            label=label if label is not None else existing.label,
            opens_at=new_opens, closes_at=new_closes, event_slugs=new_slugs,
        )
        windows[window_id] = updated
    for slug in new_slugs:
        _ensure_test(window_id, existing.season_id, slug, existing.created_by)
    return updated


def update_window_assignments(window_id: str, event_slug: str, usernames: list[str]) -> TestWindow:
    with _windows_transaction() as windows:
        existing = windows.get(window_id)
        if existing is None:
            raise ValueError(f"unknown window {window_id!r}")
        if event_slug not in existing.event_slugs:
            raise ValueError(f"{event_slug!r} is not part of window {window_id!r}")
        assignments = dict(existing.assignments)
        assignments[event_slug] = list(usernames)
        updated = replace(existing, assignments=assignments)
        windows[window_id] = updated
    return updated


# ---------------------------------------------------------------------------
# Test — one event's test within one window.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Test:
    test_id: str
    window_id: str
    season_id: str
    event_slug: str
    status: str = "preparing"     # preparing|published|live|closed|graded|released
    kept: list = field(default_factory=list)            # [{bucket, number, max_points}]
    snapshot: list | None = None                          # frozen question content, set at publish
    snapshot_contexts: dict = field(default_factory=dict)  # frozen shared-context blocks, keyed "bucket::id"
    overrides: dict = field(default_factory=dict)        # {student_username: {opens_at, closes_at, granted_by, granted_at, reason}}
    published_at: str | None = None
    published_by: str | None = None
    live_at: str | None = None
    live_by: str | None = None
    last_edited_by: str | None = None
    last_edited_at: str | None = None
    created_at: str = ""
    created_by: str = ""


def _test_to_dict(t: Test) -> dict:
    return {
        "test_id": t.test_id, "window_id": t.window_id, "season_id": t.season_id,
        "event_slug": t.event_slug, "status": t.status, "kept": list(t.kept),
        "snapshot": t.snapshot, "snapshot_contexts": dict(t.snapshot_contexts),
        "overrides": dict(t.overrides),
        "published_at": t.published_at, "published_by": t.published_by,
        "live_at": t.live_at, "live_by": t.live_by,
        "last_edited_by": t.last_edited_by, "last_edited_at": t.last_edited_at,
        "created_at": t.created_at, "created_by": t.created_by,
    }


def _dict_to_test(d: dict) -> Test:
    # Lazy migration: older tests.json records may still say "building" (the
    # status was renamed to "preparing" — "build" is Sci-Oly jargon for
    # building *events*, which confused volunteers when reused for tests).
    # Normalizing here means any such record reads as "preparing" right away
    # and gets rewritten on its next save, with no separate migration script.
    status = d.get("status", "preparing")
    if status == "building":
        status = "preparing"
    return Test(
        test_id=d["test_id"], window_id=d.get("window_id", ""), season_id=d.get("season_id", ""),
        event_slug=d.get("event_slug", ""), status=status,
        kept=list(d.get("kept") or []), snapshot=d.get("snapshot"),
        snapshot_contexts=dict(d.get("snapshot_contexts") or {}),
        overrides=dict(d.get("overrides") or {}),
        published_at=d.get("published_at"), published_by=d.get("published_by"),
        live_at=d.get("live_at"), live_by=d.get("live_by"),
        last_edited_by=d.get("last_edited_by"), last_edited_at=d.get("last_edited_at"),
        created_at=d.get("created_at", ""), created_by=d.get("created_by", ""),
    )


def load_tests() -> dict[str, Test]:
    raw = _load_json(TESTS_FILE, {})
    return {tid: _dict_to_test(d) for tid, d in raw.items()}


def get_test(test_id: str) -> Test | None:
    return load_tests().get(test_id)


def get_test_for(window_id: str, event_slug: str) -> Test | None:
    for t in load_tests().values():
        if t.window_id == window_id and t.event_slug == event_slug:
            return t
    return None


def tests_for_window(window_id: str) -> list[Test]:
    return [t for t in load_tests().values() if t.window_id == window_id]


def _ensure_test(window_id: str, season_id: str, event_slug: str, created_by: str = "") -> Test:
    """Lazily creates a Test for (window_id, event_slug) if one doesn't
    already exist — never overwrites an existing Test (re-adding an event
    that already has a Test, e.g. after it was removed and re-added to a
    window, must not wipe out a test someone already built)."""
    # Fast-path check outside the lock (the common case: the test already
    # exists, so no write is needed). Re-checked inside the transaction
    # below to close the race where two threads both see "doesn't exist yet"
    # and would otherwise create two Test records for the same pair.
    existing = get_test_for(window_id, event_slug)
    if existing is not None:
        return existing
    with _tests_transaction() as tests:
        existing = next((t for t in tests.values()
                         if t.window_id == window_id and t.event_slug == event_slug), None)
        if existing is not None:
            return existing
        test_id = uuid.uuid4().hex
        t = Test(test_id=test_id, window_id=window_id, season_id=season_id, event_slug=event_slug,
                  created_at=_now_iso(), created_by=created_by)
        tests[test_id] = t
        return t


def update_test_kept(test_id: str, kept: list, edited_by: str = "") -> Test:
    """Autosave for the test-builder's persistent kept-set. Rejects once the
    test is no longer "preparing" (published/live and beyond) — edits past
    that point must go through the explicit unpublish exception path."""
    with _tests_transaction() as tests:
        existing = tests.get(test_id)
        if existing is None:
            raise ValueError(f"unknown test {test_id!r}")
        if existing.status != "preparing":
            raise ValueError(f"test is {existing.status!r}, not editable — unpublish first")
        cleaned = []
        for item in kept:
            cleaned.append({
                "bucket": item.get("bucket", ""),
                "number": str(item.get("number", "")),
                "max_points": float(item.get("max_points") or 1),
            })
        updated = replace(existing, kept=cleaned, last_edited_by=edited_by, last_edited_at=_now_iso())
        tests[test_id] = updated
    return updated


def _snapshot_one_question(q: dict, bucket: str, max_points: float) -> dict:
    """Freeze one bank question's content/answer/rubric into the
    publish-time snapshot shape — never re-read live after this. correct
    answer comes from `answer` (mcq/frq) or `matching.pairs` (matching);
    `source_question_ref` is for traceability only, never followed back to
    the live bank by any grading/rendering code."""
    qtype = q.get("qtype") or ("mcq" if q.get("choices") else "frq")
    entry = {
        "bucket": bucket, "number": q.get("number"), "qtype": qtype,
        "text": q.get("text", ""), "max_points": max_points,
        "images": list(q.get("images") or []),
        "image_descriptions": dict(q.get("image_descriptions") or {}),
        "context_id": q.get("context_id"),
        "source_question_ref": {"bucket": bucket, "number": q.get("number")},
    }
    if qtype == "matching":
        entry["matching"] = q.get("matching") or {"left": [], "right": [], "pairs": {}}
    else:
        entry["choices"] = list(q.get("choices") or [])
        entry["correct_answer"] = q.get("answer", "")
    return entry


def publish_test(test_id: str, published_by: str = "") -> dict:
    """Builds `snapshot`/`snapshot_contexts` from the live question bank for
    every kept question, sets status="published". A kept question deleted
    from the bank since being kept is skipped (not a hard failure) — the
    publish still succeeds with whatever could be resolved, and the caller
    is told what got skipped so it can be surfaced as a toast.

    Returns {"test": Test, "skipped": [{"bucket","number"}]}.
    """
    import build_question_bank as bqb

    with _tests_transaction() as tests:
        existing = tests.get(test_id)
        if existing is None:
            raise ValueError(f"unknown test {test_id!r}")
        if existing.status != "preparing":
            raise ValueError(f"test is already {existing.status!r}")
        if not existing.kept:
            raise ValueError("cannot publish an empty test — keep at least one question first")

        bqb.set_event(existing.event_slug)
        state = bqb._load_state()
        questions_by_bucket = state.get("questions", {})
        contexts = bqb._all_contexts()  # {"bucket::id": Context}

        snapshot: list[dict] = []
        snapshot_contexts: dict[str, dict] = {}
        skipped: list[dict] = []
        for item in existing.kept:
            bucket, number = item.get("bucket", ""), str(item.get("number", ""))
            bank_q = next((q for q in (questions_by_bucket.get(bucket) or [])
                           if str(q.get("number")) == number), None)
            if bank_q is None:
                skipped.append({"bucket": bucket, "number": number})
                continue
            entry = _snapshot_one_question(bank_q, bucket, float(item.get("max_points") or 1))
            snapshot.append(entry)
            ctx_id = bank_q.get("context_id")
            if ctx_id:
                ctx_key = f"{bucket}::{ctx_id}"
                ctx = contexts.get(ctx_key)
                if ctx:
                    snapshot_contexts[ctx_key] = ctx

        if not snapshot:
            raise ValueError("every kept question was removed from the bank — nothing to publish")

        updated = replace(
            existing, status="published", snapshot=snapshot, snapshot_contexts=snapshot_contexts,
            published_at=_now_iso(), published_by=published_by,
        )
        tests[test_id] = updated
    return {"test": updated, "skipped": skipped}


def unpublish_test(test_id: str) -> Test:
    """Reverts a published/live test back to "preparing" for edits. Caller
    (review_app.py's route) is responsible for the guardrail checks (window
    not yet open, no saved responses) before calling this — this function
    itself only enforces the status precondition, not the timing/response
    guardrails, since those need the TestWindow and Response data this
    module-level function isn't handed."""
    with _tests_transaction() as tests:
        existing = tests.get(test_id)
        if existing is None:
            raise ValueError(f"unknown test {test_id!r}")
        if existing.status not in ("published", "live"):
            raise ValueError(f"test is {existing.status!r}, not published/live")
        updated = replace(existing, status="preparing", snapshot=None, snapshot_contexts={},
                           published_at=None, published_by=None, live_at=None, live_by=None)
        tests[test_id] = updated
    return updated


def go_live_test(test_id: str, live_by: str = "") -> Test:
    with _tests_transaction() as tests:
        existing = tests.get(test_id)
        if existing is None:
            raise ValueError(f"unknown test {test_id!r}")
        if existing.status != "published":
            raise ValueError(f"test is {existing.status!r}, must be 'published' first")
        updated = replace(existing, status="live", live_at=_now_iso(), live_by=live_by)
        tests[test_id] = updated
    return updated


def set_test_overrides(test_id: str, student_username: str, opens_at: str | None,
                       closes_at: str | None, granted_by: str = "", reason: str = "") -> Test:
    """Upsert (opens_at/closes_at not None) or revoke (both None) a personal
    makeup-window override for one student on one test. A personal override
    is an INDEPENDENT clock from the class-wide window, not an extension of
    it — see effective_window()."""
    with _tests_transaction() as tests:
        existing = tests.get(test_id)
        if existing is None:
            raise ValueError(f"unknown test {test_id!r}")
        overrides = dict(existing.overrides)
        if opens_at is None and closes_at is None:
            overrides.pop(student_username, None)
        else:
            if not opens_at or not closes_at or opens_at >= closes_at:
                raise ValueError("opens_at must be before closes_at")
            overrides[student_username] = {
                "opens_at": opens_at, "closes_at": closes_at,
                "granted_by": granted_by, "granted_at": _now_iso(), "reason": reason,
            }
        updated = replace(existing, overrides=overrides)
        tests[test_id] = updated
    return updated


def effective_window(test: Test, window: TestWindow, username: str) -> tuple[str, str]:
    """A personal override, if one exists for this student on this test,
    wins outright over the class-wide window — independent clock, not an
    extension of it. Returns (opens_at, closes_at) as ISO8601 strings."""
    ov = test.overrides.get(username)
    if ov:
        return ov["opens_at"], ov["closes_at"]
    return window.opens_at, window.closes_at


def is_window_open(test: Test, window: TestWindow, username: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    opens_s, closes_s = effective_window(test, window, username)
    opens = datetime.fromisoformat(opens_s)
    closes = datetime.fromisoformat(closes_s)
    if opens.tzinfo is None:
        opens = opens.replace(tzinfo=timezone.utc)
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return opens <= now <= closes


def is_window_past(test: Test, window: TestWindow, username: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    _, closes_s = effective_window(test, window, username)
    closes = datetime.fromisoformat(closes_s)
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return now > closes


# ---------------------------------------------------------------------------
# Response — one student's answers + grading for one Test.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    student_username: str
    test_id: str
    question_order: list = field(default_factory=list)   # indices into Test.snapshot, stored once, stable
    answers: dict = field(default_factory=dict)            # {number: {qtype, picked|text|picks}}
    auto_grade: dict = field(default_factory=dict)         # {number: {...,points_earned,points_possible}}
    manual_grade: dict = field(default_factory=dict)       # {number: {points_earned,points_possible,graded_by,graded_at,comment}}
    status: str = "in_progress"     # in_progress|submitted|auto_submitted_late
    started_at: str = ""
    last_saved_at: str = ""
    submitted_at: str | None = None
    released: bool = False
    released_at: str | None = None
    released_by: str | None = None


def _response_to_dict(r: Response) -> dict:
    return {
        "student_username": r.student_username, "test_id": r.test_id,
        "question_order": list(r.question_order), "answers": dict(r.answers),
        "auto_grade": dict(r.auto_grade), "manual_grade": dict(r.manual_grade),
        "status": r.status, "started_at": r.started_at, "last_saved_at": r.last_saved_at,
        "submitted_at": r.submitted_at, "released": r.released,
        "released_at": r.released_at, "released_by": r.released_by,
    }


def _dict_to_response(d: dict) -> Response:
    return Response(
        student_username=d.get("student_username", ""), test_id=d.get("test_id", ""),
        question_order=list(d.get("question_order") or []), answers=dict(d.get("answers") or {}),
        auto_grade=dict(d.get("auto_grade") or {}), manual_grade=dict(d.get("manual_grade") or {}),
        status=d.get("status", "in_progress"), started_at=d.get("started_at", ""),
        last_saved_at=d.get("last_saved_at", ""), submitted_at=d.get("submitted_at"),
        released=bool(d.get("released", False)), released_at=d.get("released_at"),
        released_by=d.get("released_by"),
    )


def _load_all_responses() -> dict[str, dict[str, Response]]:
    raw = _load_json(RESPONSES_FILE, {})
    return {tid: {u: _dict_to_response(d) for u, d in by_user.items()} for tid, by_user in raw.items()}


def get_response(test_id: str, username: str) -> Response | None:
    return _load_all_responses().get(test_id, {}).get(username)


def get_responses_for_test(test_id: str) -> dict[str, Response]:
    return _load_all_responses().get(test_id, {})


def start_or_get_response(test_id: str, username: str, num_questions: int) -> Response:
    """First call for a given (test, student) creates the Response with a
    freshly shuffled question_order, stored immediately so it never
    changes again for this student on this test — content stays identical
    for everyone, only display order is per-student and stable across
    reloads.

    The existence check and the create must happen inside one transaction
    (not get_response() followed by a separate save) — otherwise two
    near-simultaneous first-loads for the same student would each shuffle
    their own order and the second save would silently replace the first,
    leaving the student's already-rendered page out of sync with what's on
    disk."""
    import random

    with _responses_transaction() as data:
        by_user = data.setdefault(test_id, {})
        existing = by_user.get(username)
        if existing is not None:
            return existing
        order = list(range(num_questions))
        random.shuffle(order)
        r = Response(student_username=username, test_id=test_id, question_order=order,
                     started_at=_now_iso(), last_saved_at=_now_iso())
        by_user[username] = r
        return r


def save_answer(test_id: str, username: str, number: str, answer_payload: dict) -> Response:
    """Merges one question's answer into the student's `answers` dict. Reads
    the existing response and writes the merged result inside the same
    transaction — composing a separate get_response() + save would still
    lose answers (two autosave requests close together would each merge
    into their own stale copy of `answers`, and the second save would wipe
    out whatever the first one added)."""
    with _responses_transaction() as data:
        existing = (data.get(test_id) or {}).get(username)
        if existing is None:
            raise ValueError("no in-progress response — load the test first")
        if existing.status != "in_progress":
            raise ValueError(f"response is already {existing.status!r} — can't edit further")
        answers = dict(existing.answers)
        answers[str(number)] = answer_payload
        updated = replace(existing, answers=answers, last_saved_at=_now_iso())
        data.setdefault(test_id, {})[username] = updated
    return updated


def _grade_mcq(picked: str | None, correct_answer: str) -> dict:
    correct_raw = (correct_answer or "").strip()
    ok = bool(picked) and picked.strip().upper() == correct_raw.upper()[:1]
    return {"correct": ok, "points_earned": 1.0 if ok else 0.0, "points_possible": 1.0}


def _grade_matching(matching: dict, picks: dict, max_points: float) -> dict:
    """Direct Python port of quiz.html's submitMatchingAnswer() partial-
    credit logic — a real test cannot trust a client-computed score, so
    this must be re-derived server-side from the snapshot's own pairs."""
    left = matching.get("left") or []
    correct_pairs = matching.get("pairs") or {}
    total_pairs = len(correct_pairs)
    per_pair = []
    for l in left:
        expected = correct_pairs.get(l.get("label"))
        if expected is None:
            continue
        given = picks.get(l.get("label"))
        per_pair.append({"label": l.get("label"), "given": given, "expected": expected,
                         "ok": given is not None and given == expected})
    num_correct = sum(1 for p in per_pair if p["ok"])
    credit_fraction = (num_correct / total_pairs) if total_pairs else 0.0
    return {"per_pair": per_pair, "points_earned": round(credit_fraction * max_points, 4),
            "points_possible": max_points}


def submit_response(test_id: str, username: str, snapshot: list, now: datetime | None = None,
                    late: bool = False) -> Response:
    """Computes auto_grade for every MCQ/matching answer from the snapshot
    (never trusting client-side grading), sets status. FRQ items are left
    for manual grading (Part 5) — grading_status is derived on read from
    whether every FRQ has a manual_grade, not stored here."""
    with _responses_transaction() as data:
        existing = (data.get(test_id) or {}).get(username)
        if existing is None:
            raise ValueError("no in-progress response to submit")
        if existing.status != "in_progress":
            raise ValueError(f"already {existing.status!r}")
        auto_grade = {}
        for q in snapshot:
            number = str(q.get("number"))
            answer = existing.answers.get(number)
            if not answer:
                continue
            if q.get("qtype") == "mcq":
                auto_grade[number] = _grade_mcq(answer.get("picked"), q.get("correct_answer", ""))
            elif q.get("qtype") == "matching":
                auto_grade[number] = _grade_matching(q.get("matching") or {}, answer.get("picks") or {},
                                                     float(q.get("max_points") or 1))
            # frq: no auto-grade entry — graded manually (Part 5)
        updated = replace(existing, auto_grade=auto_grade,
                          status="auto_submitted_late" if late else "submitted",
                          submitted_at=_now_iso())
        data.setdefault(test_id, {})[username] = updated
    return updated


def test_grading_complete(test_id: str, snapshot: list) -> bool:
    """True iff every response with status in (submitted, auto_submitted_late)
    has a non-null manual_grade.points_earned for every FRQ in the snapshot.
    Recomputed on read (cheap — bounded by roster size x FRQ count) rather
    than stored, to avoid a second source of truth that could drift."""
    frq_numbers = [str(q.get("number")) for q in snapshot if q.get("qtype") == "frq"]
    if not frq_numbers:
        return True
    for r in get_responses_for_test(test_id).values():
        if r.status not in ("submitted", "auto_submitted_late"):
            continue
        for num in frq_numbers:
            g = r.manual_grade.get(num)
            if not g or g.get("points_earned") is None:
                return False
    return True


def set_manual_grade(test_id: str, student_username: str, number: str, points_earned: float,
                     max_points: float, graded_by: str = "", comment: str = "") -> Response:
    if not (0 <= points_earned <= max_points):
        raise ValueError(f"points_earned must be between 0 and {max_points}")
    with _responses_transaction() as data:
        resp = (data.get(test_id) or {}).get(student_username)
        if resp is None:
            raise ValueError("no response on file for this student")
        manual_grade = dict(resp.manual_grade)
        manual_grade[str(number)] = {
            "points_earned": points_earned, "points_possible": max_points,
            "graded_by": graded_by, "graded_at": _now_iso(), "comment": comment,
        }
        updated = replace(resp, manual_grade=manual_grade)
        data.setdefault(test_id, {})[student_username] = updated
    return updated


def release_grades(test_id: str, snapshot: list, released_by: str = "") -> int:
    """Batch-flips released=True (+released_at/released_by) on every
    submitted response for this test simultaneously. Per-response storage
    (not a single Test-level flag) because "released" is fundamentally
    about what a student can see of THEIR OWN response — a per-response
    fact, even though every response for a test is released at once.
    Requires test_grading_complete() first; raises otherwise (the route
    re-checks this server-side regardless of whether the UI's button was
    disabled, never trusting client state)."""
    if not test_grading_complete(test_id, snapshot):
        raise ValueError("not every free-response question has been graded yet")
    with _responses_transaction() as data:
        by_user = data.setdefault(test_id, {})
        count = 0
        for username, r in list(by_user.items()):
            if r.status not in ("submitted", "auto_submitted_late"):
                continue
            by_user[username] = replace(r, released=True, released_at=_now_iso(), released_by=released_by)
            count += 1
    return count
