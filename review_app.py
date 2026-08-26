"""
Flask review UI for Sci-Oly question banks (multi-event; see events.py).

Run:
  pip install flask
  python review_app.py [--port 5000]

Workflow:
  1. Browse all test PDFs (sort by name / modified / size / question count).
  2. Open a PDF to extract its questions page by page.
  3. Edit question text, topic, choices, answer. Add or delete questions.
  4. Reassign images to questions: click an image in the bay -> click a
     question card -> assigned. Click the X on an attached image to detach.
  5. "OCR this page" calls Haiku vision and shows suggestions you can accept.
  6. "Reprocess PDF" wipes that PDF's cache and re-runs the pipeline.
  7. "Save" persists edits to .qbank_state.json.

All edits land in the same .qbank_state.json and question_bank.md the CLI uses.
"""

from __future__ import annotations

import argparse
import base64
import collections
import copy
import json
import hashlib
import uuid
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

try:
    from flask import (
        Flask, jsonify, request, send_file, abort, Response, render_template,
        session, redirect, url_for, g,
    )
except ImportError:
    print("Flask not installed. Run: pip install flask", file=sys.stderr)
    sys.exit(1)

import fitz

sys.path.insert(0, str(Path(__file__).parent))
# Import module-style so build_question_bank.BASE_DIR etc. follow set_event().
import build_question_bank as bqb  # noqa: E402
from build_question_bank import (  # noqa: E402
    _vision_available, vision_extract_text_page,
    vision_to_latex, validate_answer, region_image_b64,
    classify_topic, split_choices, split_choices_by_lines, _strip_points,
    vision_extract_region, split_column_items, vision_extract_column,
    process_pair, apply_annotations,
)
from events import EVENTS, get_event, add_custom_event, is_builtin, DATA_ROOT, relative_data_path  # noqa: E402
import text_utils  # noqa: E402
import texts as texts_mod  # noqa: E402
import qgen  # noqa: E402
import scrape_scioly  # noqa: E402
import download_event  # noqa: E402
import presence  # noqa: E402
import deletion  # noqa: E402
import tournament_archive  # noqa: E402
import archive_map  # noqa: E402
import archive_ops  # noqa: E402
import archive_import  # noqa: E402
import llm_providers  # noqa: E402
import auth  # noqa: E402
import seasons  # noqa: E402
import assessments  # noqa: E402
import archive  # noqa: E402
import pdf_safety  # noqa: E402
import doc_convert  # noqa: E402
import jobs  # noqa: E402
from common_ui import COMMON_CSS as _COMMON_CSS, COMMON_JS as _COMMON_JS  # noqa: E402

app = Flask(__name__)
# Cap user uploads at 300 MB. Without this Flask accepts the full body into
# memory; a stray multi-GB file would OOM the process. Raised from the
# original 50 MB once real shared textbooks (e.g. a 251 MB scanned PDF)
# started getting rejected by the upload form. If fronted by a reverse
# proxy (Caddy/nginx), its own client-body-size limit must be raised to
# match or it'll reject large uploads before Flask ever sees them.
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

# Session signing key. In production set FLASK_SECRET_KEY so sessions survive
# a restart; falling back to a random per-process key keeps local/dev usage
# working with zero setup (everyone just gets logged out on restart).
_secret_key_set = bool(os.environ.get("FLASK_SECRET_KEY"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not _secret_key_set:
    print("WARNING: FLASK_SECRET_KEY not set — using an ephemeral key; "
          "all sessions will be invalidated on restart.", file=sys.stderr)
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Only mark cookies Secure once actually served over HTTPS (e.g. behind
# Caddy) — over plain local-dev HTTP the browser would otherwise refuse to
# send the cookie at all and login would silently never "stick".
_session_cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true"
app.config["SESSION_COOKIE_SECURE"] = _session_cookie_secure

# SESSION_COOKIE_SECURE=true is the operator's own signal that this instance
# is being served over real HTTPS (e.g. behind Caddy) — i.e. a production
# deploy, not a `python review_app.py` localhost dev session. Refuse to
# start in that case without a real FLASK_SECRET_KEY: the alternative is
# silently issuing sessions signed with a key that's regenerated (and every
# existing session invalidated) on every restart, which is exactly the kind
# of "ran fine until it didn't" misconfiguration this check exists to catch
# before it reaches a real user. Local dev (SESSION_COOKIE_SECURE unset)
# keeps working with zero config, exactly as today.
if _session_cookie_secure and not _secret_key_set:
    sys.exit(
        "FATAL: SESSION_COOKIE_SECURE=true (a production/HTTPS deploy) but "
        "FLASK_SECRET_KEY is not set. Generate one with "
        "`python -c \"import secrets; print(secrets.token_hex(32))\"` and "
        "set it in .env before starting.")

# When the app is reverse-proxied under a path prefix (e.g. Caddy forwarding
# https://host/testbank/ncms/* straight through to this process), set
# APPLICATION_ROOT so Flask's url_for()/request.script_root know about it.
# Unset (the local-dev default) makes this a no-op.
APPLICATION_ROOT = os.environ.get("APPLICATION_ROOT", "").rstrip("/")


class _PrefixMiddleware:
    """Strips a mount-point prefix from PATH_INFO into SCRIPT_NAME so
    url_for() and request.script_root produce prefixed URLs. Standard
    Werkzeug pattern for apps sitting behind a path-based reverse proxy."""

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

# Scope cookies to the mount prefix so two independently-mounted instances
# sharing one domain (e.g. /testbank/ncms and /testbank/chs) never receive
# each other's session/CSRF cookies. Flask's APPLICATION_ROOT config key
# (distinct from the plain APPLICATION_ROOT variable above, which only
# drives _PrefixMiddleware) is what SESSION_COOKIE_PATH falls back to when
# unset, so set both explicitly.
app.config["APPLICATION_ROOT"] = APPLICATION_ROOT or "/"
app.config["SESSION_COOKIE_PATH"] = APPLICATION_ROOT or "/"

# Question-type discriminator values a client is allowed to set. Anything
# outside this set is rejected/ignored rather than persisted, so a stray or
# malformed value can never silently skip the type-specific auto-grading
# path (assessments._grade_mcq/_grade_tf/_grade_matching each key off qtype).
_VALID_QTYPES = {"mcq", "frq", "tf", "matching"}

# Routes reachable without being logged in.
_PUBLIC_ENDPOINTS = {"login", "favicon", "static"}

# Background pollers that must NOT count as activity. Both badges in
# _user_badge.html refresh themselves every 20s, so stamping presence on
# them would make every *open tab* permanently "active" — the number would
# measure tabs rather than people, and would stop correlating with load at
# all (an idle tab costs almost nothing; a reprocess costs a lot). With
# these exempt, "active" means the user actually did something.
_PRESENCE_EXEMPT_ENDPOINTS = {"api_jobs_active_count", "api_presence"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    username = session.get("username")
    user = auth.get_user(username) if username else None
    if user is None or user.disabled:
        # Treat a disabled account exactly like a deleted one — kicks any
        # already-logged-in session on its very next request.
        session.clear()
        return redirect(url_for("login", next=request.script_root + request.path))
    g.user = user
    if request.endpoint not in _PRESENCE_EXEMPT_ENDPOINTS:
        presence.touch(user.username, user.role)
    return None


@app.context_processor
def _inject_user():
    return {"current_user": getattr(g, "user", None)}


@app.context_processor
def _inject_school_logo():
    """Per-instance branding, opt-in via SCHOOL_LOGO in the instance's .env.

    The file lives in static/ rather than DATA_ROOT because static/ is part
    of the code allow-list _apply-update.sh syncs, so it deploys with the
    code and needs no manual copy onto the server. Both schools therefore
    have the file, and it is the env var -- not the file's presence -- that
    decides who shows it, which is what keeps CHS unbranded while sharing
    one code tree.

    Only the basename is honoured: this is operator-set config rather than
    user input, but a value that can walk out of static/ is not worth
    allowing for the sake of a filename nobody needs to nest.
    """
    raw = (os.environ.get("SCHOOL_LOGO") or "").strip()
    if not raw:
        return {"school_logo": None}
    name = os.path.basename(raw)
    if not (_STATIC_DIR / name).is_file():
        # Wrong filename is far likelier than a missing deploy, and a
        # silently absent logo is hard to diagnose from the browser.
        app.logger.warning("SCHOOL_LOGO=%r not found in static/ — no logo shown", raw)
        return {"school_logo": None}
    return {"school_logo": name}


@app.context_processor
def _inject_school_name():
    return {"school_name": os.environ.get("SCHOOL_NAME", "NCMS").upper()}


@app.context_processor
def _inject_nav():
    """Accessible events for the navicon's per-event accordions
    (templates/_user_badge.html) — same access rule index() already uses
    (`role != "coach" and slug not in user.events`), but lighter (no per-
    event PDF/state scan) since this now runs on every page render, not
    just the landing page.

    Students are excluded explicitly rather than by assuming their
    `user.events` is empty. That assumption used to be written here and it
    was wrong: a student can carry event slugs (the Club Management CSV
    bulk-add takes an `events` column, and a coach can set them from Manage
    Users), which put Question bank / Test bank / Primary sources in a
    student's menu. Every one of those links 403s at _select_event, so it
    was never an access hole — but it listed event names to students and
    offered them destinations that only ever fail. Matching
    _select_event()'s blanket student block here keeps the two in step."""
    user = getattr(g, "user", None)
    if user is None:
        return {}
    if user.role == "student":
        return {"nav_events": []}
    nav_events = [
        {"slug": slug, "name": ev.name}
        for slug, ev in sorted(EVENTS.items())
        if not ev.archived and (user.role == "coach" or slug in user.events)
    ]
    return {"nav_events": nav_events}


# Routes that mutate state via a plain HTML <form> POST (not fetch()) can't
# attach a custom header, so they're exempt from the CSRF check below —
# both are auth-flow routes, not data mutations, and login CSRF/logout CSRF
# aren't meaningful threats in this app's model.
_CSRF_EXEMPT_ENDPOINTS = {"login", "logout"}


@app.before_request
def _check_csrf():
    """Double-submit-cookie CSRF check for every mutating request. The
    matching `csrf_token` cookie is issued on login (non-HttpOnly, so the
    frontend's `window.fetch` patch — see _COMMON_JS — can read it and
    attach it as X-CSRF-Token on every request)."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None
    cookie_token = request.cookies.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token")
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        abort(403, "Missing or invalid CSRF token")
    return None


def hard_delete_required(view):
    """Gate a route on ALLOW_HARD_DELETE being set for this instance.

    Layered under @coach_required, never instead of it: the flag decides
    whether the capability exists on this box at all, the role decides who
    may use it. Checked per request rather than at import so flipping the
    .env and restarting is all it takes, and so the 403 explains itself
    instead of the route simply not existing."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not deletion.enabled():
            abort(403, "Permanent deletion is disabled on this instance "
                       "(set ALLOW_HARD_DELETE in its .env to enable it)")
        return view(*args, **kwargs)
    return wrapped


def _home_url_for(user) -> str:
    """Where a role lands after logging in with no `next` to honour.

    Coaches and volunteers go to the assessments dashboard rather than the
    event list: during a season the recurring job is preparing, running and
    grading the week's assessments, while curating the question bank is the
    off-season task. The event list stays at "/" and is one click away in
    the menu — several pages use "/" as their "back to event list" target,
    so it keeps meaning what it always did.

    Students go straight to their own page instead of bouncing through "/"
    only to be redirected out of it by index().
    """
    if user.role == "student":
        return url_for("my_assessments_page")
    if user.role in ("coach", "volunteer"):
        return url_for("assessments_dashboard_page")
    return url_for("index")


def coach_required(view):
    """Gate a route to coaches only. Apply directly under @app.route."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None or user.role != "coach":
            abort(403, "Coach access required")
        return view(*args, **kwargs)
    return wrapped


def coach_or_volunteer_required(view):
    """Gate a route to coaches and volunteers — excludes students outright.
    Used by the Assessments dashboard/builder routes, which both roles can reach
    (a volunteer only sees/acts on their own assigned tests; see
    _select_assessment for the finer-grained per-test check)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None or user.role not in ("coach", "volunteer"):
            abort(403, "Coach or volunteer access required")
        return view(*args, **kwargs)
    return wrapped


def _select_assessment(assessment_id: str) -> assessments.Assessment:
    """Loads an Assessment, 404s if unknown, 403s unless the caller is a coach or
    appears in that test's window's assignments[event_slug].

    Deliberately independent of _select_event() — an Assessment spans
    season/window/event, and an assigned volunteer may have nothing in
    User.events at all (test-building assignment is a different grant than
    bank-edit access; conflating the two would either over- or under-grant).
    Routes that operate on an Assessment use this, never _select_event()."""
    test = assessments.get_assessment(assessment_id)
    if test is None:
        abort(404, f"Unknown test: {assessment_id}")
    user = getattr(g, "user", None)
    if user is None:
        abort(403)
    if user.role == "coach":
        return test
    window = assessments.get_window(test.window_id)
    assigned = window.assignments.get(test.event_slug, []) if window else []
    if user.role != "volunteer" or user.username not in assigned:
        abort(403, "You're not assigned to this test")
    return test


# In-memory failed-login tracker, keyed by source IP. Resets on process
# restart — a real improvement over no rate limiting at all, not a claim of
# perfect brute-force resistance (deliberately simple given the single
# gunicorn worker this app is deployed with — see README's Deploying
# section). Deliberately uses request.remote_addr, NOT X-Forwarded-For: an
# untrusted client could spoof that header to bypass a per-IP limit, and
# this app doesn't run a ProxyFix-style trusted-proxy config in front. If
# deployed behind Caddy, every client appears as Caddy's own address —
# which rate-limits the whole app together rather than per real visitor,
# but that fails safe (over-restrictive) rather than spoofable.
_LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5
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


def _safe_next_url(raw_next: str | None, fallback: str) -> str:
    if not raw_next:
        return fallback
    next_url = raw_next.strip()
    parsed = urlsplit(next_url)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not next_url.startswith("/") or next_url.startswith("//"):
        return fallback
    return next_url


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _login_rate_limited(ip):
            return render_template(
                "login.html",
                error="Too many failed attempts. Try again in a few minutes."), 429
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        user = auth.verify_login(username, password)
        if user is None:
            _record_login_failure(ip)
            return render_template("login.html", error="Invalid username or password")
        _clear_login_failures(ip)
        session.clear()
        session["username"] = user.username
        next_url = _safe_next_url(request.args.get("next"), _home_url_for(user))
        resp = redirect(next_url)
        # Non-HttpOnly by design — the frontend's fetch patch needs to read
        # this to attach X-CSRF-Token. It's a CSRF defense, not a secret.
        resp.set_cookie("csrf_token", secrets.token_hex(32),
                         httponly=False, secure=app.config["SESSION_COOKIE_SECURE"],
                         samesite="Lax", path=app.config["SESSION_COOKIE_PATH"])
        return resp
    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = redirect(url_for("login"))
    resp.delete_cookie("csrf_token")
    return resp

_STATIC_DIR = Path(__file__).parent / "static"


@app.route("/favicon.ico")
def favicon():
    # Browsers request this path directly regardless of any <link rel=icon>
    # tag, so serve it here instead of leaving every page to 404 on it.
    return send_file(_STATIC_DIR / "favicon.ico", mimetype="image/vnd.microsoft.icon")


def _request_llm_keys() -> dict:
    """Per-provider API keys the browser sent for THIS request, read from
    the `X-LLM-Keys` header (a small JSON object — see the Settings panel in
    `_COMMON_JS`). These keys live only in the user's browser localStorage;
    nothing here writes them to disk. Falls back to the server's own
    ANTHROPIC_API_KEY (`llm_providers.default_keys()`) when the browser sent
    nothing, so existing behavior is unchanged for anyone who hasn't opened
    Settings."""
    raw = request.headers.get("X-LLM-Keys")
    if not raw:
        return llm_providers.default_keys()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return llm_providers.default_keys()
    if not isinstance(parsed, dict):
        return llm_providers.default_keys()
    keys = {
        p: v.strip() for p, v in parsed.items()
        if p in llm_providers.PROVIDER_ORDER and isinstance(v, str) and v.strip()
    }
    return keys or llm_providers.default_keys()


def _with_vision_key(fn):
    """Decorator for the request-scoped vision routes (OCR, region-vision,
    column-vision, extract-math) — binds the browser-supplied Anthropic key
    (if any) to build_question_bank's per-context vision ContextVar for the
    duration of this request, so _get_client()/_vision_available() honour it
    instead of only the server's ANTHROPIC_API_KEY. Always reset afterward:
    Flask/gunicorn worker threads are reused across many requests, so a
    ContextVar left bound would leak one user's key into the next unrelated
    request handled on the same thread. Background-job vision (reprocess/
    upload/scan-process) is bound separately, by jobs.py's worker thread —
    see build_question_bank.set_vision_key()'s docstring."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        keys = _request_llm_keys()
        token = bqb.set_vision_key(keys.get("anthropic"))
        try:
            return fn(*args, **kwargs)
        finally:
            bqb.reset_vision_key(token)
    return wrapper


# fitz.Document handles are deliberately NOT cached across requests.
#
# There used to be a bounded LRU here, which handed the same Document to all
# of gunicorn's threads. PyMuPDF Documents are not safe for concurrent use,
# so two volunteers previewing the same PDF — or one browser fetching
# several pages at once — shared one handle with no lock, and the cache dict
# itself was an unsynchronised OrderedDict being mutated from those threads.
#
# It bought almost nothing: opening this repo's own circuit_lab test PDF
# measures 0.54ms against 26.5ms to render a single page at 120dpi. Paying
# 0.54ms per request removes an entire class of concurrency bug, and the
# render cache below means the render itself usually doesn't happen at all.
# CPython frees the Document when the request's locals go out of scope.


def _select_event(slug: str):
    """Bind the active event for this request.

    After the B#1 deep fix, `bqb.set_event` writes to a ContextVar — each
    request/thread sees its own active event. The serialising lock that used
    to live here is no longer needed; multi-threaded WSGI is safe.

    Every `/event/<slug>/...` route calls this first, which makes it the one
    place to enforce per-event access: coaches reach every event implicitly,
    volunteers only the events a coach assigned them.
    """
    user = getattr(g, "user", None)
    # Blanket exclusion: students never get any question-bank access at
    # all, not even read-only — practice-quiz/browse exposure could leak
    # content that ends up on a future official test. Every /event/<slug>/
    # route calls _select_event() first, so this one line blocks all of
    # extract.html/browse.html/quiz.html/sources.html/event_index.html for
    # students in one place; their own surface lives entirely under
    # /my-tests (see review_app.py's student route block).
    if user is not None and user.role == "student":
        abort(403, "Students don't have access to the question bank")
    if slug not in EVENTS:
        abort(404, f"Unknown event: {slug}")
    if EVENTS[slug].archived:
        abort(404, f"Event archived: {slug} — a coach can unarchive it from the landing page")
    if user is not None and not auth.user_can_access_event(user, slug):
        abort(403, f"You don't have access to {slug}")
    # Below every access check above on purpose: this is the per-event
    # gate, so presence can never be recorded for an event the user would
    # have been 403'd out of.
    if user is not None:
        presence.touch_event(slug, user.username)
    current = bqb.current_event()
    if current is None or current.slug != slug:
        bqb.set_event(slug)
    return bqb.EVENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_join(base_dir: Path, user_supplied_name: str) -> Path:
    """Resolve `user_supplied_name` under `base_dir`, aborting 400 if it
    would escape it. `secure_filename()` already strips path separators and
    "..", but a bare ".." segment alone (no slash) still passes it — this
    is the containment check that catches that case."""
    from werkzeug.utils import secure_filename
    name = secure_filename(user_supplied_name)
    if not name:
        abort(400, "bad filename")
    resolved_base = base_dir.resolve()
    candidate = (resolved_base / name).resolve()
    if not candidate.is_relative_to(resolved_base):
        abort(400, "bad filename")
    return candidate


def _sanitize_svg(svg_text: str) -> str:
    """Strip the obvious script-execution vectors from an uploaded SVG
    before it touches disk: <script> elements and on*= event-handler
    attributes. Not a full sanitizer (e.g. doesn't touch <foreignObject> or
    external references) — defense-in-depth on top of the app only ever
    rendering these via <img> tags (which don't execute embedded scripts),
    never <iframe>/<object>."""
    svg_text = re.sub(r"(?is)<script\b.*?</script>", "", svg_text)
    svg_text = re.sub(r'(?i)\son[a-z]+\s*=\s*"[^"]*"', "", svg_text)
    svg_text = re.sub(r"(?i)\son[a-z]+\s*=\s*'[^']*'", "", svg_text)
    return svg_text


def _open_pdf(name: str) -> fitz.Document:
    return fitz.open(str(_resolve_pdf_path(name)))


# ---------------------------------------------------------------------------
# Rendered-page cache
#
# Rendering a page costs real CPU on the one gunicorn worker that students'
# answer saves also share: measured on this repo's circuit_lab test PDF,
# 1.6ms at 24dpi, 26.5ms at 120dpi, 72ms at 200dpi. The review workflow
# re-renders the same page constantly — zooming, capturing regions, paging
# back and forth — and every one of those was a fresh render, because the
# frontend appended a cache-busting query param and the response carried no
# validators at all.
#
# Two layers now sit in front of it. An ETag lets the browser skip the
# transfer entirely (304, no render). A PNG on disk means even a cold
# client costs a file read rather than a rasterise. DATA_ROOT has orders of
# magnitude more room than the bank itself, so trading disk for CPU is the
# right way round here.
#
# Lives at DATA_ROOT/.render_cache/, deliberately dot-prefixed: both
# backup-bulk-data.sh and migrate-data-root.sh discover directories with a
# bare `*/` glob, which skips dotfiles, so derived data never lands in the
# nightly restic snapshot or gets copied on a DATA_ROOT migration.
# ---------------------------------------------------------------------------

#: 0 disables the disk cache entirely (the ETag layer still applies).
RENDER_CACHE_MAX_MB = int(os.environ.get("RENDER_CACHE_MAX_MB") or "2048")
_RENDER_CACHE_DIR = DATA_ROOT / ".render_cache"
#: Sweeping the directory on every write would cost more than the renders
#: it saves; every Nth write is enough to keep growth bounded.
_RENDER_PRUNE_EVERY = 200
_render_write_count = 0
_render_prune_lock = threading.Lock()


#: Cache scope for archive PDFs. They belong to no event, and borrowing a
#: slug would file them in that event's shard, where clearing it would take
#: them out too. The leading dot cannot collide with a real slug.
ARCHIVE_RENDER_SCOPE = ".archive"


def _render_cache_key(path: Path, pno: int, dpi: int, scope: str = "") -> str:
    """Identity of one rendered page.

    Includes the source file's mtime and size, so replacing a PDF (a
    test/key swap, a re-upload, a .docx conversion) produces a different
    key rather than serving the previous document's pages. That is why this
    stats the file instead of trusting the filename.
    """
    try:
        st = path.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    # Archive filenames are not unique across the corpus — "test.pdf" a
    # thousand times over — so under a scope the full path identifies the
    # file, where inside an event's shard the basename already does.
    ident = str(path) if scope else path.name
    raw = f"{scope or bqb.EVENT.slug}\0{ident}\0{pno}\0{dpi}\0{stamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _render_cache_path(key: str, scope: str = "") -> Path:
    """Sharded by event slug (or explicit scope) first, then by the key's
    first two hex chars.

    The slug level is what makes per-event clearing possible at all — the
    filename is a hash, so without it there is no way to tell one event's
    cached pages from another's short of deleting everything.
    """
    return _RENDER_CACHE_DIR / (scope or bqb.EVENT.slug) / key[:2] / f"{key}.png"


def _render_cache_read(key: str, scope: str = "") -> bytes | None:
    if RENDER_CACHE_MAX_MB <= 0:
        return None
    f = _render_cache_path(key, scope)
    try:
        data = f.read_bytes()
    except OSError:
        return None
    # Touch so pruning can evict least-recently-USED rather than oldest-
    # written; a page nobody opens should go before one in daily use.
    try:
        os.utime(f, None)
    except OSError:
        pass
    return data


def _render_cache_write(key: str, png: bytes, scope: str = "") -> None:
    if RENDER_CACHE_MAX_MB <= 0:
        return
    f = _render_cache_path(key, scope)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        # Same tempfile + os.replace as every other write in this codebase:
        # two threads rendering the same page must not tear a reader.
        tmp = f.with_suffix(f".png.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(png)
        os.replace(tmp, f)
    except OSError as e:
        # A cache is an optimisation. Never fail a render over it.
        app.logger.warning("render cache write failed: %s", e)
        return
    global _render_write_count
    with _render_prune_lock:
        _render_write_count += 1
        due = _render_write_count % _RENDER_PRUNE_EVERY == 0
    if due:
        _render_cache_prune()


def _render_cache_prune() -> None:
    """Evict least-recently-used files until the cache is under its cap."""
    cap = RENDER_CACHE_MAX_MB * 1024 * 1024
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p)
                 for p in _RENDER_CACHE_DIR.rglob("*.png")]
    except OSError:
        return
    total = sum(size for _m, size, _p in files)
    if total <= cap:
        return
    files.sort()                       # oldest access first
    for _mtime, size, path in files:
        if total <= cap:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass
    app.logger.info("render cache pruned to %.0f MB", total / 1024 / 1024)


def clear_render_cache_for_event(slug: str) -> int:
    """Drop every cached page for one event. Not needed for correctness —
    the key already includes the source file's mtime and size, so a changed
    PDF simply misses — but reclaiming the space is worth doing when an
    event is deleted."""
    removed = 0
    for p in (_RENDER_CACHE_DIR / slug).rglob("*.png"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _resolve_pdf_path(name: str) -> Path:
    """The test PDF's path, 404ing if it isn't there. Split out from
    _open_pdf so the render cache can stat the file for its key without
    opening it."""
    path = bqb.BASE_DIR / name
    if not path.exists():
        abort(404, "PDF not found")
    return path


def _list_test_pdfs() -> list[Path]:
    return sorted(bqb.BASE_DIR.glob(f"{bqb.EVENT.filename_prefix}_*_test.pdf"))


def _supplementary_docs(test_pdf: Path) -> list[Path]:
    """Sibling PDFs sharing this test's filename prefix that aren't the
    test PDF or its key — e.g. `_sheet.pdf`, `_notes.pdf`, `_notes1.pdf`,
    whatever scioly.org happened to attach via `other_links`. No hardcoded
    suffix list, so any such file already sitting in the event directory
    (downloaded but never surfaced anywhere) is picked up automatically.

    Despite the literal `_notes.pdf` filename scioly.org sometimes uses,
    everything this function returns is figures/images *attached to this
    one test* — browsed via the extract page's target toggle, never fed to
    the LLM. This is NOT the same thing as the Scan page's `role="notes"`
    onboarding option (`api_scan_rename`), which is event-wide *source
    material* for question generation, moved into `texts_dir`. The
    filename coincidence is scioly.org's, not this codebase's — don't let
    it blur the two concepts when working on either one."""
    if not test_pdf.name.endswith("_test.pdf"):
        return []
    prefix = test_pdf.name[: -len("_test.pdf")]
    key = _key_path(test_pdf)
    exclude = {test_pdf.name} | ({key.name} if key else set())
    return [p for p in sorted(test_pdf.parent.glob(f"{prefix}_*.pdf"))
            if p.name not in exclude]


def _open_target_pdf(pdfname: str, target: str) -> fitz.Document:
    """Single resolution+cache point for the `target` param used by
    page-render, region/math extraction, and pick-image routes — replaces
    5 near-identical `if target == "key": ... else: ...` blocks. `target`
    is "test" (default) for the main test PDF, "key" for the answer key, or
    any filename from _supplementary_docs() for a sheet/notes/etc. document
    attached to this test. A filename that exists on disk but isn't
    actually one of *this* test's supplementary docs still 404s — the
    membership check, not just _safe_join's containment check, is what
    prevents that."""
    return fitz.open(str(_resolve_target_path(pdfname, target)))


def _resolve_target_path(pdfname: str, target: str) -> Path:
    """Path resolution for the `target` param, shared by the opener above
    and the render cache (which needs to stat the file, not open it)."""
    test_pdf = bqb.BASE_DIR / pdfname
    # "test" here is the wire value the frontend sends (?target=test, and
    # PD_PAGE_COUNTS in event_index.html), naming the TEST PDF as opposed
    # to the key or a supplementary doc. It is external-test vocabulary,
    # not the renamed Assessment concept, and must not follow that rename.
    if not target or target == "test":
        return _resolve_pdf_path(pdfname)
    if target == "key":
        path = _key_path(test_pdf)
        if not path:
            abort(404, "No key PDF")
        return path
    candidate = _safe_join(bqb.BASE_DIR, target)
    if candidate not in _supplementary_docs(test_pdf):
        abort(404, "Not a supplementary document for this test")
    return candidate


def _key_path(test_pdf: Path) -> Path | None:
    k = test_pdf.parent / test_pdf.name.replace("_test.pdf", "_key.pdf")
    if k.exists():
        return k
    # Fallback: a .docx/.doc key with no converted PDF sibling yet. Unlike
    # the test PDF's conversion (job-queued — see api_upload_test_pdf and
    # _pending_doc_conversions — since it's the primary, often-larger
    # document), this converts inline: it's a one-time cost (the next call
    # finds the cached PDF and skips straight past this branch), the key is
    # usually small, and key lookups already happen inside hot page-render
    # paths where a job round-trip would be awkward to thread through. A
    # failure here just means "no key available yet," not a broken request.
    for ext in (".docx", ".doc"):
        src = test_pdf.parent / test_pdf.name.replace("_test.pdf", f"_key{ext}")
        if src.exists():
            try:
                return doc_convert.convert_to_pdf(src, src.parent)
            except doc_convert.DocConvertError:
                return None
    return None


def _pending_doc_conversions() -> list[Path]:
    """.docx/.doc test files discovered alongside this event's PDFs that
    don't have a converted PDF sibling yet — surfaced by the scan page
    (Part 4) with a one-click job-queued "Convert" action, since converting
    on every page load (like _key_path's lazy fallback above) would be too
    slow/unpredictable for the primary test document."""
    pending = []
    for ext in ("docx", "doc"):
        for src in bqb.BASE_DIR.glob(f"{bqb.EVENT.filename_prefix}_*_test.{ext}"):
            if not src.with_suffix(".pdf").exists():
                pending.append(src)
    return sorted(pending)


_FILENAME_ROLE_RE = re.compile(r"^.+_(test|key)\.(pdf|docx|doc)$", re.IGNORECASE)


def _explained_filenames(base_dir: Path) -> tuple[set[str], list[Path]]:
    """Every filename in `base_dir` already accounted for by a recognized
    role (test/key/supplementary), plus the list of `_test.*` files found —
    shared by _scan_event_files() (full bucketed view) and
    _count_unrecognized() (landing-page count) so the two never disagree
    about what counts as "explained"."""
    explained: set[str] = set()
    test_files: list[Path] = []
    if not base_dir.exists():
        return explained, test_files
    for f in sorted(base_dir.iterdir()):
        if not f.is_file():
            continue
        m = _FILENAME_ROLE_RE.match(f.name)
        if not m:
            continue
        explained.add(f.name)
        # _FILENAME_ROLE_RE captures "test" or "key" from a filename on
        # disk (<prefix>_<year>_<div>_<source>_test.pdf). Comparing to
        # anything else can never match.
        if m.group(1).lower() == "test":
            test_files.append(f)
    for f in test_files:
        pdf_form = f if f.suffix.lower() == ".pdf" else f.with_suffix(".pdf")
        for sup in _supplementary_docs(pdf_form):
            explained.add(sup.name)
    return explained, test_files


def _guess_test_metadata(filename: str) -> dict:
    """Best-effort year/division guess from a non-conforming filename, to
    pre-fill the scan page's rename form — never authoritative, always
    user-editable before the rename actually happens."""
    year_m = re.search(r"(19|20)\d{2}", filename)
    div_m = re.search(r"(?<![a-zA-Z])(BC|B|C)(?![a-zA-Z])", filename)
    return {"year": year_m.group(0) if year_m else "",
            "division": div_m.group(0).upper() if div_m else ""}


def _scan_event_files() -> dict:
    """Bucket every file in this event's base_dir for the manual file-drop
    onboarding page: files copied straight into the directory (e.g. scp'd
    in from another machine) rather than coming through the upload form or
    the scioly.org scrape won't be discovered by anything else in the
    pipeline until they're either already-conforming or get renamed into
    convention here.

    Returns {"ready": [...], "needs_conversion": [...], "unrecognized": [...]}
      - ready: conforming test PDFs (including already-converted .docx/.doc)
        with no entry in state["questions"] yet — one-click bulk-processable.
      - needs_conversion: conforming .docx/.doc test files with no PDF
        sibling yet — surfaced separately since they need api_convert_doc
        run first (see _pending_doc_conversions).
      - unrecognized: .pdf/.docx/.doc files that don't match the naming
        convention and aren't a known test's supplementary document either —
        candidates for the rename-onboarding form."""
    base_dir = bqb.BASE_DIR
    state = bqb._load_state()
    processed = set(state.get("questions", {}).keys())
    explained, test_files = _explained_filenames(base_dir)

    ready, needs_conversion = [], []
    for f in test_files:
        ext = f.suffix.lower()
        if ext in (".docx", ".doc"):
            pdf_sibling = f.with_suffix(".pdf")
            if pdf_sibling.exists():
                if pdf_sibling.name not in processed:
                    ready.append({"filename": pdf_sibling.name})
            else:
                needs_conversion.append({"filename": f.name})
        elif f.name not in processed:
            ready.append({"filename": f.name})

    unrecognized = []
    for f in sorted(base_dir.iterdir()) if base_dir.exists() else []:
        if not f.is_file() or f.name in explained:
            continue
        if f.suffix.lower() not in (".pdf", ".docx", ".doc"):
            continue
        unrecognized.append({"filename": f.name, "size": f.stat().st_size,
                             "guess": _guess_test_metadata(f.name)})

    return {
        "ready": sorted(ready, key=lambda r: r["filename"]),
        "needs_conversion": sorted(needs_conversion, key=lambda r: r["filename"]),
        "unrecognized": unrecognized,
    }


def _count_unrecognized(ev) -> int:
    """Count-only version of _scan_event_files()'s "unrecognized" bucket
    for an arbitrary Event object, used by the landing page to show a count
    per event without switching the "current event" ContextVar for each row
    the way _select_event()/_scan_event_files() do."""
    explained, _ = _explained_filenames(ev.base_dir)
    if not ev.base_dir.exists():
        return 0
    return sum(1 for f in ev.base_dir.iterdir()
              if f.is_file() and f.suffix.lower() in (".pdf", ".docx", ".doc")
              and f.name not in explained)


def _compute_pages(pdfname: str, questions: list[dict]) -> None:
    """Set q['page'] for each question by searching PDF text for its head."""
    doc = _open_pdf(pdfname)
    page_texts = [(i + 1, p.get_text("text")) for i, p in enumerate(doc)]
    last_page = 1
    for q in questions:
        snippet = (q.get("text") or "")[:40].strip()
        if not snippet:
            q.setdefault("page", last_page)
            continue
        needle = snippet[:25]
        hits = [pno for pno, txt in page_texts if needle in txt]
        if not hits:
            q.setdefault("page", last_page)
        else:
            best = min((h for h in hits if h >= last_page), default=hits[0])
            q["page"] = best
            last_page = best


def _pdf_status(pdf: Path, state: dict) -> dict:
    st = pdf.stat()
    qs = state.get("questions", {}).get(pdf.name, [])
    return {
        "name": pdf.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_h": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "size_h": f"{st.st_size // 1024} KB",
        "processed": pdf.name in state.get("questions", {}),
        "manual_edited": pdf.name in state.get("manual", {}),
        "n_questions": len(qs),
        "n_with_img": sum(1 for q in qs if q.get("images")),
        "n_with_ans": sum(1 for q in qs if q.get("answer")),
        "has_key": _key_path(pdf) is not None,
    }


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Event picker — landing page across all configured events.

    Coaches see every non-archived event; volunteers only the ones a coach
    assigned them — so a volunteer never sees a link to an event
    _select_event() would 403 them on anyway. Archived events are hidden
    here but never deleted from disk; coaches see them in a separate
    "Show archived" section with an Unarchive action.

    Students have no bank access at all, so this page would just be an
    empty event list for them — send them straight to their actual landing
    page instead."""
    if g.user.role == "student":
        return redirect(url_for("my_assessments_page"))
    rows = []
    archived_rows = []
    active_by_event = presence.active_by_event(exclude_user=g.user.username)
    for slug, ev in sorted(EVENTS.items()):
        if g.user.role != "coach" and slug not in g.user.events:
            continue
        if ev.archived:
            if g.user.role == "coach":
                archived_rows.append({"slug": slug, "name": ev.name})
            continue
        ev.base_dir.mkdir(exist_ok=True)
        n_pdfs = len(list(ev.base_dir.glob(f"{ev.filename_prefix}_*_test.pdf")))
        state = json.loads(ev.state_file.read_text(encoding="utf-8")) \
                if ev.state_file.exists() else {}
        qs_by_pdf = state.get("questions", {})
        n_q = sum(len(v) for v in qs_by_pdf.values())
        n_processed_pdfs = len([k for k in qs_by_pdf if not k.startswith("_")])
        rows.append({
            "slug": slug, "name": ev.name,
            "n_pdfs": n_pdfs, "n_questions": n_q,
            "n_processed_pdfs": n_processed_pdfs,
            "base_dir": relative_data_path(ev.base_dir),
            "is_builtin": is_builtin(slug),
            "n_unrecognized": _count_unrecognized(ev),
            # Other people only — see presence.active_by_event's
            # exclude_user note.
            "n_active": active_by_event.get(slug, 0),
        })
    return render_template("events.html", rows=rows, archived_rows=archived_rows)


@app.route("/api/events/<slug>", methods=["GET"])
def api_get_event(slug: str):
    """Fetch the editable fields for a single event. Used by the edit-event modal."""
    if slug not in EVENTS:
        return jsonify({"error": f"unknown event: {slug}"}), 404
    ev = EVENTS[slug]
    return jsonify({
        "slug": ev.slug,
        "name": ev.name,
        "filename_prefix": ev.filename_prefix,
        "event_match": list(ev.event_match),
        "wiki_page": ev.wiki_page,
        "topics": list(ev.topics),
        "foci": list(ev.foci),
        "is_builtin": is_builtin(slug),
        "has_build": ev.has_build,
    })


@app.route("/api/events/<slug>", methods=["PATCH"])
@coach_required
def api_edit_event(slug: str):
    """Edit a user-registered event's foci/topics/wiki_page in place.
    Built-ins are immutable; edit them in events.py."""
    if is_builtin(slug):
        return jsonify({"error": "built-in events cannot be edited via API"}), 400
    if slug not in EVENTS:
        return jsonify({"error": f"unknown event: {slug}"}), 404
    data = request.get_json() or {}
    cur = EVENTS[slug]
    # Build a fresh event object preserving slug/prefix; mutate config fields
    def _parse_csv(x):
        if isinstance(x, str): return [s.strip() for s in x.split(",") if s.strip()]
        return [str(s).strip() for s in (x or []) if str(s).strip()]
    topics = _parse_csv(data.get("topics", list(cur.topics)))
    foci   = _parse_csv(data.get("foci",   list(cur.foci)))
    match  = _parse_csv(data.get("event_match", list(cur.event_match)))
    wiki   = (data.get("wiki_page", cur.wiki_page) or "").strip()
    name   = (data.get("name", cur.name) or "").strip() or cur.name
    has_build = bool(data.get("has_build", cur.has_build))
    if "Other / General" not in topics:
        topics.append("Other / General")
    from events import Event, _save_custom_events
    EVENTS[slug] = Event(
        slug=slug, name=name,
        event_match=tuple(s.lower() for s in match),
        filename_prefix=cur.filename_prefix,
        topics=tuple(topics),
        topic_keywords=cur.topic_keywords,
        foci=tuple(foci),
        wiki_page=wiki,
        has_build=has_build,
    )
    _save_custom_events()
    return jsonify({"ok": True, "slug": slug})


@app.route("/api/events/<slug>", methods=["DELETE"])
@coach_required
def api_delete_event(slug: str):
    """"Delete" an event — archives it (hides from the landing page) rather
    than removing anything. The event's directory/PDFs/state file are never
    touched; see events.archive_custom_event and /api/events/<slug>/unarchive."""
    if is_builtin(slug):
        return jsonify({"error": "cannot remove a built-in event"}), 400
    try:
        from events import archive_custom_event
        archive_custom_event(slug)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "slug": slug})


@app.route("/api/events/<slug>/unarchive", methods=["POST"])
@coach_required
def api_unarchive_event(slug: str):
    try:
        from events import unarchive_custom_event
        unarchive_custom_event(slug)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "slug": slug})


# ---------------------------------------------------------------------------
# Routes — account settings (everyone) + user management (coach-only)
# ---------------------------------------------------------------------------

@app.route("/settings")
def settings_page():
    """Every logged-in user gets My Account (display name + password
    change) and LLM API Keys — one unified surface instead of a floating
    LLM-keys button every other page injected separately. Manage Users
    lives on the Club Management page now (coach-only, see
    club_management_page())."""
    return render_template("settings.html")


@app.route("/api/account/password", methods=["POST"])
def api_change_password():
    data = request.get_json() or {}
    try:
        auth.change_own_password(g.user.username,
                                  data.get("current_password") or "",
                                  data.get("new_password") or "")
    except auth.WrongPasswordError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/account/display-name", methods=["POST"])
def api_set_display_name():
    data = request.get_json() or {}
    try:
        updated = auth.set_display_name(g.user.username, data.get("display_name") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "display_name": updated.display_name})


