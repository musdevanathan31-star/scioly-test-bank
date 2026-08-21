"""
Coverage for presence.py — the in-memory active-user registry behind the
header's "N active" badge and the landing page's per-event counts.

Every test drives the clock explicitly via the `now=` keyword rather than
sleeping: the production window is 5 minutes, so a sleep-based test would
either take 5 minutes or need the window monkeypatched down to something
that no longer resembles what ships.

Run with: `python -m pytest tests/test_presence.py -q`
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import presence  # noqa: E402

T0 = 1_000_000.0
W = presence.WINDOW_SECONDS


def setup_function(_fn):
    presence.reset()


def test_empty_registry_reports_nobody():
    assert presence.active_summary(now=T0) == {"count": 0, "students": 0}
    assert presence.active_by_event(now=T0) == {}


def test_distinct_users_counted_once_each_however_many_requests():
    # The registry is keyed by username, so a user clicking around fast
    # must not inflate the count — that's the whole difference between
    # "people active" and "requests served".
    for i in range(10):
        presence.touch("sarah", "volunteer", now=T0 + i)
    presence.touch("mukund", "coach", now=T0)
    assert presence.active_summary(now=T0 + 10)["count"] == 2


def test_students_are_counted_and_broken_out():
    presence.touch("coach1", "coach", now=T0)
    presence.touch("stu1", "student", now=T0)
    presence.touch("stu2", "student", now=T0)
    assert presence.active_summary(now=T0) == {"count": 3, "students": 2}


def test_user_drops_out_after_the_window():
    presence.touch("sarah", "volunteer", now=T0)
    # Still inside the window, right up to the boundary.
    assert presence.active_summary(now=T0 + W - 1)["count"] == 1
    # Past it, gone.
    assert presence.active_summary(now=T0 + W + 1)["count"] == 0


def test_activity_refreshes_the_window_rather_than_stacking():
    presence.touch("sarah", "volunteer", now=T0)
    presence.touch("sarah", "volunteer", now=T0 + W - 1)
    # The second request re-stamps, so she survives past where the first
    # would have expired.
    assert presence.active_summary(now=T0 + W + 1)["count"] == 1
    assert presence.active_summary(now=T0 + 2 * W)["count"] == 0


def test_per_event_counts_are_isolated():
    presence.touch_event("anatomy", "sarah", now=T0)
    presence.touch_event("anatomy", "mukund", now=T0)
    presence.touch_event("ecology", "sarah", now=T0)
    counts = presence.active_by_event(now=T0)
    assert counts == {"anatomy": 2, "ecology": 1}


def test_absent_event_is_simply_missing_not_zero():
    # Callers use .get(slug, 0); the dict deliberately doesn't carry an
    # entry per known event, so it needs no event list passed in.
    presence.touch_event("anatomy", "sarah", now=T0)
    assert "ecology" not in presence.active_by_event(now=T0)


def test_leaving_one_event_does_not_expire_another_users_entry():
    presence.touch_event("anatomy", "sarah", now=T0)
    presence.touch_event("anatomy", "mukund", now=T0 + W - 1)
    # Sarah's entry has aged out; Mukund's has not.
    assert presence.active_by_event(now=T0 + W + 1) == {"anatomy": 1}


def test_event_presence_is_separate_from_server_wide_presence():
    # touch_event alone must not invent a server-wide active user: the two
    # stamps come from different hooks (_select_event vs _require_login),
    # and conflating them would double-count nothing but confuse the
    # meaning of each number.
    presence.touch_event("anatomy", "sarah", now=T0)
    assert presence.active_summary(now=T0)["count"] == 0
    presence.touch("sarah", "volunteer", now=T0)
    assert presence.active_summary(now=T0)["count"] == 1


def test_concurrent_touches_do_not_lose_users():
    # gunicorn runs --threads 8, so several requests genuinely land at
    # once. Without the module's lock, dict mutation from many threads can
    # drop entries.
    names = [f"user{i}" for i in range(50)]
    barrier = threading.Barrier(len(names))

    def _touch(name):
        barrier.wait()
        presence.touch(name, "volunteer", now=T0)
        presence.touch_event("anatomy", name, now=T0)

    threads = [threading.Thread(target=_touch, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert presence.active_summary(now=T0)["count"] == len(names)
    assert presence.active_by_event(now=T0)["anatomy"] == len(names)


def test_reads_prune_so_the_registry_does_not_grow_without_bound():
    for i in range(100):
        presence.touch(f"user{i}", "volunteer", now=T0)
        presence.touch_event("anatomy", f"user{i}", now=T0)
    presence.active_summary(now=T0 + W + 1)
    presence.active_by_event(now=T0 + W + 1)
    assert presence._users == {}
    assert presence._events == {}
