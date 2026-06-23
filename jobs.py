"""
In-process job queue for long-running operations (PDF extraction with vision
OCR, bulk validation, LLM question generation, scio.ly scraping/downloading).

Why not Celery/RQ+Redis: the server runs gunicorn --workers 1 (load-bearing
today — see deploy/qbank.service and build_question_bank.py's per-event
state lock, an in-process threading.RLock that only serialises correctly
within one process), so there's only ever one process to coordinate within.
A dedicated task-queue service would add a new dependency, a new thing to
run/monitor, and more memory pressure on a machine already described as
underpowered, for no benefit a single in-process worker thread doesn't
already provide.

Design:
  - Exactly one job runs at a time, globally, across every event — this
    machine can't do two PDF extractions/vision-OCR loads concurrently.
    Everything else queues (FIFO) until the running job finishes.
  - Job metadata is small and persisted per-event to <event>/.qbank_jobs.json
    (same atomic tempfile+os.replace idiom as build_question_bank.py's
    _save_state), so it survives `systemctl restart qbank`. Console output
    lives separately, in <event>/.qbank_jobs/<job_id>.log — kept out of the
    JSON index so a chatty vision-OCR job's output doesn't bloat the file
    every poller re-reads on every status tick.
  - Cancellation is cooperative, not a hard kill — Python threads can't be
    safely force-killed mid-API-call anyway. A job's target function accepts
    a `should_cancel()` callback and checks it at natural checkpoints (once
    per PDF page, once per LLM chunk/candidate, etc.); when true, it raises
    JobCancelled, which the worker loop catches and persists as "cancelled"
    rather than "failed".
  - If the process restarts mid-job (crash, redeploy), the job's OS thread
    is gone — recover_interrupted_jobs() (call once at app startup) marks
    any record still "running" as "interrupted" rather than pretending it's
    still alive. The user re-triggers the same action; build_question_bank's
    process_pair() checkpoint-saves its vision cache after every page (see
    that module), so re-running isn't starting over from scratch.
"""

from __future__ import annotations

import builtins
import contextvars
import json
import os
import re
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import events as events_mod

# Same shape as the ids this module mints (uuid4().hex[:12]) — every route
# that takes a job_id from the URL validates against this before it's ever
# used to build a filesystem path, the same containment principle as
# review_app.py's _safe_join, just simpler since the shape is fixed.
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
ACTIVE_STATUSES = ("queued", "running")

_MAX_JOBS_PER_EVENT_INDEX = 200   # oldest entries drop off the index past this
_MAX_LOG_LINES = 2000             # per job; oldest lines truncated past this
_PERSIST_INTERVAL = 1.0           # seconds; throttles disk writes during chatty loops


class JobCancelled(Exception):
    """Raised by a job target function when should_cancel() goes true.
    Caught by the worker loop and persisted as status="cancelled", not
    "failed"."""


class JobQueueFull(Exception):
    """Raised by submit_job() when the target event already has
    max_queued_per_event queued/running jobs."""


class JobNotAuthorized(Exception):
    """Raised by request_cancel() when the caller is neither the job's
    starter nor a coach."""


class JobNotFound(Exception):
    pass


@dataclass
class JobRecord:
    id: str
    event: str
    kind: str                      # "reprocess" | "upload_extract" | "scioly_download"
                                    # | "scioly_scrape" | "generate" | "wiki_scrape"
    label: str                     # human-readable, e.g. "Reprocess foo_test.pdf"
    started_by: str                # username — stable across logout/login
    status: str = "queued"
    phase: str = ""
    done_count: int = 0
    total: int = 0
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    error: str | None = None       # truncated to 500 chars; full traceback goes to the log file
    result: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Per-event index persistence (.qbank_jobs.json)
# ---------------------------------------------------------------------------

_index_locks: dict[str, threading.Lock] = {}
_index_locks_guard = threading.Lock()


