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
    r = coach.get(f"{base_url}/api/assessments/{test_id}", timeout=REQUEST_TIMEOUT)
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


# ---------------------------------------------------------------------------
# Scenarios — what the synthetic students actually do
#
# The default ("answer") measures the answer-autosave hot path. The others
# exist because that path is deliberately the *cheapest* one in the app:
# it writes one small per-(assessment, student) file under its own lock, so
# it parallelises well and tells you almost nothing about the paths that
# don't.
#
# The expensive paths all funnel through auth.py's single _users_lock,
# which guards one JSON file for every account. Password hashing is scrypt
# (~80ms per operation on typical hardware), and change_own_password holds
# that global lock across TWO of them — a verify and a generate. Meanwhile
# review_app's _require_login calls auth.get_user() on every authenticated
# request, taking the same lock to read. So a burst of password changes can
# in principle stall page loads and answer saves for everyone, not just the
# students changing passwords. "mixed" is the scenario that can actually
# show that, because it runs both at once and reports them separately.
# ---------------------------------------------------------------------------

SCENARIOS = ("answer", "login", "login-storm", "password", "mixed")


#: Activities whose failures are the point of the exercise rather than a
#: symptom, so they are reported but never judged against the thresholds.
EXPECTED_FAILURE_KINDS = ("login-typo",)


@dataclass
class OpResult:
    """One timed request. `kind` lets a mixed run report each activity
    separately — an average across "answer save" and "password change" would
    hide exactly the interference this is looking for."""
    kind: str
    ok: bool
    elapsed: float
    error: str = ""
    status: int = 0


def _timed(fn) -> tuple[float, object]:
    t0 = time.monotonic()
    result = fn()
    return time.monotonic() - t0, result


def op_login(base_url: str, student: SyntheticStudent, password: str = None) -> OpResult:
    """A full fresh login, including the scrypt verify. Uses its own Session
    so no cookie from an earlier login short-circuits it."""
    s = requests.Session()
    s.get(f"{base_url}/login", timeout=REQUEST_TIMEOUT)
    try:
        elapsed, r = _timed(lambda: s.post(
            f"{base_url}/login",
            data={"username": student.username,
                  "password": password if password is not None else student.password},
            allow_redirects=False, timeout=REQUEST_TIMEOUT))
    except requests.RequestException as e:
        return OpResult("login", False, 0.0, str(e))
    # 302 = success; 200 = the login page re-rendered with an error;
    # 429 = the per-IP rate limiter refused before checking credentials.
    ok = r.status_code in (302, 303)
    if ok:
        student.session = s
    return OpResult("login", ok, elapsed,
                    "" if ok else f"HTTP {r.status_code}", r.status_code)


def op_change_password(base_url: str, student: SyntheticStudent) -> OpResult:
    """Change the password to a new value and keep it on the student, so
    repeated rounds stay valid. This is the path that holds the global user
    lock across two scrypt operations."""
    s = student.session
    new_password = random_password()
    try:
        elapsed, r = _timed(lambda: s.post(
            f"{base_url}/api/account/password",
            json={"current_password": student.password, "new_password": new_password},
            headers=csrf_headers(s), timeout=REQUEST_TIMEOUT))
    except requests.RequestException as e:
        return OpResult("password", False, 0.0, str(e))
    ok = r.status_code == 200
    if ok:
        student.password = new_password
    return OpResult("password", ok, elapsed,
                    "" if ok else f"HTTP {r.status_code}: {r.text[:120]}", r.status_code)


def op_browse(base_url: str, student: SyntheticStudent) -> OpResult:
    """A plain authenticated page load. Cheap in itself, which is the point:
    it is the canary for _users_lock contention, because _require_login
    reads the user table on every single request."""
    s = student.session
    try:
        elapsed, r = _timed(lambda: s.get(f"{base_url}/my-assessments",
                                          timeout=REQUEST_TIMEOUT))
    except requests.RequestException as e:
        return OpResult("browse", False, 0.0, str(e))
    ok = r.status_code == 200
    return OpResult("browse", ok, elapsed, "" if ok else f"HTTP {r.status_code}", r.status_code)


