"""
Flask review UI for Sci-Oly question banks (multi-event; see events.py).

Run:
  pip install flask
  python review_app.py [--port 5000]

Workflow:
  1. Browse all test PDFs (sort by name / modified / size / question count).
  2. Open a PDF to review it page by page.
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
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

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
import texts as texts_mod  # noqa: E402
import qgen  # noqa: E402
import scrape_scioly  # noqa: E402
import download_event  # noqa: E402
import llm_providers  # noqa: E402
import auth  # noqa: E402
import seasons  # noqa: E402
import testing  # noqa: E402
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

# Routes reachable without being logged in.
_PUBLIC_ENDPOINTS = {"login", "favicon", "static"}


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
    return None


@app.context_processor
def _inject_user():
    return {"current_user": getattr(g, "user", None)}


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
    Used by the Tests dashboard/builder routes, which both roles can reach
    (a volunteer only sees/acts on their own assigned tests; see
    _select_test for the finer-grained per-test check)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "user", None)
        if user is None or user.role not in ("coach", "volunteer"):
            abort(403, "Coach or volunteer access required")
        return view(*args, **kwargs)
    return wrapped


def _select_test(test_id: str) -> testing.Test:
    """Loads a Test, 404s if unknown, 403s unless the caller is a coach or
    appears in that test's window's assignments[event_slug].

    Deliberately independent of _select_event() — a Test spans
    season/window/event, and an assigned volunteer may have nothing in
    User.events at all (test-building assignment is a different grant than
    bank-edit access; conflating the two would either over- or under-grant).
    Routes that operate on a Test use this, never _select_event()."""
    test = testing.get_test(test_id)
    if test is None:
        abort(404, f"Unknown test: {test_id}")
    user = getattr(g, "user", None)
    if user is None:
        abort(403)
    if user.role == "coach":
        return test
    window = testing.get_window(test.window_id)
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
        next_url = request.args.get("next") or url_for("index")
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


# Bounded LRU for opened fitz.Document handles. Unbounded growth was a leak
# (60+ Anatomy PDFs ≈ tens of MB sticking around forever) AND a correctness
# trap — switching events kept stale doc handles keyed by bare filename, so a
# same-named PDF in a different event would return the wrong doc.
import collections as _collections
_PDF_CACHE_MAX = 16
_pdf_cache: "_collections.OrderedDict[tuple[str, str], fitz.Document]" = _collections.OrderedDict()


def _pdf_cache_evict_excess() -> None:
    while len(_pdf_cache) > _PDF_CACHE_MAX:
        _, doc = _pdf_cache.popitem(last=False)
        try:
            doc.close()
        except Exception:
            pass


def _pdf_cache_clear_event(slug: str) -> None:
    """Drop all cached PDFs belonging to one event. Called on _select_event."""
    keys = [k for k in _pdf_cache if k[0] == slug]
    for k in keys:
        try:
            _pdf_cache[k].close()
        except Exception:
            pass
        del _pdf_cache[k]


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
    # review.html/browse.html/quiz.html/sources.html/event_index.html for
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
    key = (bqb.EVENT.slug, name)
    doc = _pdf_cache.get(key)
    if doc is not None:
        _pdf_cache.move_to_end(key)
        return doc
    path = bqb.BASE_DIR / name
    if not path.exists():
        abort(404, "PDF not found")
    doc = fitz.open(str(path))
    _pdf_cache[key] = doc
    _pdf_cache_evict_excess()
    return doc


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
    one test* — browsed via the review page's target toggle, never fed to
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
    test_pdf = bqb.BASE_DIR / pdfname
    if not target or target == "test":
        return _open_pdf(pdfname)
    if target == "key":
        path = _key_path(test_pdf)
        if not path:
            abort(404, "No key PDF")
    else:
        candidate = _safe_join(bqb.BASE_DIR, target)
        if candidate not in _supplementary_docs(test_pdf):
            abort(404, "Not a supplementary document for this test")
        path = candidate
    cache_key = (bqb.EVENT.slug, path.name)
    if cache_key not in _pdf_cache:
        _pdf_cache[cache_key] = fitz.open(str(path))
        _pdf_cache_evict_excess()
    else:
        _pdf_cache.move_to_end(cache_key)
    return _pdf_cache[cache_key]


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
    "Show archived" section with an Unarchive action."""
    rows = []
    archived_rows = []
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
    change) and LLM API Keys; coaches additionally get Manage Users
    (the former standalone /admin/users page, now a section here) —
    one unified surface instead of a coach-only page plus a floating
    LLM-keys button every other page injected separately."""
    is_coach = g.user.role == "coach"
    users = sorted(auth.load_users().values(), key=lambda u: u.username) if is_coach else []
    all_events = sorted(EVENTS.keys()) if is_coach else []
    return render_template("settings.html", users=users, all_events=all_events)


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
# roster grid / which a TestWindow can be created against; it has zero
# effect on question-bank curation access (see seasons.py's docstring).
# ---------------------------------------------------------------------------

@app.route("/club")
@coach_required
def club_management_page():
    all_seasons = sorted(seasons.load_seasons().values(),
                          key=lambda s: s.season_id, reverse=True)
    current = seasons.get_current_season()
    selected_id = request.args.get("season") or (current.season_id if current else
                                                  (all_seasons[0].season_id if all_seasons else ""))
    selected = seasons.get_season(selected_id) if selected_id else None
    students = sorted(
        (u for u in auth.load_users().values() if u.role == "student" and not u.disabled),
        key=lambda u: (u.display_name or u.username),
    )
    roster = seasons.get_full_roster(selected_id) if selected else {}
    return render_template(
        "club_management.html",
        all_seasons=all_seasons,
        current_season_id=current.season_id if current else None,
        selected=selected,
        all_events=sorted(EVENTS.keys()),
        students=students,
        roster=roster,
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
# Routes — Tests dashboard + test-builder + publish (coach + volunteer)
#
# Deliberately NOT under /event/<slug>/... — see _select_test()'s docstring
# for why these never call _select_event(). A season's event lineup only
# scopes which events a window can be created against (seasons.py); it has
# zero effect on bank-curation access.
# ---------------------------------------------------------------------------

@app.route("/tests")
@coach_or_volunteer_required
def tests_dashboard_page():
    all_seasons = sorted(seasons.load_seasons().values(), key=lambda s: s.season_id, reverse=True)
    current = seasons.get_current_season()
    selected_id = request.args.get("season") or (current.season_id if current else
                                                  (all_seasons[0].season_id if all_seasons else ""))
    selected = seasons.get_season(selected_id) if selected_id else None

    windows = []
    if selected:
        for w in sorted(testing.load_windows().values(), key=lambda w: w.opens_at):
            if w.season_id != selected.season_id or w.archived:
                continue
            if g.user.role == "volunteer" and not any(
                g.user.username in (w.assignments.get(slug) or []) for slug in w.event_slugs
            ):
                continue
            window_tests = []
            for slug in w.event_slugs:
                t = testing.get_test_for(w.window_id, slug)
                if g.user.role == "volunteer" and g.user.username not in (w.assignments.get(slug) or []):
                    continue
                window_tests.append({"event_slug": slug, "test": t,
                                     "assigned": w.assignments.get(slug) or []})
            windows.append({"window": w, "tests": window_tests})

    volunteers = sorted((u.username for u in auth.load_users().values()
                         if u.role == "volunteer" and not u.disabled))
    return render_template(
        "tests_dashboard.html",
        all_seasons=all_seasons, selected=selected, windows=windows,
        all_volunteers=volunteers,
    )


@app.route("/api/test-windows", methods=["POST"])
@coach_required
def api_create_test_window():
    data = request.get_json() or {}
    try:
        w = testing.create_window(
            season_id=data.get("season_id", ""),
            opens_at=data.get("opens_at", ""), closes_at=data.get("closes_at", ""),
            event_slugs=data.get("event_slugs") or [],
            label=data.get("label", ""), created_by=g.user.username,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "window_id": w.window_id})


@app.route("/api/test-windows/<window_id>", methods=["PATCH"])
@coach_required
def api_update_test_window(window_id):
    data = request.get_json() or {}
    try:
        w = testing.update_window(
            window_id, label=data.get("label"), opens_at=data.get("opens_at"),
            closes_at=data.get("closes_at"), event_slugs=data.get("event_slugs"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/test-windows/<window_id>/assignments", methods=["PATCH"])
@coach_required
def api_update_test_assignments(window_id):
    data = request.get_json() or {}
    event_slug = data.get("event_slug", "")
    users = auth.load_users()
    usernames = [u for u in (data.get("usernames") or [])
                 if u in users and users[u].role == "volunteer"]
    try:
        testing.update_window_assignments(window_id, event_slug, usernames)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "usernames": usernames})


@app.route("/tests/<test_id>/build")
@coach_or_volunteer_required
def test_builder_page(test_id):
    test = _select_test(test_id)
    window = testing.get_window(test.window_id)
    ev = EVENTS.get(test.event_slug)
    return render_template(
        "test_builder.html",
        test_id=test_id, event_slug=test.event_slug,
        event_name=ev.name if ev else test.event_slug,
        window_label=window.label if window else "",
        status=test.status,
    )


@app.route("/api/tests/<test_id>", methods=["GET"])
@coach_or_volunteer_required
def api_get_test(test_id):
    test = _select_test(test_id)
    return jsonify({
        "test_id": test.test_id, "status": test.status, "kept": test.kept,
        "event_slug": test.event_slug, "window_id": test.window_id,
        "last_edited_by": test.last_edited_by, "last_edited_at": test.last_edited_at,
    })


@app.route("/api/tests/<test_id>", methods=["PATCH"])
@coach_or_volunteer_required
def api_update_test_kept(test_id):
    _select_test(test_id)
    data = request.get_json() or {}
    try:
        updated = testing.update_test_kept(test_id, data.get("kept") or [], edited_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "last_edited_by": updated.last_edited_by,
                    "last_edited_at": updated.last_edited_at})


@app.route("/tests/<test_id>/publish", methods=["POST"])
@coach_or_volunteer_required
def api_publish_test(test_id):
    _select_test(test_id)
    try:
        result = testing.publish_test(test_id, published_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "snapshot_count": len(result["test"].snapshot or []),
                    "skipped": result["skipped"]})


@app.route("/tests/<test_id>/go-live", methods=["POST"])
@coach_required
def api_go_live_test(test_id):
    try:
        testing.go_live_test(test_id, live_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/tests/<test_id>/unpublish", methods=["POST"])
@coach_required
def api_unpublish_test(test_id):
    """Reverts a published/live test to "building" for edits. Blocked once
    EITHER the class-wide window has opened OR any student response has a
    saved answer — a personal-makeup student could already be mid-test
    even before the class window opens, so both conditions are checked
    independently rather than just the window."""
    test = testing.get_test(test_id)
    if test is None:
        abort(404)
    window = testing.get_window(test.window_id)
    if window:
        from datetime import datetime as _dt, timezone as _tz
        opens = _dt.fromisoformat(window.opens_at)
        if opens.tzinfo is None:
            opens = opens.replace(tzinfo=_tz.utc)
        if _dt.now(_tz.utc) >= opens:
            return jsonify({"error": "the test window has already opened — can't un-publish"}), 400
    for resp in testing.get_responses_for_test(test_id).values():
        if resp.answers:
            return jsonify({"error": "a student has already saved an answer — can't un-publish"}), 400
    try:
        testing.unpublish_test(test_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/tests/<test_id>/overrides", methods=["POST"])
@coach_required
def api_set_test_override(test_id):
    data = request.get_json() or {}
    try:
        testing.set_test_overrides(
            test_id, data.get("student_username", ""),
            data.get("opens_at"), data.get("closes_at"),
            granted_by=g.user.username, reason=data.get("reason", ""),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/tests/<test_id>/overrides/<student_username>", methods=["DELETE"])
@coach_required
def api_revoke_test_override(test_id, student_username):
    try:
        testing.set_test_overrides(test_id, student_username, None, None, granted_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — student-facing "My Tests" surface
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


def _student_test_context(test_id: str):
    """404s an unknown test; 403s if the caller's role isn't student or
    they're not rostered on this test's event for this test's season.
    Returns (test, window, season)."""
    import seasons as seasons_mod

    test = testing.get_test(test_id)
    if test is None:
        abort(404, f"Unknown test: {test_id}")
    window = testing.get_window(test.window_id)
    if window is None:
        abort(404)
    user = g.user
    if test.event_slug not in seasons_mod.student_events(test.season_id, user.username):
        abort(403, "You're not rostered for this test's event this season")
    return test, window


@app.route("/my-tests")
@student_required
def my_tests_page():
    import seasons as seasons_mod
    from datetime import datetime as _dt, timezone as _tz

    season = seasons_mod.get_current_season()
    upcoming, current, past = [], [], []
    if season:
        my_events = set(seasons_mod.student_events(season.season_id, g.user.username))
        for w in testing.load_windows().values():
            if w.season_id != season.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                if slug not in my_events:
                    continue
                t = testing.get_test_for(w.window_id, slug)
                if t is None or t.status not in ("live", "closed", "graded", "released"):
                    continue
                resp = testing.get_response(t.test_id, g.user.username)
                entry = {"test": t, "window": w, "event_slug": slug, "response": resp}
                already_submitted = resp is not None and resp.status != "in_progress"
                if already_submitted or testing.is_window_past(t, w, g.user.username):
                    # Already submitted moves straight to Past even if the
                    # class-wide window is technically still open — nothing
                    # left to do, and the take-page itself blocks re-entry
                    # for exactly this reason.
                    past.append(entry)
                elif testing.is_window_open(t, w, g.user.username):
                    current.append(entry)
                else:
                    upcoming.append(entry)
    return render_template("my_tests.html", upcoming=upcoming, current=current, past=past,
                            season=season)


@app.route("/my-tests/<test_id>/take")
@student_required
def test_take_page(test_id):
    test, window = _student_test_context(test_id)
    if test.status != "live" or not testing.is_window_open(test, window, g.user.username):
        return redirect(url_for("my_tests_page"))
    existing = testing.get_response(test_id, g.user.username)
    if existing is not None and existing.status != "in_progress":
        # Already submitted — nothing left to do here even though the
        # class-wide window is still technically open; avoid a confusing
        # "took" page whose autosave silently rejects every edit.
        return redirect(url_for("my_tests_page"))
    ev = EVENTS.get(test.event_slug)
    return render_template("test_take.html", test_id=test_id,
                            event_name=ev.name if ev else test.event_slug)


@app.route("/api/my-tests")
@student_required
def api_my_tests():
    import seasons as seasons_mod
    season = seasons_mod.get_current_season()
    out = []
    if season:
        my_events = set(seasons_mod.student_events(season.season_id, g.user.username))
        for w in testing.load_windows().values():
            if w.season_id != season.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                if slug not in my_events:
                    continue
                t = testing.get_test_for(w.window_id, slug)
                if t is None or t.status not in ("live", "closed", "graded", "released"):
                    continue
                resp = testing.get_response(t.test_id, g.user.username)
                already_submitted = resp is not None and resp.status != "in_progress"
                if already_submitted or testing.is_window_past(t, w, g.user.username):
                    bucket = "past"
                elif testing.is_window_open(t, w, g.user.username):
                    bucket = "current"
                else:
                    bucket = "upcoming"
                out.append({
                    "test_id": t.test_id, "event_slug": slug, "window_label": w.label,
                    "opens_at": w.opens_at, "closes_at": w.closes_at, "bucket": bucket,
                    "response_status": resp.status if resp else None,
                    "released": resp.released if resp else False,
                })
    return jsonify({"tests": out})


@app.route("/api/my-tests/<test_id>/take")
@student_required
def api_take_test(test_id):
    test, window = _student_test_context(test_id)
    if test.status != "live" or not testing.is_window_open(test, window, g.user.username):
        abort(403, "This test isn't open right now")
    existing = testing.get_response(test_id, g.user.username)
    if existing is not None and existing.status != "in_progress":
        abort(403, "You've already submitted this test")
    resp = testing.start_or_get_response(test_id, g.user.username, len(test.snapshot or []))
    snapshot = test.snapshot or []
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


@app.route("/api/my-tests/<test_id>/answer", methods=["POST"])
@student_required
def api_save_test_answer(test_id):
    test, window = _student_test_context(test_id)
    if test.status != "live" or not testing.is_window_open(test, window, g.user.username):
        abort(403, "This test isn't open right now")
    data = request.get_json() or {}
    try:
        updated = testing.save_answer(test_id, g.user.username, data.get("number", ""),
                                      data.get("answer") or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "last_saved_at": updated.last_saved_at})


@app.route("/api/my-tests/<test_id>/submit", methods=["POST"])
@student_required
def api_submit_test(test_id):
    test, window = _student_test_context(test_id)
    if test.status != "live":
        abort(403, "This test isn't live")
    is_open = testing.is_window_open(test, window, g.user.username)
    is_past = testing.is_window_past(test, window, g.user.username)
    existing = testing.get_response(test_id, g.user.username)
    if not is_open:
        # Never discard already-autosaved work just because the window
        # closed at the wire — but a brand-new attempt with zero prior
        # activity past a fully-elapsed window (no override) has nothing
        # to clamp, so it's rejected outright.
        if not (is_past and existing and existing.answers):
            abort(403, "This test isn't open right now")
    try:
        updated = testing.submit_response(test_id, g.user.username, test.snapshot or [],
                                          late=(not is_open))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "status": updated.status})


# ---------------------------------------------------------------------------
# Routes — Grading + release (coach + assigned volunteer for grading;
# release itself is coach-only — a deliberate final checkpoint before
# students see anything, distinct from the grading work itself).
# ---------------------------------------------------------------------------

@app.route("/tests/<test_id>/grade")
@coach_or_volunteer_required
def test_grading_page(test_id):
    test = _select_test(test_id)
    ev = EVENTS.get(test.event_slug)
    return render_template("test_grading.html", test_id=test_id,
                            event_name=ev.name if ev else test.event_slug)


@app.route("/api/tests/<test_id>/grading")
@coach_or_volunteer_required
def api_get_grading(test_id):
    test = _select_test(test_id)
    snapshot_frqs = [q for q in (test.snapshot or []) if q.get("qtype") == "frq"]
    responses = {u: {"answers": r.answers, "manual_grade": r.manual_grade, "status": r.status}
                for u, r in testing.get_responses_for_test(test_id).items()}
    return jsonify({"snapshot_frqs": snapshot_frqs, "responses": responses,
                    "grading_complete": testing.test_grading_complete(test_id, test.snapshot or [])})


@app.route("/api/tests/<test_id>/grading/<student_username>/<number>", methods=["PATCH"])
@coach_or_volunteer_required
def api_set_manual_grade(test_id, student_username, number):
    test = _select_test(test_id)
    q = next((x for x in (test.snapshot or []) if str(x.get("number")) == str(number)), None)
    if q is None or q.get("qtype") != "frq":
        return jsonify({"error": "not a free-response question on this test"}), 400
    data = request.get_json() or {}
    try:
        points_earned = float(data.get("points_earned"))
    except (TypeError, ValueError):
        return jsonify({"error": "points_earned must be a number"}), 400
    try:
        testing.set_manual_grade(test_id, student_username, number, points_earned,
                                 float(q.get("max_points") or 1), graded_by=g.user.username,
                                 comment=data.get("comment", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/tests/<test_id>/release-grades", methods=["POST"])
@coach_required
def api_release_grades(test_id):
    test = testing.get_test(test_id)
    if test is None:
        abort(404)
    try:
        count = testing.release_grades(test_id, test.snapshot or [], released_by=g.user.username)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "released_count": count})


@app.route("/my-tests/<test_id>/results")
@student_required
def test_results_page(test_id):
    test, window = _student_test_context(test_id)
    resp = testing.get_response(test_id, g.user.username)
    if resp is None or not resp.released:
        abort(403, "Results aren't released yet")
    return _render_test_results(test, resp, viewer_is_self=True, student_username=g.user.username)


def _render_test_results(test: "testing.Test", resp: "testing.Response",
                         viewer_is_self: bool, student_username: str):
    """Shared by test_results_page (a student viewing their own released
    results) and score_detail_page (a coach/grading-volunteer drilling into
    a specific student's response from the Scores page)."""
    ev = EVENTS.get(test.event_slug)
    rows = []
    for q in (test.snapshot or []):
        number = str(q.get("number"))
        answer = resp.answers.get(number) or {}
        auto = resp.auto_grade.get(number)
        manual = resp.manual_grade.get(number)
        rows.append({"q": q, "answer": answer, "auto": auto, "manual": manual})
    total_earned = sum((r["auto"] or r["manual"] or {}).get("points_earned") or 0 for r in rows)
    total_possible = sum(float(q.get("max_points") or 1) for q in (test.snapshot or []))
    return render_template("test_results.html", event_name=ev.name if ev else test.event_slug,
                           rows=rows, total_earned=total_earned, total_possible=total_possible,
                           viewer_is_self=viewer_is_self, student_username=student_username)


# ---------------------------------------------------------------------------
# Routes — Scores page (every role, including students, can see named
# scores; response-detail drill-down is restricted — see
# can_view_response_detail()).
# ---------------------------------------------------------------------------

def can_view_response_detail(viewer: "auth.User", test: "testing.Test", student_username: str,
                             response: "testing.Response | None", window: "testing.TestWindow | None") -> bool:
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
    selected_id = request.args.get("season") or (current.season_id if current else
                                                  (all_seasons[0].season_id if all_seasons else ""))
    selected = seasons_mod.get_season(selected_id) if selected_id else None

    students_seen: dict[str, str] = {}
    columns = []  # [{test, window, event_slug, label}]
    grid = {}     # {username: {test_id: {earned, possible, pending, detail_ok}}}

    if selected:
        users = auth.load_users()
        for slug in selected.event_slugs:
            for u in seasons_mod.get_roster(selected.season_id, slug):
                students_seen[u] = u
        for w in testing.load_windows().values():
            if w.season_id != selected.season_id or w.archived:
                continue
            for slug in w.event_slugs:
                t = testing.get_test_for(w.window_id, slug)
                if t is None or not t.snapshot:
                    continue
                if not testing.test_grading_complete(t.test_id, t.snapshot):
                    continue
                columns.append({"test": t, "window": w, "event_slug": slug,
                               "label": f"{slug} — {w.label or w.opens_at[:10]}"})
                responses = testing.get_responses_for_test(t.test_id)
                for username, resp in responses.items():
                    if resp.status not in ("submitted", "auto_submitted_late"):
                        continue
                    earned = sum((resp.auto_grade.get(str(q.get("number"))) or
                                 resp.manual_grade.get(str(q.get("number"))) or {}).get("points_earned") or 0
                                 for q in t.snapshot)
                    possible = sum(float(q.get("max_points") or 1) for q in t.snapshot)
                    detail_ok = (user.role == "coach") or can_view_response_detail(
                        user, t, username, resp, w)
                    grid.setdefault(username, {})[t.test_id] = {
                        "earned": earned, "possible": possible,
                        "pending": not resp.released, "detail_ok": detail_ok,
                    }

    return render_template("scores.html", all_seasons=all_seasons, selected=selected,
                           columns=columns, students=sorted(students_seen.values()), grid=grid)


@app.route("/scores/<test_id>/<student_username>")
def score_detail_page(test_id, student_username):
    test = testing.get_test(test_id)
    if test is None:
        abort(404)
    window = testing.get_window(test.window_id)
    resp = testing.get_response(test_id, student_username)
    if resp is None:
        abort(404)
    if not can_view_response_detail(g.user, test, student_username, resp, window):
        abort(403, "You don't have access to this student's responses")
    return _render_test_results(test, resp, viewer_is_self=(g.user.username == student_username),
                                student_username=student_username)


@app.route("/event/<event_slug>/")
def event_index(event_slug):
    _select_event(event_slug)
    return render_template("event_index.html",
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name)


@app.route("/event/<event_slug>/review/<pdfname>")
def review(event_slug, pdfname):
    _select_event(event_slug)
    return render_template("review.html",
                            pdf_name=pdfname,
                            event_slug=event_slug,
                            event_name=bqb.EVENT.name)


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
def api_pdf(event_slug, pdfname):
    _select_event(event_slug)
    state = bqb._load_state()
    qs = state.get("questions", {}).get(pdfname, [])
    if qs and not all("page" in q for q in qs):
        _compute_pages(pdfname, qs)
        state.setdefault("questions", {})[pdfname] = qs
        bqb._save_state(state)
    doc = _open_pdf(pdfname)
    ann = state.get("annotations", {}).get(pdfname, {})
    key_path = _key_path(bqb.BASE_DIR / pdfname)
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
    """Backs review.html's dynamically-added target-toggle buttons (one per
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
    _select_event(event_slug)
    dpi = int(request.args.get("dpi", "120"))
    target = request.args.get("target", "test")
    doc = _open_target_pdf(pdfname, target)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return Response(pix.tobytes("png"), mimetype="image/png")


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
    state = bqb._load_state()
    state.setdefault("questions", {})[pdfname] = cleaned
    state.setdefault("manual", {})[pdfname] = {
        "edited_at": datetime.now().isoformat(timespec="seconds"),
    }
    ann = data.get("annotations") or {}
    if ann:
        state.setdefault("annotations", {})[pdfname] = ann
    bqb._save_state(state)
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


@app.route("/admin/jobs")
@coach_required
def admin_jobs_page():
    return render_template("admin_jobs.html")


@app.route("/api/cookies/status")
@coach_required
def api_cookies_status():
    """Backs the header's Anubis-cookie freshness badge (coach-only — it's
    an operational/server concern, not something a volunteer needs surfaced).
    Lets the coach scp a fresh `.scioly_cookies.json` from a Playwright-capable
    machine before a scioly.org download run silently starts failing
    mid-batch on a headless server that can't launch a browser itself."""
    return jsonify(download_event.cookie_expiry_status() or {})


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
    state = bqb._load_state()
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
    bqb._save_state(state)
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
    # automatically by _supplementary_docs()'s glob on the next review-page
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
                                 g.user.username, _target)
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
                                     _make_target(pdfname, bqb.BASE_DIR / pdfname))
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
    state = bqb._load_state()
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
    test_pdf = bqb.BASE_DIR / pdfname
    if not test_pdf.exists():
        abort(404)
    if data.get("manual_mode"):
        # Manual mode: skip auto-extraction; user will rebuild via region capture.
        # Annotations have already been wiped above. Just persist an empty question list.
        state.setdefault("questions", {})[pdfname] = []
        bqb._save_state(state)
        return jsonify({"ok": True, "n_questions": 0,
                        "discarded_annotations": True,
                        "manual_mode": True})
    bqb._save_state(state)  # persist the pre-job wipe before the job re-reads state

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
                                 g.user.username, _target)
    except jobs.JobQueueFull as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "job_id": job_id})


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
    state = bqb._load_state()
    if snap.get("annotations") is not None:
        state.setdefault("annotations", {})[pdfname] = snap["annotations"]
    if snap.get("manual") is not None:
        state.setdefault("manual", {})[pdfname] = snap["manual"]
    if snap.get("questions") is not None:
        state.setdefault("questions", {})[pdfname] = snap["questions"]
    bqb._save_state(state)
    return jsonify({"ok": True, "restored_from": filename})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>/ocr", methods=["POST"])
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
    all_imgs: list[str] = []
    for img in bqb.IMAGE_DIR.iterdir():
        if src_prefix.lower().replace("-", "_") in img.name.lower():
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