def _index_lock(slug: str) -> threading.Lock:
    with _index_locks_guard:
        lk = _index_locks.get(slug)
        if lk is None:
            lk = _index_locks[slug] = threading.Lock()
        return lk


def _load_index(slug: str) -> list[JobRecord]:
    f = events_mod.EVENTS[slug].jobs_file
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for d in data.get("jobs", []):
        try:
            out.append(JobRecord.from_dict(d))
        except Exception:
            continue
    return out


def _atomic_replace(tmp: Path, dest: Path) -> None:
    # This index is written far more often than the other atomic-write files
    # in this codebase (a progress tick every ~1s during a long job, vs. an
    # occasional Save click) — on Windows, os.replace can transiently raise
    # PermissionError if something else has dest open for reading at that
    # exact instant (no such issue on POSIX, where rename is unconditionally
    # atomic). A short retry absorbs that without changing behavior anywhere
    # writes are rare, so the rest of the codebase's bare os.replace calls
    # are left as-is.
    for attempt in range(5):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def _save_index(slug: str, records: list[JobRecord]) -> None:
    f = events_mod.EVENTS[slug].jobs_file
    f.parent.mkdir(parents=True, exist_ok=True)
    records = records[:_MAX_JOBS_PER_EVENT_INDEX]
    tmp = f.with_suffix(f.suffix + ".tmp")
    tmp.write_text(json.dumps({"jobs": [r.to_dict() for r in records]}, indent=2),
                    encoding="utf-8")
    _atomic_replace(tmp, f)


def _upsert(slug: str, record: JobRecord) -> None:
    with _index_lock(slug):
        records = _load_index(slug)
        records = [r for r in records if r.id != record.id]
        records.insert(0, record)
        _save_index(slug, records)


# ---------------------------------------------------------------------------
# Per-job console log (.qbank_jobs/<job_id>.log)
# ---------------------------------------------------------------------------

def _log_file(slug: str, job_id: str) -> Path:
    d = events_mod.EVENTS[slug].jobs_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job_id}.log"


def _append_log(slug: str, job_id: str, lines: list[str]) -> None:
    if not lines:
        return
    f = _log_file(slug, job_id)
    with f.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    _truncate_log_if_needed(f)


def _truncate_log_if_needed(f: Path) -> None:
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = text.splitlines()
    if len(lines) > _MAX_LOG_LINES:
        kept = lines[-_MAX_LOG_LINES:]
        f.write_text("...[truncated]...\n" + "\n".join(kept) + "\n", encoding="utf-8")


def read_log_tail(slug: str, job_id: str, after: int = 0) -> tuple[list[str], int]:
    """Poll-friendly: pass back the line count you got last time as `after`
    to get only the lines appended since. Returns (new_lines, new_total)."""
    f = _log_file(slug, job_id)
    if not f.exists():
        return [], 0
    lines = f.read_text(encoding="utf-8").splitlines()
    return lines[after:], len(lines)


# ---------------------------------------------------------------------------
# print() capture — thread-isolated via contextvars, fixing a real bug in
# the pattern this replaces (review_app.py's old _DOWNLOAD_JOBS captured
# output by globally monkey-patching builtins.print for the duration of one
# job's thread, then restoring it in a finally — with two job threads alive
# at once, whichever finished first would restore the *global* print out
# from under the other, and lines could leak between jobs' logs). A
# ContextVar's value is local to whichever thread set it, so two jobs
# running back-to-back (the only way they can run, given the global
# concurrency cap of 1) never see each other's output, and the underlying
# print() patch only needs to be installed once, ever.
# ---------------------------------------------------------------------------

_job_log_var: "contextvars.ContextVar[list[str] | None]" = contextvars.ContextVar(
    "job_log", default=None)
_real_print = builtins.print
_print_patch_lock = threading.Lock()
_print_patched = False


