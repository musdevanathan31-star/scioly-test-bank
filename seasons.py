"""
Season-long competition-season tracking: one "current" season at a time
(year-labeled, e.g. "2027"), each declaring its own event lineup, plus a
per-season student roster (which students compete in which of that season's
events).

A season's event lineup (`Season.event_slugs`) is **purely a roster/
scheduling scoping concept** — it controls which event columns appear on
the Club Management roster grid and which events a TestWindow (testing.py)
can be created against. It has **zero effect on question-bank curation
access**: a coach or a volunteer with existing `auth.User.events` access
can still browse/edit/curate the question bank of any event at any time,
including ones that aren't part of the current season's lineup. Nothing in
this module is ever consulted by review_app.py's `_select_event()`.

Stored as two flat JSON files at DATA_ROOT, same pattern as auth.py's
auth_users.json / events.py's events_custom.json — no DB, atomic tempfile+
os.replace writes, one lock per file for concurrent writers.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).parent
# Mirrors auth.py's/events.py's independent DATA_ROOT resolution — see
# events.py's DATA_ROOT docstring for the full rationale on why this small
# constant is duplicated rather than imported.
DATA_ROOT = Path(os.environ.get("DATA_ROOT") or REPO_ROOT)
SEASONS_FILE = DATA_ROOT / "seasons.json"
ROSTERS_FILE = DATA_ROOT / "season_rosters.json"

_seasons_lock = threading.Lock()
_rosters_lock = threading.Lock()


@dataclass(frozen=True)
class Season:
    season_id: str                      # e.g. "2027" — dict key and user-facing label
    label: str = ""                      # defaults to season_id if blank, see .display_label
    event_slugs: tuple[str, ...] = ()     # this season's event lineup
    is_current: bool = False
    # Soft-delete flag, mirrors events.py's archived pattern — never hard-deleted.
    archived: bool = False
    created_at: str = ""
    created_by: str = ""

    @property
    def display_label(self) -> str:
        return self.label or self.season_id


def _load_seasons_unlocked() -> dict[str, Season]:
    # Caller must already hold _seasons_lock.
    if not SEASONS_FILE.exists():
        return {}
    try:
        data = json.loads(SEASONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, Season] = {}
    for season_id, d in data.items():
        try:
            out[season_id] = Season(
                season_id=season_id,
                label=d.get("label", ""),
                event_slugs=tuple(d.get("event_slugs") or ()),
                is_current=bool(d.get("is_current", False)),
                archived=bool(d.get("archived", False)),
                created_at=d.get("created_at", ""),
                created_by=d.get("created_by", ""),
            )
        except Exception:
            continue
    return out


def _save_seasons_unlocked(seasons: dict[str, Season]) -> None:
    # Caller must already hold _seasons_lock.
    data = {
        s.season_id: {
            "label": s.label,
            "event_slugs": list(s.event_slugs),
            "is_current": s.is_current,
            "archived": s.archived,
            "created_at": s.created_at,
            "created_by": s.created_by,
        }
        for s in seasons.values()
    }
    tmp = SEASONS_FILE.with_suffix(SEASONS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SEASONS_FILE)


def _load_seasons() -> dict[str, Season]:
    # Reads and writes share the same lock (not just writes) — on Windows,
    # os.replace() targeting a file another thread currently has open for
    # read can fail with PermissionError.
    with _seasons_lock:
        return _load_seasons_unlocked()


def _save_seasons(seasons: dict[str, Season]) -> None:
    with _seasons_lock:
        _save_seasons_unlocked(seasons)


@contextlib.contextmanager
def _seasons_transaction():
    """Hold _seasons_lock across the full load -> mutate -> save cycle —
    see auth.py's _users_transaction() for the lost-update bug this avoids
    (two mutators loading the same pre-mutation snapshot, last save wins).
    Save only runs if the `with`-block body doesn't raise."""
    with _seasons_lock:
        seasons = _load_seasons_unlocked()
        yield seasons
        _save_seasons_unlocked(seasons)


