"""
Per-user vision keys for background jobs.

Vision (OCR/region-capture/matching-detection/image-assignment/equation-
LaTeX) used to read only the server's own ANTHROPIC_API_KEY, via a single
module-level client singleton in build_question_bank.py. A browser can now
supply its own Anthropic key (X-LLM-Keys header, same mechanism already used
by answer validation/qgen/diagram-chat), and jobs.submit_job()'s `vision_key`
kwarg carries that key across the request/worker-thread boundary so
process_pair()'s vision calls, running on jobs.py's single background worker
thread, use the submitter's own key instead of silently falling back to the
operator's.

The owner was explicit that the key must never be written anywhere on disk
or into any log line — these tests pin that end to end (reading the actual
files back off disk, not just asserting in prose), pin that two jobs
submitted with two different keys each use their own key (the failure mode
that would quietly bill the wrong person), and pin that the in-memory side
map holding the key does not outlive the job it belongs to.

Run with: `python -m pytest tests/test_vision_key.py -q`
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")

SENTINEL_A = "sk-ant-TESTSENTINEL0123456789AAA"
SENTINEL_B = "sk-ant-TESTSENTINEL0123456789BBB"


@pytest.fixture(scope="module")
def env():
    # Module-scoped (not per-test): each importlib.reload() below races
    # against jobs.py's single persistent background worker thread, which is
    # never itself reloaded (see the comment on jobs.py's local `import
    # build_question_bank` for why) and keeps running across the reload.
    # Doing this reload cycle once for the whole file instead of once per
    # test avoids stacking up that race ten times over; every test below
    # still gets its own uuid4 job id, so nothing here needs per-test
    # isolation, only isolation from other test *files* sharing the process.
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    data_root = tempfile.mkdtemp(prefix="visionkey-")
    old_data_root = __import__("os").environ.get("DATA_ROOT")
    old_secret = __import__("os").environ.get("FLASK_SECRET_KEY")
    __import__("os").environ["DATA_ROOT"] = data_root
    __import__("os").environ["FLASK_SECRET_KEY"] = "test"
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app, jobs
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    bqb.set_event(slug)

    yield bqb, jobs, review_app, slug

    import os
    if old_data_root is None:
        os.environ.pop("DATA_ROOT", None)
    else:
        os.environ["DATA_ROOT"] = old_data_root
    if old_secret is None:
        os.environ.pop("FLASK_SECRET_KEY", None)
    else:
        os.environ["FLASK_SECRET_KEY"] = old_secret
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _wait_terminal(jobs_mod, slug, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = jobs_mod.get_job(slug, job_id)
        if rec.status in TERMINAL:
            return rec
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never went terminal")


def _index_file_text(slug: str) -> str:
    import events as events_mod
    f = events_mod.EVENTS[slug].jobs_file
    return f.read_text(encoding="utf-8") if f.exists() else ""


def _log_file_text(slug: str, job_id: str) -> str:
    import events as events_mod
    f = events_mod.EVENTS[slug].jobs_dir / f"{job_id}.log"
    return f.read_text(encoding="utf-8") if f.exists() else ""


# ---------------------------------------------------------------------------
# Security: the key must never touch disk.
# ---------------------------------------------------------------------------

def test_key_never_reaches_index_json_or_log_file(env):
    bqb, jobs, review_app, slug = env

    def target(should_cancel, on_progress):
        # Deliberately do NOT print the key itself here -- that would just be
        # testing our own test bug, not the app. Print only a boolean so we
        # can still confirm the ContextVar was actually bound to something.
        print(f"vision key bound: {bool(bqb._current_vision_key())}")
        print("doing vision work")
        return {"ok": True}

    job_id = jobs.submit_job(slug, "reprocess", "Reprocess demo.pdf", "coach1",
                             target, vision_key=SENTINEL_A)
    rec = _wait_terminal(jobs, slug, job_id)
    assert rec.status == "succeeded"

    index_text = _index_file_text(slug)
    log_text = _log_file_text(slug, job_id)
    assert SENTINEL_A not in index_text, index_text
    assert SENTINEL_A not in log_text, log_text
    # Sanity: the log DOES contain the rest of the print, proving the log
    # capture path itself works and this isn't a false negative.
    assert "doing vision work" in log_text, log_text


def test_key_absent_from_job_to_public_dict(env):
    bqb, jobs, review_app, slug = env

    def target(should_cancel, on_progress):
        return {"ok": True}

    job_id = jobs.submit_job(slug, "reprocess", "Reprocess demo.pdf", "coach1",
                             target, vision_key=SENTINEL_A)
    _wait_terminal(jobs, slug, job_id)
    rec = jobs.get_job(slug, job_id)
    d = jobs.job_to_public_dict(rec, "coach1", is_coach=True)
    blob = json.dumps(d)
    assert SENTINEL_A not in blob, blob
    assert "vision_key" not in d


def test_side_map_does_not_outlive_a_succeeded_job(env):
    bqb, jobs, review_app, slug = env

    def target(should_cancel, on_progress):
        return {"ok": True}

    job_id = jobs.submit_job(slug, "reprocess", "Reprocess demo.pdf", "coach1",
                             target, vision_key=SENTINEL_A)
    _wait_terminal(jobs, slug, job_id)
    assert job_id not in jobs._job_vision_keys


def test_side_map_does_not_outlive_a_failing_job_and_log_stays_clean(env):
    bqb, jobs, review_app, slug = env

    def target(should_cancel, on_progress):
        print("about to explode")
        # A careless call site could embed a caught exception's message (which
        # might itself echo back invalid-key text from a provider) into the
        # job's `error`/log — make sure our own code never constructs one
        # that contains the key, by not doing so here either; this asserts
        # the current key still isn't in the log even though the job fails.
        raise RuntimeError("boom during vision extraction")

    job_id = jobs.submit_job(slug, "reprocess", "Reprocess demo.pdf", "coach1",
                             target, vision_key=SENTINEL_A)
    rec = _wait_terminal(jobs, slug, job_id)
    assert rec.status == "failed"
    assert job_id not in jobs._job_vision_keys

    log_text = _log_file_text(slug, job_id)
    index_text = _index_file_text(slug)
    assert SENTINEL_A not in log_text, log_text
    assert SENTINEL_A not in index_text, index_text
    assert (rec.error or "") .find(SENTINEL_A) == -1


def test_side_map_cleaned_up_when_cancelled_while_queued(env):
    bqb, jobs, review_app, slug = env

    # Occupy the (single, global) worker with a slow job so the next
    # submission stays queued long enough to cancel before it ever runs.
    import threading
    release = threading.Event()

    def blocker(should_cancel, on_progress):
        release.wait(timeout=5)
        return {}

    blocker_id = jobs.submit_job(slug, "reprocess", "Blocker", "coach1", blocker)
    # Give the worker a moment to actually pick up the blocker.
    time.sleep(0.1)

    def queued_target(should_cancel, on_progress):
        return {}

    queued_id = jobs.submit_job(slug, "reprocess", "Queued", "coach1",
                                queued_target, vision_key=SENTINEL_B)
    assert queued_id in jobs._job_vision_keys

    jobs.request_cancel(slug, queued_id, "coach1", is_coach=True)
    assert queued_id not in jobs._job_vision_keys

    release.set()
    _wait_terminal(jobs, slug, blocker_id)


# ---------------------------------------------------------------------------
# Correct attribution: two jobs, two different keys, no cross-talk.
# ---------------------------------------------------------------------------

def test_two_jobs_with_different_keys_each_use_their_own_key(env, monkeypatch):
    bqb, jobs, review_app, slug = env

    seen_keys = []

    class _FakeClient:
        def __init__(self, key):
            self.key = key

    def fake_client_for_key(key):
        seen_keys.append(key)
        return _FakeClient(key)

    monkeypatch.setattr(bqb, "_client_for_key", fake_client_for_key)

    results = {}

    def make_target(label):
        def target(should_cancel, on_progress):
            client = bqb._get_client()
            results[label] = client.key
            return {}
        return target

    job_a = jobs.submit_job(slug, "reprocess", "A", "coach1",
                            make_target("a"), vision_key=SENTINEL_A)
    _wait_terminal(jobs, slug, job_a)
    job_b = jobs.submit_job(slug, "reprocess", "B", "coach1",
                            make_target("b"), vision_key=SENTINEL_B)
    _wait_terminal(jobs, slug, job_b)

    assert results["a"] == SENTINEL_A
    assert results["b"] == SENTINEL_B
    assert results["a"] != results["b"]


def test_client_cache_is_keyed_per_key_not_a_shared_singleton(env, monkeypatch):
    """The old code cached one module-level client and reused it regardless
    of which key was active. _client_for_key() must construct/return a
    distinct client per distinct key."""
    bqb, jobs, review_app, slug = env

    built = []

    class _FakeAnthropic:
        def __init__(self, api_key, **kw):
            self.api_key = api_key
            built.append(api_key)

    import types
    fake_anthropic_module = types.SimpleNamespace(Anthropic=_FakeAnthropic)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    c1 = bqb._client_for_key(SENTINEL_A)
    c2 = bqb._client_for_key(SENTINEL_A)
    c3 = bqb._client_for_key(SENTINEL_B)

    assert c1 is c2, "same key should reuse the same cached client"
    assert c1 is not c3, "different keys must not share a client"
    assert built.count(SENTINEL_A) == 1, "second call for the same key must not rebuild"
    assert SENTINEL_B in built


# ---------------------------------------------------------------------------
# Fallback behaviour unchanged.
# ---------------------------------------------------------------------------

def test_no_per_user_key_falls_back_to_env_key(env, monkeypatch):
    bqb, jobs, review_app, slug = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ENVFALLBACK000")

    seen = []

    def target(should_cancel, on_progress):
        seen.append(bqb._get_client())
        return {}

    job_id = jobs.submit_job(slug, "reprocess", "No user key", "coach1", target)
    _wait_terminal(jobs, slug, job_id)
    assert seen[0] is not None


def test_neither_key_present_vision_unavailable(env, monkeypatch):
    bqb, jobs, review_app, slug = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    seen = []

    def target(should_cancel, on_progress):
        seen.append(bqb._vision_available())
        return {}

    job_id = jobs.submit_job(slug, "reprocess", "No key at all", "coach1", target)
    _wait_terminal(jobs, slug, job_id)
    assert seen[0] is False


def test_vision_available_true_with_only_per_user_key(env, monkeypatch):
    bqb, jobs, review_app, slug = env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    seen = []

    def target(should_cancel, on_progress):
        seen.append(bqb._vision_available())
        return {}

    job_id = jobs.submit_job(slug, "reprocess", "User key only", "coach1",
                             target, vision_key=SENTINEL_A)
    _wait_terminal(jobs, slug, job_id)
    assert seen[0] is True