def _patched_print(*args, **kwargs):
    sink = _job_log_var.get()
    if sink is not None:
        try:
            sep = kwargs.get("sep", " ")
            sink.append(sep.join(str(a) for a in args))
        except Exception:
            pass
    _real_print(*args, **kwargs)


def _ensure_print_patched() -> None:
    global _print_patched
    with _print_patch_lock:
        if not _print_patched:
            builtins.print = _patched_print
            _print_patched = True


# ---------------------------------------------------------------------------
# Worker loop — exactly one job runs at a time, globally.
# ---------------------------------------------------------------------------

_queue: "deque[tuple[JobRecord, Callable]]" = deque()
_queue_lock = threading.Lock()
_cancel_events: dict[str, threading.Event] = {}
_cancel_events_lock = threading.Lock()
_worker_started = False
_worker_started_lock = threading.Lock()


def _start_worker_once() -> None:
    global _worker_started
    with _worker_started_lock:
        if _worker_started:
            return
        _ensure_print_patched()
        # Daemon: on an unclean process exit, this thread is simply killed —
        # exactly the "interrupted" case recover_interrupted_jobs() handles
        # on the next startup. A non-daemon thread would risk hanging
        # shutdown waiting for a vision-OCR call that may never return.
        threading.Thread(target=_worker_loop, name="jobs-worker", daemon=True).start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        item = None
        with _queue_lock:
            if _queue:
                item = _queue.popleft()
        if item is None:
            time.sleep(0.2)
            continue
        record, target = item
        _run_job(record, target)