def load_seasons() -> dict[str, Season]:
    return _load_seasons()


def get_season(season_id: str) -> Season | None:
    return _load_seasons().get(season_id)


def get_current_season() -> Season | None:
    for s in _load_seasons().values():
        if s.is_current:
            return s
    return None


def create_season(season_id: str, label: str = "", event_slugs: list[str] | None = None,
                   created_by: str = "") -> Season:
    from datetime import datetime, timezone
    import events as events_mod

    season_id = (season_id or "").strip()
    if not season_id:
        raise ValueError("season_id is required")
    event_slugs = [s for s in (event_slugs or [])]
    unknown = [s for s in event_slugs if s not in events_mod.EVENTS]
    if unknown:
        raise ValueError(f"unknown event slug(s): {', '.join(unknown)}")
    with _seasons_transaction() as seasons:
        if season_id in seasons:
            raise ValueError(f"season {season_id!r} already exists")
        season = Season(
            season_id=season_id,
            label=(label or "").strip(),
            event_slugs=tuple(event_slugs),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            created_by=created_by,
        )
        seasons[season_id] = season
    return season


def update_season_events(season_id: str, event_slugs: list[str]) -> Season:
    """Add/remove events from a season's lineup after creation. Removing an
    event that already has roster entries leaves those entries
    orphaned-but-harmless in season_rosters.json — simply no longer shown
    on the Club Management grid, never deleted, consistent with this app's
    never-silently-destroy-data convention everywhere else."""
    import events as events_mod

    unknown = [s for s in event_slugs if s not in events_mod.EVENTS]
    if unknown:
        raise ValueError(f"unknown event slug(s): {', '.join(unknown)}")
    with _seasons_transaction() as seasons:
        existing = seasons.get(season_id)
        if existing is None:
            raise ValueError(f"unknown season {season_id!r}")
        updated = replace(existing, event_slugs=tuple(event_slugs))
        seasons[season_id] = updated
    return updated


def set_current_season(season_id: str) -> Season:
    """Flips is_current on the target and unsets it everywhere else — the
    "exactly one current season" invariant lives here, nowhere else."""
    with _seasons_transaction() as seasons:
        if season_id not in seasons:
            raise ValueError(f"unknown season {season_id!r}")
        for sid, s in seasons.items():
            if s.is_current and sid != season_id:
                seasons[sid] = replace(s, is_current=False)
        seasons[season_id] = replace(seasons[season_id], is_current=True)
        result = seasons[season_id]
    return result


def _set_archived(season_id: str, archived: bool) -> Season:
    with _seasons_transaction() as seasons:
        existing = seasons.get(season_id)
        if existing is None:
            raise ValueError(f"unknown season {season_id!r}")
        updated = replace(existing, archived=archived)
        seasons[season_id] = updated
    return updated


def archive_season(season_id: str) -> Season:
    return _set_archived(season_id, True)


def unarchive_season(season_id: str) -> Season:
    return _set_archived(season_id, False)


# ---------------------------------------------------------------------------
# Per-season roster: season_id -> event_slug -> [usernames]
# ---------------------------------------------------------------------------

