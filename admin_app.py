"""
Server-admin web app: stop/start/restart instances, pull+apply code updates
from GitHub (with an automatic pre-update backup), roll back to a previous
backup, and view each instance's systemd journal — all from a browser
instead of SSH.

Deliberately a separate, small Flask app from review_app.py: this app's job
(controlling the OS-level lifecycle of the *other* apps) is a categorically
different privilege boundary from question-bank review, so it gets its own
process, its own system user, and its own narrow sudoers grants. See
deploy/instances.conf for the registry of instances it manages, and
spec.md's section on this app for why the action scripts it shells out to
validate their own arguments instead of being argument-less like
qbank-apply-update.sh.

Run:
  python admin_app.py [--port 5002]

Single hardcoded username "admin" (see ADMIN_USERNAME below); password hash
comes from the ADMIN_PASSWORD_HASH env var — generate one with:
  python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

try:
    from flask import (
        Flask, jsonify, request, abort, render_template,
        session, redirect, url_for,
    )
except ImportError:
    print("Flask not installed. Run: pip install flask", file=sys.stderr)
    sys.exit(1)

from werkzeug.security import check_password_hash

from common_ui import COMMON_CSS, COMMON_JS

REPO_ROOT = Path(__file__).parent


def _load_dotenv() -> None:
    """Same minimal loader as build_question_bank.py — kept duplicated
    rather than imported so this app has no dependency on review_app.py's
    import chain (fitz, anthropic, etc. have no business in this process)."""
    env = REPO_ROOT / ".env"
    if env.exists():
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

app = Flask(__name__)

# Shared across every page template via Jinja globals — see templates/*.html
# `{{ common_css|safe }}` / `{{ common_js|safe }}`. Same constants
# review_app.py uses, imported from common_ui.py rather than duplicated.
app.jinja_env.globals["common_css"] = COMMON_CSS
app.jinja_env.globals["common_js"] = COMMON_JS

# ---------------------------------------------------------------------------
# Security config — same patterns as review_app.py (secret key fail-fast,
# session cookie scoping, subpath mounting, CSRF double-submit cookie).
# ---------------------------------------------------------------------------

_secret_key_set = bool(os.environ.get("FLASK_SECRET_KEY"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not _secret_key_set:
    print("WARNING: FLASK_SECRET_KEY not set — using an ephemeral key; "
          "all sessions will be invalidated on restart.", file=sys.stderr)
app.config["SESSION_COOKIE_HTTPONLY"] = True
_session_cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true"
app.config["SESSION_COOKIE_SECURE"] = _session_cookie_secure

if _session_cookie_secure and not _secret_key_set:
    sys.exit(
        "FATAL: SESSION_COOKIE_SECURE=true (a production/HTTPS deploy) but "
        "FLASK_SECRET_KEY is not set. Generate one with "
        "`python -c \"import secrets; print(secrets.token_hex(32))\"` and "
        "set it in .env before starting.")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
if not ADMIN_PASSWORD_HASH:
    sys.exit(
        "FATAL: ADMIN_PASSWORD_HASH is not set. There's no sensible default "
        "for this account — generate one with:\n"
        "  python -c \"from werkzeug.security import generate_password_hash; "
        "print(generate_password_hash('your-password'))\"\n"
        "and set it in .env before starting.")

APPLICATION_ROOT = os.environ.get("APPLICATION_ROOT", "").rstrip("/")


class _PrefixMiddleware:
    """See review_app.py's identical class — same reverse-proxy subpath
    pattern, copied rather than imported to keep this app standalone."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


if APPLICATION_ROOT:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, APPLICATION_ROOT)  # type: ignore[method-assign]

app.config["APPLICATION_ROOT"] = APPLICATION_ROOT or "/"
app.config["SESSION_COOKIE_PATH"] = APPLICATION_ROOT or "/"

