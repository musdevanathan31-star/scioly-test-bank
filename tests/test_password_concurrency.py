"""
change_own_password under concurrency.

The scrypt calls were moved outside auth.py's global _users_lock because
holding it across two ~80ms hashes serialised every password change AND —
since review_app's _require_login reads that same lock on every
authenticated request — stalled the whole instance while a burst ran.

Moving them out means the read and the write are no longer one atomic
step, so the swap does a compare-and-swap on the stored hash. These tests
pin both halves: that the lost-update race is actually closed, and that
the expensive work really does overlap.

Run with: `python -m pytest tests/test_password_concurrency.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def auth_mod(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="pwconc-"))
    import auth
    importlib.reload(auth)
    return auth


def test_changing_a_password_works_and_preserves_every_other_field(auth_mod):
    auth_mod.create_user("u1", "password123", "volunteer", events=["circuit_lab"])
    before = auth_mod.get_user("u1")
    after = auth_mod.change_own_password("u1", "password123", "newpassword1")
    assert auth_mod.verify_login("u1", "newpassword1") is not None
    assert auth_mod.verify_login("u1", "password123") is None
    for field in ("username", "role", "events", "disabled", "display_name"):
        assert getattr(after, field) == getattr(before, field), field


def test_two_concurrent_changes_cannot_lose_an_update(auth_mod):
    """Both racers verify against the same starting hash. Exactly one may
    win; the loser must be told, not silently overwritten."""
    auth_mod.create_user("u1", "password123", "student")
    barrier = threading.Barrier(2)
    results: list = []

    def attempt(new_password):
        barrier.wait()
        try:
            auth_mod.change_own_password("u1", "password123", new_password)
            results.append(("ok", new_password))
        except auth_mod.WrongPasswordError:
            results.append(("refused", new_password))

    threads = [threading.Thread(target=attempt, args=(p,))
               for p in ("alphapass111", "betapass2222")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [p for status, p in results if status == "ok"]
    assert len(winners) == 1, results
    # The winner's password is the one that actually took effect — not a
    # mix where one thread's write survived under the other's hash.
    assert auth_mod.verify_login("u1", winners[0]) is not None
    losers = [p for status, p in results if status == "refused"]
    assert auth_mod.verify_login("u1", losers[0]) is None


def test_a_stale_verification_is_refused_rather_than_applied(auth_mod):
    # Simulates the race deterministically: verify against the old password,
    # have someone else change it, then try to complete.
    auth_mod.create_user("u1", "password123", "student")
    auth_mod.change_own_password("u1", "password123", "interloper11")
    with pytest.raises(auth_mod.WrongPasswordError):
        auth_mod.change_own_password("u1", "password123", "toolate12345")
    assert auth_mod.verify_login("u1", "interloper11") is not None


def test_concurrent_changes_overlap_instead_of_serialising(auth_mod):
    """The point of the change. Eight password changes across eight threads
    must take meaningfully less than eight times one change.

    Threshold is deliberately loose (60% of serial) so this asserts "the
    hashing overlaps" rather than a specific speedup — scrypt cost and core
    count vary by machine, and a flaky performance test gets deleted.
    """
    for i in range(8):
        auth_mod.create_user(f"u{i}", "password123", "student")

    t0 = time.monotonic()
    auth_mod.change_own_password("u0", "password123", "serialpass1")
    one = time.monotonic() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(lambda i: auth_mod.change_own_password(
            f"u{i}", "password123", f"parallelpw{i}"), range(1, 8)))
    seven = time.monotonic() - t0

    assert seven < one * 7 * 0.6, (
        f"7 concurrent changes took {seven:.2f}s vs {one:.2f}s for one — "
        f"that is close to fully serial, so the hashing is still inside the "
        f"lock")


def test_the_users_lock_is_not_held_across_hashing(auth_mod):
    # Structural check, so the property survives a refactor that happens to
    # keep the timing test passing on a fast machine.
    import inspect
    src = inspect.getsource(auth_mod.change_own_password)
    body = src.split('"""', 2)[-1]          # drop the docstring
    lock_at = body.index("_users_transaction")
    assert body.index("generate_password_hash") < lock_at, \
        "new hash must be computed before the lock is taken"
    assert body.index("check_password_hash") < lock_at, \
        "current password must be verified before the lock is taken"