def _load_rosters_unlocked() -> dict:
    # Caller must already hold _rosters_lock.
    if not ROSTERS_FILE.exists():
        return {}
    try:
        return json.loads(ROSTERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_rosters_unlocked(data: dict) -> None:
    # Caller must already hold _rosters_lock.
    tmp = ROSTERS_FILE.with_suffix(ROSTERS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, ROSTERS_FILE)


def _load_rosters() -> dict:
    # Same read/write-share-one-lock fix as _load_seasons() above.
    with _rosters_lock:
        return _load_rosters_unlocked()


def _save_rosters(data: dict) -> None:
    with _rosters_lock:
        _save_rosters_unlocked(data)


@contextlib.contextmanager
def _rosters_transaction():
    """Hold _rosters_lock across the full load -> mutate -> save cycle —
    see _seasons_transaction()/auth.py's _users_transaction() for why.
    Critically, add_to_roster() must do its read-merge-write *inside one*
    of these transactions rather than composing get_roster() + set_roster()
    (two separate transactions) — otherwise two coaches adding different
    students to the same roster around the same time would still race,
    even with set_roster() itself made atomic."""
    with _rosters_lock:
        data = _load_rosters_unlocked()
        yield data
        _save_rosters_unlocked(data)


def get_roster(season_id: str, event_slug: str) -> list[str]:
    return list((_load_rosters().get(season_id) or {}).get(event_slug) or [])


def get_full_roster(season_id: str) -> dict[str, list[str]]:
    """The whole {event_slug: [usernames]} map for one season, in one call —
    backs the Club Management grid's single-round-trip load."""
    return dict(_load_rosters().get(season_id) or {})


def set_roster(season_id: str, event_slug: str, usernames: list[str]) -> None:
    """Full replace for one event's roster within one season. Rejects an
    event_slug that isn't part of this season's declared lineup."""
    season = get_season(season_id)
    if season is None:
        raise ValueError(f"unknown season {season_id!r}")
    if event_slug not in season.event_slugs:
        raise ValueError(f"{event_slug!r} is not in season {season_id!r}'s event lineup")
    with _rosters_transaction() as data:
        data.setdefault(season_id, {})[event_slug] = list(dict.fromkeys(usernames))  # dedupe, preserve order


def add_to_roster(season_id: str, event_slug: str, usernames: list[str]) -> None:
    """Additive variant of set_roster — unions into whatever's already on
    the roster rather than replacing it. Used by the CSV bulk-import (Part
    2), which must never wipe existing roster entries a CSV upload didn't
    mention.

    Reads and writes inside a single transaction (rather than composing
    get_roster() + set_roster(), two separate transactions) so two
    concurrent additive imports to the same event don't lose one's
    usernames to the other's overwrite."""
    season = get_season(season_id)
    if season is None:
        raise ValueError(f"unknown season {season_id!r}")
    if event_slug not in season.event_slugs:
        raise ValueError(f"{event_slug!r} is not in season {season_id!r}'s event lineup")
    with _rosters_transaction() as data:
        current = data.setdefault(season_id, {}).get(event_slug) or []
        data[season_id][event_slug] = list(dict.fromkeys(list(current) + list(usernames)))


def copy_roster_forward(from_season_id: str, to_season_id: str,
                         event_slugs: list[str] | None = None) -> dict[str, int]:
    """Copies username lists for events present in BOTH seasons' lineups —
    an event only in one or the other is silently skipped (nothing to copy
    to/from). Drops any username that no longer exists or is disabled in
    auth.load_users() — a coach reviews the result on the roster grid right
    after, no separate confirmation step needed. Returns {event_slug: count}
    for a toast summary."""
    import auth

    from_season = get_season(from_season_id)
    to_season = get_season(to_season_id)
    if from_season is None:
        raise ValueError(f"unknown season {from_season_id!r}")
    if to_season is None:
        raise ValueError(f"unknown season {to_season_id!r}")
    users = auth.load_users()
    candidate_slugs = event_slugs if event_slugs is not None else list(from_season.event_slugs)
    overlap = [s for s in candidate_slugs if s in to_season.event_slugs]
    copied: dict[str, int] = {}
    for slug in overlap:
        usable = [
            u for u in get_roster(from_season_id, slug)
            if u in users and not users[u].disabled
        ]
        if usable:
            set_roster(to_season_id, slug, usable)
        copied[slug] = len(usable)
    return copied


def student_events(season_id: str, username: str) -> list[str]:
    """Reverse-lookup: every event this student is rostered on for this
    season. Computed on read (O(events), cheap at this app's scale) rather
    than stored as a second representation that could drift from the
    per-event roster lists above."""
    roster = _load_rosters().get(season_id) or {}
    return [slug for slug, usernames in roster.items() if username in usernames]