def op_answer_round(base_url: str, test_id: str, student: SyntheticStudent,
                    think: tuple) -> list[OpResult]:
    """One pass through every question on the test, saving each answer."""
    s = student.session
    out: list[OpResult] = []
    try:
        r = s.get(f"{base_url}/api/my-assessments/{test_id}/take", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        questions = r.json().get("questions", [])
    except requests.RequestException as e:
        return [OpResult("answer", False, 0.0, f"take failed: {e}")]
    for q in questions:
        time.sleep(random.uniform(*think))
        payload = {"number": str(q.get("number", "")), "answer": answer_for(q)}
        try:
            elapsed, resp = _timed(lambda: s.post(
                f"{base_url}/api/my-assessments/{test_id}/answer", json=payload,
                headers=csrf_headers(s), timeout=REQUEST_TIMEOUT))
            ok = resp.status_code == 200 and resp.json().get("ok") is True
            out.append(OpResult("answer", ok, elapsed,
                                "" if ok else resp.text[:160], resp.status_code))
        except requests.RequestException as e:
            out.append(OpResult("answer", False, 0.0, str(e)))
    return out


def _student_work(scenario: str, base_url: str, test_id: str,
                  student: SyntheticStudent, index: int, think: tuple) -> list[OpResult]:
    if scenario == "answer":
        return op_answer_round(base_url, test_id, student, think)
    if scenario == "login":
        return [op_login(base_url, student)]
    if scenario == "login-storm":
        # Every 4th student fumbles their password first, then retries
        # correctly — the realistic shape of a room full of students typing
        # a password they were handed five minutes ago. All of them share
        # one public IP, which is the whole point: review_app's rate limiter
        # counts failures per IP, not per account.
        out = []
        if index % 4 == 0:
            # Tagged as its own activity: this attempt is SUPPOSED to fail,
            # so counting it as an error would both misreport the failure
            # rate and trip the breaking-point threshold on a healthy run.
            # What matters from these is the 429 column, not err%.
            bad = op_login(base_url, student, password="definitely-wrong")
            bad.kind = "login-typo"
            out.append(bad)
        out.append(op_login(base_url, student))
        return out
    if scenario == "password":
        return [op_change_password(base_url, student)]
    if scenario == "mixed":
        # Proportions of an actual club meeting: most students working
        # through the test, a few arriving late, one or two changing the
        # password they were just given.
        bucket = index % 10
        if bucket == 0:
            return [op_change_password(base_url, student)]
        if bucket == 1:
            return [op_login(base_url, student)]
        if bucket == 2:
            return [op_browse(base_url, student)]
        return op_answer_round(base_url, test_id, student, think)
    raise LoadTestError(f"unknown scenario: {scenario}")


def run_step(base_url: str, test_id: str, students: list[SyntheticStudent],
             scenario: str = "answer", think: tuple = JITTER_RANGE_S) -> list[OpResult]:
    """Every student acts at once — genuinely concurrent, one thread each,
    all released together rather than trickled in."""
    all_results: list[OpResult] = []
    with ThreadPoolExecutor(max_workers=len(students)) as pool:
        futures = [pool.submit(_student_work, scenario, base_url, test_id, s, i, think)
                   for i, s in enumerate(students)]
        for f in futures:
            all_results.extend(f.result())
    return all_results


def summarize_by_kind(results: list[OpResult]) -> dict:
    """Per-activity stats. A mixed run averaged into one number would hide
    the thing worth finding: whether the slow path drags the fast one down."""
    out = {}
    for kind in sorted({r.kind for r in results}):
        rows = [r for r in results if r.kind == kind]
        lat = [r.elapsed for r in rows if r.ok]
        errs = [r for r in rows if not r.ok]
        p95 = (statistics.quantiles(lat, n=20)[18] if len(lat) >= 20
               else (max(lat) if lat else float("nan")))
        out[kind] = {
            "ops": len(rows), "errors": len(errs),
            "error_rate": len(errs) / len(rows) if rows else 0.0,
            "p50": statistics.median(lat) if lat else float("nan"),
            "p95": p95, "max": max(lat) if lat else float("nan"),
            "rate_limited": sum(1 for r in rows if r.status == 429),
            "sample_errors": [e.error for e in errs[:2]],
        }
    return out


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
    parser.add_argument("--scenario", default="answer", choices=SCENARIOS,
                         help="what the students do concurrently. answer=autosave hot path "
                              "(default); login=fresh simultaneous logins; login-storm=logins "
                              "with some wrong passwords from one IP, to exercise the per-IP "
                              "rate limiter; password=simultaneous password changes (holds the "
                              "global user lock across two scrypt hashes); mixed=a club "
                              "meeting, reported per activity")
    parser.add_argument("--think-time", default="0.05,0.25", metavar="MIN,MAX",
                         help="seconds to pause between a student's successive answer saves "
                              "(default 0.05,0.25 = burst). Use something like 3,15 to "
                              "simulate students actually reading the questions.")
    parser.add_argument("--yes", action="store_true",
                         help="skip the typed confirmation prompt (for repeat/scripted use only)")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    try:
        lo, hi = (float(x) for x in args.think_time.split(","))
        if lo < 0 or hi < lo:
            raise ValueError
        think = (lo, hi)
    except ValueError:
        print("ERROR: --think-time must be MIN,MAX seconds with 0 <= MIN <= MAX",
              file=sys.stderr)
        return 2
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
    print(f"Scenario: {args.scenario}"
          + (f"   think-time between saves: {think[0]}-{think[1]}s"
             if args.scenario in ("answer", "mixed") else ""))
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
        header = (f"{'students':>9} | {'activity':>9} | {'ops':>5} | {'errors':>6} | "
                  f"{'err%':>6} | {'429s':>5} | {'p50s':>7} | {'p95s':>7} | {'maxs':>7}")
        print(header)
        print("-" * len(header))

        broke = False
        by_kind: dict = {}
        for n in steps:
            active = students[:n]
            results = run_step(base_url, args.test_id, active, args.scenario, think)
            by_kind = summarize_by_kind(results)

            worst_p95, worst_err = 0.0, 0.0
            for kind, st in by_kind.items():
                p95 = 0.0 if math.isnan(st["p95"]) else st["p95"]
                if kind not in EXPECTED_FAILURE_KINDS:
                    worst_p95 = max(worst_p95, p95)
                    worst_err = max(worst_err, st["error_rate"])
                print(f"{n:>9} | {kind:>9} | {st['ops']:>5} | {st['errors']:>6} | "
                      f"{st['error_rate'] * 100:>5.1f}% | {st['rate_limited']:>5} | "
                      f"{st['p50']:>7.2f} | {st['p95']:>7.2f} | {st['max']:>7.2f}")
                for e in st["sample_errors"]:
                    print(f"    e.g. {e}")

            # Judge the ramp on the worst activity, not an average: a
            # scenario is only as usable as its slowest path.
            if worst_err > args.max_error_rate or worst_p95 > args.max_p95_seconds:
                print(f"\n>>> Breaking point reached at {n} concurrent students "
                      f"(worst activity: {worst_err * 100:.1f}% errors, "
                      f"p95 {worst_p95:.2f}s). Stopping ramp.")
                broke = True
                break
            if n != steps[-1]:
                time.sleep(STEP_PAUSE_SECONDS)

        if not broke:
            print(f"\nNo breaking point hit up to {steps[-1]} concurrent students. "
                  f"Try a larger --steps list to find the real ceiling.")
        if args.scenario == "mixed":
            print("\nIn a mixed run, compare the 'answer'/'browse' rows against 'password': if "
                  "the cheap paths slow down in step with the expensive one, they are queueing "
                  "behind the same global user lock (auth.py's _users_lock) rather than "
                  "competing for CPU.")
        if any(st["rate_limited"] for st in by_kind.values()):
            print("\n>>> Some requests got HTTP 429. review_app's login rate limiter counts "
                  "FAILED ATTEMPTS PER SOURCE IP (5 per 15 min), and a room of students shares "
                  "one public IP, so a handful of mistyped passwords can lock out everyone "
                  "else, correct password or not.")

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