@app.route("/admin/users")
@coach_required
def admin_users_page():
    """Folded into /settings's Manage Users section — kept as a redirect
    so old links/bookmarks still land somewhere sensible."""
    return redirect(url_for("settings_page"))


@app.route("/admin/users", methods=["POST"])
@coach_required
def admin_create_user():
    data = request.get_json() or {}
    username = data.get("username") or ""
    password = data.get("password") or ""
    role = (data.get("role") or "volunteer").strip()
    events = [s for s in (data.get("events") or []) if s in EVENTS]
    try:
        auth.create_user(username, password, role=role, events=events)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/users/<username>", methods=["PATCH"])
@coach_required
def admin_edit_user(username):
    data = request.get_json() or {}
    role = data.get("role")
    events = data.get("events")
    disabled = data.get("disabled")
    if events is not None:
        events = [s for s in events if s in EVENTS]
    try:
        auth.update_user(username, role=role, events=events, disabled=disabled)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/users/<username>", methods=["DELETE"])
@coach_required
def admin_delete_user(username):
    """"Remove" a user — disables the account (blocks login, kicks any
    active session) rather than deleting it. Reversible via PATCH
    /admin/users/<username> with {"disabled": false}. The account, and all
    event data, stay on disk; see auth.disable_user."""
    if username == g.user.username:
        return jsonify({"error": "cannot disable your own account while logged in"}), 400
    try:
        auth.disable_user(username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — Club Management (seasons + per-season student roster, coach-only)
#
# Deliberately NOT under /event/<slug>/... — a season/roster spans every
# event in its lineup, so none of these go through _select_event(). A
# season's event_slugs lineup only scopes which events appear on the
# roster grid / which an AssessmentWindow can be created against; it has zero
# effect on question-bank curation access (see seasons.py's docstring).
# ---------------------------------------------------------------------------

@app.route("/club")
@coach_required
def club_management_page():
    all_seasons = sorted(seasons.load_seasons().values(),
                          key=lambda s: s.season_id, reverse=True)
    current = seasons.get_current_season()
    selected_id = seasons.resolve_season_id(request.args.get("season"))
    selected = seasons.get_season(selected_id) if selected_id else None
    students = sorted(
        (u for u in auth.load_users().values() if u.role == "student" and not u.disabled),
        key=lambda u: (u.display_name or u.username),
    )
    roster = seasons.get_full_roster(selected_id) if selected else {}
    users = sorted(auth.load_users().values(), key=lambda u: u.username)
    return render_template(
        "club_management.html",
        all_seasons=all_seasons,
        current=current,
        selected=selected,
        all_events=sorted(EVENTS.keys()),
        students=students,
        roster=roster,
        users=users,
    )


@app.route("/api/seasons", methods=["POST"])
@coach_required
def api_create_season():
    data = request.get_json() or {}
    event_slugs = data.get("event_slugs") or []
    try:
        s = seasons.create_season(
            data.get("season_id", ""),
            label=data.get("label", ""),
            event_slugs=event_slugs,
            created_by=g.user.username,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "season_id": s.season_id})


@app.route("/api/seasons/<season_id>/events", methods=["PATCH"])
@coach_required
def api_update_season_events(season_id):
    data = request.get_json() or {}
    try:
        s = seasons.update_season_events(season_id, data.get("event_slugs") or [])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "event_slugs": list(s.event_slugs)})


@app.route("/api/seasons/<season_id>/set-current", methods=["POST"])
@coach_required
def api_set_current_season(season_id):
    try:
        seasons.set_current_season(season_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/seasons/<season_id>/archive", methods=["POST"])
@coach_required
def api_archive_season(season_id):
    try:
        seasons.archive_season(season_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/seasons/<season_id>/unarchive", methods=["POST"])
@coach_required
def api_unarchive_season(season_id):
    try:
        seasons.unarchive_season(season_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/seasons/<season_id>/roster", methods=["GET"])
@coach_required
def api_get_roster(season_id):
    return jsonify({"roster": seasons.get_full_roster(season_id)})


@app.route("/api/seasons/<season_id>/roster/<event_slug>", methods=["PUT"])
@coach_required
def api_set_roster(season_id, event_slug):
    data = request.get_json() or {}
    users = auth.load_users()
    usernames = [u for u in (data.get("usernames") or [])
                 if u in users and users[u].role == "student"]
    try:
        seasons.set_roster(season_id, event_slug, usernames)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "usernames": usernames})


@app.route("/api/seasons/<season_id>/copy-roster-from", methods=["POST"])
@coach_required
def api_copy_roster_from(season_id):
    data = request.get_json() or {}
    from_season_id = data.get("from_season_id", "")
    event_slugs = data.get("event_slugs")
    try:
        copied = seasons.copy_roster_forward(from_season_id, season_id, event_slugs)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "copied": copied})


@app.route("/api/seasons/<season_id>/students/bulk-csv", methods=["POST"])
@coach_required
def api_bulk_csv_students(season_id):
    """Parses an uploaded CSV (display_name, username, password, events
    columns; only display_name is required per row) and creates+rosters
    students in one step. Additive on the roster side — unions into
    whatever's already there, never wipes existing entries a row didn't
    mention. Continues past a bad row rather than aborting the whole batch
    (see auth.create_users_bulk's docstring)."""
    import csv
    import io

    season = seasons.get_season(season_id)
    if season is None:
        return jsonify({"error": f"unknown season {season_id!r}"}), 400
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "CSV must be UTF-8 encoded"}), 400
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    row_events: list[list[str]] = []
    for raw_row in reader:
        normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        rows.append({
            "display_name": normalized.get("display_name", ""),
            "username": normalized.get("username", ""),
            "password": normalized.get("password", ""),
        })
        events_field = normalized.get("events", "")
        row_events.append([s.strip() for s in events_field.split(";") if s.strip()])

    result = auth.create_users_bulk(rows, season_id=season_id)

    rostered: dict[str, int] = {}
    for created in result["created"]:
        # created["row"] indexes back into the original CSV rows (and thus
        # row_events) regardless of how many earlier/later rows failed —
        # never assume positional alignment with result["created"]'s own
        # order, which only contains the successes.
        wanted_events = row_events[created["row"]]
        valid_events = [e for e in wanted_events if e in season.event_slugs]
        for slug in valid_events:
            seasons.add_to_roster(season_id, slug, [created["username"]])
            rostered[slug] = rostered.get(slug, 0) + 1

    return jsonify({"created": result["created"], "errors": result["errors"], "rostered": rostered})


# ---------------------------------------------------------------------------
# Routes — Assessments dashboard + test-builder + publish (coach + volunteer)
#
# Deliberately NOT under /event/<slug>/... — see _select_assessment()'s docstring
# for why these never call _select_event(). A season's event lineup only
# scopes which events a window can be created against (seasons.py); it has
# zero effect on bank-curation access.
# ---------------------------------------------------------------------------

@app.route("/assessments")
@coach_or_volunteer_required
def assessments_dashboard_page():
    all_seasons = sorted(seasons.load_seasons().values(), key=lambda s: s.season_id, reverse=True)
    current = seasons.get_current_season()
    selected_id = seasons.resolve_season_id(request.args.get("season"))
    selected = seasons.get_season(selected_id) if selected_id else None

    all_users = auth.load_users()

    def _candidates_for(slug):
        # Coaches have implicit bank access to every event; volunteers only
        # qualify for events they're explicitly granted (user.events) — a
        # deliberate departure from this module's prior "assignment is
        # independent of bank access" design, per the explicit request that
        # only people who can edit an event's bank may be called in to
        # prepare its test. Pre-existing assignments that predate this rule
        # are left untouched (see `assigned` below) — only the *picker* is
        # constrained going forward.
        return sorted(
            u.username for u in all_users.values()
            if not u.disabled and (u.role == "coach" or (u.role == "volunteer" and slug in u.events))
        )

    def _students_for(slug):
        """Rostered students for one event, for the makeup-window picker.

        Scoped to the season roster rather than every student account: a
        personal makeup window only means anything for someone actually
        sitting this event, and a coach searching a whole school's student
        list would be picking from mostly-wrong names. Disabled or deleted
        accounts on a stale roster entry are dropped rather than offered.
        """
        if not selected:
            return []
        out = []
        for username in seasons.get_roster(selected.season_id, slug):
            u = all_users.get(username)
            if u is None or u.disabled:
                continue
            out.append({"username": u.username,
                        "display_name": u.display_name or u.username})
        return sorted(out, key=lambda d: d["display_name"].lower())

    windows = []
    candidates_by_event = {}
    students_by_event = {}
    if selected:
        for w in sorted(assessments.load_windows().values(), key=lambda w: w.opens_at):
            if w.season_id != selected.season_id or w.archived:
                continue
            if g.user.role == "volunteer" and not any(
                g.user.username in (w.assignments.get(slug) or []) for slug in w.event_slugs
            ):
                continue
            window_tests = []
            for slug in w.event_slugs:
                if g.user.role == "volunteer" and g.user.username not in (w.assignments.get(slug) or []):
                    continue
                t = assessments.get_assessment_for(w.window_id, slug)
                window_tests.append({"event_slug": slug, "assessment": t, "kind": "exam",
                                     "assigned": w.assignments.get(slug) or []})
                # An event with a build component gets a second row here —
                # a build assessment for the same (window, event), created
                # alongside the exam one whenever the event was added to
                # this window (see assessments._ensure_assessments_for_event).
                ev = EVENTS.get(slug)
                if ev is not None and ev.has_build:
                    bt = assessments.get_assessment_for(w.window_id, slug, kind="build")
                    if bt is not None:
                        window_tests.append({"event_slug": slug, "assessment": bt, "kind": "build",
                                             "assigned": w.assignments.get(slug) or []})
                candidates_by_event.setdefault(slug, _candidates_for(slug))
                students_by_event.setdefault(slug, _students_for(slug))
            windows.append({"window": w, "assessments": window_tests})

    return render_template(
        "assessments_dashboard.html",
        all_seasons=all_seasons, current=current, selected=selected, windows=windows,
        candidates_by_event=candidates_by_event,
        students_by_event=students_by_event,
    )


@app.route("/api/assessment-windows", methods=["POST"])
@coach_required
def api_create_assessment_window():
    data = request.get_json() or {}
    try:
        w = assessments.create_window(
            season_id=data.get("season_id", ""),
            opens_at=data.get("opens_at", ""), closes_at=data.get("closes_at", ""),
            event_slugs=data.get("event_slugs") or [],
            label=data.get("label", ""), created_by=g.user.username,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "window_id": w.window_id})


@app.route("/api/assessment-windows/<window_id>", methods=["PATCH"])
@coach_required
def api_update_assessment_window(window_id):
    data = request.get_json() or {}
    try:
        w = assessments.update_window(
            window_id, label=data.get("label"), opens_at=data.get("opens_at"),
            closes_at=data.get("closes_at"), event_slugs=data.get("event_slugs"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/assessment-windows/<window_id>/assignments", methods=["PATCH"])
@coach_required
def api_update_assessment_assignments(window_id):
    data = request.get_json() or {}
    event_slug = data.get("event_slug", "")
    users = auth.load_users()
    # Coaches are now assignable too (previously volunteer-only) — matches
    # the new picker's candidate pool (see assessments_dashboard_page()).
    usernames = [u for u in (data.get("usernames") or [])
                 if u in users and users[u].role in ("coach", "volunteer")]
    try:
        assessments.update_window_assignments(window_id, event_slug, usernames)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "usernames": usernames})


@app.route("/assessments/<assessment_id>/build")
@coach_or_volunteer_required
def assessment_builder_page(assessment_id):
    test = _select_assessment(assessment_id)
    window = assessments.get_window(test.window_id)
    ev = EVENTS.get(test.event_slug)
    return render_template(
        "assessment_builder.html",
        assessment_id=assessment_id, event_slug=test.event_slug,
        event_name=ev.name if ev else test.event_slug,
        window_label=window.label if window else "",
        status=test.status,
        # Every question already committed to another test this season, so
        # the pool can hide repeats by default — see assessments.used_question_keys.
        used_keys=sorted(assessments.used_question_keys(test.season_id, assessment_id)),
    )


@app.route("/api/assessments/<assessment_id>", methods=["GET"])
@coach_or_volunteer_required
def api_get_assessment(assessment_id):
    test = _select_assessment(assessment_id)
    return jsonify({
        "assessment_id": test.assessment_id, "status": test.status, "kept": test.kept,
        "event_slug": test.event_slug, "window_id": test.window_id,
        "last_edited_by": test.last_edited_by, "last_edited_at": test.last_edited_at,
    })


@app.route("/api/assessments/<assessment_id>", methods=["PATCH"])
@coach_or_volunteer_required
def api_update_assessment_kept(assessment_id):
    _select_assessment(assessment_id)
    data = request.get_json() or {}
    try:
        updated = assessments.update_assessment_kept(assessment_id, data.get("kept") or [], edited_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "last_edited_by": updated.last_edited_by,
                    "last_edited_at": updated.last_edited_at})


@app.route("/assessments/<assessment_id>/publish", methods=["POST"])
@coach_or_volunteer_required
def api_publish_assessment(assessment_id):
    _select_assessment(assessment_id)
    try:
        result = assessments.publish_assessment(assessment_id, published_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "snapshot_count": len(result["test"].snapshot or []),
                    "skipped": result["skipped"], "ungradeable": result.get("ungradeable", [])})


