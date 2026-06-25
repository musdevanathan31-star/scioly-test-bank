"""
Regression coverage for the lost-update race in this codebase's flat-JSON
persistence: every mutator used to do `_load()` then `_save()` as two
separate lock acquisitions, leaving a window where two near-simultaneous
mutators could each load the same pre-mutation snapshot and whichever saved
last would silently overwrite the other's change. Confirmed directly: 15
threads calling auth.create_user() concurrently for distinct usernames
persisted only 1 of 15 accounts before the fix (auth._users_transaction(),
seasons._seasons_transaction()/_rosters_transaction(),
testing._windows_transaction()/_tests_transaction()/_responses_transaction(),
build_question_bank._state_transaction()).

Each test below uses a threading.Barrier so every worker thread starts its
mutation at roughly the same instant, maximizing the race window — this
makes the test a reliable regression check rather than a flaky one.

Run with: `python -m pytest tests/test_concurrency.py -q`
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth                          # noqa: E402
import build_question_bank as bqb    # noqa: E402
import seasons                       # noqa: E402
import testing as testing_mod        # noqa: E402

N = 20


def _run_concurrently(fns: list) -> None:
    """Start every callable in `fns` from its own thread, released together
    via a barrier so they race as hard as possible."""
    barrier = threading.Barrier(len(fns))

    def _wrapped(fn):
        barrier.wait()
        fn()

    threads = [threading.Thread(target=_wrapped, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------------------
# auth.py
# ---------------------------------------------------------------------------

def test_auth_concurrent_create_user_no_lost_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "auth_users.json")

    def _make(i):
        return lambda: auth.create_user(f"user{i}", "password123", role="volunteer")

    _run_concurrently([_make(i) for i in range(N)])

    users = auth.load_users()
    assert len(users) == N, f"expected {N} accounts, found {len(users)} — lost updates"
    for i in range(N):
        assert f"user{i}" in users


def test_auth_concurrent_update_user_distinct_fields_no_lost_updates(tmp_path, monkeypatch):
    """Two different fields on the SAME user, updated concurrently, must
    both land — not just whichever update_user() call saved last."""
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "auth_users.json")
    auth.create_user("alice", "password123", role="volunteer", events=[])

    _run_concurrently([
        lambda: auth.update_user("alice", events=["circuit_lab"]),
        lambda: auth.set_display_name("alice", "Alice A."),
    ])

    alice = auth.get_user("alice")
    assert alice.events == ("circuit_lab",)
    assert alice.display_name == "Alice A."


# ---------------------------------------------------------------------------
# seasons.py
# ---------------------------------------------------------------------------

def test_seasons_concurrent_create_season_no_lost_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(seasons, "SEASONS_FILE", tmp_path / "seasons.json")

    def _make(i):
        return lambda: seasons.create_season(f"season{i}", event_slugs=[])

    _run_concurrently([_make(i) for i in range(N)])

    found = seasons.load_seasons()
    assert len(found) == N, f"expected {N} seasons, found {len(found)} — lost updates"


def test_seasons_concurrent_add_to_roster_no_lost_updates(tmp_path, monkeypatch):
    """Distinct students added to the same event's roster by N concurrent
    callers (e.g. two coaches' CSV imports overlapping) must all survive —
    add_to_roster() composed get_roster()+set_roster() as two separate
    transactions, which still lost updates even after set_roster() itself
    was made atomic."""
    monkeypatch.setattr(seasons, "SEASONS_FILE", tmp_path / "seasons.json")
    monkeypatch.setattr(seasons, "ROSTERS_FILE", tmp_path / "season_rosters.json")
    # "circuit_lab" is one of events.py's seeded default events, always
    # registered — no need to register a fake one for this test.
    seasons.create_season("2027", event_slugs=["circuit_lab"])

    def _make(i):
        return lambda: seasons.add_to_roster("2027", "circuit_lab", [f"student{i}"])

    _run_concurrently([_make(i) for i in range(N)])

    roster = seasons.get_roster("2027", "circuit_lab")
    assert len(roster) == N, f"expected {N} roster entries, found {len(roster)} — lost updates"
    for i in range(N):
        assert f"student{i}" in roster


# ---------------------------------------------------------------------------
# testing.py
# ---------------------------------------------------------------------------

def test_testing_concurrent_save_answer_no_lost_updates(tmp_path, monkeypatch):
    """The live student-test-autosave path: distinct question numbers saved
    concurrently for the SAME (test_id, username) must all survive — the
    highest real-world-severity instance of this bug (many students
    autosaving during one timed window, all hitting one shared file)."""
    monkeypatch.setattr(testing_mod, "RESPONSES_FILE", tmp_path / "test_responses.json")
    testing_mod.start_or_get_response("test1", "bob", num_questions=N)

    def _make(i):
        return lambda: testing_mod.save_answer("test1", "bob", str(i), {"qtype": "mcq", "picked": "A"})

    _run_concurrently([_make(i) for i in range(N)])

    resp = testing_mod.get_response("test1", "bob")
    assert len(resp.answers) == N, f"expected {N} saved answers, found {len(resp.answers)} — lost updates"
    for i in range(N):
        assert str(i) in resp.answers


def test_testing_concurrent_start_or_get_response_creates_once(tmp_path, monkeypatch):
    """Two simultaneous first-loads of the same (test, student) must not
    each shuffle their own question_order and silently clobber the other —
    every caller must observe the SAME response."""
    monkeypatch.setattr(testing_mod, "RESPONSES_FILE", tmp_path / "test_responses.json")
    results: list = [None] * N

    def _make(i):
        def _go():
            results[i] = testing_mod.start_or_get_response("test1", "carol", num_questions=10)
        return _go

    _run_concurrently([_make(i) for i in range(N)])

    orders = {tuple(r.question_order) for r in results}
    assert len(orders) == 1, "concurrent first-loads produced different question_order snapshots"
    by_user = testing_mod._load_all_responses().get("test1", {})
    assert len(by_user) == 1


def test_testing_concurrent_set_manual_grade_distinct_questions_no_lost_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(testing_mod, "RESPONSES_FILE", tmp_path / "test_responses.json")
    testing_mod.start_or_get_response("test1", "dave", num_questions=N)

    def _make(i):
        return lambda: testing_mod.set_manual_grade("test1", "dave", str(i), 1.0, 1.0, graded_by="coach")

    _run_concurrently([_make(i) for i in range(N)])

    resp = testing_mod.get_response("test1", "dave")
    assert len(resp.manual_grade) == N, (
        f"expected {N} manual grades, found {len(resp.manual_grade)} — lost updates"
    )


# ---------------------------------------------------------------------------
# build_question_bank.py
# ---------------------------------------------------------------------------

def test_bqb_concurrent_state_transaction_no_lost_updates(tmp_path, monkeypatch):
    fake_event = SimpleNamespace(slug="concurrency_test", state_file=tmp_path / "state.json")
    monkeypatch.setattr(bqb, "_ev", lambda: fake_event)
    # Each test run gets a fresh slug-less lock entry (module-level
    # _state_locks dict is keyed by slug and never cleared) — fine, a
    # never-reused fake slug per test avoids cross-test interference.

    def _make(i):
        def _go():
            with bqb._state_transaction() as state:
                state.setdefault("questions", {})[f"pdf{i}.pdf"] = [{"number": "1"}]
        return _go

    _run_concurrently([_make(i) for i in range(N)])

    final_state = bqb._load_state()
    assert len(final_state["questions"]) == N, (
        f"expected {N} question buckets, found {len(final_state['questions'])} — lost updates"
    )
