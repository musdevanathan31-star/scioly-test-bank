"""
Job console output reaching the browser.

The progress popover's console appeared empty after an upload, so this
pins the whole path end to end: a job's print() output is captured, is
served by the /log endpoint the modal polls, and the lines flushed as the
job finishes are still retrievable after the status has gone terminal —
which is when the modal stops polling.

Run with: `python -m pytest tests/test_job_log_streaming.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


@pytest.fixture()
def client(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="joblog-"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app, jobs
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    yield c, slug, jobs

    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _drain(c, slug, job_id, timeout=12.0):
    """Poll exactly the way the modal does, including the final fetch after
    the status goes terminal."""
    after, lines, deadline = 0, [], time.time() + timeout
    status = "queued"
    while time.time() < deadline:
        status = c.get(f"/event/{slug}/api/jobs/{job_id}").get_json()["status"]
        payload = c.get(f"/event/{slug}/api/jobs/{job_id}/log?after={after}").get_json()
        lines += payload.get("lines") or []
        after = payload.get("total", after)
        if status in TERMINAL:
            final = c.get(f"/event/{slug}/api/jobs/{job_id}/log?after={after}").get_json()
            lines += final.get("lines") or []
            break
        time.sleep(0.05)
    return status, lines


def test_a_jobs_printed_output_reaches_the_client(client):
    c, slug, jobs = client

    def target(should_cancel, on_progress):
        print("  [PROC]  extracting demo_test.pdf")
        on_progress(phase="extracting")
        time.sleep(0.2)
        print("  [OK]    12 questions")
        return {"n_questions": 12}

    job_id = jobs.submit_job(slug, "upload_extract", "Extract demo", "coach1", target)
    status, lines = _drain(c, slug, job_id)
    assert status == "succeeded"
    assert any("[PROC]" in ln for ln in lines), lines
    # The last line is the one that says what happened, and it is flushed as
    # the job ends — the case the modal used to stop polling before seeing.
    assert any("12 questions" in ln for ln in lines), lines


def test_output_printed_right_before_finishing_is_not_lost(client):
    # No sleep at all: the job prints and returns immediately, so its output
    # is flushed in the same instant the status becomes terminal.
    c, slug, jobs = client

    def target(should_cancel, on_progress):
        print("FINAL LINE")
        return {}

    job_id = jobs.submit_job(slug, "quick", "Quick", "coach1", target)
    status, lines = _drain(c, slug, job_id)
    assert status == "succeeded"
    assert any("FINAL LINE" in ln for ln in lines), lines


def test_a_failing_jobs_traceback_reaches_the_client(client):
    # A failed job's console is the only place the reason exists, which is
    # why the modal deliberately stays open for one.
    c, slug, jobs = client

    def target(should_cancel, on_progress):
        print("about to fail")
        raise RuntimeError("boom in extraction")

    job_id = jobs.submit_job(slug, "upload_extract", "Failing", "coach1", target)
    status, lines = _drain(c, slug, job_id)
    assert status == "failed"
    assert any("boom in extraction" in ln for ln in lines), lines


def test_a_job_with_no_total_still_reports_running(client):
    # Only the vision-OCR path calls on_progress with a total, so an
    # ordinary text-extractable PDF reports total=0 throughout. The modal
    # must not read that as finished — it now shows an indeterminate bar.
    c, slug, jobs = client
    seen = []

    def target(should_cancel, on_progress):
        time.sleep(0.4)
        return {}

    job_id = jobs.submit_job(slug, "upload_extract", "No total", "coach1", target)
    deadline = time.time() + 8
    while time.time() < deadline:
        job = c.get(f"/event/{slug}/api/jobs/{job_id}").get_json()
        seen.append((job["status"], job.get("total")))
        if job["status"] in TERMINAL:
            break
        time.sleep(0.05)
    running = [t for st, t in seen if st == "running"]
    assert running, seen
    assert all(not t for t in running), (
        "this fixture is meant to have no total; if that changed, the "
        "indeterminate-bar behaviour needs rechecking")