@app.route("/assessments/<assessment_id>/go-live", methods=["POST"])
@coach_required
def api_go_live_assessment(assessment_id):
    try:
        assessments.go_live_assessment(assessment_id, live_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/assessments/<assessment_id>/unpublish", methods=["POST"])
@coach_required
def api_unpublish_assessment(assessment_id):
    """Reverts a published/live test to "preparing" for edits. Blocked once
    EITHER the class-wide window has opened OR any student response has a
    saved answer — a personal-makeup student could already be mid-test
    even before the class window opens, so both conditions are checked
    independently rather than just the window."""
    test = assessments.get_assessment(assessment_id)
    if test is None:
        abort(404)
    window = assessments.get_window(test.window_id)
    if window:
        from datetime import datetime as _dt, timezone as _tz
        opens = _dt.fromisoformat(window.opens_at)
        if opens.tzinfo is None:
            opens = opens.replace(tzinfo=_tz.utc)
        if _dt.now(_tz.utc) >= opens:
            return jsonify({"error": "the test window has already opened — can't un-publish"}), 400
    for resp in assessments.get_responses_for_assessment(assessment_id).values():
        if resp.answers:
            return jsonify({"error": "a student has already saved an answer — can't un-publish"}), 400
    try:
        assessments.unpublish_assessment(assessment_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/assessments/<assessment_id>/overrides", methods=["POST"])
@coach_required
def api_set_assessment_override(assessment_id):
    data = request.get_json() or {}
    # Accepts a list; the singular key is still honoured so an older client
    # (or a hand-rolled call) keeps working.
    usernames = data.get("student_usernames")
    if not usernames:
        single = (data.get("student_username") or "").strip()
        usernames = [single] if single else []
    try:
        updated = assessments.set_assessment_overrides_bulk(
            assessment_id, usernames,
            data.get("opens_at"), data.get("closes_at"),
            granted_by=g.user.username, reason=data.get("reason", ""),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "granted": len(usernames),
                    "overrides": updated.overrides})


@app.route("/api/assessments/<assessment_id>/overrides/<student_username>", methods=["DELETE"])
@coach_required
def api_revoke_assessment_override(assessment_id, student_username):
    try:
        assessments.set_assessment_overrides(assessment_id, student_username, None, None, granted_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — student-facing "My Assessments" surface
#
# A wholly separate route prefix that never calls _select_event() — see
# that function's blanket student-block for the corresponding server-side
# enforcement on the OTHER side (students can't reach /event/... at all).
# ---------------------------------------------------------------------------

def student_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None or user.role != "student":
            abort(403, "Student access required")
        return view(*args, **kwargs)
    return wrapped


def _student_assessment_context(assessment_id: str):
    """404s an unknown test; 403s if the caller's role isn't student or
    they're not rostered on this test's event for this test's season.
    Returns (test, window, season)."""
    import seasons as seasons_mod

    test = assessments.get_assessment(assessment_id)
    if test is None:
        abort(404, f"Unknown test: {assessment_id}")
    window = assessments.get_window(test.window_id)
    if window is None:
        abort(404)
    user = g.user
    if test.event_slug not in seasons_mod.student_events(test.season_id, user.username):
        abort(403, "You're not rostered for this test's event this season")
    return test, window


# A build assessment never reaches "live" (go_live_assessment refuses it —
# see assessments.py) and status flows preparing -> graded -> released
# instead (set_build_grade / release_grades). "preparing" is deliberately
# INCLUDED for kind="build" — that status is exactly "scheduled, not yet
# graded" for a build event, whereas for an exam "preparing" means the
# coach hasn't finished building the test yet and must stay hidden. Keep
# these two tuples in sync with the two docstrings above; an exam's tuple
# must never change, or every existing "which statuses list" test breaks.
_MY_ASSESSMENT_STATUSES = {
    "exam": ("live", "closed", "graded", "released"),
    "build": ("preparing", "graded", "released"),
}


def _my_assessment_bucket(t: "assessments.Assessment", w: "assessments.AssessmentWindow",
                          resp: "assessments.Response | None", username: str) -> str:
    """upcoming/current/past for one assessment as seen by one student.

    An exam buckets around a submission: once the student has submitted (or
    the window has closed) there is nothing left to do, so it moves to
    Past. A build assessment never gets a submission — the coach records
    the result directly — so its "nothing left to do here" signal is the
    score being released instead. Once neither applies, both kinds fall
    back to the same window-date check (open now vs. not yet open), so an
    exam's bucketing is completely unchanged by this shared helper existing."""
    if t.kind == "exam":
        done = resp is not None and resp.status != "in_progress"
    else:
        done = resp is not None and resp.released
    if done or assessments.is_window_past(t, w, username):
        # Already done moves straight to Past even if the class-wide window
        # is technically still open — nothing left to do, and (for an exam)
        # the take-page itself blocks re-entry for exactly this reason.
        return "past"
    elif assessments.is_window_open(t, w, username):
        return "current"
    return "upcoming"


@app.route("/my-assessments")
@student_required
def my_assessments_page():
    import seasons as seasons_mod
    from datetime import datetime as _dt, timezone as _tz

    season = seasons_mod.get_season(seasons_mod.resolve_season_id())
    upcoming, current, past = [], [], []
    if season:
        my_events = set(seasons_mod.student_events(season.season_id, g.user.username))
        for w in assessments.load_windows().values():
            if w.season_id != season.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                if slug not in my_events:
                    continue
                for kind in ("exam", "build"):
                    t = assessments.get_assessment_for(w.window_id, slug, kind=kind)
                    if t is None or t.status not in _MY_ASSESSMENT_STATUSES[kind]:
                        continue
                    resp = assessments.get_response(t.assessment_id, g.user.username)
                    entry = {"assessment": t, "window": w, "event_slug": slug, "response": resp}
                    bucket = _my_assessment_bucket(t, w, resp, g.user.username)
                    {"upcoming": upcoming, "current": current, "past": past}[bucket].append(entry)
    return render_template("my_assessments.html", upcoming=upcoming, current=current, past=past,
                            season=season)


@app.route("/my-assessments/<assessment_id>/take")
@student_required
def assessment_take_page(assessment_id):
    test, window = _student_assessment_context(assessment_id)
    if test.kind == "build":
        # A build assessment has no snapshot and nothing to serve — it can
        # never reach status "live" (go_live_assessment refuses it), so
        # this can only be reached by guessing an assessment_id directly.
        abort(400, "This is a build assessment — there's nothing to take. "
                   "Your coach records your score directly.")
    if test.status != "live" or not assessments.is_window_open(test, window, g.user.username):
        return redirect(url_for("my_assessments_page"))
    existing = assessments.get_response(assessment_id, g.user.username)
    if existing is not None and existing.status != "in_progress":
        # Already submitted — nothing left to do here even though the
        # class-wide window is still technically open; avoid a confusing
        # "took" page whose autosave silently rejects every edit.
        return redirect(url_for("my_assessments_page"))
    ev = EVENTS.get(test.event_slug)
    return render_template("assessment_take.html", assessment_id=assessment_id,
                            event_name=ev.name if ev else test.event_slug)


def _assessment_image_names(test: "assessments.Assessment") -> set[str]:
    """Every image filename this assessment's frozen snapshot references.

    Derived from the snapshot, never the live question bank — same rule the
    rest of the grading path follows (see _snapshot_one_question): editing
    the bank mid-window must not change what a live test serves. Covers the
    three places an image can be referenced: a question's own `images`, a
    matching row's per-cell `image`, and a shared context block's `images`.
    """
    names: set[str] = set()
    for q in (test.snapshot or []):
        names.update(q.get("images") or [])
        matching = q.get("matching") or {}
        for side in ("left", "right"):
            for item in matching.get(side) or []:
                if item.get("image"):
                    names.add(item["image"])
    for ctx in (test.snapshot_contexts or {}).values():
        names.update((ctx or {}).get("images") or [])
    return names


@app.route("/my-assessments/<assessment_id>/image/<fname>")
def serve_assessment_image(assessment_id, fname):
    """Serve one figure referenced by an assessment the caller is entitled to.

    Students are blocked from `/event/<slug>/images/<fname>` on purpose —
    _select_event() 403s them so practice-quiz/browse exposure can't leak
    content bound for a future official test. That rule is right, but it
    left students with no way to load *any* figure, including the diagram a
    grouped question set is built around. This route is the narrow opening:
    it serves only filenames this specific assessment's snapshot actually
    references, so entitlement to one test never becomes a directory listing
    of the whole event's images/.

    Coaches and volunteers share the route (rather than a second URL in the
    templates) since the results page is rendered for both a student viewing
    their own release and a coach drilling in from Scores.
    """
    user = g.user
    if user.role == "student":
        test, window = _student_assessment_context(assessment_id)   # 404/403s
        resp = assessments.get_response(assessment_id, user.username)
        taking = (test.status == "live"
                  and assessments.is_window_open(test, window, user.username))
        reviewing = resp is not None and resp.released
        if not (taking or reviewing):
            abort(403, "This test isn't open to you right now")
    else:
        test = assessments.get_assessment(assessment_id)
        if test is None:
            abort(404, f"Unknown test: {assessment_id}")
        if not auth.user_can_access_event(user, test.event_slug):
            abort(403, "You don't have access to this test's event")
    # Membership check first: this is what makes the route an allowlist
    # rather than a file server. _safe_join is still applied afterwards as
    # defense in depth, not as the primary control.
    if fname not in _assessment_image_names(test):
        abort(404)
    ev = EVENTS.get(test.event_slug)
    if ev is None:
        abort(404)
    p = _safe_join(ev.image_dir, fname)
    if not p.exists():
        abort(404)
    return send_file(str(p))


@app.route("/api/my-assessments")
@student_required
def api_my_assessments():
    import seasons as seasons_mod
    season = seasons_mod.get_season(seasons_mod.resolve_season_id())
    out = []
    if season:
        my_events = set(seasons_mod.student_events(season.season_id, g.user.username))
        for w in assessments.load_windows().values():
            if w.season_id != season.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                if slug not in my_events:
                    continue
                for kind in ("exam", "build"):
                    t = assessments.get_assessment_for(w.window_id, slug, kind=kind)
                    if t is None or t.status not in _MY_ASSESSMENT_STATUSES[kind]:
                        continue
                    resp = assessments.get_response(t.assessment_id, g.user.username)
                    bucket = _my_assessment_bucket(t, w, resp, g.user.username)
                    out.append({
                        "assessment_id": t.assessment_id, "event_slug": slug, "window_label": w.label,
                        "kind": kind,
                        "opens_at": w.opens_at, "closes_at": w.closes_at, "bucket": bucket,
                        "response_status": resp.status if resp else None,
                        # Never a score, even after release — only whether
                        # one has been released. The result (earned/possible)
                        # is fetched separately from the results route, which
                        # gates on this same `released` flag.
                        "released": resp.released if resp else False,
                    })
    return jsonify({"assessments": out})


@app.route("/api/my-assessments/<assessment_id>/take")
@student_required
def api_take_assessment(assessment_id):
    test, window = _student_assessment_context(assessment_id)
    if test.kind == "build":
        abort(400, "This is a build assessment — there's nothing to take.")
    if test.status != "live" or not assessments.is_window_open(test, window, g.user.username):
        abort(403, "This test isn't open right now")
    existing = assessments.get_response(assessment_id, g.user.username)
    if existing is not None and existing.status != "in_progress":
        abort(403, "You've already submitted this test")
    snapshot = test.snapshot or []
    resp = assessments.start_or_get_response(assessment_id, g.user.username, snapshot)
    ordered = [snapshot[i] for i in resp.question_order if i < len(snapshot)]
    # Never leak correct_answer/matching.pairs to the student during the test.
    sanitized = []
    for q in ordered:
        clean = {k: v for k, v in q.items() if k not in ("correct_answer", "source_question_ref")}
        if clean.get("qtype") == "matching" and "matching" in clean:
            m = dict(clean["matching"])
            m.pop("pairs", None)
            clean["matching"] = m
        sanitized.append(clean)
    return jsonify({
        "questions": sanitized, "answers": resp.answers, "closes_at": window.closes_at,
        "contexts": test.snapshot_contexts,
    })


@app.route("/api/my-assessments/<assessment_id>/answer", methods=["POST"])
@student_required
def api_save_assessment_answer(assessment_id):
    test, window = _student_assessment_context(assessment_id)
    if test.status != "live" or not assessments.is_window_open(test, window, g.user.username):
        abort(403, "This test isn't open right now")
    data = request.get_json() or {}
    try:
        updated = assessments.save_answer(assessment_id, g.user.username, data.get("number", ""),
                                      data.get("answer") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "last_saved_at": updated.last_saved_at})


@app.route("/api/my-assessments/<assessment_id>/submit", methods=["POST"])
@student_required
def api_submit_assessment(assessment_id):
    test, window = _student_assessment_context(assessment_id)
    if test.status != "live":
        abort(403, "This test isn't live")
    is_open = assessments.is_window_open(test, window, g.user.username)
    is_past = assessments.is_window_past(test, window, g.user.username)
    existing = assessments.get_response(assessment_id, g.user.username)
    if not is_open:
        # Never discard already-autosaved work just because the window
        # closed at the wire — but a brand-new attempt with zero prior
        # activity past a fully-elapsed window (no override) has nothing
        # to clamp, so it's rejected outright.
        if not (is_past and existing and existing.answers):
            abort(403, "This test isn't open right now")
    try:
        updated = assessments.submit_response(assessment_id, g.user.username, test.snapshot or [],
                                          late=(not is_open))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "status": updated.status})


# ---------------------------------------------------------------------------
# Routes — Grading + release (coach + assigned volunteer for grading;
# release itself is coach-only — a deliberate final checkpoint before
# students see anything, distinct from the grading work itself).
# ---------------------------------------------------------------------------

@app.route("/assessments/<assessment_id>/grade")
@coach_or_volunteer_required
def assessment_grading_page(assessment_id):
    test = _select_assessment(assessment_id)
    ev = EVENTS.get(test.event_slug)
    if test.kind == "build":
        # A roster table (one row per rostered student, one column per
        # rubric line), not the FRQ-per-block layout below — different
        # enough data shape (no snapshot, no per-question answers) that a
        # separate template is clearer than branching assessment_grading.html
        # throughout.
        return render_template("assessment_grading_build.html", assessment_id=assessment_id,
                                event_name=ev.name if ev else test.event_slug)
    return render_template("assessment_grading.html", assessment_id=assessment_id,
                            event_name=ev.name if ev else test.event_slug)


def _assessment_pdf(snapshot: list, title: str, subtitle: str,
                    layout: str, image_dir: "Path | None" = None) -> bytes:
    """Render an assessment to PDF, figures included.

    This is why PDF is worth having over the markdown export at all:
    markdown cannot carry an image, so a question whose stem is "identify
    the component labelled X" printed as a filename reference and was
    useless on paper. Anything the pipeline attached to a question is
    embedded here.

    `layout` follows EXPORT_LAYOUTS: "none" for the student copy, "key" for
    questions followed by an answer-key page.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether,
        Table, TableStyle, Image as RLImage,
    )
    import io as _io

    buf = _io.BytesIO()
    margin = 0.75 * inch
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=margin,
                            rightMargin=margin, topMargin=margin,
                            bottomMargin=margin, title=title)
    usable_width = LETTER[0] - 2 * margin
    # Leave room for the question stem and choices above/below the figure;
    # a full-page image would technically fit but push everything else off.
    usable_height = (LETTER[1] - 2 * margin) * 0.72

    styles = getSampleStyleSheet()
    h1, h2 = styles["Heading1"], styles["Heading2"]
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=11,
                          leading=14, spaceAfter=6)
    choice_style = ParagraphStyle("choice", parent=body, leftIndent=18,
                                  fontSize=10, leading=13)
    meta_style = ParagraphStyle("meta", parent=body, fontSize=8,
                                textColor="#888", spaceAfter=4)
    answer_style = ParagraphStyle("answer", parent=body, leftIndent=18,
                                  fontSize=10, textColor="#1a6b32")
    context_style = ParagraphStyle("context", parent=body, backColor="#fffbeb",
                                   borderColor="#e8c875", borderWidth=1,
                                   borderPadding=8, spaceAfter=8)

    def _e(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Figures MUST resolve against the assessment's own event. _select_assessment
    # is deliberately independent of _select_event() — an assessment spans
    # season/window/event and an assigned volunteer may hold no bank access at
    # all — so bqb.EVENT here is whatever the request context happened to
    # carry, not this test's event. Reading images from it resolved every
    # figure against the wrong directory, and every question printed
    # "[figure not found]".
    figures_dir = image_dir if image_dir is not None else bqb.EVENT.image_dir

    def _figure(fname: str, desc: str):
        """One image, scaled to fit the text column and never upscaled."""
        path = figures_dir / os.path.basename(fname)
        if not path.is_file():
            # Say so rather than dropping it: a question referring to a
            # figure that silently isn't there is worse than a note saying
            # which file is missing.
            return Paragraph(f"<i>[figure not found: {_e(fname)}]</i>", meta_style)
        try:
            iw, ih = ImageReader(str(path)).getSize()
            # Fit BOTH axes and never upscale. Clamping width alone lets a
            # tall figure (a scanned page, the common case) still overflow
            # the frame, and reportlab refuses to lay out a flowable larger
            # than the page rather than shrinking it.
            scale = min(usable_width / iw, usable_height / ih, 1.0)
            return RLImage(str(path), width=iw * scale, height=ih * scale)
        except Exception as e:
            app.logger.warning("assessment PDF: skipping figure %s (%s)", fname, e)
            return Paragraph(f"<i>[figure could not be embedded: {_e(fname)}]</i>",
                             meta_style)

    story = []
    logo_name = (os.environ.get("SCHOOL_LOGO") or "").strip()
    if logo_name:
        logo_path = _STATIC_DIR / os.path.basename(logo_name)
        if logo_path.is_file():
            try:
                iw, ih = ImageReader(str(logo_path)).getSize()
                w = 1.9 * inch
                story += [RLImage(str(logo_path), width=w, height=w * ih / iw),
                          Spacer(1, 0.12 * inch)]
            except Exception as e:
                app.logger.warning("assessment PDF: logo skipped (%s)", e)

    story.append(Paragraph(_e(title), h1))
    for line in (subtitle or "").split("\n"):
        if line.strip():
            story.append(Paragraph(_e(line.strip()), meta_style))
    story.append(Spacer(1, 0.18 * inch))

    seen_contexts: set = set()
    answer_lines: list[str] = []
    for i, q in enumerate(snapshot, start=1):
        ctx, ctx_id = q.get("_context"), q.get("context_id")
        if ctx and ctx_id and ctx_id not in seen_contexts:
            seen_contexts.add(ctx_id)
            heading = "Shared context" + (f": {ctx['title']}" if ctx.get("title") else "")
            story.append(Paragraph(f"<b>{_e(heading)}</b><br/>{_e(ctx.get('text',''))}",
                                   context_style))

        pts = q.get("max_points", 1)
        block = [Paragraph(f"<b>{i}.</b> ({pts} {'pt' if pts == 1 else 'pts'}) "
                           f"{_e(q.get('text',''))}", body)]
        for fname in q.get("images") or []:
            block.append(_figure(fname, (q.get("image_descriptions") or {}).get(fname, "")))
            block.append(Spacer(1, 4))

        qtype = q.get("qtype") or "frq"
        if qtype == "matching":
            m = q.get("matching") or {}
            left, right = m.get("left") or [], m.get("right") or []
            rows = [["#", "Column A", "#", "Column B"]]
            for n in range(max(len(left), len(right))):
                l = left[n] if n < len(left) else {}
                r = right[n] if n < len(right) else {}
                rows.append([l.get("label", ""), _e(l.get("text", "")) or "—",
                             r.get("label", ""), _e(r.get("text", "")) or "—"])
            tbl = Table(rows, colWidths=[0.3 * inch, 2.6 * inch, 0.3 * inch, 2.6 * inch])
            tbl.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
            ]))
            block.append(tbl)
            pairs = m.get("pairs") or {}
            answer = ", ".join(f"{k}\u2192{v}" for k, v in sorted(pairs.items())) or "—"
        else:
            for c in q.get("choices") or []:
                block.append(Paragraph(
                    f"<b>{_e(c.get('letter','?'))}.</b> {_e(c.get('text',''))}",
                    choice_style))
            answer = _e(q.get("correct_answer") or "—")
            if not q.get("choices") and layout == "none":
                # Free response on the student copy needs somewhere to write.
                block.append(Spacer(1, 0.55 * inch))

        answer_lines.append(f"{i}. {answer}")
        block.append(Spacer(1, 8))
        # KeepTogether so a question, its figure and its choices are never
        # split across a page break -- the one formatting rule that actually
        # matters on paper.
        story.append(KeepTogether(block))

    if layout == "key":
        story.append(PageBreak())
        story.append(Paragraph("Answer Key", h2))
        for line in answer_lines:
            story.append(Paragraph(line, answer_style))

    total = sum(float(q.get("max_points") or 0) for q in snapshot)
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(
        f"{len(snapshot)} question{'' if len(snapshot) == 1 else 's'}, "
        f"{total:g} point{'' if total == 1 else 's'} total.", meta_style))

    doc.build(story)
    return buf.getvalue()


@app.route("/assessments/<assessment_id>/export/<which>.md")
@app.route("/assessments/<assessment_id>/export/<which>")
@coach_or_volunteer_required
def api_export_assessment_markdown(assessment_id: str, which: str):
    """Download an assessment to administer on paper.

    Prefers PDF, because markdown cannot carry an image: a question asking
    about a labelled diagram exported as a bare filename reference and was
    useless on the printed page. Falls back to markdown when reportlab
    isn't installed, so the button always produces something rather than an
    error about a dependency the coach can't install.

    The explicit `.md` URL always gets markdown, for anyone who wants the
    text to edit.

    `which` is "test" (questions only) or "key" (questions with an answer
    key section after them). Renders from the published snapshot when there
    is one, so a printed key can never disagree with what students actually
    saw; a still-preparing test renders from the live bank and is stamped
    DRAFT so a draft print can't be mistaken for the real thing."""
    if which not in ("test", "key"):
        abort(404)
    test = _select_assessment(assessment_id)
    window = assessments.get_window(test.window_id)
    snapshot, is_draft = assessments.snapshot_for_render(test)
    if not snapshot:
        return jsonify({"error": "this test has no questions yet"}), 400

    ev = EVENTS.get(test.event_slug)
    label = (window.label if window else "") or (window.opens_at[:10] if window else "")
    title = f"{ev.name if ev else test.event_slug}" + (f" — {label}" if label else "")
    if which == "key":
        title += " — ANSWER KEY"
    subtitle_bits = []
    if is_draft:
        subtitle_bits.append("**DRAFT — not published.** Rendered from the live "
                             "question bank, so it may not match what students see.")
    if window:
        subtitle_bits.append(f"Window: {window.opens_at.replace('T', ' ')} "
                             f"→ {window.closes_at.replace('T', ' ')}")
    # Two trailing spaces before the newline is a markdown hard line break,
    # so the DRAFT warning and the window dates stay on separate lines.
    md = assessments.render_questions_markdown(
        snapshot, title=title, subtitle="  \n".join(subtitle_bits),
        answers="section" if which == "key" else "none")

    stem = f"{test.event_slug}-{label or assessment_id[:8]}-{which}".replace(" ", "_")

    # request.path ending in .md is the explicit "give me text" request.
    wants_markdown = request.path.endswith(".md")
    dep_error = None if wants_markdown else _optional_dep_error("reportlab")
    if dep_error:
        # Logged, not swallowed. This branch used to fall through to markdown
        # in silence, so an install that landed in the wrong interpreter was
        # indistinguishable from a deliberate choice of format -- the coach
        # just kept getting .md files with nothing anywhere saying why.
        app.logger.warning("assessment PDF unavailable, sending markdown: %s",
                           dep_error)
    if not wants_markdown and dep_error is None:
        try:
            pdf = _assessment_pdf(
                snapshot, title=title,
                # Markdown bold markers mean nothing to reportlab; strip
                # them rather than printing literal asterisks.
                subtitle="\n".join(b.replace("**", "") for b in subtitle_bits),
                layout="key" if which == "key" else "none",
                image_dir=ev.image_dir if ev else None)
            return Response(pdf, mimetype="application/pdf",
                            headers={"Content-Disposition":
                                     f"attachment; filename={stem}.pdf"})
        except Exception as e:
            # Never let a rendering problem cost the coach their download —
            # markdown below is a worse document, not a failure.
            app.logger.warning("assessment PDF failed, falling back to markdown: %s", e)

    headers = {"Content-Disposition": f"attachment; filename={stem}.md"}
    if not wants_markdown:
        # Says why this is markdown when a PDF was expected, without costing
        # the coach the download.
        headers["X-Export-Fallback"] = (dep_error or "PDF rendering failed")[:400]
    return Response(md, content_type="text/markdown; charset=utf-8",
                    headers=headers)


@app.route("/api/assessments/<assessment_id>/grading")
@coach_or_volunteer_required
def api_get_grading(assessment_id):
    test = _select_assessment(assessment_id)
    snapshot_frqs = [q for q in (test.snapshot or []) if q.get("qtype") == "frq"]
    responses = {u: {"answers": r.answers, "manual_grade": r.manual_grade, "status": r.status}
                for u, r in assessments.get_responses_for_assessment(assessment_id).items()}
    return jsonify({"snapshot_frqs": snapshot_frqs, "responses": responses,
                    "grading_complete": assessments.assessment_grading_complete(assessment_id, test.snapshot or [])})


@app.route("/api/assessments/<assessment_id>/grading/<student_username>/<number>", methods=["PATCH"])
@coach_or_volunteer_required
def api_set_manual_grade(assessment_id, student_username, number):
    test = _select_assessment(assessment_id)
    q = next((x for x in (test.snapshot or []) if str(x.get("number")) == str(number)), None)
    if q is None or q.get("qtype") != "frq":
        return jsonify({"error": "not a free-response question on this test"}), 400
    data = request.get_json() or {}
    try:
        points_earned = float(data.get("points_earned"))
    except (TypeError, ValueError):
        return jsonify({"error": "points_earned must be a number"}), 400
    try:
        assessments.set_manual_grade(assessment_id, student_username, number, points_earned,
                                 float(q.get("max_points") or 1), graded_by=g.user.username,
                                 comment=data.get("comment", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/assessments/<assessment_id>/rubric", methods=["PATCH"])
@coach_or_volunteer_required
def api_set_assessment_rubric(assessment_id):
    """Replaces a build assessment's rubric wholesale — see
    assessments.set_assessment_rubric. Rejects for an exam assessment (only
    a build assessment has a rubric at all)."""
    test = _select_assessment(assessment_id)
    if test.kind != "build":
        return jsonify({"error": "only a build assessment has a rubric"}), 400
    data = request.get_json() or {}
    try:
        updated = assessments.set_assessment_rubric(assessment_id, data.get("rubric") or [],
                                                     edited_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "rubric": updated.rubric})


@app.route("/api/assessments/<assessment_id>/rubric/copy", methods=["POST"])
@coach_or_volunteer_required
def api_copy_assessment_rubric(assessment_id):
    """"Copy rubric from…" — populate this build assessment's rubric from
    another build assessment's, so a coach edits last year's rubric rather
    than retyping it. The source must be a build assessment too; the picker
    on the grading page only offers other build assessments in the same
    season (see api_get_build_grading's `other_build_assessments`), but this
    is re-validated server-side regardless."""
    test = _select_assessment(assessment_id)
    if test.kind != "build":
        return jsonify({"error": "only a build assessment has a rubric"}), 400
    data = request.get_json() or {}
    source_id = data.get("source_assessment_id") or ""
    try:
        updated = assessments.copy_rubric_from(assessment_id, source_id, edited_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "rubric": updated.rubric})


@app.route("/api/assessments/<assessment_id>/build-grading")
@coach_or_volunteer_required
def api_get_build_grading(assessment_id):
    """Everything the build grading page needs in one round trip: the
    rubric, the rostered students (there's no "submission" list — a build
    assessment is graded straight off the season roster), every response
    recorded so far, and a picker of other build assessments this season a
    rubric could be copied from."""
    import seasons as seasons_mod

    test = _select_assessment(assessment_id)
    if test.kind != "build":
        return jsonify({"error": "not a build assessment"}), 400
    users = auth.load_users()
    roster = []
    for username in seasons_mod.get_roster(test.season_id, test.event_slug):
        u = users.get(username)
        if u is None or u.disabled:
            continue
        roster.append({"username": u.username, "display_name": u.display_name or u.username})
    roster.sort(key=lambda d: d["display_name"].lower())
    responses = {u: {"rubric_values": r.rubric_values,
                     "manual_grade": r.manual_grade.get(assessments.BUILD_GRADE_KEY)}
                for u, r in assessments.get_responses_for_assessment(assessment_id).items()}
    other_builds = [
        {"assessment_id": t.assessment_id, "event_slug": t.event_slug,
         "window_label": (assessments.get_window(t.window_id).label
                          if assessments.get_window(t.window_id) else "")}
        for t in assessments.assessments_for_season(test.season_id)
        if t.kind == "build" and t.assessment_id != assessment_id and t.rubric
    ]
    return jsonify({
        "rubric": test.rubric, "roster": roster, "responses": responses,
        "other_build_assessments": other_builds,
        "grading_complete": assessments.assessment_grading_complete(
            assessment_id, [], kind="build", season_id=test.season_id, event_slug=test.event_slug),
    })


@app.route("/api/assessments/<assessment_id>/build-grading/<student_username>", methods=["PATCH"])
@coach_or_volunteer_required
def api_set_build_grade(assessment_id, student_username):
    test = _select_assessment(assessment_id)
    if test.kind != "build":
        return jsonify({"error": "not a build assessment"}), 400
    data = request.get_json() or {}
    override = data.get("override")
    override_max = data.get("override_max")
    try:
        if override is not None:
            override = float(override)
        if override_max is not None:
            override_max = float(override_max)
        assessments.set_build_grade(
            assessment_id, student_username,
            rubric_values=data.get("rubric_values") or {},
            override=override, override_max=override_max,
            graded_by=g.user.username, comment=data.get("comment", ""))
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/assessments/<assessment_id>/release-grades", methods=["POST"])
@coach_required
def api_release_grades(assessment_id):
    test = assessments.get_assessment(assessment_id)
    if test is None:
        abort(404)
    try:
        if test.kind == "build":
            count = assessments.release_grades(assessment_id, [], released_by=g.user.username,
                                                kind="build", season_id=test.season_id,
                                                event_slug=test.event_slug)
        else:
            count = assessments.release_grades(assessment_id, test.snapshot or [], released_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "released_count": count})


@app.route("/my-assessments/<assessment_id>/results")
@student_required
def assessment_results_page(assessment_id):
    test, window = _student_assessment_context(assessment_id)
    resp = assessments.get_response(assessment_id, g.user.username)
    if resp is None or not resp.released:
        abort(403, "Results aren't released yet")
    return _render_assessment_results(test, resp, viewer_is_self=True, student_username=g.user.username)


def _render_assessment_results(test: "assessments.Assessment", resp: "assessments.Response",
                         viewer_is_self: bool, student_username: str):
    """Shared by assessment_results_page (a student viewing their own released
    results) and score_detail_page (a coach/grading-volunteer drilling into
    a specific student's response from the Scores page)."""
    ev = EVENTS.get(test.event_slug)
    if test.kind == "build":
        grade = resp.manual_grade.get(assessments.BUILD_GRADE_KEY) or {}
        # rubric lines paired with the student's recorded value, split into
        # scored (counts toward the total) and measured (recorded, never
        # summed — see assessments.compute_build_total) for the template to
        # render as two visually separate groups.
        scored_rows, measured_rows = [], []
        for line in (test.rubric or []):
            value = (resp.rubric_values or {}).get(line["id"])
            row = {"line": line, "value": value}
            (scored_rows if line.get("kind") == "scored" else measured_rows).append(row)
        return render_template("assessment_results.html", event_name=ev.name if ev else test.event_slug,
                               is_build=True, scored_rows=scored_rows, measured_rows=measured_rows,
                               total_earned=grade.get("points_earned"), total_possible=grade.get("points_possible"),
                               comment=grade.get("comment") or "",
                               viewer_is_self=viewer_is_self, student_username=student_username,
                               assessment_id=test.assessment_id)
    contexts = test.snapshot_contexts or {}
    rows = []
    for q in (test.snapshot or []):
        number = str(q.get("number"))
        answer = resp.answers.get(number) or {}
        auto = resp.auto_grade.get(number)
        manual = resp.manual_grade.get(number)
        # Resolve the shared-context block here rather than in the template:
        # a question stores a bare context_id while the snapshot map is keyed
        # "bucket::id", and that suffix match is awkward in Jinja.
        ctx_id = q.get("context_id")
        ctx = contexts.get(f"{q.get('bucket')}::{ctx_id}") if ctx_id else None
        rows.append({"q": q, "answer": answer, "auto": auto, "manual": manual,
                     "context": ctx})
    total_earned = sum((r["auto"] or r["manual"] or {}).get("points_earned") or 0 for r in rows)
    total_possible = sum(float(q.get("max_points") or 1) for q in (test.snapshot or []))
    return render_template("assessment_results.html", event_name=ev.name if ev else test.event_slug,
                           is_build=False, rows=rows, total_earned=total_earned, total_possible=total_possible,
                           viewer_is_self=viewer_is_self, student_username=student_username,
                           assessment_id=test.assessment_id)


# ---------------------------------------------------------------------------
# Routes — Scores page (every role, including students, can see named
# scores; response-detail drill-down is restricted — see
# can_view_response_detail()).
# ---------------------------------------------------------------------------

def can_view_response_detail(viewer: "auth.User", test: "assessments.Assessment", student_username: str,
                             response: "assessments.Response | None", window: "assessments.AssessmentWindow | None") -> bool:
    """Coaches always; a volunteer only for tests THEY personally graded
    (stamped via graded_by on at least one manual grade) — with one
    fallback: if a test has zero FRQ items (nothing to manually grade),
    its assigned volunteer(s) still get drill-down, since the literal
    "personally graded" rule would otherwise lock them out of their own
    test's results for no good reason. Students see only their own."""
    if viewer.role == "coach":
        return True
    if viewer.role == "volunteer":
        if response and any(grade.get("graded_by") == viewer.username for grade in response.manual_grade.values()):
            return True
        has_frq = any(q.get("qtype") == "frq" for q in (test.snapshot or []))
        if not has_frq and window:
            return viewer.username in (window.assignments.get(test.event_slug) or [])
        return False
    if viewer.role == "student":
        return viewer.username == student_username
    return False


@app.route("/scores")
def scores_page():
    import seasons as seasons_mod

    user = g.user
    if user.role not in ("coach", "volunteer", "student"):
        abort(403)
    all_seasons = sorted(seasons_mod.load_seasons().values(), key=lambda s: s.season_id, reverse=True)
    current = seasons_mod.get_current_season()
    selected_id = seasons_mod.resolve_season_id(request.args.get("season"))
    selected = seasons_mod.get_season(selected_id) if selected_id else None

    students_seen: dict[str, str] = {}
    columns = []  # [{test, window, event_slug, label}]
    grid = {}     # {username: {assessment_id: {earned, possible, pending, detail_ok}}}

    if selected:
        users = auth.load_users()
        for slug in selected.event_slugs:
            for u in seasons_mod.get_roster(selected.season_id, slug):
                students_seen[u] = u
        for w in assessments.load_windows().values():
            if w.season_id != selected.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                t = assessments.get_assessment_for(w.window_id, slug)
                if t is None or not t.snapshot:
                    continue
                if not assessments.assessment_grading_complete(t.assessment_id, t.snapshot):
                    continue
                columns.append({"assessment": t, "window": w, "event_slug": slug,
                               "label": f"{slug} — {w.label or w.opens_at[:10]}"})
                responses = assessments.get_responses_for_assessment(t.assessment_id)
                for username, resp in responses.items():
                    if resp.status not in ("submitted", "auto_submitted_late"):
                        continue
                    earned = sum((resp.auto_grade.get(str(q.get("number"))) or
                                 resp.manual_grade.get(str(q.get("number"))) or {}).get("points_earned") or 0
                                 for q in t.snapshot)
                    possible = sum(float(q.get("max_points") or 1) for q in t.snapshot)
                    detail_ok = (user.role == "coach") or can_view_response_detail(
                        user, t, username, resp, w)
                    grid.setdefault(username, {})[t.assessment_id] = {
                        "earned": earned, "possible": possible,
                        "pending": not resp.released, "detail_ok": detail_ok,
                    }

    return render_template("scores.html", all_seasons=all_seasons, selected=selected,
                           columns=columns, students=sorted(students_seen.values()), grid=grid)