_PUBLIC_ENDPOINTS = {"login", "favicon"}
_CSRF_EXEMPT_ENDPOINTS = {"login", "logout"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if not session.get("admin"):
        session.clear()
        return redirect(url_for("login", next=request.script_root + request.path))
    return None


@app.before_request
def _check_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        abort(403, "Missing or invalid CSRF token")
    return None


# Tighter than review_app.py's 5/15min — this surface can stop/start/
# rollback production, so a brute-force attempt should run out of tries
# faster.
_LOGIN_ATTEMPT_WINDOW_SECONDS = 30 * 60
_LOGIN_MAX_ATTEMPTS = 3
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)
_login_attempts_lock = threading.Lock()


def _login_rate_limited(ip: str) -> bool:
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts[ip]
        attempts[:] = [t for t in attempts if now - t < _LOGIN_ATTEMPT_WINDOW_SECONDS]
        return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts[ip].append(time.time())


def _clear_login_failures(ip: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


# ---------------------------------------------------------------------------
# Step-up password confirmation for redeploy/restart/rollback/threads.
#
# These actions already require a valid session (the operator is logged
# in), but that alone means a stolen session cookie could trigger them
# silently. Re-checking the admin password at the moment of the action adds
# a second factor the cookie alone doesn't carry — without introducing a
# new credential: this reuses the exact same ADMIN_PASSWORD_HASH check
# login() already does, never a literal Unix/sudo password (qbank-admin and
# qbank-deploy are nologin service accounts with no real password set
# today; piping one through a web request would be a new, harder-to-rotate
# secret with a much bigger blast radius for no extra benefit here).
#
# Separate rate-limit bucket from the login one above — without this, an
# attacker who already has a valid session (e.g. via a stolen cookie) could
# brute-force the password against these endpoints with no limit at all,
# since they'd never need to touch the actual /login route.
# ---------------------------------------------------------------------------

_STEPUP_ATTEMPT_WINDOW_SECONDS = _LOGIN_ATTEMPT_WINDOW_SECONDS
_STEPUP_MAX_ATTEMPTS = _LOGIN_MAX_ATTEMPTS
_stepup_attempts: dict[str, list[float]] = collections.defaultdict(list)
_stepup_attempts_lock = threading.Lock()


def _stepup_rate_limited(ip: str) -> bool:
    now = time.time()
    with _stepup_attempts_lock:
        attempts = _stepup_attempts[ip]
        attempts[:] = [t for t in attempts if now - t < _STEPUP_ATTEMPT_WINDOW_SECONDS]
        return len(attempts) >= _STEPUP_MAX_ATTEMPTS


def _record_stepup_failure(ip: str) -> None:
    with _stepup_attempts_lock:
        _stepup_attempts[ip].append(time.time())


def _clear_stepup_failures(ip: str) -> None:
    with _stepup_attempts_lock:
        _stepup_attempts.pop(ip, None)


def _check_admin_password(password: str) -> bool:
    return check_password_hash(ADMIN_PASSWORD_HASH, password or "")


def _require_password_or_403(data: dict):
    """Call as the first line of any gated route, passing the parsed
    request body. Returns a Flask response to return immediately on
    failure (429 rate-limited, 403 wrong/missing password), or None if the
    password checked out — the caller proceeds exactly as before."""
    ip = request.remote_addr or "unknown"
    if _stepup_rate_limited(ip):
        return jsonify({"error": "Too many failed confirmations. Try again in a while."}), 429
    password = (data or {}).get("password") or ""
    if not _check_admin_password(password):
        _record_stepup_failure(ip)
        audit(f"{request.endpoint}.confirm", request.path, "DENIED: bad password")
        return jsonify({"error": "Incorrect password"}), 403
    _clear_stepup_failures(ip)
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_rate_limited(ip):
            return render_template(
                "admin/login.html",
                error="Too many failed attempts. Try again in a while."), 429
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if username != ADMIN_USERNAME or not check_password_hash(ADMIN_PASSWORD_HASH, password):
            _record_login_failure(ip)
            return render_template("admin/login.html", error="Invalid username or password")
        _clear_login_failures(ip)
        session.clear()
        session["admin"] = True
        next_url = request.args.get("next") or url_for("dashboard")
        resp = redirect(next_url)
        resp.set_cookie("csrf_token", secrets.token_hex(32),
                         httponly=False, secure=app.config["SESSION_COOKIE_SECURE"],
                         samesite="Lax", path=app.config["SESSION_COOKIE_PATH"])
        return resp
    return render_template("admin/login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    resp.delete_cookie("csrf_token")
    return resp


@app.route("/favicon.ico")
def favicon():
    return "", 404


# ---------------------------------------------------------------------------
# Instance registry — read from deploy/instances.conf (shared with
# deploy/_apply-update.sh, deploy/qbank-rollback.sh, deploy/qbank-service-ctl.sh)
# rather than hardcoded here. Re-read on every request: the file is tiny and
# changes rarely, so there's no point caching it.
# ---------------------------------------------------------------------------

INSTANCES_CONF = REPO_ROOT / "deploy" / "instances.conf"
BACKUP_ROOT = Path(os.environ.get("QBANK_BACKUP_ROOT", "/opt/qbank-backups"))
SERVICE_CTL_SCRIPT = "/usr/local/sbin/qbank-service-ctl.sh"
ROLLBACK_SCRIPT = "/usr/local/sbin/qbank-rollback.sh"
SET_THREADS_SCRIPT = "/usr/local/sbin/qbank-set-threads.sh"
UPDATE_SCRIPT = "/opt/qbank-deploy/update-from-github.sh"
AUDIT_LOG = Path(os.environ.get("ADMIN_AUDIT_LOG", "/var/log/qbank-admin-actions.log"))


@dataclass(frozen=True)
class Instance:
    name: str
    label: str
    app_dir: str
    service: str
    user: str
    port: int


def load_instances() -> list[Instance]:
    if not INSTANCES_CONF.exists():
        return []
    instances = []
    for line in INSTANCES_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 6:
            continue
        name, label, app_dir, service, user, port = parts
        try:
            instances.append(Instance(name, label, app_dir, service, user, int(port)))
        except ValueError:
            continue
    return instances


def get_instance(name: str) -> Instance | None:
    for inst in load_instances():
        if inst.name == name:
            return inst
    return None


def audit(action: str, target: str, outcome: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {action} {target} -> {outcome}\n"
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"WARNING: could not write audit log: {e}", file=sys.stderr)


def service_status(service: str) -> str:
    """'active' / 'inactive' / 'failed' / etc. Reading unit state needs no
    privilege — only stop/start/restart do."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or result.stderr or "unknown").strip()
    except Exception as e:
        return f"error: {e}"


_THREADS_RE = re.compile(r"--threads\s+(\d+)")
_WORKERS_RE = re.compile(r"--workers\s+(\d+)")


def instance_resource_config(service: str) -> dict:
    """Reads the *live, currently-in-effect* --threads/--workers off the
    running unit (systemd merges any drop-in override at parse time, so
    this always reflects reality — never a cached file that could drift
    from it; see qbank-set-threads.sh's header comment for why no file
    under deploy/ or an instance's app_dir is used to store this).

    Read-only unit introspection needs no privilege, same as
    service_status()'s `systemctl is-active` above."""
    try:
        result = subprocess.run(
            ["systemctl", "show", service, "--property=ExecStart", "--value"],
            capture_output=True, text=True, timeout=10,
        )
        line = result.stdout or result.stderr or ""
    except Exception:
        line = ""
    threads_m = _THREADS_RE.search(line)
    workers_m = _WORKERS_RE.search(line)
    return {
        "threads": int(threads_m.group(1)) if threads_m else None,
        "workers": int(workers_m.group(1)) if workers_m else None,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _instance_rows() -> list[dict]:
    rows = []
    for inst in load_instances():
        row = {**inst.__dict__, "status": service_status(inst.service)}
        row.update(instance_resource_config(inst.service))
        rows.append(row)
    return rows


@app.route("/")
def dashboard():
    return render_template("admin/dashboard.html", instances=_instance_rows())


@app.route("/api/status")
def api_status():
    return jsonify({"instances": _instance_rows()})


# ---------------------------------------------------------------------------
# Stop / start / restart
# ---------------------------------------------------------------------------

@app.route("/api/service/<instance_name>/<verb>", methods=["POST"])
def api_service_ctl(instance_name, verb):
    if verb not in ("stop", "start", "restart"):
        return jsonify({"error": f"unknown verb: {verb}"}), 400
    inst = get_instance(instance_name)
    if inst is None:
        return jsonify({"error": f"unknown instance: {instance_name}"}), 404
    # Only "restart" is password-gated (and the same gate covers Set
    # threads below, which also restarts) — stop/start are lower-risk
    # (no code change, frequently used for quick triage) and stay as they
    # were.
    if verb == "restart":
        resp = _require_password_or_403(request.get_json(silent=True) or {})
        if resp:
            return resp
    try:
        result = subprocess.run(
            ["sudo", SERVICE_CTL_SCRIPT, verb, instance_name],
            capture_output=True, text=True, timeout=30,
        )
        ok = result.returncode == 0
        audit(f"service.{verb}", instance_name, "OK" if ok else f"FAILED: {result.stderr.strip()}")
        return jsonify({"ok": ok, "output": (result.stdout + result.stderr).strip()}), (200 if ok else 500)
    except Exception as e:
        audit(f"service.{verb}", instance_name, f"ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Threads — gunicorn --threads is configurable per instance via a systemd
# drop-in (see qbank-set-threads.sh's header comment for why instances.conf
# itself is never used to store this). --workers stays hardcoded at 1 in
# that script, independent of anything validated here — see
# build_question_bank.py's per-event state lock and jobs.py's single-
# worker-process job queue for why.
# ---------------------------------------------------------------------------

@app.route("/api/instance/<instance_name>/threads", methods=["POST"])
def api_set_threads(instance_name):
    inst = get_instance(instance_name)
    if inst is None:
        return jsonify({"error": f"unknown instance: {instance_name}"}), 404
    data = request.get_json(silent=True) or {}
    try:
        threads = int(data.get("threads"))
    except (TypeError, ValueError):
        return jsonify({"error": "threads must be an integer"}), 400
    if not (1 <= threads <= 64):
        return jsonify({"error": "threads must be between 1 and 64"}), 400
    # Restarts the instance, same blast radius as a standalone Restart —
    # gated the same way.
    resp = _require_password_or_403(data)
    if resp:
        return resp
    try:
        result = subprocess.run(
            ["sudo", SET_THREADS_SCRIPT, instance_name, str(threads)],
            capture_output=True, text=True, timeout=30,
        )
        ok = result.returncode == 0
        audit("threads.set", f"{instance_name}={threads}", "OK" if ok else f"FAILED: {result.stderr.strip()}")
        return jsonify({"ok": ok, "output": (result.stdout + result.stderr).strip()}), (200 if ok else 500)
    except Exception as e:
        audit("threads.set", f"{instance_name}={threads}", f"ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Update from GitHub — background job, mirrors review_app.py's
# _DOWNLOAD_JOBS pattern (api_download_start/api_download_status) but drives
# a subprocess instead of an in-process function.
# ---------------------------------------------------------------------------

_UPDATE_JOBS: dict = {}


@app.route("/api/update/start", methods=["POST"])
def api_update_start():
    resp = _require_password_or_403(request.get_json(silent=True) or {})
    if resp:
        return resp
    for jid, job in _UPDATE_JOBS.items():
        if not job.get("finished"):
            return jsonify({"error": "an update is already in progress", "job_id": jid}), 409

    job_id = uuid.uuid4().hex[:12]
    job = {
        "log": [],
        "started_at": time.time(),
        "elapsed_s": 0,
        "finished": False,
        "success": False,
    }
    _UPDATE_JOBS[job_id] = job

    def _run():
        try:
            proc = subprocess.Popen(
                # Exec the script directly (not "bash <script>") -- sudoers
                # matches the command being exec'd, and the NOPASSWD grant
                # for qbank-admin is scoped to this exact path; wrapping it
                # in "bash" makes sudo see "bash" as the command and demand
                # a password instead.
                ["sudo", "-u", "qbank-deploy", UPDATE_SCRIPT],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    job["log"].append(line)
                    if len(job["log"]) > 500:
                        job["log"] = job["log"][-500:]
            proc.wait()
            job["success"] = proc.returncode == 0
        except Exception as e:
            job["log"].append(f"FATAL: {e}")
            job["success"] = False
        finally:
            job["finished"] = True
            job["elapsed_s"] = int(time.time() - job["started_at"])
            audit("update.run", "all", "OK" if job["success"] else "FAILED")

    def _watch():
        while not job["finished"]:
            job["elapsed_s"] = int(time.time() - job["started_at"])
            time.sleep(0.5)

    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_watch, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/update/<job_id>")
def api_update_status(job_id):
    job = _UPDATE_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

@app.route("/api/backups/<instance_name>")
def api_list_backups(instance_name):
    inst = get_instance(instance_name)
    if inst is None:
        return jsonify({"error": f"unknown instance: {instance_name}"}), 404
    instance_backup_dir = BACKUP_ROOT / instance_name
    if not instance_backup_dir.is_dir():
        return jsonify({"backups": []})
    backups = sorted((p.name for p in instance_backup_dir.iterdir() if p.is_dir()), reverse=True)
    return jsonify({"backups": backups})


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    data = request.get_json(silent=True) or {}
    instance_name = data.get("instance") or ""
    ts = data.get("timestamp") or ""
    inst = get_instance(instance_name)
    if inst is None:
        return jsonify({"error": f"unknown instance: {instance_name}"}), 404
    if not ts:
        return jsonify({"error": "missing timestamp"}), 400
    resp = _require_password_or_403(data)
    if resp:
        return resp
    try:
        result = subprocess.run(
            ["sudo", ROLLBACK_SCRIPT, instance_name, ts],
            capture_output=True, text=True, timeout=60,
        )
        ok = result.returncode == 0
        audit("rollback", f"{instance_name}@{ts}", "OK" if ok else f"FAILED: {result.stderr.strip()}")
        return jsonify({"ok": ok, "output": (result.stdout + result.stderr).strip()}), (200 if ok else 500)
    except Exception as e:
        audit("rollback", f"{instance_name}@{ts}", f"ERROR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Console (journalctl) — read-only, no sudo: relies on this app's system
# user being a member of the systemd-journal group.
# ---------------------------------------------------------------------------

@app.route("/api/console/<instance_name>")
def api_console(instance_name):
    inst = get_instance(instance_name)
    if inst is None:
        return jsonify({"error": f"unknown instance: {instance_name}"}), 404
    try:
        result = subprocess.run(
            ["journalctl", "-u", inst.service, "-n", "200", "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
        lines = (result.stdout or result.stderr or "").splitlines()
        return jsonify({"lines": lines})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Server-admin app for the Sci-Oly question-bank instances")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not _secret_key_set:
        sys.exit(
            f"FATAL: --host {args.host} is not loopback-only, but "
            "FLASK_SECRET_KEY is not set. Set it in .env (or the "
            "environment) before binding to a non-local address.")
    print(f"\nOpen http://{args.host}:{args.port}/ in your browser.")
    print("Press Ctrl+C to stop.\n")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