def _run_job(record: JobRecord, target: Callable) -> None:
    slug = record.event
    cancel_event = threading.Event()
    with _cancel_events_lock:
        _cancel_events[record.id] = cancel_event

    log_buffer: list[str] = []
    token = _job_log_var.set(log_buffer)

    record.status = "running"
    record.started_at = datetime.now(timezone.utc).isoformat()
    _upsert(slug, record)

    last_flush = time.time()
    last_persist = time.time()

    def _flush_log(force: bool = False) -> None:
        nonlocal last_flush
        if not log_buffer:
            return
        if force or (time.time() - last_flush) >= _PERSIST_INTERVAL:
            pending = log_buffer[:]
            log_buffer.clear()
            _append_log(slug, record.id, pending)
            last_flush = time.time()

    def on_progress(phase: str | None = None, done: int | None = None,
                    total: int | None = None) -> None:
        nonlocal last_persist
        changed = False
        if phase is not None and phase != record.phase:
            record.phase = phase
            changed = True
        if done is not None:
            record.done_count = done
        if total is not None:
            record.total = total
        _flush_log()
        if changed or (time.time() - last_persist) >= _PERSIST_INTERVAL:
            _upsert(slug, record)
            last_persist = time.time()

    def should_cancel() -> bool:
        return cancel_event.is_set()

    try:
        result = target(should_cancel=should_cancel, on_progress=on_progress)
        record.status = "succeeded"
        record.result = result if isinstance(result, dict) else None
    except JobCancelled:
        record.status = "cancelled"
    except Exception as e:
        record.status = "failed"
        record.error = str(e)[:500]
        log_buffer.append(f"FATAL: {e}")
        log_buffer.append(traceback.format_exc())
        _real_print(f"[job {record.id}] FAILED: {e}")
    finally:
        record.finished_at = datetime.now(timezone.utc).isoformat()
        _flush_log(force=True)
        _upsert(slug, record)
        _job_log_var.reset(token)
        with _cancel_events_lock:
            _cancel_events.pop(record.id, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_job(event: str, kind: str, label: str, started_by: str,
               target: Callable[..., dict | None],
               max_queued_per_event: int = 8) -> str:
    """Enqueue work. `target` is called as
    target(should_cancel=<callable>, on_progress=<callable>) on the worker
    thread and may return a dict to store as the job's `result`.

    Raises JobQueueFull if `event` already has too many queued/running
    jobs — callers should turn that into an HTTP 429."""
    _start_worker_once()
    existing = _load_index(event)
    active = sum(1 for r in existing if r.status in ACTIVE_STATUSES)
    if active >= max_queued_per_event:
        raise JobQueueFull(f"{event!r} already has {active} queued/running job(s)")
    record = JobRecord(
        id=uuid.uuid4().hex[:12],
        event=event, kind=kind, label=label, started_by=started_by,
        status="queued", created_at=datetime.now(timezone.utc).isoformat(),
    )
    _upsert(event, record)
    with _queue_lock:
        _queue.append((record, target))
    return record.id


def get_job(event: str, job_id: str) -> JobRecord:
    for r in _load_index(event):
        if r.id == job_id:
            return r
    raise JobNotFound(job_id)


def list_jobs(event: str) -> list[JobRecord]:
    """Most-recent-first (matches on-disk order — newest is always
    inserted at index 0 by _upsert)."""
    return _load_index(event)


def can_cancel(record: JobRecord, username: str, is_coach: bool) -> bool:
    return record.started_by == username or is_coach


def job_to_public_dict(record: JobRecord, username: str, is_coach: bool) -> dict:
    d = record.to_dict()
    d["can_cancel"] = can_cancel(record, username, is_coach)
    return d


def request_cancel(event: str, job_id: str, username: str, is_coach: bool) -> JobRecord:
    """Raises JobNotFound / JobNotAuthorized — callers turn those into
    404 / 403 respectively."""
    record = get_job(event, job_id)
    if not can_cancel(record, username, is_coach):
        raise JobNotAuthorized(job_id)
    if record.status == "queued":
        with _queue_lock:
            for i, (r, _t) in enumerate(_queue):
                if r.id == job_id:
                    del _queue[i]
                    break
        record.status = "cancelled"
        record.finished_at = datetime.now(timezone.utc).isoformat()
        _upsert(event, record)
    elif record.status == "running":
        record.cancel_requested = True
        _upsert(event, record)
        with _cancel_events_lock:
            ev = _cancel_events.get(job_id)
        if ev is not None:
            ev.set()
    return record


def active_job_summary(accessible_slugs: list[str]) -> dict:
    """Backs the header badge: total queued+running job count across every
    event slug the caller can see, plus which of those slugs actually have
    one — lets the badge link straight to the one relevant event's Jobs page
    when there's only one, instead of being a dead-end count for a
    volunteer assigned to several events."""
    count = 0
    slugs_with_active: list[str] = []
    for slug in accessible_slugs:
        if slug not in events_mod.EVENTS:
            continue
        try:
            n = sum(1 for r in _load_index(slug) if r.status in ACTIVE_STATUSES)
        except Exception:
            continue
        if n:
            count += n
            slugs_with_active.append(slug)
    return {"count": count, "slugs": slugs_with_active}


def list_all_jobs() -> list[JobRecord]:
    """Every job across every known event — backs the coach-only global
    dashboard. Most-recent-first within each event, events concatenated in
    no particular order (the dashboard sorts by created_at itself)."""
    out: list[JobRecord] = []
    for slug in events_mod.EVENTS:
        try:
            out.extend(_load_index(slug))
        except Exception:
            continue
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out


def recover_interrupted_jobs() -> int:
    """Call once at app startup. Any job record still "running" is leftover
    from a crash/restart — its OS thread is gone. Mark it "interrupted" (a
    terminal state, not auto-resumed; the user re-triggers the same action).
    Returns the number of jobs recovered."""
    n = 0
    for slug in list(events_mod.EVENTS.keys()):
        try:
            records = _load_index(slug)
        except Exception:
            continue
        changed = False
        for r in records:
            if r.status == "running":
                r.status = "interrupted"
                r.finished_at = datetime.now(timezone.utc).isoformat()
                changed = True
                n += 1
        if changed:
            with _index_lock(slug):
                _save_index(slug, records)
    return n
