"""Who is actually using this instance right now.

Backs two indicators: a server-wide "N people active" badge in the header
(same shape as the existing jobs badge) and a per-event count on the
landing page, so a volunteer can see whether someone else is already
working in an event -- or whether the box is busy -- before kicking off a
reprocess or an LLM generation run.

Deliberately in-memory, with no file backing and no cross-process story.

**This is exact only because gunicorn runs `--workers 1`.** One process
handles every request for an instance, so one process-local dict sees all
of them. Under two or more workers each would see roughly half the traffic
and every count here would silently read low -- not crash, just quietly
lie, which is the worst failure mode for a number people make decisions
from. That puts this module on the same list as build_question_bank.py's
in-process state RLock and jobs.py's single-process queue: things that
must be redesigned *before* `--workers` is ever raised, not after. See
spec.md section 16 and deploy/qbank.service's header.

Why not persist it: presence is worthless the moment it's stale, the whole
dataset is rebuilt from live traffic within one window, and a restart
legitimately means nobody is active yet. Writing it to disk would add I/O
on the hot path of every request to buy nothing.
"""

from __future__ import annotations

import threading
import time

# How long after someone's last request they still count as "active".
# Long enough to cover reading a page, editing a question, or thinking;
# short enough that the number means "now" and not "this session".
WINDOW_SECONDS = 300

_lock = threading.Lock()

# username -> (last_seen_epoch, role)
_users: dict[str, tuple[float, str]] = {}

# (event_slug, username) -> last_seen_epoch. Keyed by the pair rather than
# slug -> set so expiry is per user per event: leaving an event should drop
# you from its count on the normal schedule without touching anyone else's
# entry, and without a nested structure to prune.
_events: dict[tuple[str, str], float] = {}


def touch(username: str, role: str, *, now: float | None = None) -> None:
    """Record that `username` just made a request. Called once per
    authenticated request from review_app's _require_login."""
    ts = time.time() if now is None else now
    with _lock:
        _users[username] = (ts, role)


def touch_event(slug: str, username: str, *, now: float | None = None) -> None:
    """Record that `username` just touched event `slug`. Called from
    review_app's _select_event, which every /event/<slug>/... route funnels
    through -- and which is also the per-event access gate, so presence can
    never be recorded for an event the user isn't allowed to reach."""
    ts = time.time() if now is None else now
    with _lock:
        _events[(slug, username)] = ts


def _prune_unlocked(cutoff: float) -> None:
    for username in [u for u, (ts, _) in _users.items() if ts < cutoff]:
        del _users[username]
    for key in [k for k, ts in _events.items() if ts < cutoff]:
        del _events[key]


def active_summary(*, now: float | None = None) -> dict:
    """Server-wide counts: everyone active, and how many of them are
    students. Students are counted because a live test window is exactly
    when this instance is under real load -- see review_app's api_presence
    for why they nonetheless don't get shown the badge."""
    ts = time.time() if now is None else now
    cutoff = ts - WINDOW_SECONDS
    with _lock:
        _prune_unlocked(cutoff)
        roles = [role for _, role in _users.values()]
    return {
        "count": len(roles),
        "students": sum(1 for r in roles if r == "student"),
    }


def active_by_event(*, now: float | None = None) -> dict[str, int]:
    """slug -> number of distinct users active in that event. Only events
    with at least one active user appear; callers treat a missing slug as
    zero rather than needing the full event list passed in."""
    ts = time.time() if now is None else now
    cutoff = ts - WINDOW_SECONDS
    counts: dict[str, int] = {}
    with _lock:
        _prune_unlocked(cutoff)
        for (slug, _username) in _events:
            counts[slug] = counts.get(slug, 0) + 1
    return counts


def reset() -> None:
    """Drop all recorded presence. For tests only."""
    with _lock:
        _users.clear()
        _events.clear()