@app.route("/event/<event_slug>/api/q/<bucket>/<num>", methods=["PATCH"])
def api_patch_question(event_slug, bucket, num):
    """Apply a single-field edit to one question, without going through the
    review page. Used by the browse-page inline editor."""
    _select_event(event_slug)
    data = request.get_json() or {}
    state = bqb._load_state()
    bucket_qs = state.setdefault("questions", {}).get(bucket)
    if bucket_qs is None:
        return jsonify({"error": f"bucket not found: {bucket}"}), 404
    q = next((x for x in bucket_qs if str(x.get("number")) == str(num)), None)
    if not q:
        return jsonify({"error": f"question #{num} not in {bucket}"}), 404

    edited_fields: list[str] = []

    # Apply edits
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
    if "image_descriptions" in data and isinstance(data["image_descriptions"], dict):
        cleaned = {str(fn): str(d).strip() for fn, d in data["image_descriptions"].items()
                   if str(d or "").strip()}
        q["image_descriptions"] = cleaned
        edited_fields.append("image_descriptions")
    if "validation" in data:
        v = data["validation"]
        if v is None:
            # "(unset)" in the manual validation dropdown — clear any
            # existing AI or human verdict outright.
            q.pop("validation", None)
        elif isinstance(v, dict):
            status = v.get("status")
            if status is not None and status not in _VALIDATION_STATUSES:
                return jsonify({"error": f"invalid validation status: {status!r}"}), 400
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
    bqb._save_state(state)
    return jsonify({"ok": True, "question": q})