@app.route("/scores/<assessment_id>/<student_username>")
def score_detail_page(assessment_id, student_username):
    test = assessments.get_assessment(assessment_id)
    if test is None:
        abort(404)
    window = assessments.get_window(test.window_id)
    resp = assessments.get_response(assessment_id, student_username)
    if resp is None:
        abort(404)
    if not can_view_response_detail(g.user, test, student_username, resp, window):
        abort(403, "You don't have access to this student's responses")
    return _render_assessment_results(test, resp, viewer_is_self=(g.user.username == student_username),
                                student_username=student_username)


@app.route("/event/<event_slug>/")
def event_index(event_slug):
    _select_event(event_slug)
    # _select_event above has already stamped this request, so the viewer
    # must be excluded or they would always count themselves.
    return render_template(
        "event_index.html",
        event_slug=event_slug,
        event_name=bqb.EVENT.name,
        n_active=presence.active_by_event(
            exclude_user=g.user.username).get(event_slug, 0))


@app.route("/event/<event_slug>/extract/<pdfname>")
def extract_pdf(event_slug, pdfname):
    _select_event(event_slug)
    return render_template("extract.html",
                            pdf_name=pdfname,
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name,
                            # Source text (not a compiled RegExp) for the "Remove
                            # point markers" tool's worded-form preset, so the
                            # client-side preview reuses the exact same pattern
                            # text_utils.strip_points() applies server-side
                            # instead of a hand-copied duplicate that could drift.
                            points_re_source=text_utils._POINTS_RE.pattern)


@app.route("/event/<event_slug>/review/<pdfname>")
def review_redirect(event_slug, pdfname):
    """Permanent redirect from the old page name. The per-PDF workflow here
    was never reviewing a trustworthy automatic result — the extraction is
    not robust, so this is where you re-extract the questions by hand. Old
    bookmarks and links use /review/, so it has to keep resolving."""
    return redirect(url_for("extract_pdf", event_slug=event_slug, pdfname=pdfname),
                     code=301)


# ---------------------------------------------------------------------------
# Routes — API (all namespaced under /event/<slug>)
# ---------------------------------------------------------------------------

@app.route("/event/<event_slug>/api/pdfs")
def api_pdfs(event_slug):
    _select_event(event_slug)
    sort = request.args.get("sort", "name")
    order = request.args.get("order", "asc")
    state = bqb._load_state()
    rows = [_pdf_status(p, state) for p in _list_test_pdfs()]
    sortmap = {"name": "name", "mtime": "mtime", "size": "size",
               "questions": "n_questions", "images": "n_with_img",
               "answers": "n_with_ans"}
    rows.sort(key=lambda r: r[sortmap.get(sort, "name")],
              reverse=(order == "desc"))
    return jsonify(rows)


