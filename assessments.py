"""
Season-long assessment administration: AssessmentWindow (a scheduled open/close span
covering one or more events), Assessment (one event's assessment within one window —
built by volunteers, published as a frozen snapshot, then administered),
and Response (one student's in-progress/submitted answers + grading for
one Assessment).

AssessmentWindow and Assessment are stored as flat JSON files at DATA_ROOT (matches
auth.py's/seasons.py's one-concept-per-file convention) — low write volume,
no concurrent-autosave pattern, so one file/lock each is plenty. Response
is different: it's by far the highest-write-volume data in this module
(every student's autosave on every MCQ click/matching pick during a live
window, no debounce), and a single combined file/lock for every response
of every assessment ever measured catastrophically under load (super-linear
latency growth with concurrent students — see loadtest_students.py and
README.md's "Measuring server capacity"). So Response gets its own,
finer-grained scheme: one file per (assessment_id, username) pair, under
DATA_ROOT/assessment_responses/<assessment_id>/<username>.json — concurrent saves from
different students, or the same student on different assessments, now acquire
different locks and touch different (small) files instead of all
serialising through one. All storage still shares the same atomic
tempfile+os.replace write helper and a lock-registry keyed by path,
mirroring build_question_bank.py's per-event _state_locks pattern —
just applied at a per-pair path instead of a per-file one for responses.

An Assessment is prepared from one event's question bank but is NOT gated by
`auth.User.events` (a volunteer's bank-edit access) — assessment-preparation
assignment is a separate grant, keyed on AssessmentWindow.assignments, enforced
by review_app.py's `_select_assessment()` guard, deliberately independent of
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

from text_utils import parse_answer_letters

REPO_ROOT = Path(__file__).parent
DATA_ROOT = Path(os.environ.get("DATA_ROOT") or REPO_ROOT)
WINDOWS_FILE = DATA_ROOT / "assessment_windows.json"
ASSESSMENTS_FILE = DATA_ROOT / "assessments.json"
RESPONSES_DIR = DATA_ROOT / "assessment_responses"
# Pre-redesign storage: one combined file for every response of every
# assessment ever (see module docstring). No longer written to —
# migrate_legacy_responses() reads it once at startup to backfill
# RESPONSES_DIR, then renames it out of the way. Kept as a constant (not
# inlined) so tests can monkeypatch it.
#
# Deliberately still spelled "test_responses.json": this is a historical
# filename on disk, not a concept. It predates the Test -> Assessment
# rename and no file was ever written under an "assessment_" name at this
# path, so renaming the constant would point the migration at something
# that has never existed and silently strand every pre-refactor response.
_LEGACY_RESPONSES_FILE = DATA_ROOT / "test_responses.json"

SCHEMA_VERSION = 1

# Synthetic "question number" a build assessment's single manual grade is
# keyed under in Response.manual_grade — a build assessment has no
# questions, but manual_grade/set_manual_grade/release_grades all key by
# question number, and reusing that storage (rather than inventing a
# parallel one) is what keeps release/Scores/grading-permission checks
# working unchanged for both kinds. Real question numbers are strings of
# digits (see build_question_bank.py); "__build__" can never collide with
# one.
BUILD_GRADE_KEY = "__build__"

_lock_registry: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


def _response_path(assessment_id: str, username: str) -> Path:
    # assessment_id is always uuid.uuid4().hex (see _ensure_assessment below) and
    # username is validated at account-creation time against auth.py's
    # _USERNAME_RE (^[a-z][a-z0-9_]{1,31}$) — both are already filesystem-
    # safe with no path-separator/traversal characters possible, so no
    # escaping is needed here.
    return RESPONSES_DIR / assessment_id / f"{username}.json"


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
def _assessments_transaction():
    """Same as _windows_transaction(), for ASSESSMENTS_FILE."""
    with _lock_for(ASSESSMENTS_FILE):
        raw = _load_json_unlocked(ASSESSMENTS_FILE, {})
        assessments = {tid: _dict_to_assessment(d) for tid, d in raw.items()}
        yield assessments
        _save_json_unlocked(ASSESSMENTS_FILE, {tid: _assessment_to_dict(t) for tid, t in assessments.items()})


class _ResponseBox:
    """Mutable single-slot holder yielded by _response_transaction() — a
    plain dict-of-dicts (the old _responses_transaction()'s shape) doesn't
    make sense once there's exactly one Response in scope per transaction.
    Read the current value via `box.value` (None if this pair has no
    response yet); assign `box.value = new_response` to have it persisted
    when the block exits normally."""
    __slots__ = ("value",)

    def __init__(self, value: "Response | None"):
        self.value = value


@contextlib.contextmanager
def _response_transaction(assessment_id: str, username: str):
    """Same load -> mutate -> save-under-one-lock shape as
    _windows_transaction()/_assessments_transaction(), but scoped to one
    (assessment_id, username) pair's own file — the whole point of the
    per-pair redesign (see module docstring) is that two different
    students, or the same student on two different assessments, never share a
    lock or a file, so concurrent autosave load no longer serialises
    globally the way the old single RESPONSES_FILE design did. Mutators
    that need to read-then-merge (save_answer's answers dict,
    set_manual_grade's manual_grade dict) still do the read and the write
    inside the SAME transaction, not via a separate get_response() call
    followed by a save — composing two transactions would still reopen a
    lost-update race, just now scoped to one pair instead of the whole
    file.

    Always re-saves box.value at exit if it's not None, even if the caller
    only read and returned early without reassigning it (e.g.
    start_or_get_response's "already exists" path) — matches the other
    two transactions' unconditional-save-at-exit behavior exactly, just
    now rewriting one small per-pair file instead of every response in
    the app on every call."""
    path = _response_path(assessment_id, username)
    with _lock_for(path):
        raw = _load_json_unlocked(path, None)
        box = _ResponseBox(_dict_to_response(raw) if raw is not None else None)
        yield box
        if box.value is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            _save_json_unlocked(path, _response_to_dict(box.value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# AssessmentWindow — a scheduled open/close span (may run a few days, not
# necessarily one) covering one or more events for one season.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssessmentWindow:
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


def _window_to_dict(w: AssessmentWindow) -> dict:
    return {
        "window_id": w.window_id, "season_id": w.season_id, "label": w.label,
        "opens_at": w.opens_at, "closes_at": w.closes_at,
        "event_slugs": list(w.event_slugs), "assignments": dict(w.assignments),
        "archived": w.archived, "created_at": w.created_at, "created_by": w.created_by,
    }


def _dict_to_window(d: dict) -> AssessmentWindow:
    return AssessmentWindow(
        window_id=d["window_id"], season_id=d.get("season_id", ""), label=d.get("label", ""),
        opens_at=d.get("opens_at", ""), closes_at=d.get("closes_at", ""),
        event_slugs=tuple(d.get("event_slugs") or ()), assignments=dict(d.get("assignments") or {}),
        archived=bool(d.get("archived", False)), created_at=d.get("created_at", ""),
        created_by=d.get("created_by", ""),
    )


def load_windows() -> dict[str, AssessmentWindow]:
    raw = _load_json(WINDOWS_FILE, {})
    return {wid: _dict_to_window(d) for wid, d in raw.items()}


def get_window(window_id: str) -> AssessmentWindow | None:
    return load_windows().get(window_id)


def create_window(season_id: str, opens_at: str, closes_at: str,
                   event_slugs: list[str], label: str = "", created_by: str = "") -> AssessmentWindow:
    """Validates event_slugs is a subset of the season's lineup and
    opens_at < closes_at, then creates the window and lazily creates one
    Assessment per event (status "preparing", kept=[])."""
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
    window = AssessmentWindow(
        window_id=window_id, season_id=season_id, label=label,
        opens_at=opens_at, closes_at=closes_at, event_slugs=tuple(event_slugs),
        created_at=_now_iso(), created_by=created_by,
    )
    with _windows_transaction() as windows:
        windows[window_id] = window
    for slug in event_slugs:
        _ensure_assessments_for_event(window_id, season_id, slug, created_by)
    return window


def update_window(window_id: str, label: str | None = None, opens_at: str | None = None,
                   closes_at: str | None = None, event_slugs: list[str] | None = None) -> AssessmentWindow:
    """Edit a window's fields. Adding an event_slug lazily creates its Assessment;
    removing one does NOT delete its Assessment record (it just stops appearing
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
        _ensure_assessments_for_event(window_id, existing.season_id, slug, existing.created_by)
    return updated


def update_window_assignments(window_id: str, event_slug: str, usernames: list[str]) -> AssessmentWindow:
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
# Assessment — one event's assessment within one window.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Assessment:
    assessment_id: str
    window_id: str
    season_id: str
    event_slug: str
    status: str = "preparing"     # preparing|published|live|closed|graded|released
    # "exam" (the original, question-bank-built assessment) or "build" (a
    # coach-graded build event — bridge, rocket, robot, ... — with no
    # questions to publish/take, see the module docstring's Build events
    # section). Independent of `status`'s legacy "building" value below —
    # that was a status string renamed to "preparing" years before `kind`
    # existed, and nothing here ever infers one from the other.
    kind: str = "exam"
    # Build-only: the season-specific scoring rubric a coach defines by
    # hand — never a formula (see set_build_grade/compute_build_total).
    # Each line: {"id", "kind": "scored"|"measured", "label",
    # "max_points" (scored only), "unit" (measured only, optional)}.
    # Always [] for an exam assessment.
    rubric: list = field(default_factory=list)
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


def _assessment_to_dict(t: Assessment) -> dict:
    return {
        "assessment_id": t.assessment_id, "window_id": t.window_id, "season_id": t.season_id,
        "event_slug": t.event_slug, "status": t.status, "kind": t.kind,
        "rubric": [dict(line) for line in t.rubric], "kept": list(t.kept),
        "snapshot": t.snapshot, "snapshot_contexts": dict(t.snapshot_contexts),
        "overrides": dict(t.overrides),
        "published_at": t.published_at, "published_by": t.published_by,
        "live_at": t.live_at, "live_by": t.live_by,
        "last_edited_by": t.last_edited_by, "last_edited_at": t.last_edited_at,
        "created_at": t.created_at, "created_by": t.created_by,
    }


def _dict_to_assessment(d: dict) -> Assessment:
    # Lazy migration: older assessments.json records may still say "building" (the
    # status was renamed to "preparing" — "build" is Sci-Oly jargon for
    # building *events*, which confused volunteers when reused for assessments).
    # Normalizing here means any such record reads as "preparing" right away
    # and gets rewritten on its next save, with no separate migration script.
    status = d.get("status", "preparing")
    if status == "building":
        status = "preparing"
    return Assessment(
        # Records written before the Test -> Assessment rename carry
        # "test_id". Read either, exactly like the "building" status above:
        # the record reads correctly straight away and is rewritten with the
        # current key on its next save, so no separate migration script and
        # no flag day. Renaming the FILES alone was not enough — the field
        # inside each record needed this too.
        assessment_id=d.get("assessment_id") or d["test_id"],
        window_id=d.get("window_id", ""), season_id=d.get("season_id", ""),
        event_slug=d.get("event_slug", ""), status=status,
        # `kind` is a wholly separate field from the `status` migration
        # above — deliberately read straight from the record with no
        # cross-referencing of `status` at all, so a legacy "building"
        # status record (an exam, always) can never be misread as a build
        # assessment just because the two happen to share the word "build".
        kind=d.get("kind") or "exam",
        rubric=[dict(line) for line in (d.get("rubric") or [])],
        kept=list(d.get("kept") or []), snapshot=d.get("snapshot"),
        snapshot_contexts=dict(d.get("snapshot_contexts") or {}),
        overrides=dict(d.get("overrides") or {}),
        published_at=d.get("published_at"), published_by=d.get("published_by"),
        live_at=d.get("live_at"), live_by=d.get("live_by"),
        last_edited_by=d.get("last_edited_by"), last_edited_at=d.get("last_edited_at"),
        created_at=d.get("created_at", ""), created_by=d.get("created_by", ""),
    )


def load_assessments() -> dict[str, Assessment]:
    raw = _load_json(ASSESSMENTS_FILE, {})
    return {tid: _dict_to_assessment(d) for tid, d in raw.items()}


def get_assessment(assessment_id: str) -> Assessment | None:
    return load_assessments().get(assessment_id)


def get_assessment_for(window_id: str, event_slug: str, kind: str = "exam") -> Assessment | None:
    """One (window, event) pair can now hold up to two Assessments — an
    "exam" and a "build" — since an event may have both study material and
    a build component (see Event.has_build). `kind` defaults to "exam" so
    every pre-existing caller keeps resolving exactly the assessment it did
    before this parameter existed."""
    for t in load_assessments().values():
        if t.window_id == window_id and t.event_slug == event_slug and t.kind == kind:
            return t
    return None


def assessments_for_window(window_id: str) -> list[Assessment]:
    return [t for t in load_assessments().values() if t.window_id == window_id]


def _ensure_assessment(window_id: str, season_id: str, event_slug: str, created_by: str = "",
                        kind: str = "exam") -> Assessment:
    """Lazily creates an Assessment for (window_id, event_slug, kind) if one
    doesn't already exist — never overwrites an existing Assessment
    (re-adding an event that already has an Assessment, e.g. after it was
    removed and re-added to a window, must not wipe out a test someone
    already built). `kind` defaults to "exam" so every pre-existing caller
    creates exactly the assessment it did before this parameter existed;
    see _ensure_assessments_for_event for how a build assessment gets
    created alongside it."""
    # Fast-path check outside the lock (the common case: the test already
    # exists, so no write is needed). Re-checked inside the transaction
    # below to close the race where two threads both see "doesn't exist yet"
    # and would otherwise create two Assessment records for the same pair.
    existing = get_assessment_for(window_id, event_slug, kind)
    if existing is not None:
        return existing
    with _assessments_transaction() as assessments:
        existing = next((t for t in assessments.values()
                         if t.window_id == window_id and t.event_slug == event_slug and t.kind == kind), None)
        if existing is not None:
            return existing
        assessment_id = uuid.uuid4().hex
        t = Assessment(assessment_id=assessment_id, window_id=window_id, season_id=season_id, event_slug=event_slug,
                  kind=kind, created_at=_now_iso(), created_by=created_by)
        assessments[assessment_id] = t
        return t


def _ensure_assessments_for_event(window_id: str, season_id: str, event_slug: str,
                                   created_by: str = "") -> None:
    """Ensures the "exam" Assessment for (window, event) exists, exactly as
    every window always has — plus a "build" Assessment too when the event
    has a build component (Event.has_build). An event can carry both a
    study-material assessment and a build assessment in the same window; a
    coach schedules both just by adding the event to a window, same as
    today, with no separate "schedule a build" step."""
    _ensure_assessment(window_id, season_id, event_slug, created_by, kind="exam")
    import events as events_mod

    ev = events_mod.EVENTS.get(event_slug)
    if ev is not None and ev.has_build:
        _ensure_assessment(window_id, season_id, event_slug, created_by, kind="build")


def update_assessment_kept(assessment_id: str, kept: list, edited_by: str = "") -> Assessment:
    """Autosave for the test-builder's persistent kept-set. Rejects once the
    test is no longer "preparing" (published/live and beyond) — edits past
    that point must go through the explicit unpublish exception path."""
    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown test {assessment_id!r}")
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
        assessments[assessment_id] = updated
    return updated


# ---------------------------------------------------------------------------
# Build-event rubric — see the module docstring's "Build events" note and
# CLAUDE.md/spec.md for the full design. The app deliberately never
# computes a scoring formula: a coach defines rubric *lines* (scored or
# measured), and the total is always just the sum of the scored lines'
# values, unless a manual override is set. There is no per-event code path
# and nothing here changes when a season's scoring rules change.
# ---------------------------------------------------------------------------

def _clean_rubric(rubric: list) -> list:
    """Validates and normalizes a rubric line list. Each line must be
    "scored" (contributes to the total; requires a positive max_points) or
    "measured" (recorded, shown, and carried on every response, but NEVER
    summed — see compute_build_total). A missing/blank id is assigned here
    so the caller (the rubric editor) doesn't have to invent one for a
    brand-new line."""
    cleaned = []
    seen_ids: set[str] = set()
    for line in rubric:
        kind = line.get("kind")
        if kind not in ("scored", "measured"):
            raise ValueError(f"rubric line kind must be 'scored' or 'measured', got {kind!r}")
        label = (line.get("label") or "").strip()
        if not label:
            raise ValueError("every rubric line needs a label")
        line_id = (line.get("id") or "").strip() or uuid.uuid4().hex[:12]
        if line_id in seen_ids:
            raise ValueError(f"duplicate rubric line id {line_id!r}")
        seen_ids.add(line_id)
        entry = {"id": line_id, "kind": kind, "label": label}
        if kind == "scored":
            try:
                max_points = float(line.get("max_points"))
            except (TypeError, ValueError):
                raise ValueError(f"rubric line {label!r} needs a numeric max_points")
            if max_points <= 0:
                raise ValueError(f"rubric line {label!r}'s max_points must be > 0")
            entry["max_points"] = max_points
        else:
            unit = (line.get("unit") or "").strip()
            if unit:
                entry["unit"] = unit
        cleaned.append(entry)
    return cleaned


def set_assessment_rubric(assessment_id: str, rubric: list, edited_by: str = "") -> Assessment:
    """Replaces a build assessment's rubric wholesale — the rubric editor
    always sends the full line list back, same shape as
    update_assessment_kept for an exam's kept-set. Blocked once grades have
    been released: changing the definition of "scored" after students have
    seen a final number would silently invalidate what was already shown."""
    cleaned = _clean_rubric(rubric)
    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown assessment {assessment_id!r}")
        if existing.kind != "build":
            raise ValueError("only a build assessment has a rubric")
        if existing.status == "released":
            raise ValueError("grades are already released — can't change the rubric now")
        updated = replace(existing, rubric=cleaned, last_edited_by=edited_by, last_edited_at=_now_iso())
        assessments[assessment_id] = updated
    return updated


def copy_rubric_from(assessment_id: str, source_assessment_id: str, edited_by: str = "") -> Assessment:
    """Populates a build assessment's rubric from another build assessment's
    — "copy rubric from last year's window" — rather than a coach retyping
    an unchanged rubric every season. Line ids are copied as-is: they're
    scoped per-assessment (Response.rubric_values is keyed per
    assessment_id), so reusing them across two different assessments never
    collides."""
    source = get_assessment(source_assessment_id)
    if source is None:
        raise ValueError(f"unknown source assessment {source_assessment_id!r}")
    if source.kind != "build":
        raise ValueError("source assessment is not a build assessment")
    return set_assessment_rubric(assessment_id, [dict(line) for line in source.rubric], edited_by=edited_by)


def compute_build_total(rubric: list, rubric_values: dict, override: float | None) -> float | None:
    """The entire scoring calculation for a build assessment: sum the
    "scored" lines' recorded values, unless a manual override is set, in
    which case the override wins outright. "measured" lines never
    contribute — they're the raw record (mass, load, elapsed time, ...) a
    coach keeps for comparing across a season, not inputs to a formula this
    app computes. Returns None only when there is nothing to total yet (no
    override and no scored lines) — the no-rubric case must always be
    scored via an override, see set_build_grade."""
    if override is not None:
        return float(override)
    scored = [line for line in rubric if line.get("kind") == "scored"]
    if not scored:
        return None
    return sum(float((rubric_values or {}).get(line["id"], 0) or 0) for line in scored)


def build_rubric_possible(rubric: list) -> float:
    """Sum of every "scored" line's max_points — the default denominator
    for a build assessment's total. 0 when the rubric has no scored lines
    (the no-rubric case), in which case the caller must supply an explicit
    override_max instead."""
    return sum(float(line.get("max_points") or 0) for line in rubric if line.get("kind") == "scored")


def set_build_grade(assessment_id: str, student_username: str, rubric_values: dict | None = None,
                    override: float | None = None, override_max: float | None = None,
                    graded_by: str = "", comment: str = "") -> Response:
    """Records one student's build-event grade. There is no "take" step for
    a build assessment, so unlike set_manual_grade this also creates the
    Response if one doesn't exist yet — the first time a coach records a
    grade for a rostered student IS how their Response comes to exist.
    Status is set to "submitted" (not a new value) so this response is
    picked up by release_grades' existing "submitted or auto_submitted_late"
    filter with no changes there.

    The total is computed by compute_build_total — sum of the rubric's
    scored lines, or the override if one is given — and stored as the
    authoritative manual_grade[BUILD_GRADE_KEY], exactly the shape
    set_manual_grade already uses for an exam FRQ. rubric_values is stored
    alongside it, never summed by anything downstream."""
    test = get_assessment(assessment_id)
    if test is None:
        raise ValueError(f"unknown assessment {assessment_id!r}")
    if test.kind != "build":
        raise ValueError("not a build assessment")
    rubric_values = dict(rubric_values or {})
    earned = compute_build_total(test.rubric, rubric_values, override)
    if earned is None:
        raise ValueError("this assessment has no scored rubric lines — enter an override score")
    possible = build_rubric_possible(test.rubric)
    if override is not None and override_max is not None:
        possible = float(override_max)
    elif not test.rubric:
        # No-rubric case: build_rubric_possible() is 0 with nothing to sum,
        # so a max must come from the coach explicitly — this is the
        # "single score per student" path the app must support with zero
        # rubric setup.
        if override_max is None:
            raise ValueError("this assessment has no rubric — enter a max points value with the score")
        possible = float(override_max)
    if possible <= 0:
        raise ValueError("max points must be greater than 0")

    with _response_transaction(assessment_id, student_username) as box:
        resp = box.value
        if resp is None:
            resp = Response(student_username=student_username, assessment_id=assessment_id,
                            status="submitted", started_at=_now_iso(), last_saved_at=_now_iso(),
                            submitted_at=_now_iso())
        manual_grade = dict(resp.manual_grade)
        manual_grade[BUILD_GRADE_KEY] = {
            "points_earned": earned, "points_possible": possible,
            "graded_by": graded_by, "graded_at": _now_iso(), "comment": comment,
        }
        updated = replace(resp, manual_grade=manual_grade, rubric_values=rubric_values,
                          status="submitted", last_saved_at=_now_iso())
        box.value = updated

    # Status flow for a build assessment is scheduled(preparing) -> graded
    # -> released, reusing the existing vocabulary rather than inventing
    # new values (see the module docstring). This is the "graded" leg:
    # once every rostered student has a recorded grade, flip out of
    # "preparing" automatically -- there's no separate "mark as graded"
    # button to click, since completeness is exactly this same condition
    # the grading page's release button already gates on.
    if assessment_grading_complete(assessment_id, [], kind="build",
                                   season_id=test.season_id, event_slug=test.event_slug):
        with _assessments_transaction() as assessments:
            current = assessments.get(assessment_id)
            if current is not None and current.status == "preparing":
                assessments[assessment_id] = replace(current, status="graded")
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
        choices = list(q.get("choices") or [])
        answer = q.get("answer", "")
        entry["choices"] = choices
        entry["correct_answer"] = answer
        if qtype == "mcq":
            # Safe to send to students unsanitized (see api_take_assessment):
            # it only says HOW MANY choices to pick, never WHICH ones are
            # correct. Derived from the answer having more than one letter --
            # never set for tf/frq/matching, and never set for a prose MCQ
            # answer (parse_answer_letters returns empty for those, same as
            # a genuinely single-answer one -- both render as single-select).
            entry["select_multiple"] = len(parse_answer_letters(answer, choices)) > 1
    return entry


def publish_assessment(assessment_id: str, published_by: str = "") -> dict:
    """Builds `snapshot`/`snapshot_contexts` from the live question bank for
    every kept question, sets status="published". A kept question deleted
    from the bank since being kept is skipped (not a hard failure) — the
    publish still succeeds with whatever could be resolved, and the caller
    is told what got skipped so it can be surfaced as a toast.

    Returns {"test": Assessment, "skipped": [{"bucket","number"}]}.
    """
    import build_question_bank as bqb

    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown test {assessment_id!r}")
        if existing.kind == "build":
            raise ValueError("build assessments have no questions to publish — "
                             "record scores on the grading page instead")
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
        ungradeable: list[dict] = []
        for item in existing.kept:
            bucket, number = item.get("bucket", ""), str(item.get("number", ""))
            bank_q = next((q for q in (questions_by_bucket.get(bucket) or [])
                           if str(q.get("number")) == number), None)
            if bank_q is None:
                skipped.append({"bucket": bucket, "number": number})
                continue
            # Non-blocking backstop: the real defence is the verification gate
            # (build_question_bank.question_gradeability, enforced at every
            # place a question can be marked validation.status="correct" —
            # see api_patch_question and api_import_generated in review_app.py).
            # This just warns, deliberately — a coach can legitimately keep a
            # question they intend to grade by hand (an FRQ with no reference
            # answer yet, say), and hard-blocking publish over that would be
            # the wrong trade. Includes a kept question regardless of its
            # current validation status: an ungradeable question that was
            # never marked "correct" at all (e.g. added straight to a test
            # without going through Validate) is just as silently unscoreable
            # once published, and the coach should hear about it either way.
            gradeable, reason = bqb.question_gradeability(bank_q)
            if not gradeable:
                ungradeable.append({"bucket": bucket, "number": number, "reason": reason})
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
        assessments[assessment_id] = updated
    return {"test": updated, "skipped": skipped, "ungradeable": ungradeable}


# ---------------------------------------------------------------------------
# Markdown rendering — printing a test (and its key) to administer by hand
#
# Pure functions over a snapshot list, deliberately taking no Assessment/Window
# object and touching no storage, so they are testable without building a
# season and can be reused by anything holding questions in snapshot shape
# (the Browse page's markdown export renders the same layouts).
# ---------------------------------------------------------------------------

#: How many blank lines to leave under a free-response question when
#: printing the paper version. Enough to actually write in; the key gets
#: none, since nobody writes on a key.
_FRQ_ANSWER_LINES = 4


def _md_escape_leading(text: str) -> str:
    """Stop a question that happens to begin with '#' or '-' from turning
    into a heading or list item when the markdown is rendered."""
    stripped = (text or "").lstrip()
    if stripped[:1] in ("#", "-", "*", ">", "|", "+"):
        return "\\" + stripped
    return text or ""


def _render_question(q: dict, index: int, *, include_answers: bool) -> list[str]:
    lines: list[str] = []
    pts = q.get("max_points", 1)
    unit = "pt" if pts == 1 else "pts"
    lines.append(f"**{index}.** ({pts} {unit}) {_md_escape_leading(q.get('text', ''))}")
    lines.append("")

    # Images can't be inlined into a markdown file that has to survive being
    # emailed or pasted somewhere — name them so whoever assembles the paper
    # copy knows which figure belongs where, and can pull it from the
    # event's images/ directory.
    for fname in q.get("images") or []:
        desc = (q.get("image_descriptions") or {}).get(fname, "")
        lines.append(f"> *[figure: `{fname}`{' — ' + desc if desc else ''}]*")
        lines.append("")

    qtype = q.get("qtype") or "frq"
    if qtype == "matching":
        matching = q.get("matching") or {}
        left = matching.get("left") or []
        right = matching.get("right") or []
        for item in left:
            lines.append(f"- {item.get('label', '')}. {item.get('text', '')} ______")
        lines.append("")
        for item in right:
            lines.append(f"    {item.get('label', '')}. {item.get('text', '')}")
        lines.append("")
        if include_answers:
            pairs = matching.get("pairs") or {}
            joined = ", ".join(f"{k}\u2192{v}" for k, v in sorted(pairs.items()))
            lines.append(f"**Answer:** {joined or '(no key recorded)'}")
            lines.append("")
    elif qtype == "tf":
        # Without this branch a tf item (choices: []) silently falls through
        # to the FRQ blank-lines branch below.
        lines.append("True / False ______")
        lines.append("")
        if include_answers:
            lines.append(f"**Answer:** {q.get('correct_answer') or '(no key recorded)'}")
            lines.append("")
    elif q.get("choices"):
        for choice in q["choices"]:
            lines.append(f"- **{choice.get('letter', '?')}.** {choice.get('text', '')}")
        lines.append("")
        if include_answers:
            lines.append(f"**Answer:** {q.get('correct_answer') or '(no key recorded)'}")
            lines.append("")
    else:
        if include_answers:
            lines.append(f"**Answer:** {q.get('correct_answer') or '(no key recorded)'}")
            lines.append("")
        else:
            lines.extend(["" for _ in range(_FRQ_ANSWER_LINES)])
    return lines


def render_questions_markdown(snapshot: list, *, title: str, subtitle: str = "",
                              answers: str = "none") -> str:
    """Render questions as markdown.

    `answers` chooses the layout, which is the whole difference between a
    student copy and something a grader can mark from:
      "none"    -- questions only (the test)
      "inline"  -- each answer immediately under its question (grouped)
      "section" -- questions first, then a separate Answer Key section, so
                   the same document is both the test and the key without
                   spoiling it on the way down
    """
    if answers not in ("none", "inline", "section"):
        raise ValueError(f"unknown answers layout: {answers!r}")

    out: list[str] = [f"# {title}", ""]
    if subtitle:
        out.extend([subtitle, ""])

    contexts_seen: set = set()
    for i, q in enumerate(snapshot, start=1):
        # A shared case-study passage is printed once, above the first
        # question that uses it, rather than repeated under each.
        ctx = q.get("_context")
        ctx_id = q.get("context_id")
        if ctx and ctx_id and ctx_id not in contexts_seen:
            contexts_seen.add(ctx_id)
            out.append(f"> **{ctx.get('title') or 'Shared passage'}**")
            for line in (ctx.get("text") or "").splitlines():
                out.append(f"> {line}")
            out.append("")
        out.extend(_render_question(q, i, include_answers=(answers == "inline")))

    if answers == "section":
        out.extend(["---", "", "## Answer key", ""])
        for i, q in enumerate(snapshot, start=1):
            if (q.get("qtype") or "") == "matching":
                pairs = (q.get("matching") or {}).get("pairs") or {}
                val = ", ".join(f"{k}\u2192{v}" for k, v in sorted(pairs.items()))
            else:
                val = q.get("correct_answer") or ""
            out.append(f"{i}. {val or '(no key recorded)'}")
        out.append("")

    total = sum(float(q.get("max_points") or 0) for q in snapshot)
    out.extend(["---", "",
                f"*{len(snapshot)} question{'' if len(snapshot) == 1 else 's'}, "
                f"{total:g} point{'' if total == 1 else 's'} total.*", ""])
    return "\n".join(out)


def snapshot_for_render(test: "Assessment") -> tuple[list, bool]:
    """The question list to print, and whether it is a draft.

    A published test prints from its frozen `snapshot` -- printing from the
    live bank instead would let a paper key disagree with what students
    actually saw, which is the one error nobody would catch until it was
    being graded. A test still being prepared has no snapshot, so it is
    resolved from `kept` against the live bank and flagged as a draft for
    the caller to stamp on the page.
    """
    if test.snapshot:
        snapshot = [dict(q) for q in test.snapshot]
        contexts = test.snapshot_contexts or {}
        for q in snapshot:
            ctx_id = q.get("context_id")
            if ctx_id:
                q["_context"] = contexts.get(f"{q.get('bucket')}::{ctx_id}")
        return snapshot, False

    import build_question_bank as bqb
    bqb.set_event(test.event_slug)
    state = bqb._load_state()
    questions_by_bucket = state.get("questions", {})
    contexts = bqb._all_contexts()
    snapshot = []
    for item in test.kept:
        bucket, number = item.get("bucket", ""), str(item.get("number", ""))
        bank_q = next((q for q in (questions_by_bucket.get(bucket) or [])
                       if str(q.get("number")) == number), None)
        if bank_q is None:
            continue
        entry = _snapshot_one_question(bank_q, bucket, float(item.get("max_points") or 1))
        ctx_id = entry.get("context_id")
        if ctx_id:
            entry["_context"] = contexts.get(f"{bucket}::{ctx_id}")
        snapshot.append(entry)
    return snapshot, True


def unpublish_assessment(assessment_id: str) -> Assessment:
    """Reverts a published/live test back to "preparing" for edits. Caller
    (review_app.py's route) is responsible for the guardrail checks (window
    not yet open, no saved responses) before calling this — this function
    itself only enforces the status precondition, not the timing/response
    guardrails, since those need the AssessmentWindow and Response data this
    module-level function isn't handed."""
    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown test {assessment_id!r}")
        if existing.status not in ("published", "live"):
            raise ValueError(f"test is {existing.status!r}, not published/live")
        updated = replace(existing, status="preparing", snapshot=None, snapshot_contexts={},
                           published_at=None, published_by=None, live_at=None, live_by=None)
        assessments[assessment_id] = updated
    return updated


def go_live_assessment(assessment_id: str, live_by: str = "") -> Assessment:
    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown test {assessment_id!r}")
        if existing.kind == "build":
            raise ValueError("build assessments have nothing to serve students — "
                             "there is no 'go live' step for a build assessment")
        if existing.status != "published":
            raise ValueError(f"test is {existing.status!r}, must be 'published' first")
        updated = replace(existing, status="live", live_at=_now_iso(), live_by=live_by)
        assessments[assessment_id] = updated
    return updated


def set_assessment_overrides(assessment_id: str, student_username: str, opens_at: str | None,
                       closes_at: str | None, granted_by: str = "", reason: str = "") -> Assessment:
    """Upsert (opens_at/closes_at not None) or revoke (both None) a personal
    makeup-window override for one student on one test. A personal override
    is an INDEPENDENT clock from the class-wide window, not an extension of
    it — see effective_window()."""
    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown test {assessment_id!r}")
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
        assessments[assessment_id] = updated
    return updated


def set_assessment_overrides_bulk(assessment_id: str, student_usernames: list[str],
                                  opens_at: str | None, closes_at: str | None,
                                  granted_by: str = "", reason: str = "") -> Assessment:
    """Grant (or revoke) the same personal makeup window for several
    students at once.

    One transaction for the whole batch, not one per student: granting a
    class-wide makeup one student at a time would take the file lock N
    times, and a failure partway would leave some students granted and
    others not, with nothing to tell the coach which. Validating up front
    means the batch either applies completely or not at all.

    Like the single-student version, a personal override is an INDEPENDENT
    clock rather than an extension of the class window (see
    effective_window()).
    """
    usernames = [u.strip() for u in student_usernames if u and u.strip()]
    if not usernames:
        raise ValueError("no students selected")
    revoking = opens_at is None and closes_at is None
    if not revoking and (not opens_at or not closes_at or opens_at >= closes_at):
        raise ValueError("opens_at must be before closes_at")

    with _assessments_transaction() as assessments:
        existing = assessments.get(assessment_id)
        if existing is None:
            raise ValueError(f"unknown assessment {assessment_id!r}")
        overrides = dict(existing.overrides)
        stamp = _now_iso()
        for username in usernames:
            if revoking:
                overrides.pop(username, None)
            else:
                overrides[username] = {
                    "opens_at": opens_at, "closes_at": closes_at,
                    "granted_by": granted_by, "granted_at": stamp, "reason": reason,
                }
        updated = replace(existing, overrides=overrides)
        assessments[assessment_id] = updated
    return updated


def effective_window(test: Assessment, window: AssessmentWindow, username: str) -> tuple[str, str]:
    """A personal override, if one exists for this student on this test,
    wins outright over the class-wide window — independent clock, not an
    extension of it. Returns (opens_at, closes_at) as ISO8601 strings."""
    ov = test.overrides.get(username)
    if ov:
        return ov["opens_at"], ov["closes_at"]
    return window.opens_at, window.closes_at


# opens_at/closes_at are stored as absolute UTC instants — offset-aware ISO
# strings written by the browser (new Date(...).toISOString(), see
# assessments_dashboard.html's #nw_opens/#nw_closes/#mk_opens/#mk_closes
# handlers) rather than the naive local wall-clock text a <input
# type="datetime-local"> yields on its own. A naive value is genuinely
# ambiguous — "18:00" means something different depending on which zone
# wrote it — so it must never be silently reinterpreted as anything.


def is_window_open(test: Assessment, window: AssessmentWindow, username: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    opens_s, closes_s = effective_window(test, window, username)
    opens = datetime.fromisoformat(opens_s)
    closes = datetime.fromisoformat(closes_s)
    # Legacy fallback ONLY: a value written before the browser started
    # converting local -> UTC on entry (or on an instance that hasn't run
    # deploy/migrate_window_times_to_utc.py yet) is naive local wall-clock
    # text, not UTC — treating it as UTC here is wrong by the writer's own
    # timezone offset, which is exactly the "extended window shows as past"
    # bug this fallback used to cause unconditionally. It stays here only so
    # an unmigrated instance keeps functioning instead of crashing; run the
    # migration to eliminate this path rather than relying on it.
    if opens.tzinfo is None:
        opens = opens.replace(tzinfo=timezone.utc)
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return opens <= now <= closes


def is_window_past(test: Assessment, window: AssessmentWindow, username: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    _, closes_s = effective_window(test, window, username)
    closes = datetime.fromisoformat(closes_s)
    # Legacy fallback — see is_window_open's comment above; same caveat
    # applies here.
    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)
    return now > closes


# ---------------------------------------------------------------------------
# Response — one student's answers + grading for one Assessment.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    student_username: str
    assessment_id: str
    question_order: list = field(default_factory=list)   # indices into Assessment.snapshot, stored once, stable
    answers: dict = field(default_factory=dict)            # {number: {qtype, picked|text|picks}}
    auto_grade: dict = field(default_factory=dict)         # {number: {...,points_earned,points_possible}}
    manual_grade: dict = field(default_factory=dict)       # {number: {points_earned,points_possible,graded_by,graded_at,comment}}
    # Build-only: the coach's per-rubric-line raw values, {line_id: value}.
    # Never summed by anything — manual_grade[BUILD_GRADE_KEY].points_earned
    # (set by set_build_grade, from compute_build_total) is the single
    # authoritative total; this is the record kept alongside it because the
    # raw measurements (mass, load, ...) are what a coach actually wants
    # back when comparing across a season, not just the final number.
    # Always {} for an exam response.
    rubric_values: dict = field(default_factory=dict)
    status: str = "in_progress"     # in_progress|submitted|auto_submitted_late
    started_at: str = ""
    last_saved_at: str = ""
    submitted_at: str | None = None
    released: bool = False
    released_at: str | None = None
    released_by: str | None = None


def _response_to_dict(r: Response) -> dict:
    return {
        "student_username": r.student_username, "assessment_id": r.assessment_id,
        "question_order": list(r.question_order), "answers": dict(r.answers),
        "auto_grade": dict(r.auto_grade), "manual_grade": dict(r.manual_grade),
        "rubric_values": dict(r.rubric_values),
        "status": r.status, "started_at": r.started_at, "last_saved_at": r.last_saved_at,
        "submitted_at": r.submitted_at, "released": r.released,
        "released_at": r.released_at, "released_by": r.released_by,
    }


def _dict_to_response(d: dict) -> Response:
    return Response(
        student_username=d.get("student_username", ""),
        # Same pre-rename fallback as _dict_to_assessment.
        assessment_id=d.get("assessment_id") or d.get("test_id", ""),
        question_order=list(d.get("question_order") or []), answers=dict(d.get("answers") or {}),
        auto_grade=dict(d.get("auto_grade") or {}), manual_grade=dict(d.get("manual_grade") or {}),
        rubric_values=dict(d.get("rubric_values") or {}),
        status=d.get("status", "in_progress"), started_at=d.get("started_at", ""),
        last_saved_at=d.get("last_saved_at", ""), submitted_at=d.get("submitted_at"),
        released=bool(d.get("released", False)), released_at=d.get("released_at"),
        released_by=d.get("released_by"),
    )


# ---------------------------------------------------------------------------
# Rename migration: Test -> Assessment
#
# The storage names changed with the terminology (test_windows.json ->
# assessment_windows.json, tests.json -> assessments.json, test_responses/
# -> assessment_responses/). An existing instance already has data under
# the old names, and gunicorn imports review_app:app directly rather than
# calling main(), so this runs at module level from review_app for the same
# reason migrate_legacy_responses() does.
#
# Renames rather than copies, and only when the new name doesn't already
# exist, so it is idempotent and a second process starting concurrently
# can't duplicate anything. Never deletes: if both names somehow exist the
# old one is left exactly where it is for a human to reconcile, because
# picking a winner automatically is how you lose a season of responses.
# ---------------------------------------------------------------------------

_RENAMED_PATHS = [
    (DATA_ROOT / "test_windows.json", WINDOWS_FILE),
    (DATA_ROOT / "tests.json", ASSESSMENTS_FILE),
    (DATA_ROOT / "test_responses", RESPONSES_DIR),
]


def migrate_test_to_assessment_names() -> list[str]:
    """Move pre-rename storage to its current name. Returns what moved,
    for the caller to log; empty on the common case of nothing to do."""
    moved: list[str] = []
    for old, new in _RENAMED_PATHS:
        if not old.exists():
            continue
        if new.exists():
            # Both present — refuse rather than merge or overwrite.
            moved.append(f"SKIPPED {old.name}: {new.name} already exists, "
                         f"reconcile by hand")
            continue
        with _lock_for(new):
            if new.exists() or not old.exists():
                continue
            old.rename(new)
            moved.append(f"{old.name} -> {new.name}")
    return moved


def migrate_legacy_responses() -> int:
    """One-time, idempotent migration from the pre-redesign single-file
    _LEGACY_RESPONSES_FILE to the current per-(assessment_id, username) file
    layout. Meant to be called once at process startup — see
    review_app.py, called at module level right next to
    jobs.recover_interrupted_jobs() for the identical reason: gunicorn
    imports review_app:app directly and never calls main(), so anything
    that must run in production has to live at module level, before the
    app starts accepting requests.

    Returns 0 immediately once the legacy file is gone (the common case
    after the first successful run). Skips any pair whose new file
    already exists, so a partial or repeated run resumes rather than
    redoing or clobbering work. Never deletes the legacy file — renames
    it to "assessment_responses.json.migrated" once every record has been
    written out, so a mistake here is trivially recoverable by hand
    instead of a silent data-loss risk."""
    if not _LEGACY_RESPONSES_FILE.exists():
        return 0
    with _lock_for(_LEGACY_RESPONSES_FILE):
        if not _LEGACY_RESPONSES_FILE.exists():
            return 0
        raw = _load_json_unlocked(_LEGACY_RESPONSES_FILE, {})
        migrated = 0
        for assessment_id, by_user in raw.items():
            for username, resp_dict in by_user.items():
                path = _response_path(assessment_id, username)
                if path.exists():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                _save_json_unlocked(path, resp_dict)
                migrated += 1
        _LEGACY_RESPONSES_FILE.rename(
            _LEGACY_RESPONSES_FILE.with_name(_LEGACY_RESPONSES_FILE.name + ".migrated"))
    return migrated


def get_response(assessment_id: str, username: str) -> Response | None:
    raw = _load_json(_response_path(assessment_id, username), None)
    return _dict_to_response(raw) if raw is not None else None


def get_responses_for_assessment(assessment_id: str) -> dict[str, Response]:
    """Every student's response for one test — only ever reads that test's
    own subdirectory, never touches any other test's data (unlike the old
    design, where this necessarily loaded every response in the app)."""
    assessment_dir = RESPONSES_DIR / assessment_id
    if not assessment_dir.is_dir():
        return {}
    result: dict[str, Response] = {}
    for path in assessment_dir.glob("*.json"):
        raw = _load_json(path, None)
        if raw is not None:
            result[path.stem] = _dict_to_response(raw)
    return result


def _grouped_shuffle_order(snapshot: list, rng) -> list[int]:
    """Shuffle whole shared-context groups, not individual snapshot indices.

    A group is every snapshot item sharing the same (bucket, context_id) —
    keyed the same shape as build_question_bank._context_key
    (f"{bucket}::{context_id}"), just read from the snapshot's own "bucket"
    key rather than "_bucket" (the live-bank question dicts' field name).
    An item with no context_id is its own singleton group. Group order is
    shuffled; each group's own internal (snapshot) order is preserved, so
    a-then-b sub-questions ("same as Q5 but R1/R2 swapped") are never
    served out of order. `rng` is injectable (anything with a `.shuffle()`
    method — the `random` module itself, or a seeded `random.Random()`) so
    this stays unit-testable without touching storage.

    Pulled out of start_or_get_response() as a pure, module-level function
    for exactly that testability — see tests/test_question_groups.py."""
    groups: list[list[int]] = []
    by_key: dict[str, list[int]] = {}
    for idx, item in enumerate(snapshot):
        cid = (item or {}).get("context_id")
        key = f"{(item or {}).get('bucket', '')}::{cid}" if cid else None
        if key is None:
            groups.append([idx])
        else:
            bucket = by_key.get(key)
            if bucket is None:
                bucket = []
                by_key[key] = bucket
                groups.append(bucket)
            bucket.append(idx)
    rng.shuffle(groups)
    return [idx for group in groups for idx in group]


def start_or_get_response(assessment_id: str, username: str, snapshot: list) -> Response:
    """First call for a given (test, student) creates the Response with a
    freshly shuffled question_order, stored immediately so it never
    changes again for this student on this test — content stays identical
    for everyone, only display order is per-student and stable across
    reloads.

    Takes the test's full `snapshot` (not just a question count) so the
    shuffle can be group-preserving (see _grouped_shuffle_order) — a
    grouped sub-question served before its predecessor, or the shared
    context banner repeated at unrelated points in the test, would be a
    correctness bug on a graded assessment, not a cosmetic one. Unlike
    quiz.html's opt-in "keep groups together" checkbox, this is
    unconditional.

    The existence check and the create must happen inside one transaction
    (not get_response() followed by a separate save) — otherwise two
    near-simultaneous first-loads for the same student would each shuffle
    their own order and the second save would silently replace the first,
    leaving the student's already-rendered page out of sync with what's on
    disk."""
    import random

    with _response_transaction(assessment_id, username) as box:
        if box.value is not None:
            return box.value
        order = _grouped_shuffle_order(snapshot, random)
        r = Response(student_username=username, assessment_id=assessment_id, question_order=order,
                     started_at=_now_iso(), last_saved_at=_now_iso())
        box.value = r
        return r


def save_answer(assessment_id: str, username: str, number: str, answer_payload: dict) -> Response:
    """Merges one question's answer into the student's `answers` dict. Reads
    the existing response and writes the merged result inside the same
    transaction — composing a separate get_response() + save would still
    lose answers (two autosave requests close together would each merge
    into their own stale copy of `answers`, and the second save would wipe
    out whatever the first one added)."""
    with _response_transaction(assessment_id, username) as box:
        existing = box.value
        if existing is None:
            raise ValueError("no in-progress response — load the test first")
        if existing.status != "in_progress":
            raise ValueError(f"response is already {existing.status!r} — can't edit further")
        answers = dict(existing.answers)
        answers[str(number)] = answer_payload
        updated = replace(existing, answers=answers, last_saved_at=_now_iso())
        box.value = updated
    return updated


# ---------------------------------------------------------------------------
# Hard deletion (see deletion.py, which composes these into cascades)
#
# These are record-level primitives only: each removes exactly its own
# storage and nothing belonging to another module, so none of them needs an
# import this module doesn't already have. Cascade policy -- what a season
# owning windows actually means -- lives in deletion.py.
#
# Every "delete" the app itself exposes elsewhere is soft (archive/disable
# flags, annotation-recorded question deletes). These are the real thing,
# reachable only when ALLOW_HARD_DELETE is set; see README's "Hard delete".
# ---------------------------------------------------------------------------

def count_responses_for_assessment(assessment_id: str) -> int:
    """How many student responses exist for a test. Counts files rather
    than parsing them -- this only ever feeds a confirmation dialog."""
    assessment_dir = RESPONSES_DIR / assessment_id
    if not assessment_dir.is_dir():
        return 0
    return sum(1 for _ in assessment_dir.glob("*.json"))


def delete_response(assessment_id: str, username: str) -> bool:
    """Remove one student's response to one test, letting them start it
    over. Returns False if there was nothing there."""
    path = _response_path(assessment_id, username)
    with _lock_for(path):
        if not path.exists():
            return False
        path.unlink()
        return True


def delete_responses_for_assessment(assessment_id: str) -> int:
    """Remove every response to one test, and the test's response
    directory with them."""
    assessment_dir = RESPONSES_DIR / assessment_id
    if not assessment_dir.is_dir():
        return 0
    n = 0
    for path in list(assessment_dir.glob("*.json")):
        with _lock_for(path):
            if path.exists():
                path.unlink()
                n += 1
    try:
        assessment_dir.rmdir()
    except OSError:
        # Something unexpected is still in there -- leave it rather than
        # forcing; the responses themselves are gone either way.
        pass
    return n


def delete_responses_for_user(username: str) -> int:
    """Remove one student's responses across every test. Used when the
    account itself is being deleted, so their answers don't outlive them as
    unattributable files."""
    if not RESPONSES_DIR.is_dir():
        return 0
    n = 0
    for assessment_dir in RESPONSES_DIR.iterdir():
        if not assessment_dir.is_dir():
            continue
        path = assessment_dir / f"{username}.json"
        with _lock_for(path):
            if path.exists():
                path.unlink()
                n += 1
    return n


def delete_assessment_record(assessment_id: str) -> bool:
    """Remove the Assessment itself. Responses are NOT touched here -- callers go
    through deletion.py, which removes them first; deleting the test alone
    would orphan a directory nothing can name any more."""
    with _assessments_transaction() as assessments:
        if assessment_id not in assessments:
            return False
        del assessments[assessment_id]
        return True


def delete_window_record(window_id: str) -> bool:
    """Remove the AssessmentWindow itself. Its Assessments are NOT touched here -- see
    delete_assessment_record's note."""
    with _windows_transaction() as windows:
        if window_id not in windows:
            return False
        del windows[window_id]
        return True


def assessments_for_season(season_id: str) -> list[Assessment]:
    """Every Assessment belonging to a season, across all its windows."""
    return [t for t in load_assessments().values() if t.season_id == season_id]


def used_question_keys(season_id: str, exclude_assessment_id: str = "") -> set[str]:
    """`bucket::number` for every question already used by another test in
    this season, so the builder can keep a coach from unknowingly setting
    the same question twice in one year.

    Reads both `kept` and `snapshot`: `kept` is what a test still being
    prepared has, `snapshot` is what a published one froze, and a question
    counts as used either way. They overlap for a published test, which the
    set handles for free.

    Scoped to the season rather than to the event because that is how reuse
    is actually judged -- the same students sit every window in a season,
    and nothing about a new season makes last year's questions stale.
    """
    used: set[str] = set()
    for test in load_assessments().values():
        if test.season_id != season_id or test.assessment_id == exclude_assessment_id:
            continue
        for item in test.kept or []:
            used.add(f"{item.get('bucket','')}::{item.get('number','')}")
        for item in test.snapshot or []:
            used.add(f"{item.get('bucket','')}::{item.get('number','')}")
    return used


def windows_for_season(season_id: str) -> list[AssessmentWindow]:
    return [w for w in load_windows().values() if w.season_id == season_id]


def _grade_mcq(picked: str | None, correct_answer: str, choices: list | None = None,
               max_points: float = 1.0) -> dict:
    """Set-based grading: a multi-answer MCQ ("A, D, E") requires every
    correct letter picked and no incorrect ones -- all-or-nothing, no partial
    credit. This is a deliberate asymmetry with matching (which DOES give
    partial credit per pair, see _grade_matching): the user was offered
    partial credit for MCQ and declined it, so a student who gets 2 of 3
    letters right on a multi-answer MCQ scores zero, same as getting 0 of 3.

    Both sides route through parse_answer_letters() so "A", "A, B", "a,b"
    etc. are parsed identically. A single-letter correct_answer degrades to
    exactly today's behavior: the student's `picked` is always a single
    letter for a single-answer MCQ (assessment_take.html only allows
    multi-select when the published snapshot's `select_multiple` flag is
    set), so the set-equality check below reduces to the old one-letter
    compare.

    Falls back to the ORIGINAL first-character compare when correct_answer
    doesn't parse as letters (prose answers with units, etc. -- 23 of these
    exist in the bank today) -- deliberately bit-for-bit unchanged, bug and
    all, rather than "fixed" here too: those 23 questions must keep grading
    exactly as they do today, not according to some new guess at what a
    prose compare should mean.
    """
    correct_letters = parse_answer_letters(correct_answer, choices)
    if not correct_letters:
        correct_raw = (correct_answer or "").strip()
        ok = bool(picked) and picked.strip().upper() == correct_raw.upper()[:1]
        return {"correct": ok, "points_earned": max_points if ok else 0.0, "points_possible": max_points}
    picked_letters = parse_answer_letters(picked, choices)
    ok = bool(picked_letters) and picked_letters == correct_letters
    return {"correct": ok, "points_earned": max_points if ok else 0.0, "points_possible": max_points}


def _grade_tf(picked: str | None, correct_answer: str, max_points: float = 1.0) -> dict:
    """True/False grading. Normalizes BOTH sides through
    bqb._normalize_tf_answer — the one source of truth for what counts as
    "True"/"False" — rather than duplicating that parsing here. Deliberately
    does NOT route through _grade_mcq: that helper's `correct_raw.upper()[:1]`
    would accidentally half-work for T/F (both "True"/"T" start with "T"),
    but would mis-grade a bare "F" answer key against a "False" pick, etc."""
    import build_question_bank as bqb
    ok = (bool(picked)
          and bqb._normalize_tf_answer(picked) is not None
          and bqb._normalize_tf_answer(picked) == bqb._normalize_tf_answer(correct_answer))
    return {"correct": ok, "points_earned": max_points if ok else 0.0, "points_possible": max_points}


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


def submit_response(assessment_id: str, username: str, snapshot: list, now: datetime | None = None,
                    late: bool = False) -> Response:
    """Computes auto_grade for every MCQ/matching answer from the snapshot
    (never trusting client-side grading), sets status. FRQ items are left
    for manual grading (Part 5) — grading_status is derived on read from
    whether every FRQ has a manual_grade, not stored here."""
    with _response_transaction(assessment_id, username) as box:
        existing = box.value
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
                auto_grade[number] = _grade_mcq(answer.get("picked"), q.get("correct_answer", ""),
                                                q.get("choices"), float(q.get("max_points") or 1))
            elif q.get("qtype") == "tf":
                auto_grade[number] = _grade_tf(answer.get("picked"), q.get("correct_answer", ""),
                                               float(q.get("max_points") or 1))
            elif q.get("qtype") == "matching":
                auto_grade[number] = _grade_matching(q.get("matching") or {}, answer.get("picks") or {},
                                                     float(q.get("max_points") or 1))
            # frq: no auto-grade entry — graded manually (Part 5)
        updated = replace(existing, auto_grade=auto_grade,
                          status="auto_submitted_late" if late else "submitted",
                          submitted_at=_now_iso())
        box.value = updated
    return updated


def assessment_grading_complete(assessment_id: str, snapshot: list, *, kind: str = "exam",
                                season_id: str = "", event_slug: str = "") -> bool:
    """True iff grading is done.

    For an exam (`kind="exam"`, the default — every pre-existing caller
    keeps this behavior unchanged): every response with status in
    (submitted, auto_submitted_late) has a non-null
    manual_grade.points_earned for every FRQ in the snapshot.

    For a build assessment (`kind="build"`): a build assessment's snapshot
    is always empty, so the FRQ-counting rule above would trivially report
    "complete" with nothing graded. Completeness instead means every
    ROSTERED student (seasons.get_roster(season_id, event_slug) — there is
    no "submission" to check the status of) has a recorded
    manual_grade[BUILD_GRADE_KEY]. An empty roster is vacuously complete,
    matching the exam branch's "no FRQs -> complete" behavior above.

    Recomputed on read (cheap — bounded by roster size) rather than stored,
    to avoid a second source of truth that could drift."""
    if kind == "build":
        import seasons as seasons_mod

        roster = seasons_mod.get_roster(season_id, event_slug) if season_id and event_slug else []
        if not roster:
            return True
        responses = get_responses_for_assessment(assessment_id)
        for username in roster:
            r = responses.get(username)
            g = r.manual_grade.get(BUILD_GRADE_KEY) if r else None
            if not g or g.get("points_earned") is None:
                return False
        return True

    frq_numbers = [str(q.get("number")) for q in snapshot if q.get("qtype") == "frq"]
    if not frq_numbers:
        return True
    for r in get_responses_for_assessment(assessment_id).values():
        if r.status not in ("submitted", "auto_submitted_late"):
            continue
        for num in frq_numbers:
            g = r.manual_grade.get(num)
            if not g or g.get("points_earned") is None:
                return False
    return True


def set_manual_grade(assessment_id: str, student_username: str, number: str, points_earned: float,
                     max_points: float, graded_by: str = "", comment: str = "") -> Response:
    if not (0 <= points_earned <= max_points):
        raise ValueError(f"points_earned must be between 0 and {max_points}")
    with _response_transaction(assessment_id, student_username) as box:
        resp = box.value
        if resp is None:
            raise ValueError("no response on file for this student")
        manual_grade = dict(resp.manual_grade)
        manual_grade[str(number)] = {
            "points_earned": points_earned, "points_possible": max_points,
            "graded_by": graded_by, "graded_at": _now_iso(), "comment": comment,
        }
        updated = replace(resp, manual_grade=manual_grade)
        box.value = updated
    return updated


def release_grades(assessment_id: str, snapshot: list, released_by: str = "", *,
                   kind: str = "exam", season_id: str = "", event_slug: str = "") -> int:
    """Flips released=True (+released_at/released_by) on every submitted
    response for this test. Per-response storage (not a single Assessment-level
    flag) because "released" is fundamentally about what a student can see
    of THEIR OWN response — a per-response fact, even though every
    response for a test is released at once. Requires
    assessment_grading_complete() first; raises otherwise (the route re-checks
    this server-side regardless of whether the UI's button was disabled,
    never trusting client state).

    `kind`/`season_id`/`event_slug` default to the exam behavior every
    pre-existing caller relies on; pass kind="build" (with the assessment's
    season_id/event_slug) to use the roster-based completeness check
    instead — see assessment_grading_complete. A build response's status is
    "submitted" too (set by set_build_grade), so the per-response release
    loop below needs no changes at all for either kind.

    Unlike the old design, this can no longer hold one lock across every
    student's release at once — each (assessment_id, username) pair has its own
    file/lock now, which is the whole point (see module docstring). So
    this reads the qualifying usernames once, then re-checks each one's
    status fresh inside ITS OWN transaction before flipping the flag —
    that per-student re-check is what keeps the same guarantee the old
    single big lock gave for free: nothing here acts on a status that's
    gone stale between the initial scan and that student's own lock."""
    if not assessment_grading_complete(assessment_id, snapshot, kind=kind,
                                       season_id=season_id, event_slug=event_slug):
        raise ValueError("not every free-response question has been graded yet" if kind != "build"
                         else "not every rostered student has a recorded grade yet")
    count = 0
    for username, r in get_responses_for_assessment(assessment_id).items():
        if r.status not in ("submitted", "auto_submitted_late"):
            continue
        with _response_transaction(assessment_id, username) as box:
            current = box.value
            if current is None or current.status not in ("submitted", "auto_submitted_late"):
                continue
            box.value = replace(current, released=True, released_at=_now_iso(), released_by=released_by)
            count += 1
    if kind == "build" and count > 0:
        # The "released" leg of the build status flow (see set_build_grade's
        # "graded" leg) -- purely a display-facing transition, same as
        # "graded"; the actual access-control fact students are gated on is
        # each response's own `released` flag flipped just above.
        with _assessments_transaction() as assessments:
            current = assessments.get(assessment_id)
            if current is not None and current.status in ("preparing", "graded"):
                assessments[assessment_id] = replace(current, status="released")
    return count