@app.route("/event/<event_slug>/api/q/<bucket>/<num>", methods=["DELETE"])
def api_delete_question(event_slug, bucket, num):
    _select_event(event_slug)
    state = bqb._load_state()
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
    bqb._save_state(state)
    return jsonify({"ok": True, "removed": str(num)})


def _find_question(state: dict, bucket: str, num: str) -> tuple[dict | None, list]:
    """Return (question_dict, bucket_list) for the (bucket, num) pair, or
    (None, []) when missing."""
    bucket_qs = state.get("questions", {}).get(bucket) or []
    for q in bucket_qs:
        if str(q.get("number")) == str(num):
            return q, bucket_qs
    return None, bucket_qs


def _slug_image_name(bucket: str, num: str, ext: str, kind: str = "img") -> str:
    """Build a stable on-disk filename for a question-attached image.
    `bucket` strips off the trailing `.pdf` plus the leading `_` so the file
    name reads cleanly: `circuitlab_2019_b_q5_img_a1b2c3d4.png`."""
    import secrets
    base = bucket
    if base.endswith(".pdf"):
        base = base[:-4]
    base = base.lstrip("_")
    return f"{base}_q{num}_{kind}_{secrets.token_hex(4)}.{ext.lstrip('.')}"


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
    state = bqb._load_state()
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
    bqb._save_state(state)
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
    state = bqb._load_state()
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
    bqb._save_state(state)
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

    state = bqb._load_state()
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
    bqb._save_state(state)
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


