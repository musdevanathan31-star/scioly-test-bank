#!/usr/bin/env python3
"""
Console load test for "how many students can log in and take a test at
once" — see HOWTO.md's "Measuring server capacity (load testing)" section
for the full walkthrough and the architectural reason this has to be
measured rather than computed from CPU/RAM: gunicorn --workers is hard-
locked to 1 (see deploy/qbank.service), so the ceiling depends on how much
load one process's --threads pool can absorb under real concurrency, not
raw compute. (Response storage itself used to be a second, worse
bottleneck here — every answer autosave round-tripped ONE global JSON file
behind ONE global lock, testing.py's old RESPONSES_FILE/
_responses_transaction() — but that's fixed as of the per-(test_id,
username)-file redesign; this tool is what proved the old design's
super-linear latency growth in the first place.)

This talks HTTP to a REAL running instance (local or the real deployed
server) rather than importing review_app.py in-process, because the whole
point is exercising gunicorn's actual concurrency, which only exists in a
running process.

PREREQUISITE — do this once, by hand, via the normal coach UI, BEFORE
running this script:
  1. Create a throwaway season (Club Management -> New season). Note its
     season_id.
  2. Create a test window for one real event that has a handful of MCQ/
     matching questions kept (those autosave on every click with no
     debounce, unlike FRQ — see templates/test_take.html — so they're the
     realistic worst case).
  3. Publish the test, then go live.
  4. Note the test_id (visible in the Tests dashboard / its URL).
This script creates and cleans up ONLY synthetic loadtest_* student
accounts. It never creates or deletes the season/window/test itself, and
never touches any real student's data.

Usage:
  python loadtest_students.py --url https://your-server --test-id <id> --season-id <id>

Credentials for a coach account are read from QBANK_LOADTEST_COACH_USER /
QBANK_LOADTEST_COACH_PASS if set, else prompted interactively. Never pass
them as a plain --flag — that lands in shell history.

Intended to be run by a human, off-hours, against a server they're
allowed to load-test. Prints the target and step plan and requires typed
confirmation before sending any load.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import math
import os
import random
import statistics
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

REQUEST_TIMEOUT = 30
STEP_PAUSE_SECONDS = 5
JITTER_RANGE_S = (0.05, 0.25)
DEFAULT_STEPS = "5,10,20,40,80,160"
DEFAULT_MAX_STUDENTS = 200


class LoadTestError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def csrf_headers(session: requests.Session) -> dict:
    token = session.cookies.get("csrf_token")
    if not token:
        raise LoadTestError("session has no csrf_token cookie — login didn't complete")
    return {"X-CSRF-Token": token}


def login(base_url: str, username: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{base_url}/login", data={"username": username, "password": password},
               timeout=REQUEST_TIMEOUT)
    if "csrf_token" not in s.cookies:
        raise LoadTestError(
            f"login failed for {username!r} (HTTP {r.status_code}) — check credentials and --url")
    return s


def random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Setup: discover the throwaway test, create synthetic students
# ---------------------------------------------------------------------------

@dataclass
class SyntheticStudent:
    username: str
    password: str
    session: requests.Session = None


def discover_test(base_url: str, coach: requests.Session, test_id: str) -> dict:
    r = coach.get(f"{base_url}/api/tests/{test_id}", timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise LoadTestError(f"GET /api/tests/{test_id} -> HTTP {r.status_code}: {r.text[:300]}")
    test = r.json()
    if test.get("status") != "live":
        raise LoadTestError(
            f"test {test_id} has status {test.get('status')!r}, not 'live' — publish it and "
            f"go live first (this is a safety check: /take and /answer both 403 otherwise)")
    return test


def create_synthetic_students(base_url: str, coach: requests.Session, season_id: str,
                               event_slug: str, count: int) -> list[SyntheticStudent]:
    """Creates up to `count` synthetic accounts via the existing bulk-CSV
    roster-import endpoint. Never raises on partial failure — returns
    whatever was actually created (see auth.create_users_bulk's docstring:
    it continues past a bad row) so the caller can still clean up a
    partial batch instead of orphaning accounts this function knows about
    but never reports."""
    run_tag = f"{random.randint(100000, 999999)}"
    rows = [{
        "display_name": f"Load Test {i:04d}",
        "username": f"loadtest_{run_tag}_{i:04d}",
        "password": random_password(),
        "events": event_slug,
    } for i in range(count)]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["display_name", "username", "password", "events"])
    writer.writeheader()
    writer.writerows(rows)

    r = coach.post(
        f"{base_url}/api/seasons/{season_id}/students/bulk-csv",
        files={"file": ("loadtest_students.csv", buf.getvalue().encode("utf-8"), "text/csv")},
        headers=csrf_headers(coach),
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        raise LoadTestError(f"bulk student creation -> HTTP {r.status_code}: {r.text[:300]}")
    result = r.json()
    for err in result.get("errors", []):
        print(f"  ! row {err['row']} failed: {err['reason']}", file=sys.stderr)

    by_username = {row["username"]: row["password"] for row in rows}
    return [SyntheticStudent(username=c["username"], password=by_username[c["username"]])
            for c in result.get("created", [])]


LOADTEST_PREFIX = "loadtest_"


def _purge_or_disable(base_url: str, coach: requests.Session, username: str) -> str:
    """Remove one synthetic account. Returns "deleted", "disabled" or "failed".

    Prefers a real delete, because these accounts are litter: they exist
    only to have been typed at by a script, and leaving them disabled means
    every later Manage Users screen is mostly load-test noise. Falls back to
    disabling when the instance has ALLOW_HARD_DELETE off (the route 403s),
    which is the correct answer there rather than an error -- that flag is
    the operator's decision, not this script's.
    """
    try:
        r = coach.delete(f"{base_url}/api/purge/user/{username}",
                         headers=csrf_headers(coach), timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return "deleted"
        if r.status_code != 403:
            print(f"  ! purge {username}: HTTP {r.status_code}", file=sys.stderr)
        # 403 = hard delete not enabled on this instance; fall through.
        r = coach.delete(f"{base_url}/admin/users/{username}",
                         headers=csrf_headers(coach), timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return "disabled"
        print(f"  ! disable {username}: HTTP {r.status_code}", file=sys.stderr)
        return "failed"
    except requests.RequestException as e:
        print(f"  ! cleanup {username}: {e}", file=sys.stderr)
        return "failed"


def _report_cleanup(counts: dict) -> None:
    if counts.get("deleted"):
        print(f"  {counts['deleted']} account(s) permanently deleted.")
    if counts.get("disabled"):
        print(f"  {counts['disabled']} account(s) disabled but NOT deleted — this instance "
              f"has ALLOW_HARD_DELETE off. Set it and re-run with --cleanup-existing "
              f"to remove them for good.")
    if counts.get("failed"):
        print(f"  {counts['failed']} account(s) could not be cleaned up (see errors above).",
              file=sys.stderr)


def disable_synthetic_students(base_url: str, coach: requests.Session,
                                students: list[SyntheticStudent]) -> None:
    if not students:
        return
    print(f"Cleaning up {len(students)} synthetic student account(s)...")
    counts: dict = {}
    for s in students:
        outcome = _purge_or_disable(base_url, coach, s.username)
        counts[outcome] = counts.get(outcome, 0) + 1
    _report_cleanup(counts)


# ---------------------------------------------------------------------------
# Load generation — the answer-save endpoint is the hot path being measured
# ---------------------------------------------------------------------------

def answer_for(question: dict) -> dict:
    """A plausible-shaped answer for any qtype. Not meant to be graded
    correctly — only to exercise the same save_answer() code path a real
    client would, with the same payload shape."""
    qtype = question.get("qtype")
    if qtype == "mcq":
        choices = question.get("choices") or []
        letter = choices[0]["letter"] if choices else "A"
        return {"qtype": "mcq", "picked": letter}
    if qtype == "matching":
        m = question.get("matching") or {}
        left = m.get("left") or []
        right = m.get("right") or []
        right_label = right[0]["label"] if right else "1"
        return {"qtype": "matching", "picks": {l["label"]: right_label for l in left}}
    return {"qtype": "frq", "text": "load test placeholder answer"}


@dataclass
class SaveResult:
    ok: bool
    elapsed: float
    error: str = ""


def run_one_student(base_url: str, test_id: str, student: SyntheticStudent) -> list[SaveResult]:
    s = student.session
    try:
        r = s.get(f"{base_url}/api/my-tests/{test_id}/take", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        questions = r.json().get("questions", [])
    except requests.RequestException as e:
        return [SaveResult(ok=False, elapsed=0.0, error=f"take failed: {e}")]

    results: list[SaveResult] = []
    for q in questions:
        time.sleep(random.uniform(*JITTER_RANGE_S))
        payload = {"number": str(q.get("number", "")), "answer": answer_for(q)}
        t0 = time.monotonic()
        try:
            r = s.post(f"{base_url}/api/my-tests/{test_id}/answer", json=payload,
                       headers=csrf_headers(s), timeout=REQUEST_TIMEOUT)
            elapsed = time.monotonic() - t0
            ok = r.status_code == 200 and r.json().get("ok") is True
            results.append(SaveResult(ok=ok, elapsed=elapsed, error="" if ok else r.text[:200]))
        except requests.RequestException as e:
            results.append(SaveResult(ok=False, elapsed=time.monotonic() - t0, error=str(e)))
    return results


def run_step(base_url: str, test_id: str, students: list[SyntheticStudent]) -> list[SaveResult]:
    all_results: list[SaveResult] = []
    with ThreadPoolExecutor(max_workers=len(students)) as pool:
        futures = [pool.submit(run_one_student, base_url, test_id, s) for s in students]
        for f in futures:
            all_results.extend(f.result())
    return all_results


def summarize(step_n: int, results: list[SaveResult]) -> dict:
    latencies = [r.elapsed for r in results if r.ok]
    errors = [r for r in results if not r.ok]
    p95 = (statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20
           else (max(latencies) if latencies else float("nan")))
    return {
        "step": step_n,
        "saves": len(results),
        "errors": len(errors),
        "error_rate": (len(errors) / len(results)) if results else 0.0,
        "p50": statistics.median(latencies) if latencies else float("nan"),
        "p95": p95,
        "max": max(latencies) if latencies else float("nan"),
        "sample_errors": [e.error for e in errors[:3]],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_steps(raw: str) -> list[int]:
    try:
        steps = sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
    except ValueError:
        raise LoadTestError(f"--steps must be comma-separated integers, got {raw!r}")
    if not steps or any(n <= 0 for n in steps):
        raise LoadTestError("--steps must be positive integers")
    return steps


def get_coach_credentials() -> tuple[str, str]:
    username = os.environ.get("QBANK_LOADTEST_COACH_USER") or input("Coach username: ").strip()
    password = os.environ.get("QBANK_LOADTEST_COACH_PASS") or getpass.getpass("Coach password: ")
    return username, password


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load-test the answer-autosave endpoint against a real running instance "
                     "to find how many students can test at once. See this file's module "
                     "docstring for the one-time manual setup step required first.")
    parser.add_argument("--url", required=True,
                         help="Base URL of the running instance, e.g. https://qbank.example.org")
    parser.add_argument("--test-id", required=True,
                         help="test_id of an already-published, already-live throwaway test")
    parser.add_argument("--season-id", required=True,
                         help="season_id the throwaway test's window belongs to")
    parser.add_argument("--steps", default=DEFAULT_STEPS,
                         help=f"comma-separated concurrent-student counts to ramp through "
                              f"(default {DEFAULT_STEPS})")
    parser.add_argument("--max-students", type=int, default=DEFAULT_MAX_STUDENTS,
                         help=f"refuse to run if the largest step exceeds this (default "
                              f"{DEFAULT_MAX_STUDENTS})")
    parser.add_argument("--max-error-rate", type=float, default=0.05,
                         help="stop ramping once a step's error rate exceeds this fraction "
                              "(default 0.05)")
    parser.add_argument("--max-p95-seconds", type=float, default=10.0,
                         help="stop ramping once a step's p95 answer-save latency exceeds this "
                              "many seconds (default 10.0)")
    parser.add_argument("--yes", action="store_true",
                         help="skip the typed confirmation prompt (for repeat/scripted use only)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    try:
        steps = parse_steps(args.steps)
    except LoadTestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if steps[-1] > args.max_students:
        print(f"Largest step ({steps[-1]}) exceeds --max-students ({args.max_students}); "
              f"pass a larger --max-students if you really mean this.", file=sys.stderr)
        return 2

    print("=" * 70)
    print("LOAD TEST — this will send real concurrent HTTP traffic to:")
    print(f"  {base_url}")
    print(f"test_id={args.test_id}  season_id={args.season_id}")
    print(f"Ramp steps (concurrent students): {steps}")
    print("Only synthetic loadtest_* accounts are created/used; no real student data is touched.")
    print("Run this off-hours.")
    print("=" * 70)
    if not args.yes:
        confirm = input("Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return 1

    username, password = get_coach_credentials()
    print(f"Logging in as coach {username!r}...")
    coach = login(base_url, username, password)

    test = discover_test(base_url, coach, args.test_id)
    event_slug = test["event_slug"]
    print(f"Confirmed test {args.test_id} is live for event {event_slug!r}.")

    max_n = steps[-1]
    print(f"Creating {max_n} synthetic student account(s)...")
    students = create_synthetic_students(base_url, coach, args.season_id, event_slug, max_n)

    try:
        if len(students) < max_n:
            raise LoadTestError(
                f"asked for {max_n} synthetic students, only {len(students)} were created (see "
                f"errors above) — aborting rather than testing at a smaller scale without you "
                f"knowing. Cleaning up the partial batch.")

        print(f"Logging in {len(students)} synthetic student(s)...")
        for s in students:
            s.session = login(base_url, s.username, s.password)

        print()
        header = (f"{'students':>9} | {'saves':>6} | {'errors':>6} | {'err%':>6} | "
                  f"{'p50s':>7} | {'p95s':>7} | {'maxs':>7}")
        print(header)
        print("-" * len(header))

        broke = False
        for n in steps:
            active = students[:n]
            results = run_step(base_url, args.test_id, active)
            stats = summarize(n, results)
            print(f"{stats['step']:>9} | {stats['saves']:>6} | {stats['errors']:>6} | "
                  f"{stats['error_rate'] * 100:>5.1f}% | {stats['p50']:>7.2f} | "
                  f"{stats['p95']:>7.2f} | {stats['max']:>7.2f}")
            for e in stats["sample_errors"]:
                print(f"    e.g. {e}")

            p95_broke = not math.isnan(stats["p95"]) and stats["p95"] > args.max_p95_seconds
            if stats["error_rate"] > args.max_error_rate or p95_broke:
                print(f"\n>>> Breaking point reached at {n} concurrent students (error rate "
                      f"{stats['error_rate'] * 100:.1f}% or p95 {stats['p95']:.2f}s past "
                      f"threshold). Stopping ramp.")
                broke = True
                break
            if n != steps[-1]:
                time.sleep(STEP_PAUSE_SECONDS)

        if not broke:
            print(f"\nNo breaking point hit up to {steps[-1]} concurrent students — try a "
                  f"larger --steps list to find the real ceiling.")
    finally:
        disable_synthetic_students(base_url, coach, students)
        print("\nReminder: the throwaway season/window/test itself was not touched by this "
              "script (no HTTP delete route exists for it) — remove it by hand via the coach "
              "UI if you're done with it.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LoadTestError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