@app.route("/event/<event_slug>/api/pdf/<pdfname>")
@_with_vision_key
def api_pdf(event_slug, pdfname):
    _select_event(event_slug)
    state = bqb._load_state()
    qs = state.get("questions", {}).get(pdfname, [])
    if qs and not all("page" in q for q in qs):
        # Re-check inside the transaction (not just re-using the outer
        # `state`/`qs` snapshot) — two near-simultaneous first-loads of the
        # same PDF could otherwise both pass this outer check, and the
        # second save would silently replace the first one's effectively-
        # identical computed page numbers. The double-check keeps this a
        # write-once operation rather than turning a conditional save into
        # an unconditional one on every page load.
        with bqb._state_transaction() as state:
            qs = state.get("questions", {}).get(pdfname, [])
            if qs and not all("page" in q for q in qs):
                _compute_pages(pdfname, qs)
                state.setdefault("questions", {})[pdfname] = qs
    doc = _open_pdf(pdfname)
    ann = state.get("annotations", {}).get(pdfname, {})
    key_path = _key_path(bqb.BASE_DIR / pdfname)
    meta = bqb._effective_pdf_meta(bqb.BASE_DIR / pdfname, state)
    return jsonify({
        "name": pdfname,
        "page_count": doc.page_count,
        "questions": qs,
        "topics": bqb.TOPICS,
        "foci":   list(bqb.EVENT.foci),
        "vision_available": _vision_available(),
        "has_key": key_path is not None,
        # Key PDF can have a different page count than the test PDF (and a
        # supplementary doc's, fetched separately via /supplementary, almost
        # always does) — the frontend keys its thumbnail/page-nav bounds off
        # a per-target map built from these, not a single shared page_count.
        "key_page_count": (fitz.open(str(key_path)).page_count if key_path else None),
        "annotations": ann,
        # Tournament/Year: an explicit override if one's been saved, else the
        # filename-derived guess — always something to pre-fill the extract
        # page's editable fields with, override or not.
        "tournament": meta["tournament"],
        "year": meta["year"],
    })


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/qboxes")
def api_question_bboxes(event_slug, pdfname, pno):
    """
    Compute on-the-fly bounding boxes for every question that starts on a
    given page. Coordinates are returned in PDF points; the frontend scales
    to image pixels using the same DPI it's already using to render the page.

    Returns: { "boxes": [{ "number", "x0","y0","x1","y1" }, ...],
               "page_height_pt": float }

    Approach: walk every text LINE on the page (via get_text("dict"), which
    gives per-line bboxes) and record any line that matches Q_START. Each
    question's bbox spans from its anchor line's y0 down to the next anchor's
    y0 (or page bottom), widened to the leftmost/rightmost edges of any line
    in that vertical slice.

    The block-level approach this replaces only inspected the first non-blank
    line of each block, which missed questions that PyMuPDF grouped into the
    same block (e.g. circuit_lab p3, where Q5-Q8 share blocks because their
    line spacing is tight).
    """
    _select_event(event_slug)
    doc = _open_pdf(pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    page_h = float(page.rect.height)

    # Collect every text line with its bbox + text content.
    lines: list[dict] = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:    # 0 = text, 1 = image
            continue
        for line in blk.get("lines", []):
            bbox = line.get("bbox") or [0, 0, 0, 0]
            x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            txt = "".join(s.get("text", "") for s in line.get("spans", []))
            stripped = txt.strip()
            if not stripped:
                continue
            lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": stripped})
    lines.sort(key=lambda l: (l["y0"], l["x0"]))

    # Anchor = any line whose text starts with a question marker.
    anchors: list[tuple[float, str, dict]] = []   # (y0, qnum, line)
    for ln in lines:
        m = bqb.Q_START.match(ln["text"])
        if m:
            anchors.append((ln["y0"], m.group(1), ln))

    if not anchors:
        return jsonify({"boxes": [], "page_height_pt": page_h})

    out: list[dict] = []
    for i, (y_start, qnum, anchor_line) in enumerate(anchors):
        y_end = anchors[i + 1][0] if i + 1 < len(anchors) else page_h
        # Lines whose top falls inside this question's vertical slice.
        spans = [l for l in lines if y_start <= l["y0"] < y_end]
        if spans:
            x0 = min(l["x0"] for l in spans)
            x1 = max(l["x1"] for l in spans)
            y1 = min(max(l["y1"] for l in spans), y_end)
        else:
            x0 = anchor_line["x0"]
            x1 = anchor_line["x1"]
            y1 = anchor_line["y1"]
        out.append({
            "number": qnum,
            "x0":     x0,
            "y0":     y_start,
            "x1":     x1,
            "y1":     y1,
        })

    return jsonify({"boxes": out, "page_height_pt": page_h})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/answer-boxes")
def api_answer_bboxes(event_slug, pdfname, pno):
    """
    The answer-key counterpart to api_question_bboxes above: on-the-fly
    bounding boxes for every answer-key LINE on a given page of the KEY PDF
    (not the test PDF -- answers live in a separate document).

    Returns: { "boxes": [{ "number", "answer", "x0","y0","x1","y1",
                            "applied" }, ...],
               "page_height_pt": float }

    Walks text lines the same way api_question_bboxes does (get_text("dict")
    per-line bboxes, not get_text("blocks") -- the block-level view merges
    lines PyMuPDF groups together) and matches each against
    build_question_bank.ANS_LINE, the same pattern extract_answers() uses.

    Only pages bqb._is_key_page() accepts are walked, so these boxes agree
    with what the extraction pipeline actually reads a key answer from --
    not a stray numbered list on a cover or instructions page.

    `applied` mirrors process_pair()'s base-count ambiguity rule exactly (via
    the shared bqb.answer_base()/answer_base_counts() helpers, not a
    reimplementation): False when this number's base occurs more than once
    among this PDF's own extracted questions, meaning process_pair skipped
    applying it -- section-restarted numbering made it ambiguous which
    question the key line belonged to.
    """
    _select_event(event_slug)
    key_path = _key_path(bqb.BASE_DIR / pdfname)
    if not key_path:
        abort(404, "No key PDF")
    doc = fitz.open(str(key_path))
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    page_h = float(page.rect.height)

    # Same page-qualification check extract_answers() itself applies -- skip
    # pages the pipeline wouldn't have read an answer from in the first
    # place. Full page text, not normalize_unicode()'d -- process_pair's key
    # branch doesn't normalize the key's page texts either.
    if not bqb._is_key_page(page.get_text("text")):
        return jsonify({"boxes": [], "page_height_pt": page_h})

    lines: list[dict] = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:    # 0 = text, 1 = image
            continue
        for line in blk.get("lines", []):
            bbox = line.get("bbox") or [0, 0, 0, 0]
            x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            txt = "".join(s.get("text", "") for s in line.get("spans", []))
            stripped = txt.strip()
            if not stripped:
                continue
            lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": stripped})

    state = bqb._load_state()
    questions = state.get("questions", {}).get(pdfname, [])
    base_counts = bqb.answer_base_counts(q["number"] for q in questions)

    out: list[dict] = []
    for ln in lines:
        m = bqb.ANS_LINE.match(ln["text"])
        if not m:
            continue
        number, ans_text = m.group(1), m.group(2).strip()
        if not ans_text:
            continue
        applied = base_counts[bqb.answer_base(number)] <= 1
        out.append({
            "number":  number,
            "answer":  ans_text,
            "x0":      ln["x0"],
            "y0":      ln["y0"],
            "x1":      ln["x1"],
            "y1":      ln["y1"],
            "applied": applied,
        })

    return jsonify({"boxes": out, "page_height_pt": page_h})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/outline")
def api_pdf_outline(event_slug, pdfname):
    """PyMuPDF-extracted outline (TOC) if the PDF has one."""
    _select_event(event_slug)
    doc = _open_pdf(pdfname)
    try:
        toc = doc.get_toc()
    except Exception:
        toc = []
    items = [{"level": l, "title": t, "page": p} for l, t, p in toc]
    return jsonify({"outline": items})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/supplementary")
def api_pdf_supplementary(event_slug, pdfname):
    """Backs extract.html's dynamically-added target-toggle buttons (one per
    discovered sheet/notes/etc. document) — see _supplementary_docs()."""
    _select_event(event_slug)
    test_pdf = bqb.BASE_DIR / pdfname
    docs = []
    for p in _supplementary_docs(test_pdf):
        prefix_len = len(pdfname) - len("_test.pdf")
        label = p.name[prefix_len + 1:-4] if prefix_len >= 0 else p.stem
        try:
            page_count = fitz.open(str(p)).page_count
        except Exception:
            page_count = None
        docs.append({"filename": p.name, "label": label.replace("_", " ").title(),
                     "page_count": page_count})
    return jsonify({"docs": docs})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>.png")
def api_render(event_slug, pdfname, pno):
    """Render one page to PNG, through two caches.

    An ETag lets a returning browser get a 304 with no render and no
    transfer. A miss then checks the on-disk cache before rasterising. The
    ETag is computed from the source file's identity rather than the
    response body, so a 304 costs a stat rather than a render — computing it
    from the bytes would mean rendering first, which defeats the point.
    """
    _select_event(event_slug)
    # Bound the DPI: it lands in a cache key and drives an allocation, so
    # an arbitrary value is both a disk-filling and a memory risk.
    try:
        dpi = max(10, min(400, int(request.args.get("dpi", "120"))))
    except ValueError:
        dpi = 120
    target = request.args.get("target", "test")
    path = _resolve_target_path(pdfname, target)

    key = _render_cache_key(path, pno, dpi)
    etag = f'"{key}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag,
                                             "Cache-Control": "private, no-cache"})

    png = _render_cache_read(key)
    if png is None:
        doc = fitz.open(str(path))
        if pno < 1 or pno > doc.page_count:
            abort(404)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        png = doc[pno - 1].get_pixmap(matrix=mat, colorspace=fitz.csRGB).tobytes("png")
        _render_cache_write(key, png)

    # no-cache means "revalidate before reuse", not "don't store": the
    # browser keeps the bytes and asks with If-None-Match. That keeps a
    # swapped or reprocessed PDF from showing stale pages, while still
    # skipping the render.
    return Response(png, mimetype="image/png",
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page-counts")
def api_pdf_page_counts(event_slug, pdfname):
    """Lightweight page-count lookup for test/key, used by the PDF-listing
    page's preview drawer (event_index.html) to bound its page-nav without
    paying api_pdf()'s heavier state-load/question-compute cost — this gets
    called every time a preview opens, possibly many times per visit to a
    long PDF list."""
    _select_event(event_slug)
    test_doc = _open_pdf(pdfname)
    key_path = _key_path(bqb.BASE_DIR / pdfname)
    # Key name is part of this endpoint's contract with event_index.html
    # (counts.test / counts.key), and refers to the test PDF.
    return jsonify({"test": test_doc.page_count,
                    "key": (fitz.open(str(key_path)).page_count if key_path else None)})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/save", methods=["POST"])
def api_save(event_slug, pdfname):
    _select_event(event_slug)
    data = request.get_json()
    new_qs = data.get("questions", [])
    cleaned = []
    for q in new_qs:
        clean_q = {
            "number":   str(q.get("number", "")).strip(),
            "topic":    q.get("topic") or "Other / General",
            "focus":    (q.get("focus") or "").strip(),
            "text":     (q.get("text") or "").strip(),
            "choices":  [{"letter": c.get("letter", "").upper(),
                          "text": (c.get("text") or "").strip()}
                         for c in (q.get("choices") or [])
                         if (c.get("text") or "").strip()],
            "answer":   (q.get("answer") or "").strip(),
            "images":   list(q.get("images") or []),
            "source":   q.get("source", ""),
            "year":     q.get("year", ""),
            "division": q.get("division", ""),
            "page":     int(q.get("page") or 1),
        }
        # qtype: whitelisted so "tf"/"matching" survive a whole-PDF manual
        # save the same way every other explicit field does (this endpoint
        # previously dropped qtype/matching entirely — a pre-existing gap,
        # not new to tf; fixed here since tf is the type that surfaced it).
        qtype = (q.get("qtype") or "").strip()
        if qtype and qtype in _VALID_QTYPES:
            clean_q["qtype"] = qtype
        if qtype == "matching" and q.get("matching") is not None:
            clean_q["matching"] = q.get("matching")
        # difficulty: additive, optional. Absent/None means unrated -- don't
        # set the key at all (matches apply_annotations' "clear pops the
        # key" semantics rather than storing a literal None).
        if q.get("difficulty") is not None:
            try:
                clean_q["difficulty"] = float(q.get("difficulty"))
            except (TypeError, ValueError):
                pass
        # Optional multi-page span list
        extra = q.get("extra_pages")
        if extra:
            clean_q["extra_pages"] = [int(p) for p in extra if isinstance(p, (int, float, str)) and str(p).strip().lstrip("-").isdigit()]
        # Optional reference to a shared context block (defined in annotations.contexts)
        ctx_id = (q.get("context_id") or "").strip()
        if ctx_id:
            clean_q["context_id"] = ctx_id
        # Optional per-image textual descriptions ({fname: description}).
        # Used as alt-text and as the seed prompt for the diagram generator
        # so the LLM understands what each existing image already covers.
        img_desc = q.get("image_descriptions")
        if isinstance(img_desc, dict):
            clean_q["image_descriptions"] = {
                str(fn): str(d) for fn, d in img_desc.items() if d
            }
        cleaned.append(clean_q)
    with bqb._state_transaction() as state:
        state.setdefault("questions", {})[pdfname] = cleaned
        state.setdefault("manual", {})[pdfname] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
        ann = data.get("annotations") or {}
        if ann:
            state.setdefault("annotations", {})[pdfname] = ann
    return jsonify({"ok": True, "saved": len(cleaned),
                    "annotations_kept": bool(ann)})


@app.route("/api/usage")
def api_usage():
    """Return the running tally of Anthropic API consumption for this process.
    Frontend polls this and renders an estimated-cost badge in the header so
    users notice runaway burn before the invoice arrives."""
    return jsonify(bqb.get_usage_stats())


def _validate_job_id(job_id: str) -> str:
    """Every route below takes job_id from the URL — validate its shape
    before it's ever used to build a filesystem path (jobs.py's log file
    lookup), the same containment principle as _safe_join, just simpler
    since job ids have one fixed, known shape (uuid4().hex[:12])."""
    if not jobs.JOB_ID_RE.match(job_id or ""):
        abort(400, "bad job id")
    return job_id


def _job_target_setup(event_slug: str):
    """Every job target closure runs on jobs.py's dedicated worker thread,
    NOT the Flask request thread that enqueued it — build_question_bank's
    "current event" is a ContextVar (see _select_event's docstring), which
    does NOT carry over to a different thread. Job targets must re-bind it
    themselves before touching bqb.EVENT/_save_state/etc."""
    bqb.set_event(event_slug)


@app.route("/event/<event_slug>/api/download/start", methods=["POST"])
def api_download_start(event_slug):
    """Kick off a background download_event run for this event, via the
    unified job queue (jobs.py) — gains cancellation and disk persistence
    the old bespoke _DOWNLOAD_JOBS dict never had."""
    _select_event(event_slug)

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        ok = download_event.download_all(
            event_slug, skip_existing=True, bypass_bot=True,
            should_cancel=should_cancel, on_progress=on_progress,
        )
        return {"success": bool(ok)}

    try:
        job_id = jobs.submit_job(event_slug, "scioly_download",
                                 f"Download PDFs for {event_slug}",
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/jobs")
def api_jobs_list(event_slug):
    """Most-recent-first job history for this event — backs the per-event
    Jobs page. Visibility matches every other event route: anyone with
    access to the event (coach implicitly, assigned volunteer) sees every
    job's full status, progress, and (via /log) console output — no
    additional restriction, by design (see spec.md)."""
    _select_event(event_slug)
    is_coach = g.user.role == "coach"
    records = jobs.list_jobs(event_slug)
    return jsonify({"jobs": [jobs.job_to_public_dict(r, g.user.username, is_coach)
                             for r in records]})


@app.route("/event/<event_slug>/api/jobs/<job_id>")
def api_job_detail(event_slug, job_id):
    _select_event(event_slug)
    _validate_job_id(job_id)
    is_coach = g.user.role == "coach"
    try:
        record = jobs.get_job(event_slug, job_id)
    except jobs.JobNotFound:
        abort(404)
    return jsonify(jobs.job_to_public_dict(record, g.user.username, is_coach))


@app.route("/event/<event_slug>/api/jobs/<job_id>/log")
def api_job_log(event_slug, job_id):
    """`?after=<line_count>` returns only lines appended since the caller's
    last poll, instead of resending the whole (potentially long) console
    output every ~1.5s."""
    _select_event(event_slug)
    _validate_job_id(job_id)
    after = max(0, int(request.args.get("after", 0) or 0))
    lines, total = jobs.read_log_tail(event_slug, job_id, after=after)
    return jsonify({"lines": lines, "total": total})


@app.route("/event/<event_slug>/api/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(event_slug, job_id):
    _select_event(event_slug)
    _validate_job_id(job_id)
    is_coach = g.user.role == "coach"
    try:
        record = jobs.request_cancel(event_slug, job_id, g.user.username, is_coach)
    except jobs.JobNotFound:
        abort(404)
    except jobs.JobNotAuthorized:
        abort(403, "Only the job's starter or a coach can cancel it")
    return jsonify(jobs.job_to_public_dict(record, g.user.username, is_coach))


@app.route("/event/<event_slug>/jobs")
def event_jobs_page(event_slug):
    _select_event(event_slug)
    return render_template("event_jobs.html", event_slug=event_slug,
                           event_name=bqb.EVENT.name)


@app.route("/api/jobs/active-count")
def api_jobs_active_count():
    """Backs the small header badge — counts queued+running jobs across
    every event the current user can see (coach: all; volunteer: assigned).
    No per-event access check needed beyond that filter, since this only
    returns a number, never job content."""
    user = g.user
    if user.role == "coach":
        slugs = list(EVENTS.keys())
    else:
        slugs = list(user.events)
    return jsonify(jobs.active_job_summary(slugs))


# ---------------------------------------------------------------------------
# Routes — tournament archive
#
# Browsing is coach-or-volunteer; a volunteer sees only subtrees whose
# <Division>/<Event> folder is mapped to an event they already hold, and the
# filtering happens at the parent so folder names alone do not disclose the
# shape of the rest of the corpus. Mapping, indexing and duplicate review
# are coach-only: triage is a coach job. See archive_map.py.
# ---------------------------------------------------------------------------

@app.route("/archive")
@coach_or_volunteer_required
def archive_page():
    return render_template("archive.html",
                           archive_name=tournament_archive.ARCHIVE_DIRNAME,
                           is_coach=g.user.role == "coach")


@app.route("/api/archive/list")
@coach_or_volunteer_required
def api_archive_list():
    """One level of the tree. `?path=` is archive-relative; empty is root."""
    rel = request.args.get("path", "") or ""
    if not archive_map.can_traverse(g.user, rel):
        # 404 rather than 403: confirming that a path exists but is barred
        # tells a volunteer what events the archive holds.
        return jsonify({"error": f"no such folder: {rel}"}), 404
    try:
        listing = tournament_archive.list_dir(rel)
    except ValueError as e:            # containment failure
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": f"no such folder: {rel}"}), 404

    if g.user.role != "coach":
        allowed = set(archive_map.visible_children(
            g.user, listing["rel"], [d["name"] for d in listing["subdirs"]]))
        listing["subdirs"] = [d for d in listing["subdirs"] if d["name"] in allowed]
        # Files directly inside an unmapped folder belong to nobody. Above
        # the mapping level there should be none anyway; if the tree
        # violates the convention, erring towards hiding is the safe way.
        if not archive_map.can_access(g.user, listing["rel"]):
            listing["files"] = []
    listing["breadcrumbs"] = tournament_archive.breadcrumbs(listing["rel"])
    return jsonify(listing)


@app.route("/api/archive/duplicates")
@coach_required
def api_archive_duplicates():
    """Byte-identical files, biggest wasted space first.

    Paginated: a corpus this size can produce a lot of groups, and the page
    only ever shows a screenful. Coach-only — a duplicate group spans the
    whole archive by definition, so it cannot be scoped to one event."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        limit, offset = 100, 0
    return jsonify(tournament_archive.duplicate_groups(limit=limit, offset=offset))


@app.route("/api/archive/status")
@coach_or_volunteer_required
def api_archive_status():
    payload = {**tournament_archive.summary(),
               "build": tournament_archive.build_status(),
               "archive_dir": str(tournament_archive.archive_root())}
    if g.user.role != "coach":
        # Whole-archive totals are a coach's view of the backlog. A
        # volunteer's numbers would be wrong for what they can see, and
        # right about what they cannot.
        # "writable"/"reason" deliberately survive: importing is a
        # volunteer action, so they need to know it will not work.
        for key in ("total_files", "total_bytes", "n_dirs", "duplicates",
                    "archive_dir"):
            payload.pop(key, None)
    return jsonify(payload)


@app.route("/api/archive/reindex", methods=["POST"])
@coach_required
def api_archive_reindex():
    if not tournament_archive.exists():
        return jsonify({"error": "the archive directory does not exist yet"}), 400
    return jsonify({"ok": True, "build": tournament_archive.start_build()})


@app.route("/api/archive/cancel", methods=["POST"])
@coach_required
def api_archive_cancel():
    """Stop a running rebuild. Cooperative, so the reply means "asked", not
    "stopped" — the page keeps polling status to see it wind down."""
    return jsonify({"ok": True, "build": tournament_archive.cancel_build()})


@app.route("/archive/map")
@coach_required
def archive_map_page():
    return render_template("archive_map.html")


@app.route("/api/archive/map")
@coach_required
def api_archive_map():
    """Every <Division>/<Event> folder, its current mapping and a suggestion.

    Also returns the name groups behind the "same slug for every division"
    shortcut, so the client does not re-derive normalisation rules that
    belong to the server."""
    return jsonify({
        "rows": archive_map.event_folders(),
        "by_name": archive_map.folders_by_name(),
        "events": [{"slug": slug, "name": ev.name}
                   for slug, ev in sorted(EVENTS.items(),
                                          key=lambda kv: kv[1].name)
                   if not ev.archived],
        "indexed": tournament_archive.load_index() is not None,
    })


@app.route("/api/archive/map", methods=["POST"])
@coach_required
def api_archive_map_save():
    """Save a screenful at once. All-or-nothing on validation."""
    data = request.get_json() or {}
    pairs = data.get("pairs")
    if not isinstance(pairs, dict):
        return jsonify({"error": "pairs must be an object"}), 400
    try:
        entries = archive_map.set_many(pairs)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "mapped": len(entries)})


# --- Phase 3: mutations ----------------------------------------------------
#
# Coach-only, and every one of them previews first. Organising inherently
# means deleting junk, so these do not sit behind ALLOW_HARD_DELETE —
# requiring it would mean leaving user/season/event deletion switched on for
# the whole triage effort. Deletes go to the shared trash instead.

def _archive_op_args(data):
    """Pull the arguments for one action out of a request body."""
    action = (data.get("action") or "").strip().lower()
    if action == "rename":
        return action, (data.get("path") or "", data.get("name") or "")
    if action == "move":
        return action, (data.get("path") or "", data.get("dest") or "")
    if action == "delete":
        return action, (data.get("path") or "",)
    if action == "create":
        return action, (data.get("path") or "", data.get("name") or "")
    raise archive_ops.ArchiveOpError(f"unknown action: {action or '(none)'}")


@app.route("/api/archive/preview", methods=["POST"])
@coach_required
def api_archive_preview():
    """What this action would do. Never touches the filesystem."""
    try:
        action, args = _archive_op_args(request.get_json() or {})
        return jsonify({"ok": True, "preview": archive_ops.PREVIEWS[action](*args)})
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:                     # containment failure
        return jsonify({"error": str(e)}), 400


@app.route("/api/archive/apply", methods=["POST"])
@coach_required
def api_archive_apply():
    """Carry out one action. The client is expected to have previewed it,
    but this re-previews internally rather than trusting that — the tree can
    change between the two calls."""
    handlers = {"rename": archive_ops.rename, "move": archive_ops.move,
                "delete": archive_ops.delete, "create": archive_ops.create_folder}
    try:
        action, args = _archive_op_args(request.get_json() or {})
        result = handlers[action](*args, by=g.user.username)
        return jsonify({"ok": True, "result": result})
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"filesystem refused: {e}"}), 500


@app.route("/api/archive/prune-empty", methods=["POST"])
@coach_required
def api_archive_prune_empty():
    """Requirement 4's other half: drop folders that hold nothing at all."""
    rel = (request.get_json() or {}).get("path") or ""
    try:
        return jsonify({"ok": True,
                        "result": archive_ops.delete_empty_folders(
                            rel, by=g.user.username)})
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/archive/ops")
@coach_required
def api_archive_ops():
    """Recent mutations, newest first — the audit trail for a 65GB reshuffle."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    return jsonify({"ops": archive_ops.read_ops(limit)})


# --- Phase 4: import into an event -----------------------------------------
#
# Open to volunteers, unlike the Phase 3 mutations: importing is the same
# power they already have through the web upload, just sourced from the
# archive instead of their laptop, and it is the main way 65GB actually gets
# triaged. Both ends are checked — they must be able to see the archive path
# *and* hold the destination event.

def _import_allowed(items, slug):
    """Raise unless this user may read every source and write that event."""
    if g.user.role != "coach":
        if slug not in (g.user.events or ()):
            raise archive_import.ImportError_(
                "you do not have access to that event")
        for item in items:
            if not archive_map.can_access(g.user, (item.get("path") or "")):
                raise archive_import.ImportError_(
                    "that file is not in an event you have access to")


@app.route("/api/archive/import/preview", methods=["POST"])
@coach_or_volunteer_required
def api_archive_import_preview():
    """Every destination name, worked out before anything moves."""
    data = request.get_json() or {}
    items = data.get("items") or []
    try:
        _import_allowed(items, data.get("slug") or "")
        plan = archive_import.plan_import(items, data.get("slug") or "",
                                          data.get("meta") or {})
        return jsonify({"ok": True, "plan": plan})
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/archive/import", methods=["POST"])
@coach_or_volunteer_required
def api_archive_import():
    data = request.get_json() or {}
    items = data.get("items") or []
    try:
        _import_allowed(items, data.get("slug") or "")
        plan = archive_import.run_import(items, data.get("slug") or "",
                                         data.get("meta") or {},
                                         by=g.user.username)
        return jsonify({"ok": True, "plan": plan})
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"filesystem refused: {e}"}), 500


@app.route("/api/archive/import/subtree")
@coach_or_volunteer_required
def api_archive_import_subtree():
    """Everything importable under one folder, grouped by year and tournament.

    The per-folder picker does not scale: a mapped event subtree holds
    hundreds of files across years, and importing them a folder at a time is
    not a workflow anyone finishes. Files already in the event are hidden by
    default and counted, because after one import the remaining duplicate
    copies elsewhere in the archive would otherwise look like fresh material.
    """
    rel = (request.args.get("path") or "").strip()
    slug = (request.args.get("slug") or "").strip()
    if not archive_map.can_traverse(g.user, rel):
        return jsonify({"error": f"no such folder: {rel}"}), 404
    if g.user.role != "coach" and slug not in (g.user.events or ()):
        return jsonify({"error": "you do not have access to that event"}), 400
    try:
        return jsonify(archive_import.subtree_files(
            rel, slug,
            include_imported=request.args.get("all") == "1"))
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/archive/import/batch", methods=["POST"])
@coach_or_volunteer_required
def api_archive_import_batch():
    """Import a selection spanning several tournament folders."""
    data = request.get_json() or {}
    items = data.get("items") or []
    slug = (data.get("slug") or "").strip()
    if not items:
        return jsonify({"error": "nothing selected"}), 400
    try:
        _import_allowed(items, slug)
        result = archive_import.run_batch_import(items, slug, by=g.user.username)
    except archive_ops.ArchiveOpError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    app.logger.info("archive batch import: user=%s slug=%s files=%d folders=%d failed=%d",
                    g.user.username, slug, result["count"], result["folders"],
                    len(result["failed"]))
    if not result["count"] and result["failed"]:
        # Every folder failed: a fault, not a partial result.
        return jsonify({"error": result["failed"][0]["error"],
                        "failed": result["failed"]}), 400
    return jsonify({"ok": True, "result": result})


@app.route("/api/archive/import/targets")
@coach_or_volunteer_required
def api_archive_import_targets():
    """Events this user can import into, plus what the path already implies.

    The metadata comes from the server because the folder-name parsing rules
    live there; the client should not re-derive them.
    """
    rel = request.args.get("path", "") or ""
    if not archive_map.can_traverse(g.user, rel):
        return jsonify({"error": f"no such folder: {rel}"}), 404
    if g.user.role == "coach":
        slugs = [s for s, ev in EVENTS.items() if not ev.archived]
    else:
        slugs = [s for s in (g.user.events or ())
                 if s in EVENTS and not EVENTS[s].archived]
    suggested = archive_map.slug_for_path(rel)
    return jsonify({
        "events": sorted(({"slug": s, "name": EVENTS[s].name}
                          for s in slugs), key=lambda e: e["name"]),
        # The mapping already records which event this subtree belongs to,
        # so the destination is usually a confirmation rather than a choice.
        "suggested": suggested if suggested in slugs else None,
        "meta": archive_import.path_metadata(rel),
    })


# --- PDF preview -----------------------------------------------------------
#
# The archive is the least-trusted content in the system: 65GB of PDFs
# downloaded from anywhere, by anyone, over years. So this opens them through
# pdf_safety rather than fitz directly, and renders through the shared disk
# cache under its own scope — archive files belong to no event, and borrowing
# a slug would file them in that event's shard.

def _archive_pdf_or_404(rel: str):
    """Resolve an archive-relative PDF, checking this user may see it."""
    if not archive_map.can_access(g.user, rel):
        abort(404, "no such file")
    try:
        path = tournament_archive.safe_path(rel)
    except ValueError:
        abort(400, "bad path")
    if not path.is_file():
        abort(404, "no such file")
    return path


ARCHIVE_DOC_PDF_SCOPE = ".archive_doc"
_ARCHIVE_DOC_EXTS = (".docx", ".doc")


def _archive_renderable_pdf(path: Path) -> Path:
    """The PDF whose pages should be rendered for an archive file.

    A `.pdf` is itself. A `.doc`/`.docx` is converted once through
    LibreOffice and cached, so that:

    - paging through a document doesn't re-run `soffice` for every page
      (conversion takes seconds; the viewer requests one PNG per page), and
    - the archive tree is never written to. It is frequently mounted
      read-only — the app already surfaces "the archive tree is read-only to
      the server" as a first-class state — so dropping a sibling `.pdf` next
      to the source is not an option.

    The cache lives beside the page-render cache and is keyed on the
    source's mtime+size, the same identity `_render_cache_key` uses, so
    replacing a document in the archive invalidates its conversion instead
    of serving the previous file's pages.

    Raises `doc_convert.DocConvertError` when conversion isn't possible
    (most often: LibreOffice isn't installed on the server). Callers surface
    that to the coach rather than 500ing — a document that can't be
    previewed is information, not a failure.
    """
    import tempfile   # local, matching this module's convention for it

    if path.suffix.lower() not in _ARCHIVE_DOC_EXTS:
        return path
    try:
        st = path.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "0:0"
    key = hashlib.sha256(f"{path.resolve()}|{stamp}".encode("utf-8")).hexdigest()
    cached = _RENDER_CACHE_DIR / ARCHIVE_DOC_PDF_SCOPE / key[:2] / f"{key}.pdf"
    if cached.is_file():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    # Convert into a scratch dir and move the result into place, so a
    # crashed or timed-out conversion never leaves a half-written PDF at the
    # cache path for the next request to read as valid.
    with tempfile.TemporaryDirectory(prefix="archive_doc_pdf_") as tmp:
        produced = doc_convert.convert_to_pdf(path, Path(tmp))
        os.replace(str(produced), str(cached))
    return cached


@app.route("/api/archive/pdf/info")
@coach_or_volunteer_required
def api_archive_pdf_info():
    """Page count and size, so the viewer knows how far it can page."""
    rel = request.args.get("path", "") or ""
    path = _archive_pdf_or_404(rel)
    # Size and name always describe the file the coach is looking at in the
    # archive, never the derived PDF a .docx was converted into — they use
    # these to decide whether to keep or delete the source document.
    src_bytes, src_name = path.stat().st_size, path.name
    try:
        render_path = _archive_renderable_pdf(path)
    except doc_convert.DocConvertError as e:
        # Same shape as the not-a-PDF case below: the viewer shows the
        # message instead of an empty frame.
        return jsonify({"error": str(e), "pages": 0, "bytes": src_bytes}), 200
    if not pdf_safety.looks_like_pdf(render_path):
        return jsonify({"error": "not a PDF", "pages": 0, "bytes": src_bytes}), 200
    try:
        doc = pdf_safety.open_pdf_safely(render_path)
    except pdf_safety.UnsafePdfError as e:
        # A malformed PDF is data here, not an incident: the coach needs to
        # be told it will not open so they can delete it.
        return jsonify({"error": str(e), "pages": 0, "bytes": src_bytes}), 200
    return jsonify({"pages": doc.page_count, "bytes": src_bytes,
                    "name": src_name,
                    "converted": render_path != path})


@app.route("/api/archive/pdf/page/<int:pno>.png")
@coach_or_volunteer_required
def api_archive_pdf_page(pno: int):
    """One page as PNG, through the same two caches the event viewer uses."""
    rel = request.args.get("path", "") or ""
    path = _archive_pdf_or_404(rel)
    try:
        # For a .doc/.docx this is the cached conversion, so the render
        # cache below keys off the derived PDF — correct, since that is what
        # is actually being rasterised, and its identity already folds in the
        # source's mtime/size via the conversion cache key.
        path = _archive_renderable_pdf(path)
    except doc_convert.DocConvertError:
        abort(415, "this document could not be converted for preview")
    try:
        dpi = max(10, min(300, int(request.args.get("dpi", "110"))))
    except ValueError:
        dpi = 110

    scope = ARCHIVE_RENDER_SCOPE
    key = _render_cache_key(path, pno, dpi, scope)
    etag = f'"{key}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag,
                                             "Cache-Control": "private, no-cache"})
    png = _render_cache_read(key, scope)
    if png is None:
        try:
            doc = pdf_safety.open_pdf_safely(path)
        except pdf_safety.UnsafePdfError:
            abort(415, "this PDF could not be opened")
        if pno < 1 or pno > doc.page_count:
            abort(404)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        png = doc[pno - 1].get_pixmap(matrix=mat, colorspace=fitz.csRGB).tobytes("png")
        _render_cache_write(key, png, scope)
    return Response(png, mimetype="image/png",
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"})


@app.route("/api/archive/tournament-names")
@coach_required
def api_archive_tournament_names():
    """Type-ahead for renaming a tournament folder.

    Standardisation is the point: showing the spellings already in use
    steers towards an existing one instead of inventing a third name for the
    same tournament."""
    return jsonify({"names": archive_ops.tournament_names(
        request.args.get("q", "") or "", limit=25)})


# --- Bulk duplicate removal ------------------------------------------------

@app.route("/api/archive/duplicates/preview", methods=["POST"])
@coach_required
def api_archive_dedupe_preview():
    """Which copy survives in each group, and which go. Touches nothing."""
    data = request.get_json() or {}
    ids = None if data.get("all") else data.get("ids")
    scope = (data.get("path") or "").strip()
    groups = (tournament_archive.groups_under(scope)
              if ids is None else tournament_archive.groups_by_hash(ids))
    if ids is not None and scope:
        prefix = scope.rstrip("/") + "/"
        groups = [{**gr, "paths": [p for p in gr["paths"] if p.startswith(prefix)]}
                  for gr in groups]
        groups = [gr for gr in groups if len(gr["paths"]) > 1]
    return jsonify({"ok": True, "plan": tournament_archive.plan_dedupe(groups)})


@app.route("/api/archive/duplicates/remove", methods=["POST"])
@coach_required
def api_archive_dedupe_remove():
    """Delete every copy but one in each named group.

    Ids, never paths: the server re-derives what to remove from its own
    index, so a client cannot ask for every copy of something to go."""
    data = request.get_json() or {}
    ids = data.get("ids") or []
    every = bool(data.get("all"))
    scope = (data.get("path") or "").strip()
    if not every and (not isinstance(ids, list) or not ids):
        return jsonify({"error": "select at least one set of duplicates"}), 400
    # Logged before the attempt, not after. A sweep that matches nothing
    # writes no ops-log entry (nothing is moved), so without this there is no
    # record that it was ever asked for — which is exactly the state a live
    # report of "nothing happened" left us in.
    app.logger.info("archive dedupe: user=%s every=%s scope=%r ids=%d first=%r",
                    g.user.username, every, scope, len(ids),
                    (ids[0] if ids else None))
    try:
        result = archive_ops.remove_duplicates(
            ids, scope=scope, by=g.user.username, every=every)
    except archive_ops.ArchiveOpError as e:
        app.logger.warning("archive dedupe refused: %s", e)
        return jsonify({"error": str(e), "requested": len(ids)}), 400
    app.logger.info("archive dedupe: matched=%d removed=%d failed=%d",
                    result.get("matched", 0), result["count"],
                    len(result.get("failed") or []))
    return jsonify({"ok": True, "result": {**result, "requested": len(ids)}})


@app.route("/api/archive/duplicates/in-folder")
@coach_required
def api_archive_duplicates_in_folder():
    """Duplicate groups with at least two copies inside one folder.

    Copies outside it are excluded from the group rather than the group being
    dropped: cleaning up "this folder" must not reach out and delete a file
    somewhere the coach is not looking."""
    rel = request.args.get("path", "") or ""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        limit, offset = 100, 0
    all_groups = tournament_archive.groups_under(rel)
    return jsonify({"total": len(all_groups), "offset": offset,
                    "groups": all_groups[offset:offset + limit]})


@app.route("/api/purge/<kind>/<path:ident>", methods=["GET"])
@coach_required
@hard_delete_required
def api_purge_preview(kind: str, ident: str):
    """What deleting this would actually take with it. Always fetched
    immediately before showing the confirmation, never cached — the
    numbers exist to be true at the moment someone agrees to them."""
    try:
        return jsonify(deletion.preview(kind, *ident.split("/")))
    except deletion.DeletionError as e:
        return jsonify({"error": str(e)}), 400
    except TypeError:
        return jsonify({"error": f"wrong identifier for kind {kind!r}"}), 400


@app.route("/api/purge/<kind>/<path:ident>", methods=["DELETE"])
@coach_required
@hard_delete_required
def api_purge(kind: str, ident: str):
    """Permanently delete. Unlike every other "delete" in this app, this
    one means it — see deletion.py's module docstring."""
    if kind == "user" and ident == g.user.username:
        return jsonify({"error": "cannot delete your own account"}), 400
    try:
        result = deletion.delete(kind, *ident.split("/"))
    except deletion.DeletionError as e:
        return jsonify({"error": str(e)}), 400
    except TypeError:
        return jsonify({"error": f"wrong identifier for kind {kind!r}"}), 400
    app.logger.warning("HARD DELETE %s %s by %s -> %s",
                       kind, ident, g.user.username, result)
    return jsonify({"ok": True, **result})


@app.route("/api/presence")
def api_presence():
    """Backs the header's "N people active" badge — a whole-instance
    number, deliberately not filtered to the caller's events the way
    api_jobs_active_count is. The point is "is this box busy right now",
    and load from an event you can't see counts against you just the same;
    a bare count leaks nothing about who or where."""
    return jsonify(presence.active_summary())


@app.route("/admin/jobs")
@coach_required
def admin_jobs_page():
    return render_template("admin_jobs.html")


@app.route("/api/cookies/status")
@coach_required
def api_cookies_status():
    """Backs the Anubis-cookie freshness badge on the Sources page (coach-only
    — it's an operational/server concern, not something a volunteer needs
    surfaced). Lets the coach scp a fresh `.scioly_cookies.json` from a
    Playwright-capable machine before a scioly.org download run silently
    starts failing mid-batch on a headless server that can't launch a
    browser itself — or use api_cookies_paste below instead of scp."""
    return jsonify(download_event.cookie_expiry_status() or {})


@app.route("/api/cookies/paste", methods=["POST"])
@coach_required
def api_cookies_paste():
    """Manual alternative to scp-ing a Playwright-exported cookie file: the
    coach pastes the raw `document.cookie` string from their own browser's
    devtools, copied right after manually clearing the Anubis challenge at
    scioly.org. A raw paste carries no real expiry, so we assign a synthetic
    one matching this app's documented real-world cookie lifetime (~7 days)
    — cookie_expiry_status() then ages it down like any other cookie."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("cookie_string") or "").strip()
    if not raw:
        return jsonify({"error": "Paste a cookie string first."}), 400
    cookies = []
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append({"name": name.strip(), "value": value.strip(),
                            "domain": "scioly.org", "path": "/",
                            "expires": time.time() + 7 * 86400})
    if not cookies:
        return jsonify({"error": "Couldn't find any name=value pairs in that string."}), 400
    download_event._save_cookies(cookies)
    return jsonify({"ok": True, "count": len(cookies)})


@app.route("/admin/jobs/api/list")
@coach_required
def admin_jobs_list():
    records = jobs.list_all_jobs()
    return jsonify({"jobs": [jobs.job_to_public_dict(r, g.user.username, True)
                             for r in records]})


@app.route("/admin/jobs/api/<event_slug>/<job_id>/cancel", methods=["POST"])
@coach_required
def admin_job_cancel(event_slug, job_id):
    if event_slug not in EVENTS:
        abort(404)
    _validate_job_id(job_id)
    try:
        record = jobs.request_cancel(event_slug, job_id, g.user.username, True)
    except jobs.JobNotFound:
        abort(404)
    except jobs.JobNotAuthorized:
        abort(403)  # unreachable — is_coach=True always authorizes, kept for symmetry
    return jsonify(jobs.job_to_public_dict(record, g.user.username, True))


@app.route("/event/<event_slug>/api/pdf/<pdfname>/delete-all-questions", methods=["POST"])
def api_delete_all_questions(event_slug, pdfname):
    """Bulk-delete every question from a PDF, recording deletions in
    annotations so they survive Reprocess. Used by the event index page."""
    _select_event(event_slug)
    with bqb._state_transaction() as state:
        qs = state.get("questions", {}).get(pdfname, []) or []
        n = len(qs)
        # Record each as an annotation delete (or strip from `added` if user-added)
        ann = state.setdefault("annotations", {}).setdefault(pdfname, {
            "field_overrides": {}, "added": [], "deleted": [],
            "image_overrides": {"assignments": {}, "detached": []},
            "regions": [], "validations": {},
        })
        ann.setdefault("added", [])
        ann.setdefault("deleted", [])
        added_nums = {a.get("number") for a in ann["added"]}
        for q in qs:
            num = q.get("number")
            if not num:
                continue
            if num in added_nums:
                ann["added"] = [a for a in ann["added"] if a.get("number") != num]
            elif num not in ann["deleted"]:
                ann["deleted"].append(num)
        state.setdefault("questions", {})[pdfname] = []
    return jsonify({"ok": True, "deleted": n})


@app.route("/event/<event_slug>/api/upload-test-pdf", methods=["POST"])
def api_upload_test_pdf(event_slug):
    """Upload a test PDF (+ optional answer key) directly into this event,
    instead of relying on the scioly.org scrape. Saves into the event's
    base dir (NOT texts_dir, which is for LLM-generation source material)
    using the same `{filename_prefix}_*_test.pdf` / `_key.pdf` naming
    `_list_test_pdfs()`/`_key_path()` already discover PDFs by, then
    immediately processes it so the upload is usable right away instead of
    needing a separate manual Reprocess click."""
    from werkzeug.utils import secure_filename
    _select_event(event_slug)
    ALLOWED_EXTS = (".pdf", ".docx", ".doc")
    if "test_file" not in request.files:
        return jsonify({"error": "no test PDF provided"}), 400
    test_f = request.files["test_file"]
    test_raw = (test_f.filename or "").strip()
    test_ext = Path(test_raw).suffix.lower()
    if not test_raw or test_ext not in ALLOWED_EXTS:
        return jsonify({"error": "test file must be a PDF, .docx, or .doc"}), 400

    base_dir = bqb.BASE_DIR
    prefix = bqb.EVENT.filename_prefix
    base_dir.mkdir(parents=True, exist_ok=True)

    def normalized_name(raw: str, suffix: str, ext: str) -> str:
        stem = secure_filename(Path(raw).stem) or "upload"
        if stem.lower().endswith(f"_{suffix}"):
            stem = stem[: -(len(suffix) + 1)]
        base = stem if stem.lower().startswith(prefix.lower()) else f"{prefix}_{stem}"
        name = f"{base}_{suffix}{ext}"
        n = 1
        while (base_dir / name).exists():
            name = f"{base}_{n}_{suffix}{ext}"
            n += 1
        return name

    def _validate_saved(dest: Path, ext: str, label: str) -> str | None:
        """Magic-byte check matching `ext` — returns an error string, or
        None if the file looks legitimate."""
        ok = (pdf_safety.looks_like_pdf(dest) if ext == ".pdf"
              else doc_convert.looks_like_docx(dest) if ext == ".docx"
              else doc_convert.looks_like_doc(dest))
        if not ok:
            dest.unlink(missing_ok=True)
            return f"{label} isn't a valid {ext} file (bad header)"
        return None

    test_name = normalized_name(test_raw, "test", test_ext)
    test_dest = base_dir / test_name
    test_f.save(str(test_dest))
    err = _validate_saved(test_dest, test_ext, "test file")
    if err:
        return jsonify({"error": err}), 400

    key_dest = None
    key_ext = None
    key_f = request.files.get("key_file")
    if key_f and (key_f.filename or "").strip():
        key_ext = Path(key_f.filename).suffix.lower()
        if key_ext not in ALLOWED_EXTS:
            return jsonify({"error": "answer key must be a PDF, .docx, or .doc"}), 400
        # Share the test file's exact (already de-duped) base so the
        # existing _key_path() string-replace lookup finds it automatically.
        key_dest = base_dir / test_name.replace(f"_test{test_ext}", f"_key{key_ext}")
        key_f.save(str(key_dest))
        err = _validate_saved(key_dest, key_ext, "answer key")
        if err:
            return jsonify({"error": err}), 400

    # Optional third document — figures/diagrams referenced by the test but
    # living in a separate file (the same situation _supplementary_docs()
    # already discovers for files dropped in some other way). Never fed to
    # process_pair(): it's pure storage/browsing material, picked up
    # automatically by _supplementary_docs()'s glob on the next extract-page
    # load purely from sharing the test's stem prefix — no further code
    # needed once it's saved under that naming convention.
    supplementary_name = None
    sup_f = request.files.get("supplementary_file")
    if sup_f and (sup_f.filename or "").strip():
        sup_ext = Path(sup_f.filename).suffix.lower()
        if sup_ext not in ALLOWED_EXTS:
            return jsonify({"error": "figures file must be a PDF, .docx, or .doc"}), 400
        # Share the test file's exact stem prefix (like key_dest above) —
        # that shared prefix is the ONLY thing _supplementary_docs() keys
        # off of to discover this file later.
        supplementary_name = test_name.replace(f"_test{test_ext}", f"_figures{sup_ext}")
        n = 1
        while (base_dir / supplementary_name).exists():
            supplementary_name = test_name.replace(f"_test{test_ext}", f"_figures_{n}{sup_ext}")
            n += 1
        sup_dest = base_dir / supplementary_name
        sup_f.save(str(sup_dest))
        err = _validate_saved(sup_dest, sup_ext, "figures file")
        if err:
            return jsonify({"error": err}), 400
    else:
        sup_dest = None
        sup_ext = None

    # If the upload is a Word doc, it gets converted to a real PDF inside
    # the job below (so a slow LibreOffice conversion doesn't block this
    # request) — but the frontend already expects the *final* pdf_name in
    # this immediate response (it titles the progress bar and the eventual
    # success message with it), so compute that name now since
    # doc_convert.convert_to_pdf()'s output naming (`stem + ".pdf"`) is
    # deterministic and doesn't depend on the conversion having happened yet.
    final_test_name = test_name if test_ext == ".pdf" else f"{Path(test_name).stem}.pdf"
    final_supplementary_name = (
        None if supplementary_name is None
        else supplementary_name if sup_ext == ".pdf"
        else f"{Path(supplementary_name).stem}.pdf"
    )

    # Captured here, in the request thread, while _request_llm_keys() can
    # still read the X-LLM-Keys header — the job below runs on jobs.py's
    # worker thread, which has no request context of its own. submit_job()
    # carries this through its own in-memory side map (never persisted, never
    # logged — see jobs.py) and binds it into build_question_bank's vision
    # ContextVar for the life of the job, so process_pair()'s vision calls
    # below use this user's key instead of falling back to the server's.
    vision_key = _request_llm_keys().get("anthropic")

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        pdf_test = test_dest
        if test_ext != ".pdf":
            on_progress(phase="converting test document to PDF")
            pdf_test = doc_convert.convert_to_pdf(test_dest, test_dest.parent)
        pdf_key = key_dest
        if key_dest is not None and key_ext != ".pdf":
            on_progress(phase="converting answer key to PDF")
            pdf_key = doc_convert.convert_to_pdf(key_dest, key_dest.parent)
        # The figures doc is never extracted (process_pair never sees it) —
        # only converted to PDF if needed, so _supplementary_docs()'s glob
        # (which only matches `.pdf`) can find it on the next page load.
        if sup_dest is not None and sup_ext != ".pdf":
            on_progress(phase="converting figures document to PDF")
            doc_convert.convert_to_pdf(sup_dest, sup_dest.parent)
        job_state = bqb._load_state()
        qs = process_pair(pdf_test, pdf_key, job_state, _vision_available(),
                          should_cancel=should_cancel, on_progress=on_progress)
        _compute_pages(pdf_test.name, qs)
        job_state.setdefault("questions", {})[pdf_test.name] = qs
        bqb._save_state(job_state)
        return {"pdf_name": pdf_test.name, "n_questions": len(qs),
                "has_key": pdf_key is not None,
                "supplementary_name": final_supplementary_name}

    try:
        job_id = jobs.submit_job(event_slug, "upload_extract", f"Extract {final_test_name}",
                                 g.user.username, _target, vision_key=vision_key)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "pdf_name": final_test_name, "has_key": key_dest is not None,
                    "supplementary_name": final_supplementary_name, "job_id": job_id})


@app.route("/event/<event_slug>/api/doc/<docname>/convert", methods=["POST"])
def api_convert_doc(event_slug, docname):
    """Job-queued conversion of a discovered .docx/.doc test file (see
    _pending_doc_conversions, surfaced by the scan page) into a real PDF —
    after this, it's an ordinary `_test.pdf` to every other route, no
    different from a test scioly.org served as a PDF to begin with."""
    _select_event(event_slug)
    src = _safe_join(bqb.BASE_DIR, docname)
    if src.suffix.lower() not in (".docx", ".doc") or not src.exists():
        abort(404, "no such pending document")

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        pdf = doc_convert.convert_to_pdf(src, src.parent)
        return {"pdf_name": pdf.name}

    try:
        job_id = jobs.submit_job(event_slug, "doc_convert", f"Convert {docname}",
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/scan")
def event_scan_page(event_slug):
    """Manual file-drop onboarding page — see _scan_event_files(). Deliberately
    on-demand (a "Scan now" button on the page, not a periodic background
    check), matching this codebase's existing manual-trigger stance for
    infrequent, operator-initiated actions (the GitHub-update mechanism is
    explicitly manual for the same reason — see spec.md)."""
    _select_event(event_slug)
    return render_template("event_scan.html", event_slug=event_slug,
                           event_name=bqb.EVENT.name)


@app.route("/event/<event_slug>/api/scan")
def api_scan(event_slug):
    _select_event(event_slug)
    return jsonify(_scan_event_files())


@app.route("/event/<event_slug>/api/scan/rename", methods=["POST"])
def api_scan_rename(event_slug):
    """Bring a manually-dropped, non-conforming file under this event's
    naming convention via a plain filesystem rename (no content change) —
    after which it's indistinguishable from anything scioly.org-sourced and
    is picked up by every existing discovery path (this module's
    _list_test_pdfs/_key_path/_supplementary_docs, build_question_bank's
    CLI) with no further code involved.

    `role="notes"` is different in kind, not just naming: a notes file is
    source *material* for the Generate page (the same category as anything
    already uploaded to <event>/texts/), not a document tied to one test —
    so it's *moved* into texts_dir instead of renamed in place under the
    test/key/supplementary convention. See `_supplementary_docs()`'s
    docstring for why supplementary stays distinct from this."""
    from werkzeug.utils import secure_filename
    _select_event(event_slug)
    data = request.get_json() or {}
    role = (data.get("role") or "").strip().lower()
    if role not in ("test", "key", "supplementary", "notes"):
        return jsonify({"error": "role must be test, key, supplementary, or notes"}), 400
    src = _safe_join(bqb.BASE_DIR, data.get("filename") or "")
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    ext = src.suffix.lower()

    if role == "notes":
        if ext not in (".pdf", ".docx", ".doc", ".md", ".txt"):
            return jsonify({"error": "notes must be a PDF, .docx, .doc, .md, or .txt"}), 400
        texts_dir = bqb.EVENT.texts_dir
        texts_dir.mkdir(parents=True, exist_ok=True)
        dest_name = secure_filename(src.name) or f"notes{ext}"
        dest = texts_dir / dest_name
        n = 1
        while dest.exists():
            dest = texts_dir / f"{Path(dest_name).stem}_{n}{Path(dest_name).suffix}"
            n += 1
        src.rename(dest)
        if dest.suffix.lower() in (".docx", ".doc"):
            # Converted in place, same directory — becomes an ordinary
            # uploaded-source PDF from here on, needing the same one-click
            # "Process → MD" step any other source PDF needs. A conversion
            # failure here doesn't undo the move — the original (now in
            # texts_dir) is still a valid source.
            try:
                doc_convert.convert_to_pdf(dest, dest.parent)
            except doc_convert.DocConvertError as e:
                return jsonify({"ok": True, "new_filename": dest.name, "moved_to": "texts",
                                "warning": str(e)})
        return jsonify({"ok": True, "new_filename": dest.name, "moved_to": "texts"})

    if ext not in (".pdf", ".docx", ".doc"):
        return jsonify({"error": "only .pdf/.docx/.doc files can be onboarded here"}), 400

    if role == "supplementary":
        # Attach to an existing test by sharing its exact stem prefix — that's
        # the only thing _supplementary_docs() actually keys off of, so this
        # is more reliable than asking the user to retype year/division/submitter.
        test_pdf = _safe_join(bqb.BASE_DIR, data.get("attach_to") or "")
        if not test_pdf.name.endswith("_test.pdf") or not test_pdf.exists():
            return jsonify({"error": "pick a valid existing test to attach this to"}), 400
        label = secure_filename((data.get("label") or "").strip()) or "sheet"
        stem_prefix = test_pdf.name[: -len("_test.pdf")]
        new_name = f"{stem_prefix}_{label}{ext}"
    else:
        prefix = bqb.EVENT.filename_prefix
        year = secure_filename((data.get("year") or "").strip()) or "unk"
        division = secure_filename((data.get("division") or "").strip()).lower() or "x"
        submitter = secure_filename((data.get("submitter") or "").strip()).lower() or "unknown"
        new_name = f"{prefix}_{year}_{division}_{submitter}_{role}{ext}"

    dest = bqb.BASE_DIR / new_name
    if dest.exists():
        return jsonify({"error": f"{new_name} already exists"}), 409
    src.rename(dest)
    return jsonify({"ok": True, "new_filename": new_name})


@app.route("/event/<event_slug>/api/scan/process-all", methods=["POST"])
def api_scan_process_all(event_slug):
    """Bulk version of the per-PDF reprocess job, for files the scan found
    already conforming but never processed — e.g. a batch scp'd straight
    into the event directory. One job per file, same job-queue pattern as
    upload/reprocess, so progress/cancellation work identically."""
    _select_event(event_slug)
    scan = _scan_event_files()
    # See the upload_extract route above: captured now (request context),
    # threaded through submit_job()'s vision_key kwarg for each queued job.
    vision_key = _request_llm_keys().get("anthropic")

    def _make_target(pdfname: str, test_pdf: Path):
        def _target(should_cancel, on_progress):
            _job_target_setup(event_slug)
            job_state = bqb._load_state()
            qs = process_pair(test_pdf, _key_path(test_pdf), job_state, _vision_available(),
                              should_cancel=should_cancel, on_progress=on_progress)
            _compute_pages(pdfname, qs)
            job_state.setdefault("questions", {})[pdfname] = qs
            bqb._save_state(job_state)
            return {"n_questions": len(qs)}
        return _target

    job_ids = []
    for entry in scan["ready"]:
        pdfname = entry["filename"]
        try:
            job_id = jobs.submit_job(event_slug, "scan_process", f"Process {pdfname}",
                                     g.user.username,
                                     _make_target(pdfname, bqb.BASE_DIR / pdfname),
                                     vision_key=vision_key)
            job_ids.append(job_id)
        except jobs.JobQueueFull:
            break
    return jsonify({"ok": True, "job_ids": job_ids})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/reprocess", methods=["POST"])
def api_reprocess(event_slug, pdfname):
    """The snapshot-before-wipe step and the destructive state pops below
    are fast (no PDF parsing, no LLM calls) and stay synchronous — only the
    slow part (process_pair's text/vision extraction) becomes a queued job.
    manual_mode short-circuits before ever touching the job queue, exactly
    as it short-circuited before any extraction call previously."""
    _select_event(event_slug)
    data = request.get_json(silent=True) or {}
    manual_mode = bool(data.get("manual_mode"))
    test_pdf = bqb.BASE_DIR / pdfname
    if not test_pdf.exists():
        abort(404)
    # The synchronous wipe (snapshot + pop questions/vision/annotations) and
    # the synchronous job-target closure below each load/save state
    # separately — they're necessarily two separate transactions (the wipe
    # must be visible before the job queues), not one. That's fine: each
    # half is internally atomic, and they're sequenced by this request
    # (wipe persists before the job is even submitted), not concurrent with
    # each other. The job closure's own load/save (out of this fix's scope —
    # see _state_transaction()'s docstring) is unaffected.
    with bqb._state_transaction() as state:
        if data.get("discard_annotations") and (
            state.get("annotations", {}).get(pdfname)
            or state.get("manual", {}).get(pdfname)
            or state.get("questions", {}).get(pdfname)
        ):
            # Snapshot before any of this destructive reprocess's data loss, so
            # it's always recoverable via the restore-snapshot route below — the
            # app never permanently discards a PDF's accumulated edits.
            archive.snapshot_pdf_state(bqb.EVENT, pdfname, state)
        state.setdefault("questions", {}).pop(pdfname, None)
        state.setdefault("vision", {}).pop(pdfname, None)
        if data.get("discard_annotations"):
            state.setdefault("annotations", {}).pop(pdfname, None)
            state.setdefault("manual", {}).pop(pdfname, None)
        if manual_mode:
            # Manual mode: skip auto-extraction; user will rebuild via region capture.
            # Annotations have already been wiped above. Just persist an empty question list.
            state.setdefault("questions", {})[pdfname] = []
    if manual_mode:
        return jsonify({"ok": True, "n_questions": 0,
                        "discarded_annotations": True,
                        "manual_mode": True})

    # See the upload_extract route's identical comment: captured now (request
    # context), threaded through submit_job()'s vision_key kwarg.
    vision_key = _request_llm_keys().get("anthropic")

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        job_state = bqb._load_state()
        qs = process_pair(test_pdf, _key_path(test_pdf), job_state, _vision_available(),
                          should_cancel=should_cancel, on_progress=on_progress)
        _compute_pages(pdfname, qs)
        job_state.setdefault("questions", {})[pdfname] = qs
        bqb._save_state(job_state)
        return {"n_questions": len(qs),
                "discarded_annotations": bool(data.get("discard_annotations"))}

    try:
        job_id = jobs.submit_job(event_slug, "reprocess", f"Reprocess {pdfname}",
                                 g.user.username, _target, vision_key=vision_key)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/swap-test-key", methods=["POST"])
def api_swap_test_key(event_slug, pdfname):
    """Fixes an upload where the test and key roles got assigned backwards
    — swaps which physical file is named `_test.pdf` vs `_key.pdf` (the
    same on-disk-rename mechanism api_scan_rename already uses to assign a
    role, just trading two existing files instead of renaming one fresh
    one). `pdfname`'s bucket in state is now stale the instant its bytes
    change underneath it — snapshotted (same pattern as api_reprocess's
    wipe path) then cleared; the caller is expected to Reprocess afterward.
    `pdf_meta` (Part 4's Tournament/Year override) is deliberately left
    alone: it describes the exam itself, not which file currently plays
    which role, and `pdfname` doesn't change across a swap."""
    _select_event(event_slug)
    test_pdf = bqb.BASE_DIR / pdfname
    if not test_pdf.exists():
        abort(404)
    key_pdf = _key_path(test_pdf)
    if key_pdf is None:
        return jsonify({"error": "No key PDF found to swap with."}), 400
    if test_pdf.suffix.lower() != ".pdf" or key_pdf.suffix.lower() != ".pdf":
        return jsonify({"error": "Both test and key must already be PDFs to swap "
                                  "(convert any .docx/.doc first)."}), 400
    tmp = test_pdf.with_suffix(test_pdf.suffix + ".swaptmp")
    try:
        test_pdf.rename(tmp)
        key_pdf.rename(test_pdf)
        tmp.rename(key_pdf)
    except OSError as e:
        return jsonify({"error": f"Swap failed: {e}"}), 500
    with bqb._state_transaction() as state:
        if (state.get("annotations", {}).get(pdfname)
                or state.get("manual", {}).get(pdfname)
                or state.get("questions", {}).get(pdfname)):
            archive.snapshot_pdf_state(bqb.EVENT, pdfname, state)
        state.setdefault("questions", {}).pop(pdfname, None)
        state.setdefault("vision", {}).pop(pdfname, None)
        state.setdefault("annotations", {}).pop(pdfname, None)
        state.setdefault("manual", {}).pop(pdfname, None)
    return jsonify({"ok": True})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/meta", methods=["PATCH"])
def api_set_pdf_meta(event_slug, pdfname):
    """Save (or clear, if blanked) this PDF's Tournament name/Year override
    and cascade it onto every question already extracted from it — so the
    correction shows up immediately, not just on a future Reprocess (which
    also durably picks it up, via process_pair's _effective_pdf_meta call).
    Division is left exactly as each question already has it; only
    `year`/`source` are rewritten here."""
    _select_event(event_slug)
    data = request.get_json(silent=True) or {}
    tournament = (data.get("tournament") or "").strip()
    year = (data.get("year") or "").strip()
    with bqb._state_transaction() as state:
        if tournament or year:
            entry = state.setdefault("pdf_meta", {}).setdefault(pdfname, {})
            if tournament:
                entry["tournament"] = tournament
            else:
                entry.pop("tournament", None)
            if year:
                entry["year"] = year
            else:
                entry.pop("year", None)
            if not entry:
                state["pdf_meta"].pop(pdfname, None)
        else:
            state.setdefault("pdf_meta", {}).pop(pdfname, None)
        meta = bqb._effective_pdf_meta(bqb.BASE_DIR / pdfname, state)
        for q in state.get("questions", {}).get(pdfname, []) or []:
            q["year"] = meta["year"]
            q["source"] = f"{meta['year']} Div-{q.get('division', meta['division'])}: {meta['tournament']}"
    return jsonify({"ok": True, "tournament": meta["tournament"], "year": meta["year"]})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/snapshots")
def api_list_snapshots(event_slug, pdfname):
    _select_event(event_slug)
    return jsonify({"snapshots": archive.list_snapshots(bqb.EVENT, pdfname)})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/restore-snapshot", methods=["POST"])
def api_restore_snapshot(event_slug, pdfname):
    """Bring back a PDF's annotations/manual/questions from a pre-wipe
    snapshot (see api_reprocess above). Same access gate as every other PDF
    route — restoring is protective, not destructive, so no extra
    restriction beyond normal event access."""
    _select_event(event_slug)
    data = request.get_json(silent=True) or {}
    filename = data.get("snapshot") or ""
    try:
        snap = archive.load_snapshot(bqb.EVENT, pdfname, filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "snapshot not found"}), 404
    with bqb._state_transaction() as state:
        if snap.get("annotations") is not None:
            state.setdefault("annotations", {})[pdfname] = snap["annotations"]
        if snap.get("manual") is not None:
            state.setdefault("manual", {})[pdfname] = snap["manual"]
        if snap.get("questions") is not None:
            state.setdefault("questions", {})[pdfname] = snap["questions"]
    return jsonify({"ok": True, "restored_from": filename})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/ocr", methods=["POST"])
@_with_vision_key
def api_ocr(event_slug, pdfname, pno):
    _select_event(event_slug)
    if not _vision_available():
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 400
    doc = _open_pdf(pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    items = vision_extract_text_page(doc[pno - 1])
    suggestions = []
    for it in items:
        if isinstance(it, dict) and it.get("number"):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            stem, choices = split_choices(text)
            suggestions.append({
                "number":  str(it["number"]),
                "text":    stem,
                "choices": choices,
                "topic":   classify_topic(text),
                "answer":  "",
                "images":  [],
                "page":    pno,
            })
    return jsonify({"suggestions": suggestions})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/images")
def api_images(event_slug, pdfname):
    _select_event(event_slug)
    state = bqb._load_state()
    qs = state.get("questions", {}).get(pdfname, [])
    used: dict[str, list[str]] = {}
    for q in qs:
        for fn in q.get("images") or []:
            used.setdefault(fn, []).append(q.get("number", ""))
    src_prefix = pdfname.replace("_test.pdf", "").replace(".pdf", "")
    # Both sides get hyphens folded to underscores. The pipeline's own
    # extracted images are already fully underscored (they go through
    # bqb._slug), but images added from the extract page keep the source
    # PDF's punctuation verbatim — _slug_image_name embeds the bucket name,
    # so a manual pick off `..._ssss-avdestroyer_test.pdf` lands on disk as
    # `..._ssss-avdestroyer_test_q5_pick_<hash>.png`. Normalising only the
    # needle meant those never matched, so every manually picked, uploaded
    # or generated image was missing from the bay — and therefore couldn't
    # be re-used on another question, which is the whole point of the bay.
    needle = src_prefix.lower().replace("-", "_")
    all_imgs: list[str] = []
    for img in bqb.IMAGE_DIR.iterdir():
        if needle in img.name.lower().replace("-", "_"):
            all_imgs.append(img.name)
    all_imgs.sort()
    return jsonify({"images": all_imgs, "used_by": used})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/extract-region",
           methods=["POST"])
def api_extract_region(event_slug, pdfname, pno):
    _select_event(event_slug)
    data = request.get_json() or {}
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 4 or h < 4:
        return jsonify({"error": "region too small"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    raw = page.get_text("text", clip=rect) or ""
    text = " ".join(raw.split())
    text = _strip_points(text)
    result = {"text": text}
    if data.get("parse_choices"):
        stem, choices = split_choices(text)
        if not choices:
            # Fallback: try splitting on the original line structure (PyMuPDF
            # preserves newlines per choice in most multi-line layouts)
            stem2, choices2 = split_choices_by_lines(raw)
            if choices2:
                stem = stem2 or stem
                choices = choices2
        result["stem"] = _strip_points(stem)
        result["choices"] = [{"letter": c["letter"], "text": _strip_points(c["text"])}
                             for c in choices]
    return jsonify(result)


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/extract-region-vision",
           methods=["POST"])
@_with_vision_key
def api_extract_region_vision(event_slug, pdfname, pno):
    """Haiku-vision fallback: extracts a region's text + choices via the LLM.
    Used when pure-Python region capture struggles with complex layouts."""
    _select_event(event_slug)
    if not _vision_available():
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 400
    data = request.get_json() or {}
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 8 or h < 8:
        return jsonify({"error": "region too small"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    b64 = region_image_b64(page, rect, dpi=200)
    result = vision_extract_region(b64)
    # Map to the same shape as /extract-region for the JS to consume uniformly
    return jsonify({
        "text":    result.get("stem", ""),
        "stem":    result.get("stem", ""),
        "choices": result.get("choices", []),
        "answer":  result.get("answer"),
        "via":     "haiku",
        "error":   result.get("error"),
    })


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/extract-region-column",
           methods=["POST"])
def api_extract_region_column(event_slug, pdfname, pno):
    """One side of the manual matching-question capture flow: crop a
    drag-selected column (left or right) and split it into labeled items.
    Sibling to api_extract_region — same fitz.Rect/_open_target_pdf
    plumbing, only the post-extraction parsing differs (split_column_items
    instead of split_choices, no item-count ceiling)."""
    _select_event(event_slug)
    data = request.get_json() or {}
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 4 or h < 4:
        return jsonify({"error": "region too small"}), 400
    label_charset = data.get("label_charset")
    if label_charset not in ("numeric", "alpha"):
        return jsonify({"error": "label_charset must be 'numeric' or 'alpha'"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    raw = page.get_text("text", clip=rect) or ""
    items = split_column_items(raw, label_charset)
    return jsonify({"items": items})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/extract-region-column-vision",
           methods=["POST"])
@_with_vision_key
def api_extract_region_column_vision(event_slug, pdfname, pno):
    """Haiku-vision fallback for one column of a matching-question capture,
    analogous to api_extract_region_vision."""
    _select_event(event_slug)
    if not _vision_available():
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 400
    data = request.get_json() or {}
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 8 or h < 8:
        return jsonify({"error": "region too small"}), 400
    label_charset = data.get("label_charset")
    if label_charset not in ("numeric", "alpha"):
        return jsonify({"error": "label_charset must be 'numeric' or 'alpha'"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    b64 = region_image_b64(page, rect, dpi=200)
    result = vision_extract_column(b64, label_charset)
    return jsonify({"items": result.get("items", []), "via": "haiku", "error": result.get("error")})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/extract-math",
           methods=["POST"])
@_with_vision_key
def api_extract_math(event_slug, pdfname, pno):
    _select_event(event_slug)
    if not _vision_available():
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 400
    data = request.get_json() or {}
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 8 or h < 8:
        return jsonify({"error": "region too small"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    b64 = region_image_b64(page, rect, dpi=240)
    latex = vision_to_latex(b64)
    if not latex:
        return jsonify({"latex": "", "delimited": "", "error": "no math detected"})
    return jsonify({"latex": latex, "delimited": f"${latex}$"})


@app.route("/event/<event_slug>/api/validate-question", methods=["POST"])
def api_validate_question(event_slug):
    _select_event(event_slug)
    keys = _request_llm_keys()
    if not llm_providers.available_providers(keys):
        return jsonify({"status": "unavailable",
                        "rationale": "No LLM API key configured. Add one in Settings."}), 200
    data = request.get_json() or {}
    q = {
        "text":    data.get("text") or "",
        "answer":  data.get("answer") or "",
        "choices": data.get("choices") or [],
        "number":  data.get("number") or "",
    }
    return jsonify(validate_answer(q, keys=keys))


@app.route("/event/<event_slug>/api/regenerate", methods=["POST"])
def api_regenerate(event_slug):
    _select_event(event_slug)
    state = bqb._load_state()
    all_q: list[dict] = []
    for bucket, qs in state.get("questions", {}).items():
        for q in qs:
            qcopy = dict(q)
            qcopy["_bucket"] = bucket
            all_q.append(qcopy)
    bqb.OUT_MD.write_text(bqb.build_markdown(all_q), encoding="utf-8")
    return jsonify({"ok": True, "path": str(bqb.OUT_MD),
                    "n_questions": len(all_q)})


@app.route("/event/<event_slug>/images/<fname>")
def serve_image(event_slug, fname):
    _select_event(event_slug)
    p = _safe_join(bqb.IMAGE_DIR, fname)
    if not p.exists():
        abort(404)
    return send_file(str(p))


# ---------------------------------------------------------------------------
# Routes — Browse all questions for an event
# ---------------------------------------------------------------------------

@app.route("/event/<event_slug>/browse")
def browse_page(event_slug):
    _select_event(event_slug)
    return render_template("browse.html",
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name)


@app.route("/event/<event_slug>/quiz")
def quiz_page(event_slug):
    _select_event(event_slug)
    return render_template("quiz.html",
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name)


@app.route("/event/<event_slug>/api/all-questions")
def api_all_questions(event_slug):
    """Flat list of every question in the bank, with bucket provenance and
    stats baskets pre-computed so the client can render the toolbar
    without a second scan."""
    _select_event(event_slug)
    state = bqb._load_state()
    qs_by_pdf = state.get("questions", {})
    manual = state.get("manual", {})
    all_qs: list[dict] = []

    # Shared context blocks (case-study passages/tables/diagrams) live per-bucket
    # under annotations[bucket].contexts and are only unique within their own
    # bucket — namespace the key as "bucket::id" so quiz.html/browse.html can
    # look one up across the whole event without collisions.
    contexts: dict[str, dict] = {}
    for bucket, ann in state.get("annotations", {}).items():
        for c in (ann.get("contexts") or []):
            cid = c.get("id")
            if cid:
                contexts[f"{bucket}::{cid}"] = c

    for bucket, qs in qs_by_pdf.items():
        # "Recently edited" works off the per-bucket edited_at timestamp.
        bucket_edited_at = (manual.get(bucket) or {}).get("edited_at", "")
        for q in qs:
            qcopy = dict(q)
            qcopy["_bucket"] = bucket
            qcopy["_synthetic_bucket"] = bucket.startswith("_")  # generated/scraped
            qcopy["_has_image"] = bool(qcopy.get("images"))
            qcopy["_is_mcq"]      = bool(qcopy.get("choices"))
            qcopy["_is_matching"] = qcopy.get("qtype") == "matching"
            qcopy["_is_tf"]       = qcopy.get("qtype") == "tf"
            qcopy["_edited_at"] = bucket_edited_at
            v = qcopy.get("validation") or {}
            qcopy["_validation_status"] = v.get("status") if v else None
            ctx_id = qcopy.get("context_id")
            if ctx_id:
                qcopy["_context_key"] = f"{bucket}::{ctx_id}"
            all_qs.append(qcopy)

    # Stat baskets
    by_topic, by_focus, by_source, by_validation = {}, {}, {}, {}
    by_bucket: dict[str, int] = {}
    for q in all_qs:
        t = q.get("topic") or "Other / General"
        by_topic[t] = by_topic.get(t, 0) + 1
        f = q.get("focus") or ""
        if f:
            by_focus[f] = by_focus.get(f, 0) + 1
        s = q.get("source") or "(no source)"
        by_source[s] = by_source.get(s, 0) + 1
        vs = q.get("_validation_status") or "unvalidated"
        by_validation[vs] = by_validation.get(vs, 0) + 1
        by_bucket[q["_bucket"]] = by_bucket.get(q["_bucket"], 0) + 1

    return jsonify({
        "questions":   all_qs,
        "contexts":    contexts,
        "topics":      bqb.TOPICS,
        "foci":        list(bqb.EVENT.foci),
        "event_name":  bqb.EVENT.name,
        "stats": {
            "total":         len(all_qs),
            "by_topic":      by_topic,
            "by_focus":      by_focus,
            "by_source":     by_source,
            "by_validation": by_validation,
            "by_bucket":     by_bucket,
        },
    })


_VALIDATION_STATUSES = {"correct", "incorrect", "uncertain", "unavailable"}


def _apply_question_field_edits(q: dict, data: dict) -> list[str]:
    """Mutates `q` in place per a PATCH body's field edits (text/topic/focus/
    answer/choices/matching/qtype/image_descriptions), returns the list of
    field names touched. Factored out of api_patch_question so the same
    exact field-resolution rules can be run twice for a status="correct"
    request: once on a scratch copy (to decide gradeability against the
    record as this request would leave it, before anything is persisted),
    and once for real inside the transaction. Never touches `validation`
    itself — that's applied by the caller, after this returns."""
    edited_fields: list[str] = []
    for k in ("text", "topic", "focus", "answer"):
        if k in data:
            q[k] = (data[k] or "").strip()
            edited_fields.append(k)
    if "choices" in data and isinstance(data["choices"], list):
        q["choices"] = [{"letter": (c.get("letter") or "").upper()[:1],
                         "text": (c.get("text") or "").strip()}
                        for c in data["choices"]
                        if (c.get("text") or "").strip()]
        for i, c in enumerate(q["choices"]):
            c["letter"] = chr(ord("A") + i)
        edited_fields.append("choices")
    if "matching" in data:
        m = data["matching"]
        if m is None:
            q.pop("matching", None)
            q.pop("qtype", None)
        elif isinstance(m, dict):
            q["matching"] = {
                "left":  [{"label": str(it.get("label") or ""),
                           "text": (it.get("text") or "").strip(),
                           "image": it.get("image") or None}
                          for it in (m.get("left") or [])],
                "right": [{"label": str(it.get("label") or ""),
                           "text": (it.get("text") or "").strip(),
                           "image": it.get("image") or None}
                          for it in (m.get("right") or [])],
                "pairs": {str(k): str(v) for k, v in (m.get("pairs") or {}).items()},
            }
            q["qtype"] = "matching"
        edited_fields.append("matching")
        edited_fields.append("qtype")
    elif "qtype" in data:
        new_qtype = (data["qtype"] or "").strip() or None
        if new_qtype == "tf":
            q["qtype"] = "tf"
            q["choices"] = []  # tf storage is always choices: []
            edited_fields.append("choices")
            edited_fields.append("qtype")
        elif new_qtype is None:
            q.pop("qtype", None)
            edited_fields.append("qtype")
        elif new_qtype in _VALID_QTYPES:
            q["qtype"] = new_qtype
            edited_fields.append("qtype")
        # else: not a recognized qtype — ignored, not persisted (see
        # _VALID_QTYPES).
    if "image_descriptions" in data and isinstance(data["image_descriptions"], dict):
        cleaned = {str(fn): str(d).strip() for fn, d in data["image_descriptions"].items()
                   if str(d or "").strip()}
        q["image_descriptions"] = cleaned
        edited_fields.append("image_descriptions")
    return edited_fields


@app.route("/event/<event_slug>/api/q/<bucket>/<num>", methods=["PATCH"])
def api_patch_question(event_slug, bucket, num):
    """Apply a single-field edit to one question, without going through the
    extract page. Used by the browse-page inline editor."""
    _select_event(event_slug)
    data = request.get_json() or {}
    # Validated up front (before the transaction, not inside it) — a `with`
    # block's body returning early is a normal exit, not an exception, so
    # the transaction would still save whatever fields were already mutated
    # before this check if it lived inside the loop below. Pre-validating
    # keeps a bad request a true no-op, matching the original behaviour
    # where _save_state() was only ever called once, at the very end.
    if "validation" in data and isinstance(data["validation"], dict):
        status = data["validation"].get("status")
        if status is not None and status not in _VALIDATION_STATUSES:
            return jsonify({"error": f"invalid validation status: {status!r}"}), 400
        if status == "correct":
            # Gradeability gate (spec.md "Verification gate"): refuse to
            # certify a question a grader could never mark right. Must be
            # judged against the record as THIS request would leave it — an
            # answer/choices/qtype edit can ride along in the same PATCH body
            # as the verdict — so peek the current record, replay the same
            # field edits onto a scratch copy, and check that. Done before
            # the transaction opens for the same true-no-op reason as the
            # status-shape check above.
            peek_state = bqb._load_state()
            peek_qs = peek_state.get("questions", {}).get(bucket) or []
            peek_q = next((x for x in peek_qs if str(x.get("number")) == str(num)), None)
            if peek_q is None:
                return jsonify({"error": f"question #{num} not in {bucket}"}), 404
            preview = copy.deepcopy(peek_q)
            _apply_question_field_edits(preview, data)
            gradeable, reason = bqb.question_gradeability(preview)
            if not gradeable:
                return jsonify({"error": f"cannot mark correct: {reason}"}), 400
    with bqb._state_transaction() as state:
        bucket_qs = state.setdefault("questions", {}).get(bucket)
        if bucket_qs is None:
            return jsonify({"error": f"bucket not found: {bucket}"}), 404
        q = next((x for x in bucket_qs if str(x.get("number")) == str(num)), None)
        if not q:
            return jsonify({"error": f"question #{num} not in {bucket}"}), 404

        edited_fields = _apply_question_field_edits(q, data)
        if "validation" in data:
            v = data["validation"]
            if v is None:
                # "(unset)" in the manual validation dropdown — clear any
                # existing AI or human verdict outright.
                q.pop("validation", None)
            elif isinstance(v, dict):
                status = v.get("status")
                # Already validated above, before the transaction opened.
                # Either the AI path (validated_by="ai", set right after the
                # stateless /api/validate-question call) or a human's own
                # verdict (validated_by="human") — whichever happens most
                # recently simply overwrites this field, by design: a human can
                # always override a stale AI verdict, and re-running AI
                # Validate can override a human's.
                q["validation"] = {
                    "status": status,
                    "rationale": v.get("rationale") or "",
                    "validated_by": v.get("validated_by") or "human",
                    **({"source": v["source"]} if v.get("source") else {}),
                    **({"correct_answer": v["correct_answer"]} if v.get("correct_answer") else {}),
                }
            edited_fields.append("validation")

        if edited_fields:
            # Server-stamped, never client-supplied — g.user is the authenticated
            # session set by _require_login, so this can't be spoofed by the
            # request body the way a "lastEditedBy" field in `data` could be.
            q["lastEditedBy"] = g.user.username
            q["lastEditedDateTime"] = datetime.now().isoformat(timespec="seconds")
            edited_fields += ["lastEditedBy", "lastEditedDateTime"]

        # Record into the annotations payload so reprocess preserves the edit
        ann = state.setdefault("annotations", {}).setdefault(bucket, {})
        overrides = ann.setdefault("field_overrides", {})
        overrides[str(num)] = {**overrides.get(str(num), {}),
                               **{k: q.get(k) for k in edited_fields}}
        state.setdefault("manual", {})[bucket] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
    return jsonify({"ok": True, "question": q})


@app.route("/event/<event_slug>/api/q/<bucket>/<num>", methods=["DELETE"])
def api_delete_question(event_slug, bucket, num):
    _select_event(event_slug)
    with bqb._state_transaction() as state:
        bucket_qs = state.get("questions", {}).get(bucket)
        if not bucket_qs:
            return jsonify({"error": "bucket not found"}), 404
        before = len(bucket_qs)
        state["questions"][bucket] = [q for q in bucket_qs
                                       if str(q.get("number")) != str(num)]
        if len(state["questions"][bucket]) == before:
            return jsonify({"error": "question not found"}), 404
        # Persist annotation so reprocess respects the deletion
        ann = state.setdefault("annotations", {}).setdefault(bucket, {})
        deleted = set(ann.get("deleted") or [])
        deleted.add(str(num))
        ann["deleted"] = sorted(deleted)
        state.setdefault("manual", {})[bucket] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
    return jsonify({"ok": True, "removed": str(num)})


def _find_question(state: dict, bucket: str, num: str) -> tuple[dict | None, list]:
    """Return (question_dict, bucket_list) for the (bucket, num) pair, or
    (None, []) when missing.

    A bucket is a PDF filename, and some of them legitimately contain `+`
    (e.g. a scraped `..._ssss-utf-8u+6211u+662f_test.pdf`). The frontend
    sends it correctly percent-encoded as `%2B`, but a proxy in front of the
    app can decode the path twice — `%2B` -> `+` -> ` ` — applying the
    query-string rule that `+` means space to a path, where it doesn't. The
    request then arrives naming a bucket that has spaces where the real one
    has plus signs, and every lookup 404s with "question not found" for that
    one PDF while every other PDF works. Reproduced directly: the same
    request succeeds with `%2B` and with a literal `+`, and 404s only in the
    space-substituted form.

    Retrying space->`+` recovers that case. It can only ever turn a miss
    into a hit on an existing bucket, so it costs nothing when the path
    arrived intact, and it is deliberately one-directional — a bucket whose
    real name contains a space is still found by the exact match first.
    """
    buckets = state.get("questions", {})
    bucket_qs = buckets.get(bucket)
    if bucket_qs is None and " " in bucket:
        bucket_qs = buckets.get(bucket.replace(" ", "+"))
    bucket_qs = bucket_qs or []
    for q in bucket_qs:
        if str(q.get("number")) == str(num):
            return q, bucket_qs
    return None, bucket_qs


def _slug_image_name(bucket: str, num: str, ext: str, kind: str = "img") -> str:
    """Build a stable on-disk filename for a question-attached image.
    `bucket` strips off the trailing `.pdf` plus the leading `_` so the file
    name reads cleanly: `circuitlab_2019_b_q5_img_a1b2c3d4.png`.

    The result is passed through `secure_filename()` here, at the single
    point of construction, because the *serving* side (`serve_image` ->
    `_safe_join`) runs `secure_filename()` on whatever the browser asks for.
    If a caller wrote the raw name to disk, any character `secure_filename()`
    strips (`+`, non-ASCII, spaces) would make the two sides disagree: the
    file exists under the raw name, the app looks for the stripped name,
    `p.exists()` is False, and the request 404s into a permanently broken
    thumbnail. The PDF's own filename is embedded here, so real-world names
    like `..._ssss-utf-8u+6211u+662f_test.pdf` hit this. Sanitizing at
    construction makes every write site correct by construction instead of
    relying on each one to remember (only upload-image ever did)."""
    import secrets
    from werkzeug.utils import secure_filename
    base = bucket
    if base.endswith(".pdf"):
        base = base[:-4]
    base = base.lstrip("_")
    return secure_filename(f"{base}_q{num}_{kind}_{secrets.token_hex(4)}.{ext.lstrip('.')}")


@app.route("/event/<event_slug>/api/q/<bucket>/<num>/upload-image", methods=["POST"])
def api_q_upload_image(event_slug, bucket, num):
    """Attach an uploaded image file to a question. Multipart form with a
    `file` field; optional `description` form field. The image is saved into
    the event's images/ dir and appended to q.images plus q.image_descriptions."""
    from werkzeug.utils import secure_filename
    _select_event(event_slug)
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    raw = (f.filename or "").strip()
    if not raw:
        return jsonify({"error": "bad filename"}), 400
    ext = raw.rsplit(".", 1)[-1].lower() if "." in raw else ""
    if ext not in {"png", "jpg", "jpeg", "gif", "svg", "webp"}:
        return jsonify({"error": "only PNG/JPG/GIF/SVG/WebP"}), 400
    with bqb._state_transaction() as state:
        q, _ = _find_question(state, bucket, num)
        if q is None:
            return jsonify({"error": "question not found"}), 404
        safe_name = secure_filename(_slug_image_name(bucket, num, ext, "up"))
        bqb.EVENT.image_dir.mkdir(parents=True, exist_ok=True)
        dest = bqb.EVENT.image_dir / safe_name
        if ext == "svg":
            # Sanitize before it ever touches disk rather than save-then-rewrite.
            dest.write_text(_sanitize_svg(f.read().decode("utf-8", errors="replace")),
                             encoding="utf-8")
        else:
            f.save(str(dest))
        q.setdefault("images", []).append(safe_name)
        desc = (request.form.get("description") or "").strip()
        if desc:
            q.setdefault("image_descriptions", {})[safe_name] = desc
        # Drop the pending-description sentinel if it was filled by this upload
        if q.get("image_descriptions", {}).get("__pending__") and desc:
            q["image_descriptions"].pop("__pending__", None)
        state.setdefault("manual", {})[bucket] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
    return jsonify({"ok": True, "image": safe_name, "size": dest.stat().st_size})


@app.route("/event/<event_slug>/api/q/<bucket>/<num>/save-svg", methods=["POST"])
def api_q_save_svg(event_slug, bucket, num):
    """Save an SVG-derived image (raw SVG markup) and attach it to the
    question. Body: {"svg": "<svg…>", "description": "…"}. The SVG goes to
    disk as `.svg` and is also rasterised to PNG so the existing image bay
    can preview it."""
    _select_event(event_slug)
    data = request.get_json() or {}
    svg = (data.get("svg") or "").strip()
    desc = (data.get("description") or "").strip()
    if not svg or not svg.lower().startswith("<svg"):
        return jsonify({"error": "no <svg> markup in body"}), 400
    with bqb._state_transaction() as state:
        q, _ = _find_question(state, bucket, num)
        if q is None:
            return jsonify({"error": "question not found"}), 404
        bqb.EVENT.image_dir.mkdir(parents=True, exist_ok=True)
        # Save the SVG itself — Pillow can't render arbitrary SVG; we rely on the
        # browser to show .svg directly via the existing IMG_BASE route. Storing
        # SVG (not PNG) keeps it crisp at any zoom.
        fname = _slug_image_name(bucket, num, "svg", "gen")
        dest = bqb.EVENT.image_dir / fname
        dest.write_text(svg, encoding="utf-8")
        q.setdefault("images", []).append(fname)
        if desc:
            q.setdefault("image_descriptions", {})[fname] = desc
        q.get("image_descriptions", {}).pop("__pending__", None)
        state.setdefault("manual", {})[bucket] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
    return jsonify({"ok": True, "image": fname, "size": len(svg.encode("utf-8"))})


@app.route("/event/<event_slug>/api/q/<bucket>/<num>/pick-image", methods=["POST"])
def api_q_pick_image(event_slug, bucket, num):
    """Crop a rectangular region of a PDF page and attach it to a question as
    a real PNG — for when the automatic extraction pipeline missed an image
    (vector diagrams, odd layouts) and there's nothing to "Upload" because it
    only exists inside the PDF. Distinct from /upload-image (a file already
    on disk) and /save-svg (an LLM-synthesized diagram).
    Body: {pdfname, page, target, x, y, w, h, dpi}"""
    _select_event(event_slug)
    data = request.get_json() or {}
    pdfname = (data.get("pdfname") or "").strip()
    if not pdfname:
        return jsonify({"error": "no pdfname"}), 400
    try:
        pno = int(data["page"])
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 8 or h < 8:
        return jsonify({"error": "region too small"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    b64 = region_image_b64(page, rect, dpi=300)

    with bqb._state_transaction() as state:
        q, _ = _find_question(state, bucket, num)
        if q is None:
            return jsonify({"error": "question not found"}), 404
        bqb.EVENT.image_dir.mkdir(parents=True, exist_ok=True)
        fname = _slug_image_name(bucket, num, "png", "pick")
        dest = bqb.EVENT.image_dir / fname
        dest.write_bytes(base64.b64decode(b64))
        q.setdefault("images", []).append(fname)
        q.get("image_descriptions", {}).pop("__pending__", None)
        state.setdefault("manual", {})[bucket] = {
            "edited_at": datetime.now().isoformat(timespec="seconds"),
        }
    return jsonify({"ok": True, "image": fname, "size": dest.stat().st_size})


@app.route("/event/<event_slug>/api/context-image", methods=["POST"])
def api_context_image(event_slug):
    """Crop a rectangular region of a PDF page and save it as a PNG for use
    as a shared-context figure (e.g. a Circuit Lab resistor-network diagram
    that several sub-questions refer back to). Sibling of api_q_pick_image,
    minus any question identity — contexts live in annotations, not
    state["questions"], so this route deliberately does NOT touch `state`;
    the caller (extract.html's applyCapture()) pushes the returned filename
    into the context's own `images` list via the normal annotations save.
    Body: {pdfname, page, target, x, y, w, h, dpi}"""
    _select_event(event_slug)
    data = request.get_json() or {}
    pdfname = (data.get("pdfname") or "").strip()
    if not pdfname:
        return jsonify({"error": "no pdfname"}), 400
    try:
        pno = int(data["page"])
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "bad region"}), 400
    if w < 8 or h < 8:
        return jsonify({"error": "region too small"}), 400
    dpi = float(data.get("dpi", 120))
    target = data.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    f = 72.0 / dpi
    rect = fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)
    b64 = region_image_b64(page, rect, dpi=300)

    bqb.EVENT.image_dir.mkdir(parents=True, exist_ok=True)
    fname = _slug_image_name(pdfname, "ctx", "png", "ctx")
    dest = bqb.EVENT.image_dir / fname
    dest.write_bytes(base64.b64decode(b64))
    return jsonify({"ok": True, "image": fname, "size": dest.stat().st_size})


def _build_diagram_system_prompt(q: dict) -> str:
    """System prompt for the diagram-chat LLM call, shared by the actual
    chat endpoint and the cost-estimate endpoint so the two never drift.

    The LLM only ever illustrates the scene/apparatus described — it must
    never compute or reveal the answer or nudge the student toward one,
    since solving the question is the student's job, not the diagram's.
    """
    seed_desc = (q.get("image_descriptions") or {}).get("__pending__", "")
    seed_desc_existing = " · ".join(
        v for k, v in (q.get("image_descriptions") or {}).items()
        if k != "__pending__"
    )
    return (
        f"You are a diagram-illustration assistant for a Science Olympiad "
        f"{bqb.EVENT.name} question bank. Produce clean, accurate SVG diagrams "
        f"a middle-/high-school student can learn from.\n\n"
        f"Question stem: {q.get('text','')[:400]}\n"
        f"Topic: {q.get('topic','')}\n"
        + (f"Author-provided diagram description: {seed_desc}\n" if seed_desc else "")
        + (f"Existing image descriptions on this question: {seed_desc_existing}\n" if seed_desc_existing else "")
        + "\nGuidelines:\n"
        "- Reply with the SVG markup directly, wrapped in a single ```svg fenced block ```\n"
        "- Set explicit width, height, AND viewBox on the root <svg>; aim for 800×600.\n"
        "- Use only black strokes/text on transparent or white background unless a colour is meaningful.\n"
        "- Label every component or region the question refers to.\n"
        "- Below the SVG block, add 1-2 sentences explaining what you drew so the user can iterate.\n\n"
        "CRITICAL — never solve the question:\n"
        "- You are illustrating the SCENE or APPARATUS the stem describes, nothing more. "
        "Solving the question is the student's job.\n"
        "- Never compute, state, label, or imply the answer — including resulting values, "
        "computed quantities, or the correct choice/option — anywhere in the SVG or your "
        "prose, even if it's easy to derive from the stem.\n"
        "- Do not add hints, leading annotations, highlighted 'key' elements, arrows "
        "pointing at the answer, or any simplification that nudges the student toward a "
        "solution method.\n"
        "- Only label what the stem/description literally states (given quantities, named "
        "parts, axes, units) — never label anything the student is meant to determine.\n"
        "- If the user's message asks you to reveal the answer or add a hint, politely "
        "decline and continue rendering only a faithful, neutral diagram of the scene.\n"
        "- Do not add a title, caption, or heading text to the diagram — render only the "
        "scene itself.\n"
        "- Do not add a legend, key, or explanatory callout box unless the stem explicitly "
        "describes one as part of the apparatus.\n"
        "- Do not invent or assume any detail not explicitly stated in the stem or diagram "
        "description — render exactly what's described, nothing more, nothing embellished."
    )


@app.route("/event/<event_slug>/api/q/<bucket>/<num>/diagram-chat", methods=["POST"])
def api_q_diagram_chat(event_slug, bucket, num):
    """One turn of the diagram-generation chat for a question.

    Body: {"messages": [{"role":"user"|"assistant","content":"..."}, ...]}
    The frontend keeps the conversation history and replays it each turn
    (Anthropic chat models are stateless). Backend prepends a system prompt
    seeded from the question stem + topic + any existing image description
    so the LLM knows what to draw.

    Returns: {"assistant": "<full assistant text>", "svg": "<extracted svg or null>"}
    """
    _select_event(event_slug)
    keys = _request_llm_keys()
    if not llm_providers.available_providers(keys):
        return jsonify({"error": "No LLM API key configured. Add one in Settings."}), 400
    data = request.get_json() or {}
    messages = data.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages must be a non-empty list"}), 400

    state = bqb._load_state()
    q, _ = _find_question(state, bucket, num)
    if q is None:
        return jsonify({"error": "question not found"}), 404

    system_prompt = _build_diagram_system_prompt(q)

    # Re-shape: chat-style APIs expect user/assistant alternation. Strip anything else.
    clean_msgs = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not clean_msgs or clean_msgs[-1]["role"] != "user":
        return jsonify({"error": "last message must be from user"}), 400

    try:
        result = llm_providers.chat(
            keys=keys, system=system_prompt, messages=clean_msgs, max_tokens=4096,
            model_overrides={"anthropic": bqb.DIAGRAM_MODEL},
        )
    except llm_providers.LLMError as e:
        return jsonify({"error": f"LLM call failed: {e}"}), 502
    if result["provider"] == "anthropic":
        bqb._track_usage_tokens(result["input_tokens"], result["output_tokens"])
    assistant_text = result["text"]

    # Extract the SVG markup if present. Accept ```svg, ```xml, or a bare <svg>.
    import re as _re
    svg = None
    fence = _re.search(r"```(?:svg|xml|html)?\s*(<svg[\s\S]*?</svg>)\s*```", assistant_text, _re.IGNORECASE)
    if fence:
        svg = fence.group(1)
    else:
        m_bare = _re.search(r"(<svg[\s\S]*?</svg>)", assistant_text, _re.IGNORECASE)
        if m_bare:
            svg = m_bare.group(1)

    return jsonify({
        "assistant": assistant_text,
        "svg":       svg,
        "model":     result["model"],
        "provider":  result["provider"],
        "stop":      result["stop_reason"],
    })


# Assumed output length for the cost *preview* shown before a diagram-chat
# Send — actual usage varies with SVG complexity, but this keeps the
# estimate in the right ballpark without spending a real generation call.
_DIAGRAM_ASSUMED_OUTPUT_TOKENS = 1500


@app.route("/event/<event_slug>/api/q/<bucket>/<num>/diagram-chat/estimate", methods=["POST"])
def api_q_diagram_chat_estimate(event_slug, bucket, num):
    """Token/cost preview for the user's in-progress diagram-chat turn.

    Body: same shape as /diagram-chat ({"messages": [...]}, may include the
    not-yet-sent draft as the trailing user message). Estimated against
    whichever provider /diagram-chat would actually use first (the earliest
    one in PROVIDER_ORDER the caller has a key for) -- exact via Anthropic's
    free count_tokens endpoint when that's Anthropic, a character-based
    approximation otherwise (see llm_providers.estimate_cost).

    Returns: {"input_tokens", "assumed_output_tokens", "estimated_cost_usd", ...}
    """
    _select_event(event_slug)
    keys = _request_llm_keys()
    if not llm_providers.available_providers(keys):
        return jsonify({"error": "No LLM API key configured. Add one in Settings."}), 400
    data = request.get_json() or {}
    messages = data.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages must be a non-empty list"}), 400

    state = bqb._load_state()
    q, _ = _find_question(state, bucket, num)
    if q is None:
        return jsonify({"error": "question not found"}), 404

    system_prompt = _build_diagram_system_prompt(q)
    clean_msgs = [
        {"role": m.get("role"), "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not clean_msgs:
        return jsonify({"error": "no usable messages"}), 400

    try:
        result = llm_providers.estimate_cost(
            keys=keys, system=system_prompt, messages=clean_msgs,
            assumed_output_tokens=_DIAGRAM_ASSUMED_OUTPUT_TOKENS,
            model_overrides={"anthropic": bqb.DIAGRAM_MODEL},
        )
    except llm_providers.LLMError as e:
        return jsonify({"error": f"token count failed: {e}"}), 502
    return jsonify(result)


@app.route("/event/<event_slug>/api/export.<fmt>", methods=["GET", "POST"])
def api_export(event_slug, fmt):
    """Export the bank. `fmt` is csv, json, apkg or pdf.

    GET exports everything. POST with {"keys": [{"bucket","number"}, ...]}
    exports just those questions, in the order sent — that ordering matters
    because the Browse page's sort is the order a coach just arranged the
    questions into, and a printed paper that ignores it is a different
    document from the one on screen.

    Only the server-rendered formats actually need this: CSV, JSON and
    markdown subsets are built client-side from data the page already has,
    but PDF (reportlab) and Anki (genanki) can only be produced here, so
    without a way to name a subset they were stuck exporting the whole
    bank."""
    _select_event(event_slug)
    state = bqb._load_state()
    all_qs = []
    for bucket, qs in state.get("questions", {}).items():
        for q in qs:
            row = dict(q)
            row["_bucket"] = bucket
            all_qs.append(row)

    subset_label = ""
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            return jsonify({"error": "POST export needs a non-empty 'keys' list"}), 400
        by_key = {(q.get("_bucket", ""), str(q.get("number", ""))): q for q in all_qs}
        picked = []
        for k in keys:
            q = by_key.get((k.get("bucket", ""), str(k.get("number", ""))))
            # Silently skip a key that no longer resolves rather than
            # failing the whole export: the page's data can lag a delete by
            # another coach, and losing one question from a printout is a
            # far better outcome than losing the printout.
            if q is not None:
                picked.append(q)
        if not picked:
            return jsonify({"error": "none of those questions are in this bank any more"}), 400
        all_qs = picked
        from werkzeug.utils import secure_filename
        # Goes straight into a Content-Disposition header, so strip it the
        # same way every other user-supplied filename in this app is.
        subset_label = secure_filename(str(payload.get("label") or "subset"))[:40] or "subset"

    # Answer layout, accepted from the query string (GET, whole bank) or the
    # body (POST, subset). Rejected rather than defaulted when unrecognised:
    # silently handing back a copy that shows the answers when the caller
    # asked for a clean one is the failure worth refusing.
    layout = (request.args.get("layout")
              or (request.get_json(silent=True) or {}).get("layout")
              or "key")
    if layout not in EXPORT_LAYOUTS:
        return jsonify({"error": f"unknown layout {layout!r}; "
                                 f"expected one of {', '.join(EXPORT_LAYOUTS)}"}), 400

    def _filename(ext: str) -> str:
        stem = bqb.EVENT.slug + (f"-{subset_label}" if subset_label else "")
        return f"{stem}.{ext}"

    if fmt == "json":
        # "_contexts" carries the shared case-study passages/tables/diagrams
        # referenced by any question's context_id (bucket::id-namespaced —
        # see bqb._all_contexts) so a JSON consumer doesn't have to separately
        # reconstruct them from the extract-page annotations.
        payload = {"questions": all_qs, "_contexts": bqb._all_contexts()}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename={_filename('json')}"})
    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["number","topic","focus","text","answer","choices",
                    "source","year","division","bucket","context_id",
                    "validation_status","rationale"])
        for q in all_qs:
            v = q.get("validation") or {}
            choices_flat = " | ".join(f"{c.get('letter','?')}. {c.get('text','')}"
                                      for c in q.get("choices") or [])
            w.writerow([
                q.get("number",""), q.get("topic",""), q.get("focus",""),
                q.get("text",""), q.get("answer",""), choices_flat,
                q.get("source",""), q.get("year",""), q.get("division",""),
                q.get("_bucket",""), q.get("context_id",""),
                v.get("status",""), v.get("rationale",""),
            ])
        return Response(buf.getvalue(),
                        mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename={_filename('csv')}"})
    if fmt == "apkg":
        err = _optional_dep_error("genanki")
        if err:
            return jsonify({"error": err}), 400
        return _export_apkg(all_qs, _filename("").rstrip("."))
    if fmt == "pdf":
        err = _optional_dep_error("reportlab")
        if err:
            return jsonify({"error": err}), 400
        stem = _filename("").rstrip(".")
        if layout != "key":
            stem += "-" + ("questions" if layout == "none" else "answers")
        return _export_pdf(all_qs, bqb._all_contexts(), stem, layout)
    return jsonify({"error": f"unsupported format: {fmt}"}), 400


def _optional_dep_error(module: str) -> str | None:
    """None if `module` imports here, otherwise a message that says enough
    to actually fix it.

    "X not installed" was wrong often enough to be worth replacing. The
    import can fail with the package plainly installed -- most commonly
    because the serving interpreter isn't the one it was installed into
    (this app runs under gunicorn from a venv, so `pip install X` in a
    login shell frequently lands somewhere the app never looks), and
    occasionally because the package is present but its own internals
    fail to import, which still raises ImportError. Naming the
    interpreter and quoting the real exception distinguishes those without
    a round trip through the logs."""
    import importlib
    try:
        importlib.import_module(module)
        return None
    except ImportError as e:
        return (f"Could not import {module!r} in the interpreter serving this "
                f"app ({sys.executable}): {e}. If it is installed elsewhere, "
                f"install it for THIS interpreter: "
                f"{sys.executable} -m pip install {module}")


#: The three answer layouts, shared by the PDF and markdown exports so the
#: same words mean the same thing in both. Mirrors assessments.py's
#: render_questions_markdown(answers=...) vocabulary.
EXPORT_LAYOUTS = ("none", "inline", "key")


def _export_pdf(all_qs: list[dict], context_lookup: dict | None = None,
                filename_stem: str = "", layout: str = "key") -> "Response":
    """Generate a printable PDF. One question per logical block, page breaks
    honoured by reportlab's SimpleDocTemplate platypus flow.

    `layout` picks what happens to the answers, matching the markdown
    export's options exactly:
      "none"   -- questions only, nothing to give the answer away
      "inline" -- each answer printed under its own question
      "key"    -- questions first, answer key on its own page at the end
    """
    if layout not in EXPORT_LAYOUTS:
        raise ValueError(f"unknown layout: {layout!r}")
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether, Table, TableStyle,
    )
    import io as _io

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"{bqb.EVENT.name} question bank"
              + ("" if layout == "none" else " (with answers)"),
    )

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = ParagraphStyle("body", parent=styles["BodyText"],
                          fontSize=11, leading=14, spaceAfter=6)
    choice_style = ParagraphStyle("choice", parent=body,
                                  leftIndent=18, fontSize=10, leading=12)
    meta_style = ParagraphStyle("meta", parent=body,
                                fontSize=8, textColor="#888",
                                spaceAfter=4)
    answer_style = ParagraphStyle("answer", parent=body,
                                  leftIndent=18, fontSize=10, leading=12,
                                  textColor="#1a6b32", spaceBefore=2)

    def _e(s: str) -> str:
        # reportlab.Paragraph parses XML, so escape the basics.
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = []
    # Branding, when this instance has any. A printed test leaves the app
    # entirely -- it goes out on paper with nothing else identifying whose
    # it is -- so the logo earns its place here more than anywhere on screen.
    logo_name = (os.environ.get("SCHOOL_LOGO") or "").strip()
    if logo_name:
        logo_path = _STATIC_DIR / os.path.basename(logo_name)
        if logo_path.is_file():
            try:
                from reportlab.platypus import Image as RLImage
                from reportlab.lib.utils import ImageReader
                iw, ih = ImageReader(str(logo_path)).getSize()
                # Fix the width and derive the height from the file's own
                # aspect ratio, so the wordmark is never stretched.
                width = 1.9 * inch
                story.append(RLImage(str(logo_path), width=width,
                                     height=width * ih / iw))
                story.append(Spacer(1, 0.12 * inch))
            except Exception as e:
                # A broken logo must never cost someone their export.
                app.logger.warning("Could not embed SCHOOL_LOGO in PDF: %s", e)

    story += [
        Paragraph(f"{_e(bqb.EVENT.name)} — Question Bank", h1),
        Paragraph(f"{len(all_qs)} questions", meta_style),
        Spacer(1, 0.2 * inch),
    ]

    # Group by topic so the PDF reads like a study guide.
    by_topic: dict[str, list[dict]] = {}
    for q in all_qs:
        by_topic.setdefault(q.get("topic") or "Other / General", []).append(q)

    context_lookup = context_lookup or {}
    context_style = ParagraphStyle("context", parent=body,
                                   backColor="#fffbeb", borderColor="#e8c875",
                                   borderWidth=1, borderPadding=8, spaceAfter=8)

    n = 0  # global counter for cross-referencing with the answer key
    answer_lines: list[str] = []
    for topic in sorted(by_topic.keys()):
        story.append(Paragraph(_e(topic), h2))
        # Cluster case-study questions so the shared passage prints once,
        # immediately before all the questions that reference it, instead of
        # being scattered across the topic wherever each question sorts to.
        for cluster in bqb._cluster_by_context(by_topic[topic], context_lookup):
            key = bqb._context_key(cluster[0])
            ctx = context_lookup.get(key) if key else None
            if ctx:
                heading = "Shared context" + (f": {ctx['title']}" if ctx.get("title") else "")
                story.append(Paragraph(f"<b>{_e(heading)}</b><br/>{_e(ctx.get('text', ''))}",
                                       context_style))
            for q in cluster:
                n += 1
                block = [
                    Paragraph(f"<b>Q{n}.</b> {_e(q.get('text',''))}", body),
                ]
                for c in q.get("choices") or []:
                    block.append(Paragraph(
                        f"<b>{_e(c.get('letter','?'))}.</b> {_e(c.get('text',''))}",
                        choice_style,
                    ))
                pairs_str = "—"
                if q.get("qtype") == "tf":
                    block.append(Paragraph("True / False ______", choice_style))
                if q.get("qtype") == "matching":
                    m = q.get("matching") or {}
                    left, right = m.get("left") or [], m.get("right") or []

                    def _e_cell(item: dict) -> str:
                        txt = _e(item.get("text") or "")
                        if item.get("image"):
                            txt = (txt + " " if txt else "") + "[figure]"
                        return txt or "—"

                    rows = [["#", "Column A", "#", "Column B"]]
                    for i in range(max(len(left), len(right))):
                        l = left[i] if i < len(left) else {}
                        r = right[i] if i < len(right) else {}
                        rows.append([l.get("label", ""), _e_cell(l),
                                     r.get("label", ""), _e_cell(r)])
                    tbl = Table(rows, colWidths=[0.3 * inch, 2.6 * inch, 0.3 * inch, 2.6 * inch])
                    tbl.setStyle(TableStyle([
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                    ]))
                    block.append(tbl)
                    pairs = m.get("pairs") or {}
                    pairs_str = ", ".join(f"{l}→{r}" for l, r in pairs.items()) or "—"
                meta_bits = []
                if q.get("source"):  meta_bits.append(_e(q["source"]))
                if q.get("focus"):   meta_bits.append(f"focus: {_e(q['focus'])}")
                if meta_bits:
                    block.append(Paragraph(" · ".join(meta_bits), meta_style))
                answer_text = (pairs_str if q.get("qtype") == "matching"
                               else _e(q.get("answer") or "—"))
                if layout == "inline":
                    # Inside the KeepTogether block, so an answer can never
                    # be orphaned onto the next page away from its question.
                    block.append(Paragraph(f"<b>Answer:</b> {answer_text}", answer_style))
                block.append(Spacer(1, 6))
                story.append(KeepTogether(block))
                answer_lines.append(f"Q{n}: {answer_text}")

    if layout == "key":
        story.append(PageBreak())
        story.append(Paragraph("Answer Key", h1))
        for line in answer_lines:
            story.append(Paragraph(line, body))

    doc.build(story)
    data = buf.getvalue()
    return Response(data,
                    mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment; filename={filename_stem or bqb.EVENT.slug}.pdf"})


def _export_apkg(all_qs: list[dict], filename_stem: str = "") -> "Response":
    """Build an Anki .apkg from the question bank.

    Layout:
      - One deck per event, with sub-decks per topic ("MyEvent::Capacitors").
      - MCQ notes use a Basic-with-choices template; FRQ a Basic template.
      - Generated/scioly questions are tagged with their bucket so the user
        can filter inside Anki.
    """
    import genanki  # type: ignore
    import io, tempfile, hashlib

    # Stable model IDs so re-exports merge cleanly into an existing collection.
    def _hash_id(s: str) -> int:
        # genanki expects a 32-bit-ish int. Use the bottom 31 bits of sha1.
        return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF

    mcq_model = genanki.Model(
        _hash_id(f"scioly:mcq:{bqb.EVENT.slug}"),
        f"Sci-Oly {bqb.EVENT.name} MCQ",
        fields=[{"name": n} for n in
                ["Question", "Choices", "Answer", "Topic", "Focus", "Source", "Note"]],
        templates=[{
            "name": "MCQ Card",
            "qfmt": "<div class=q>{{Question}}</div><br>{{Choices}}",
            "afmt": '{{FrontSide}}<hr id="answer"><div class=a>{{Answer}}</div>'
                    '<div class=meta>{{Topic}} · {{Source}}{{#Note}} · {{Note}}{{/Note}}</div>',
        }],
        css="""
.card{font-family:-apple-system,system-ui,sans-serif;font-size:16px;color:#222;text-align:left}
.q{font-weight:600;margin-bottom:10px}
.a{color:#1f8a4d;font-weight:600;font-size:18px;margin-top:8px}
.meta{font-size:11px;color:#888;margin-top:8px}
""",
    )
    frq_model = genanki.Model(
        _hash_id(f"scioly:frq:{bqb.EVENT.slug}"),
        f"Sci-Oly {bqb.EVENT.name} FRQ",
        fields=[{"name": n} for n in ["Question", "Answer", "Topic", "Focus", "Source", "Note"]],
        templates=[{
            "name": "FRQ Card",
            "qfmt": "<div class=q>{{Question}}</div>",
            "afmt": '{{FrontSide}}<hr id="answer"><div class=a>{{Answer}}</div>'
                    '<div class=meta>{{Topic}} · {{Source}}{{#Note}} · {{Note}}{{/Note}}</div>',
        }],
        css=mcq_model.css,
    )

    decks_by_topic: dict[str, "genanki.Deck"] = {}
    def _deck_for(topic: str) -> "genanki.Deck":
        topic = topic or "Other / General"
        key = topic
        if key not in decks_by_topic:
            deck_id = _hash_id(f"scioly:deck:{bqb.EVENT.slug}:{topic}")
            decks_by_topic[key] = genanki.Deck(
                deck_id,
                f"{bqb.EVENT.name}::{topic}",
            )
        return decks_by_topic[key]

    def _esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    for q in all_qs:
        bucket = q.get("_bucket", "")
        tags = []
        if bucket.startswith("_generated_"):
            tags.append("generated")
        elif bucket.startswith("_scioly_"):
            tags.append("scioly")
        if q.get("focus"):
            tags.append("focus_" + str(q["focus"]).replace(" ", "_"))
        if q.get("quality_flag"):
            tags.append("flag_" + str(q["quality_flag"]))
        note_text = (q.get("reviewer_note") or "").strip()
        if q.get("qtype") == "matching":
            m = q.get("matching") or {}

            def _cell_html(item: dict) -> str:
                txt = _esc(item.get("text") or "")
                img = item.get("image")
                if img:
                    txt = (txt + " " if txt else "") + f"[figure: {_esc(img)}]"
                return txt or "(empty)"

            rows_html = "<br>".join(
                f"<b>{_esc(l.get('label',''))}.</b> {_cell_html(l)} &mdash; "
                f"<b>{_esc(r.get('label',''))}.</b> {_cell_html(r)}"
                for l, r in zip(m.get("left") or [], m.get("right") or [])
            )
            pairs = m.get("pairs") or {}
            pairs_str = ", ".join(f"{l}→{r}" for l, r in pairs.items()) or "—"
            note = genanki.Note(
                model=frq_model,
                fields=[
                    _esc(q.get("text", "")) + ("<br>" + rows_html if rows_html else ""),
                    pairs_str,
                    _esc(q.get("topic", "")),
                    _esc(q.get("focus", "")),
                    _esc(q.get("source", "")),
                    _esc(note_text),
                ],
                tags=tags,
            )
        elif q.get("qtype") == "tf":
            # A T/F card is a Basic card (front/back), not the MCQ template
            # — there are no lettered choices to render.
            note = genanki.Note(
                model=frq_model,
                fields=[
                    "True/False: " + _esc(q.get("text", "")),
                    _esc(q.get("answer", "—")),
                    _esc(q.get("topic", "")),
                    _esc(q.get("focus", "")),
                    _esc(q.get("source", "")),
                    _esc(note_text),
                ],
                tags=tags,
            )
        elif (q.get("choices") or []):
            choices_html = "<br>".join(
                f"<b>{_esc(c.get('letter','?'))}.</b> {_esc(c.get('text',''))}"
                for c in q["choices"])
            note = genanki.Note(
                model=mcq_model,
                fields=[
                    _esc(q.get("text", "")),
                    choices_html,
                    _esc(q.get("answer", "—")),
                    _esc(q.get("topic", "")),
                    _esc(q.get("focus", "")),
                    _esc(q.get("source", "")),
                    _esc(note_text),
                ],
                tags=tags,
            )
        else:
            note = genanki.Note(
                model=frq_model,
                fields=[
                    _esc(q.get("text", "")),
                    _esc(q.get("answer", "—")),
                    _esc(q.get("topic", "")),
                    _esc(q.get("focus", "")),
                    _esc(q.get("source", "")),
                    _esc(note_text),
                ],
                tags=tags,
            )
        _deck_for(q.get("topic", "")).add_note(note)

    pkg = genanki.Package(list(decks_by_topic.values()))
    # genanki writes to a path; use a temp file then read it back.
    with tempfile.NamedTemporaryFile(suffix=".apkg", delete=False) as tf:
        tmp_path = tf.name
    try:
        pkg.write_to_file(tmp_path)
        data = Path(tmp_path).read_bytes()
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
    return Response(data,
                    mimetype="application/octet-stream",
                    headers={"Content-Disposition":
                             f"attachment; filename={filename_stem or bqb.EVENT.slug}.apkg"})


# ---------------------------------------------------------------------------
# Routes — Event registration (user-defined events)
# ---------------------------------------------------------------------------

@app.route("/api/events", methods=["POST"])
@coach_required
def api_create_event():
    data = request.get_json() or {}
    event_match = data.get("event_match") or []
    if isinstance(event_match, str):
        event_match = [s for s in event_match.split(",")]
    topics = data.get("topics") or []
    if isinstance(topics, str):
        topics = [s for s in topics.split(",")]
    foci = data.get("foci") or []
    if isinstance(foci, str):
        foci = [s for s in foci.split(",")]
    try:
        ev = add_custom_event(
            slug=data.get("slug", ""),
            name=data.get("name", ""),
            filename_prefix=data.get("filename_prefix", ""),
            event_match=event_match,
            wiki_page=data.get("wiki_page", ""),
            topics=topics,
            foci=foci,
            has_build=bool(data.get("has_build", False)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Eagerly create the event directory
    ev.base_dir.mkdir(exist_ok=True)
    return jsonify({
        "ok":   True,
        "slug": ev.slug,
        "name": ev.name,
        "url":  url_for("event_index", event_slug=ev.slug),
    })


# ---------------------------------------------------------------------------
# Routes — Source texts (wiki + user-supplied PDFs)
# ---------------------------------------------------------------------------

@app.route("/event/<event_slug>/sources")
def sources_page(event_slug):
    _select_event(event_slug)
    return render_template("sources.html",
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name,
                            wiki_url=bqb.EVENT.wiki_url)


@app.route("/event/<event_slug>/api/sources")
def api_sources(event_slug):
    _select_event(event_slug)
    sources = texts_mod.list_sources(bqb.EVENT)
    return jsonify({
        "sources":   sources,
        "texts_dir": str(bqb.EVENT.texts_dir),
        "wiki_url":  bqb.EVENT.wiki_url,
        "foci":      list(bqb.EVENT.foci),
        "event_name": bqb.EVENT.name,
    })


@app.route("/event/<event_slug>/api/sources/scrape-wiki", methods=["POST"])
def api_scrape_wiki(event_slug):
    _select_event(event_slug)
    ev = bqb.EVENT
    # Reuse the saved scioly.org cookies if we have them
    cookies = None
    try:
        from download_event import _load_cookies
        cookies = _load_cookies()
    except Exception:
        pass

    def _target(should_cancel, on_progress):
        # No internal loop to checkpoint — a single bounded HTTP fetch +
        # local HTML→markdown conversion, so no should_cancel/on_progress
        # threading needed here beyond what the job wrapper already gives
        # (queued jobs can still be cancelled before they start running).
        _job_target_setup(event_slug)
        out = texts_mod.scrape_wiki(ev, cookies=cookies)
        return {"path": out.name, "size": out.stat().st_size, "url": ev.wiki_url}

    try:
        job_id = jobs.submit_job(event_slug, "wiki_scrape", f"Scrape wiki for {event_slug}",
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/sources/<filename>/process", methods=["POST"])
def api_process_source(event_slug, filename):
    _select_event(event_slug)
    src = _safe_join(bqb.EVENT.texts_dir, filename)
    if not src.exists():
        return jsonify({"error": f"not found: {filename}"}), 404
    if src.suffix.lower() != ".pdf":
        return jsonify({"error": "only PDF inputs can be processed"}), 400
    try:
        out = texts_mod.pdf_to_markdown(src)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "path": out.name, "size": out.stat().st_size})


@app.route("/event/<event_slug>/api/sources/<filename>/raw")
def api_source_raw(event_slug, filename):
    _select_event(event_slug)
    p = _safe_join(bqb.EVENT.texts_dir, filename)
    if not p.exists():
        abort(404)
    if filename.lower().endswith(".pdf"):
        return send_file(str(p))
    return Response(p.read_text(encoding="utf-8"),
                    content_type="text/markdown; charset=utf-8")


@app.route("/event/<event_slug>/api/sources/upload", methods=["POST"])
def api_source_upload(event_slug):
    from werkzeug.utils import secure_filename
    _select_event(event_slug)
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    raw = (f.filename or "").strip()
    if not raw:
        return jsonify({"error": "bad filename"}), 400
    # secure_filename strips path separators, normalises unicode, and removes
    # ".." sequences; preserves a safe basename.
    name = secure_filename(raw)
    if not name or name.startswith("."):
        return jsonify({"error": "bad filename"}), 400
    if not name.lower().endswith((".pdf", ".md", ".txt")):
        return jsonify({"error": "only PDF, MD, or TXT"}), 400
    dest = bqb.EVENT.texts_dir / name
    dest.parent.mkdir(exist_ok=True)
    f.save(str(dest))
    if name.lower().endswith(".pdf") and not pdf_safety.looks_like_pdf(dest):
        dest.unlink(missing_ok=True)
        return jsonify({"error": "file isn't a valid PDF (bad header)"}), 400
    return jsonify({"ok": True, "name": name, "size": dest.stat().st_size})


# ---------------------------------------------------------------------------
# Routes — Shared textbooks (cross-event, NOT under /event/<slug>)
#
# Unlike per-event sources (texts_dir, above), a textbook is uploaded once
# and can be used to generate questions for ANY event — split by chapter so
# a single 500-page book doesn't get dumped wholesale into one LLM call.
# ---------------------------------------------------------------------------

TEXTBOOKS_DIR = DATA_ROOT / "textbooks"


def _textbook_pdf_path(textbook_id: str) -> Path:
    return TEXTBOOKS_DIR / f"{textbook_id}.pdf"


def _textbook_chapters_path(textbook_id: str) -> Path:
    return TEXTBOOKS_DIR / f"{textbook_id}.chapters.json"


def _load_textbook_meta(textbook_id: str) -> dict:
    p = _textbook_chapters_path(textbook_id)
    if not p.exists():
        return {"chapters": [], "source": "manual", "needs_manual_chapters": True}
    return json.loads(p.read_text(encoding="utf-8"))


@app.route("/api/textbooks")
def api_textbooks_list():
    TEXTBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for pdf in sorted(TEXTBOOKS_DIR.glob("*.pdf")):
        tid = pdf.stem
        out.append({"id": tid, "name": pdf.name, "size": pdf.stat().st_size,
                    **_load_textbook_meta(tid)})
    return jsonify({"textbooks": out})


@app.route("/api/textbooks/upload", methods=["POST"])
@coach_required
def api_textbooks_upload():
    """Upload a textbook PDF shared across all events. Converts it to
    markdown and attempts chapter detection immediately (see
    texts.detect_chapters) so the Sources page can offer a chapter picker
    right away."""
    from werkzeug.utils import secure_filename
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    raw = (f.filename or "").strip()
    if not raw or not raw.lower().endswith(".pdf"):
        return jsonify({"error": "must be a PDF"}), 400
    TEXTBOOKS_DIR.mkdir(parents=True, exist_ok=True)

    stem = secure_filename(Path(raw).stem) or "textbook"
    tid = stem
    n = 1
    while _textbook_pdf_path(tid).exists():
        tid = f"{stem}_{n}"
        n += 1
    dest = _textbook_pdf_path(tid)
    f.save(str(dest))
    if not pdf_safety.looks_like_pdf(dest):
        dest.unlink(missing_ok=True)
        return jsonify({"error": "file isn't a valid PDF (bad header)"}), 400

    try:
        md_path = texts_mod.pdf_to_markdown(dest)
        meta = texts_mod.detect_chapters(dest, md_path.read_text(encoding="utf-8"))
    except pdf_safety.UnsafePdfError as e:
        dest.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400
    _textbook_chapters_path(tid).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "id": tid, "name": dest.name, **meta})


@app.route("/api/textbooks/<textbook_id>/detect", methods=["POST"])
@coach_required
def api_textbooks_detect(textbook_id):
    """(Re-)run chapter detection on a textbook PDF already sitting in
    textbooks/ — e.g. one placed there directly on disk rather than through
    the upload form above. Regenerates the markdown dump too."""
    pdf_path = _textbook_pdf_path(textbook_id)
    if not pdf_path.exists():
        return jsonify({"error": "textbook not found"}), 404
    md_path = texts_mod.pdf_to_markdown(pdf_path)
    meta = texts_mod.detect_chapters(pdf_path, md_path.read_text(encoding="utf-8"))
    _textbook_chapters_path(textbook_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return jsonify({"ok": True, **meta})


@app.route("/api/textbooks/<textbook_id>/chapters", methods=["POST"])
@coach_required
def api_textbooks_set_chapters(textbook_id):
    """Manual chapter-boundary entry — the fallback for textbooks where
    detect_chapters() found neither an embedded TOC nor any 'Chapter N'
    style headings in the extracted text. `end_page` is optional per
    chapter — when omitted it cascades from the next chapter's start_page
    (or the PDF's last page for the final chapter), same as detect_chapters.
    Body: {"chapters": [{"title","start_page","end_page"?}, ...]}"""
    pdf_path = _textbook_pdf_path(textbook_id)
    if not pdf_path.exists():
        return jsonify({"error": "textbook not found"}), 404
    data = request.get_json() or {}
    raw = []
    for c in (data.get("chapters") or []):
        try:
            title = str(c.get("title") or "").strip()
            start = int(c["start_page"])
        except (KeyError, TypeError, ValueError):
            continue
        if not title or start < 1:
            continue
        end = c.get("end_page")
        try:
            end = int(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        raw.append((title, start, end))
    if not raw:
        return jsonify({"error": "no valid chapters provided"}), 400

    try:
        doc = fitz.open(str(pdf_path))
        last_page = doc.page_count
        doc.close()
    except Exception:
        last_page = max(start for _, start, _ in raw)

    cleaned = []
    for i, (title, start, end) in enumerate(raw):
        if end is None:
            end = raw[i + 1][1] - 1 if i + 1 < len(raw) else last_page
        cleaned.append({"title": title, "start_page": start, "end_page": max(end, start)})
    meta = {"chapters": cleaned, "source": "manual", "needs_manual_chapters": False}
    _textbook_chapters_path(textbook_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return jsonify({"ok": True, **meta})


# ---------------------------------------------------------------------------
# Routes — LLM question generation
# ---------------------------------------------------------------------------

@app.route("/event/<event_slug>/api/generate", methods=["POST"])
def api_generate(event_slug):
    _select_event(event_slug)
    keys = _request_llm_keys()
    if not llm_providers.available_providers(keys):
        return jsonify({"error": "No LLM API key configured. Add one in Settings."}), 400
    data = request.get_json() or {}
    source_name = data.get("source") or ""
    textbook_id = data.get("textbook") or ""
    n = max(1, min(20, int(data.get("n", 5))))
    types = data.get("types") or ["mc", "short", "numerical"]
    # Whole-document mode: chunk the source up to 5 pieces and call Haiku
    # per chunk, asking for ceil(n/chunks) each time. Lets users squeeze
    # questions out of long textbooks where the early chapters got picked
    # over and over.
    max_chunks = max(1, min(5, int(data.get("max_chunks", 1))))

    if textbook_id:
        # Shared-textbook mode: pull just one chapter's pages instead of a
        # whole-document chunk — see /api/textbooks above.
        chapters = _load_textbook_meta(textbook_id).get("chapters") or []
        try:
            idx = int(data.get("chapter_index"))
        except (TypeError, ValueError):
            return jsonify({"error": "no chapter selected"}), 400
        if idx < 0 or idx >= len(chapters):
            return jsonify({"error": "chapter index out of range"}), 400
        chapter = chapters[idx]
        pdf_path = _textbook_pdf_path(textbook_id)
        if not pdf_path.exists():
            return jsonify({"error": "textbook not found"}), 404
        try:
            doc = fitz.open(str(pdf_path))
            parts = [doc[pno - 1].get_text("text")
                     for pno in range(chapter["start_page"], chapter["end_page"] + 1)
                     if 1 <= pno <= doc.page_count]
            doc.close()
        except Exception as e:
            return jsonify({"error": f"could not read textbook: {e}"}), 500
        source_text = "\n\n".join(parts).strip()
        if not source_text:
            return jsonify({"error": "no extractable text in that chapter"}), 400
        source_label = f"{textbook_id} — {chapter.get('title') or f'Chapter {idx + 1}'}"
    elif source_name:
        try:
            source_text = texts_mod.read_source_text(bqb.EVENT, source_name)
        except FileNotFoundError as e:
            return jsonify({"error": str(e)}), 400
        source_label = source_name
    else:
        return jsonify({"error": "no source selected"}), 400

    state = bqb._load_state()
    existing: list[dict] = []
    for qs in state.get("questions", {}).values():
        existing.extend(qs)

    # When chunking, n is per-chunk so the total is n * max_chunks
    per_chunk_n = max(1, -(-n // max_chunks)) if max_chunks > 1 else n

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        result = qgen.generate_questions(
            source_text=source_text,
            n=per_chunk_n,
            types=types,
            existing_questions=existing,
            max_chunks=max_chunks,
            keys=keys,
            should_cancel=should_cancel,
            on_progress=on_progress,
        )
        result["source"] = source_label
        result["existing_count"] = len(existing)
        return result

    try:
        job_id = jobs.submit_job(event_slug, "generate", f"Generate questions from {source_label}",
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/generate-similar", methods=["POST"])
def api_generate_similar(event_slug):
    """Use an existing question as a seed and ask Haiku to draft N variations
    or related questions on the same concept."""
    _select_event(event_slug)
    keys = _request_llm_keys()
    if not llm_providers.available_providers(keys):
        return jsonify({"error": "No LLM API key configured. Add one in Settings."}), 400
    data = request.get_json() or {}
    seed_text = (data.get("seed_text") or "").strip()
    seed_topic = (data.get("seed_topic") or "Other / General").strip()
    n = max(1, min(10, int(data.get("n", 3))))
    if not seed_text:
        return jsonify({"error": "no seed text"}), 400

    state = bqb._load_state()
    existing: list[dict] = []
    for qs in state.get("questions", {}).values():
        existing.extend(qs)
    # Treat the seed itself as something to avoid duplicating.
    seed_blob = (
        f"# Seed question (write {n} new ones on the same concept, not "
        f"variations of identical wording)\n\nTopic: {seed_topic}\n\n{seed_text}"
    )
    result = qgen.generate_questions(
        source_text=seed_blob, n=n, types=["mc", "short", "numerical"],
        existing_questions=existing + [{"text": seed_text, "number": "SEED"}],
        max_chunks=1, keys=keys,
    )
    result["source"] = "similar:" + seed_text[:60]
    result["existing_count"] = len(existing)
    return jsonify(result)


@app.route("/event/<event_slug>/api/generate/accept", methods=["POST"])
def api_generate_accept(event_slug):
    _select_event(event_slug)
    data = request.get_json() or {}
    accepted = data.get("candidates") or []
    source_name = data.get("source") or "llm_generated"
    if not accepted:
        return jsonify({"error": "no candidates"}), 400

    # All LLM-generated questions live in a synthetic "PDF" bucket so they
    # appear alongside real ones in the question bank and in the markdown.
    cache_key = f"_generated_{bqb.EVENT.slug}.pdf"
    # Reading the global next-Q# and writing the new questions must happen
    # in one transaction — otherwise two concurrent accept calls could both
    # compute the same next_num from the same stale snapshot and mint
    # colliding question numbers.
    with bqb._state_transaction() as state:
        bucket = list(state.get("questions", {}).get(cache_key, []))

        # Pick next available numeric Q#. Synthetic buckets (`_generated_*`,
        # `_scioly_*`) draw from the GLOBAL pool across every bucket in the event,
        # so a generated question can never collide with a PDF-extracted question
        # bearing the same number — even though the browse view already shows the
        # bucket badge, a globally-unique number means any backend op that ever
        # uses number-only (or any future export) is safe.
        next_num = _next_global_q_number(state)

        label = f"Generated · {source_name}"
        added = 0
        for cand in accepted:
            if not isinstance(cand, dict):
                continue
            bucket.append(qgen.candidate_to_question(cand, str(next_num), label))
            next_num += 1
            added += 1

        # Only persist the synthetic bucket if something actually landed in it —
        # otherwise an empty `_generated_<slug>.pdf` key lingers in state forever.
        if added:
            state.setdefault("questions", {})[cache_key] = bucket
            state.setdefault("manual", {})[cache_key] = {
                "edited_at": datetime.now().isoformat(timespec="seconds"),
            }
    return jsonify({"ok": True, "added": added, "bucket_total": len(bucket),
                    "bucket": cache_key})


@app.route("/event/<event_slug>/api/sources/import-generated", methods=["POST"])
def api_import_generated(event_slug):
    """Import externally-produced questions (another LLM, hand-written JSON)
    that follow the same candidate shape qgen.py emits.

    Body: raw JSON text -- either `{"candidates": [...]}` or a bare `[...]`
    array of candidate objects ({type?, topic?, text, choices?, answer?,
    rationale?, source_snippet?}, ...). Sent as raw text (not pre-validated
    client-side) because hand-typed/LLM-exported question banks routinely
    contain LaTeX (\theta, \frac{...}, ...) whose backslashes are valid
    LaTeX but invalid bare JSON escapes -- see `bqb._parse_json` /
    `bqb.repair_json_text` for the auto-repair ladder applied below.
    Query string: mark_validated=1 to stamp validation.status="correct".

    Runs the same normalisation + dedup pass as the Generate flow
    (strip point-markers, re-letter choices, Jaccard-dedup against the whole
    bank) before appending to the `_generated_<slug>.pdf` synthetic bucket.
    """
    _select_event(event_slug)
    raw_text = request.get_data(as_text=True) or ""
    mark_validated = request.args.get("mark_validated") in ("1", "true", "True")

    repaired = False
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = bqb._parse_json(raw_text)
        repaired = parsed is not None
        if parsed is None:
            try:
                json.loads(raw_text)
            except json.JSONDecodeError as e:
                return jsonify({"error": f"Invalid JSON (auto-repair couldn't fix it): {e}"}), 400

    raw_candidates = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return jsonify({"error": "no candidates"}), 400

    cache_key = f"_generated_{bqb.EVENT.slug}.pdf"
    # Same one-transaction requirement as api_generate_accept() above — the
    # global next-Q# read and the bucket write must not straddle two
    # separate lock acquisitions.
    with bqb._state_transaction() as state:
        bucket = list(state.get("questions", {}).get(cache_key, []))

        # Dedup against the ENTIRE bank (all buckets), same as the Generate flow.
        existing: list[dict] = []
        for qs in state.get("questions", {}).values():
            existing.extend(qs)

        next_num = _next_global_q_number(state)
        added = 0
        rejected_duplicates = 0
        rejected_invalid = 0
        accepted_this_batch: list[dict] = []
        added_questions: list[dict] = []
        skipped_validation_ungradeable: list[dict] = []

        for cand in raw_candidates:
            if not isinstance(cand, dict):
                rejected_invalid += 1
                continue
            text = (cand.get("text") or "").strip()
            if len(text) < 12:
                rejected_invalid += 1
                continue
            text = bqb._strip_points(text)
            ans = bqb._strip_points(cand.get("answer") or "")
            choices: list[dict] = []
            for c in (cand.get("choices") or []):
                if isinstance(c, dict):
                    ctxt = bqb._strip_points(c.get("text") or "")
                    if ctxt:
                        choices.append({"letter": (c.get("letter") or "").upper()[:1], "text": ctxt})
            for i, c in enumerate(choices):
                c["letter"] = chr(ord("A") + i)

            is_dup, matched = qgen.is_duplicate({"text": text}, existing + accepted_this_batch)
            if is_dup:
                rejected_duplicates += 1
                continue

            topic = cand.get("topic") or "Other / General"
            if topic not in bqb.EVENT.topics:
                topic = classify_topic(text) or "Other / General"

            # Carry through a textual diagram description if the external source
            # provided one — under either of two field names, since hand-written
            # / other-LLM JSON doesn't follow our internal "image_description"
            # naming. qgen.candidate_to_question() turns this into a pending
            # diagram hint that seeds the "Generate diagram" chat.
            image_description = (
                cand.get("image_description") or cand.get("image_context") or ""
            ).strip()

            normalized = {
                "type":              cand.get("type") or ("mc" if choices else "short"),
                "topic":             topic,
                "text":              text,
                "choices":           choices,
                "answer":            ans,
                "rationale":         (cand.get("rationale") or "").strip(),
                "source_snippet":    (cand.get("source_snippet") or "").strip()[:240],
                "image_description": image_description,
            }
            q = qgen.candidate_to_question(normalized, str(next_num), "Imported")
            if mark_validated:
                # Gradeability gate (build_question_bank.question_gradeability):
                # this is the fastest way to certify a whole pile of questions
                # in one click, so it's the chokepoint that matters most --
                # importing a prose-answer MCQ here and stamping it "correct"
                # is exactly how one of the 23 ungradeable questions in the
                # bank got certified in the first place. The question is
                # still imported either way (its content isn't the problem,
                # the correctness claim is) -- it's just left unvalidated
                # instead, and named in the response so the caller can see
                # what didn't get auto-certified.
                gradeable, reason = bqb.question_gradeability(q)
                if gradeable:
                    q["validation"] = {
                        "status":               "correct",
                        "correct_answer":       None,
                        "rationale":            normalized["rationale"] or "Marked validated on import.",
                        "source":               "Imported (manually marked validated)",
                        "validated_at":         datetime.now().isoformat(timespec="seconds"),
                        "model":                "import",
                        "text_at_validation":   q["text"][:300],
                        "answer_at_validation": q["answer"],
                    }
                else:
                    skipped_validation_ungradeable.append({"number": q["number"], "reason": reason})
            bucket.append(q)
            accepted_this_batch.append(normalized)
            added_questions.append(q)
            next_num += 1
            added += 1

        # Only persist the synthetic bucket if something actually landed in it —
        # otherwise an empty `_generated_<slug>.pdf` key lingers in state forever.
        if added:
            state.setdefault("questions", {})[cache_key] = bucket
            state.setdefault("manual", {})[cache_key] = {
                "edited_at": datetime.now().isoformat(timespec="seconds"),
            }
    return jsonify({
        "ok": True,
        "added": added,
        "repaired": repaired,
        "rejected_duplicates": rejected_duplicates,
        "rejected_invalid": rejected_invalid,
        "skipped_validation_ungradeable": skipped_validation_ungradeable,
        "bucket": cache_key,
        "bucket_total": len(bucket),
        "questions": [dict(q, _bucket=cache_key) for q in added_questions],
    })


# ---------------------------------------------------------------------------
# Routes — scio.ly/practice scraper
# ---------------------------------------------------------------------------

def _scioly_bucket_key() -> str:
    return f"_scioly_{bqb.EVENT.slug}.pdf"


def _next_global_q_number(state: dict) -> int:
    """Return the next numeric Q# that no question in any bucket already uses.

    Synthetic buckets (`_generated_*`, `_scioly_*`) feed off this so newly
    accepted questions never share a number with a PDF-extracted question or
    with each other across buckets. Trailing letter suffixes (`1`, `1b`, `1c`)
    are stripped before comparison.
    """
    used: set[int] = set()
    for qs in state.get("questions", {}).values():
        for q in qs or []:
            try:
                used.add(int(re.sub(r"[a-z]+$", "", str(q.get("number", "0")))))
            except (ValueError, TypeError):
                continue
    return (max(used) + 1) if used else 1


@app.route("/event/<event_slug>/api/scioly/scrape", methods=["POST"])
def api_scioly_scrape(event_slug):
    """
    Scrape candidates from scio.ly/practice and (optionally) validate each
    one with Haiku to filter out incomplete questions.

    Body:
      {
        "event_name": "<Sci-Oly event display name, e.g. 'Circuit Lab'>",  // scio.ly display name
        "count":      20,
        "types":      ["mcq", "frq"],
        "division":   "",                   // "B"|"C"|""
        "validate":   true
      }
    """
    _select_event(event_slug)
    data = request.get_json() or {}
    event_name = (data.get("event_name") or bqb.EVENT.name).strip()
    count = max(1, min(100, int(data.get("count", 20))))
    types = data.get("types") or ["mcq", "frq"]
    division = (data.get("division") or "").strip().upper()[:1]
    validate = bool(data.get("validate", True))
    focus = (data.get("focus") or "").strip()
    keys = _request_llm_keys()
    bucket_key = _scioly_bucket_key()

    def _target(should_cancel, on_progress):
        _job_target_setup(event_slug)
        on_progress(phase="fetching candidates from scio.ly")
        # Re-resolved inside the job (not captured from the enqueueing
        # request) so dedup runs against whatever's actually in the bank by
        # the time this job runs, not a stale snapshot from enqueue time.
        state = bqb._load_state()
        existing_ids = {
            q.get("_scioly_id")
            for q in state.get("questions", {}).get(bucket_key, [])
            if q.get("_scioly_id")
        }
        all_existing: list = []
        for qs in state.get("questions", {}).values():
            all_existing.extend(qs)

        result = scrape_scioly.scrape_questions(
            event_name=event_name,
            count=count,
            types=types,
            division=division,
            existing_scioly_ids=existing_ids,
            existing_questions=all_existing,
            focus=focus,
        )
        questions = result["questions"]
        can_validate = validate and bool(llm_providers.available_providers(keys))

        # Optionally run each through the validator so the user can drop the
        # obviously-broken ones (missing context, wrong answer in the data).
        if can_validate:
            for i, q in enumerate(questions):
                if should_cancel():
                    raise jobs.JobCancelled()
                on_progress(phase=f"validating {i+1}/{len(questions)}",
                           done=i, total=len(questions))
                try:
                    q["validation"] = validate_answer({
                        "text":    q["text"],
                        "answer":  q["answer"],
                        "choices": q["choices"],
                        "number":  q["number"],
                    }, keys=keys)
                except Exception as e:
                    q["validation"] = {"status": "unavailable",
                                       "rationale": f"validator error: {e}"}
                time.sleep(0.05)

        return {
            "candidates":          questions,
            "raw_count":           result["raw_count"],
            "fetched_per_type":    result["fetched_per_type"],
            "skipped_id_dups":     result["skipped_id_dups"],
            "rejected_text_dups":  result["rejected_text_dups"],
            "errors":              result["errors"],
            "validated":           can_validate,
            "event_name":          event_name,
            "scioly_in_bank":      len(existing_ids),
            "bank_total":          len(all_existing),
        }

    try:
        job_id = jobs.submit_job(event_slug, "scioly_scrape",
                                 f"Scrape scio.ly candidates for {event_name}",
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/scioly/accept", methods=["POST"])
def api_scioly_accept(event_slug):
    _select_event(event_slug)
    data = request.get_json() or {}
    accepted = data.get("candidates") or []
    if not accepted:
        return jsonify({"error": "no candidates"}), 400

    bucket_key = _scioly_bucket_key()
    # Same one-transaction requirement as api_generate_accept() above.
    with bqb._state_transaction() as state:
        bucket = list(state.get("questions", {}).get(bucket_key, []))

        # See api_generate_accept comment — synthetic-bucket numbers are drawn
        # from the event-wide pool so they don't collide with PDF-extracted ones.
        next_num = _next_global_q_number(state)

        added = 0
        for cand in accepted:
            if not isinstance(cand, dict):
                continue
            q = {
                "number":   str(next_num),
                "topic":    cand.get("topic") or "Other / General",
                "focus":    (cand.get("focus") or "").strip(),
                "text":     (cand.get("text") or "").strip(),
                "choices":  list(cand.get("choices") or []),
                "answer":   (cand.get("answer") or "").strip(),
                "images":   [],
                "source":   cand.get("source") or "scio.ly",
                "year":     cand.get("year", "") or "",
                "division": cand.get("division", "") or "",
                "page":     1,
                "_scioly_id": cand.get("_scioly_id"),
            }
            # Additive: only set when the scraped candidate actually carries
            # one (a re-scrape from before this field existed, or a
            # candidate the LLM/manual path added, has none -- stays unrated).
            cand_difficulty = cand.get("difficulty")
            if cand_difficulty is not None:
                try:
                    q["difficulty"] = float(cand_difficulty)
                except (TypeError, ValueError):
                    pass
            v = cand.get("validation")
            if v:
                q["validation"] = v
            # Carry quality_flag + reviewer_note from the UI editor so the user can
            # find these later in the bank and finish the review.
            flag = (cand.get("_flag") or cand.get("quality_flag") or "").strip()
            note = (cand.get("_reviewer_note") or cand.get("reviewer_note") or "").strip()
            if flag:
                q["quality_flag"] = flag        # "likely-wrong" | "definitely-wrong" | "needs-review"
            if note:
                q["reviewer_note"] = note
            bucket.append(q)
            next_num += 1
            added += 1

        # Only persist the synthetic bucket if something actually landed in it —
        # otherwise an empty `_scioly_<slug>.pdf` key lingers in state forever.
        if added:
            state.setdefault("questions", {})[bucket_key] = bucket
            state.setdefault("manual", {})[bucket_key] = {
                "edited_at": datetime.now().isoformat(timespec="seconds"),
            }
    return jsonify({"ok": True, "added": added,
                    "bucket_total": len(bucket), "bucket": bucket_key})


# ---------------------------------------------------------------------------
# HTML pages (single-file, embedded)
# ---------------------------------------------------------------------------


# Shared across every page template via Jinja globals — see templates/*.html
# `{{ common_css|safe }}` / `{{ common_js|safe }}`.
app.jinja_env.globals["common_css"] = _COMMON_CSS
app.jinja_env.globals["common_js"] = _COMMON_JS
# Lets templates hide destructive UI entirely rather than offering a
# button that would only 403 — see deletion.py.
app.jinja_env.globals["hard_delete_enabled"] = deletion.enabled

# Runs at import time, NOT inside main() — gunicorn imports this module's
# `app` object directly and never calls main() (see main()'s own comment
# below), so anything that must run in production has to live at module
# level like this, not inside the `if __name__ == "__main__"` dev-server path.
# Any job record still "running" here is leftover from the previous
# process's crash/restart; mark it "interrupted" before any request can see
# a job that's actually dead.
_n_recovered = jobs.recover_interrupted_jobs()
if _n_recovered:
    print(f"[startup] marked {_n_recovered} leftover 'running' job(s) as interrupted")

# Runs BEFORE the response backfill below, because that one looks for
# "assessment_responses.json" and a pre-rename instance still calls it
# "test_responses.json" — see assessments.migrate_test_to_assessment_names().
_renamed = assessments.migrate_test_to_assessment_names()
for _line in _renamed:
    print(f"[startup] rename migration: {_line}")

# One-time, idempotent: backfills the pre-redesign single-file
# assessment_responses.json into the current per-(assessment_id, username) file layout
# — see assessments.py's migrate_legacy_responses() docstring. No-ops instantly
# once the legacy file is gone (the common case after the first run).
_n_migrated = assessments.migrate_legacy_responses()
if _n_migrated:
    print(f"[startup] migrated {_n_migrated} response(s) from legacy assessment_responses.json "
          f"to per-test/per-student files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review UI for Sci-Oly question banks")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    # Catches the other way this could ship insecurely: someone runs the
    # dev server bound to a real network interface (not just localhost)
    # without ever setting FLASK_SECRET_KEY. (The production/gunicorn path
    # is covered separately above, keyed off SESSION_COOKIE_SECURE — that
    # check can't see --host at all since gunicorn never calls main().)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not _secret_key_set:
        sys.exit(
            f"FATAL: --host {args.host} is not loopback-only, but "
            "FLASK_SECRET_KEY is not set. Set it in .env (or the "
            "environment) before binding to a non-local address.")
    # Drain in-flight state writes on Ctrl+C so a save that's mid-flight
    # finishes cleanly instead of being abandoned at signal-time.
    bqb.install_graceful_shutdown()
    print(f"\nOpen http://{args.host}:{args.port}/ in your browser.")
    print("Press Ctrl+C to stop.\n")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