@app.route("/event/<event_slug>/api/export.<fmt>")
def api_export(event_slug, fmt):
    """Export the current bank — `fmt` is csv or json. Whole bank;
    the browse page sends the filtered subset of `q` UUIDs separately when
    a filter is active."""
    _select_event(event_slug)
    state = bqb._load_state()
    all_qs = []
    for bucket, qs in state.get("questions", {}).items():
        for q in qs:
            row = dict(q)
            row["_bucket"] = bucket
            all_qs.append(row)
    if fmt == "json":
        # "_contexts" carries the shared case-study passages/tables/diagrams
        # referenced by any question's context_id (bucket::id-namespaced —
        # see bqb._all_contexts) so a JSON consumer doesn't have to separately
        # reconstruct them from the review-page annotations.
        payload = {"questions": all_qs, "_contexts": bqb._all_contexts()}
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename={bqb.EVENT.slug}.json"})
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
                                 f"attachment; filename={bqb.EVENT.slug}.csv"})
    if fmt == "apkg":
        try:
            import genanki  # type: ignore
        except ImportError:
            return jsonify({
                "error": "genanki not installed. Run: pip install genanki",
            }), 400
        return _export_apkg(all_qs)
    if fmt == "pdf":
        try:
            import reportlab  # type: ignore  # noqa: F401
        except ImportError:
            return jsonify({
                "error": "reportlab not installed. Run: pip install reportlab",
            }), 400
        return _export_pdf(all_qs, bqb._all_contexts())
    return jsonify({"error": f"unsupported format: {fmt}"}), 400


def _export_pdf(all_qs: list[dict], context_lookup: dict | None = None) -> "Response":
    """Generate a printable PDF: questions front-to-back, answer key at the
    end. One question per logical block, page breaks honoured by reportlab's
    SimpleDocTemplate platypus flow."""
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
        title=f"{bqb.EVENT.name} question bank",
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

    def _e(s: str) -> str:
        # reportlab.Paragraph parses XML, so escape the basics.
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = [
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
                heading = "Case study" + (f": {ctx['title']}" if ctx.get("title") else "")
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
                block.append(Spacer(1, 6))
                story.append(KeepTogether(block))
                if q.get("qtype") == "matching":
                    answer_lines.append(f"Q{n}: {pairs_str}")
                else:
                    answer_lines.append(f"Q{n}: {_e(q.get('answer') or '—')}")

    # Answer key page
    story.append(PageBreak())
    story.append(Paragraph("Answer Key", h1))
    for line in answer_lines:
        story.append(Paragraph(line, body))

    doc.build(story)
    data = buf.getvalue()
    return Response(data,
                    mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f"attachment; filename={bqb.EVENT.slug}.pdf"})


def _export_apkg(all_qs: list[dict]) -> "Response":
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
                             f"attachment; filename={bqb.EVENT.slug}.apkg"})


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
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Eagerly create the event directory
    ev.base_dir.mkdir(exist_ok=True)
    return jsonify({
        "ok":   True,
        "slug": ev.slug,
        "name": ev.name,
        "url":  f"/event/{ev.slug}/",
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
                    mimetype="text/markdown; charset=utf-8")


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
    state = bqb._load_state()
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
        bqb._save_state(state)
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
    state = bqb._load_state()
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
        bqb._save_state(state)
    return jsonify({
        "ok": True,
        "added": added,
        "repaired": repaired,
        "rejected_duplicates": rejected_duplicates,
        "rejected_invalid": rejected_invalid,
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
    state = bqb._load_state()
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
        bqb._save_state(state)
    return jsonify({"ok": True, "added": added,
                    "bucket_total": len(bucket), "bucket": bucket_key})


# ---------------------------------------------------------------------------
# HTML pages (single-file, embedded)
# ---------------------------------------------------------------------------


# Shared across every page template via Jinja globals — see templates/*.html
# `{{ common_css|safe }}` / `{{ common_js|safe }}`.
app.jinja_env.globals["common_css"] = _COMMON_CSS
app.jinja_env.globals["common_js"] = _COMMON_JS

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
