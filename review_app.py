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
    vision_extract_region,
    process_pair, apply_annotations,
)
from events import EVENTS, get_event, add_custom_event, is_builtin, REPO_ROOT  # noqa: E402
import texts as texts_mod  # noqa: E402
import qgen  # noqa: E402
import scrape_scioly  # noqa: E402
import download_event  # noqa: E402
import llm_providers  # noqa: E402
import auth  # noqa: E402
import archive  # noqa: E402
import pdf_safety  # noqa: E402

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
                         samesite="Lax")
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


# In-memory background-job tracking for download_event runs. Keyed by job_id;
# each entry carries phase/log/done_count/finished. Survives only the current
# process lifetime — that's intentional (downloads are short).
_DOWNLOAD_JOBS: dict = {}

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
    if slug not in EVENTS:
        abort(404, f"Unknown event: {slug}")
    if EVENTS[slug].archived:
        abort(404, f"Event archived: {slug} — a coach can unarchive it from the landing page")
    user = getattr(g, "user", None)
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


def _key_path(test_pdf: Path) -> Path | None:
    k = test_pdf.parent / test_pdf.name.replace("_test.pdf", "_key.pdf")
    return k if k.exists() else None


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
            "base_dir": str(ev.base_dir),
            "is_builtin": is_builtin(slug),
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
# Routes — user management (coach-only)
# ---------------------------------------------------------------------------

@app.route("/admin/users")
@coach_required
def admin_users_page():
    users = sorted(auth.load_users().values(), key=lambda u: u.username)
    return render_template("admin_users.html", users=users,
                            all_events=sorted(EVENTS.keys()))


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
    return jsonify({
        "name": pdfname,
        "page_count": doc.page_count,
        "questions": qs,
        "topics": bqb.TOPICS,
        "foci":   list(bqb.EVENT.foci),
        "vision_available": _vision_available(),
        "has_key": _key_path(bqb.BASE_DIR / pdfname) is not None,
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


@app.route("/event/<event_slug>/api/pdf/<pdfname>/page/<int:pno>.png")
def api_render(event_slug, pdfname, pno):
    _select_event(event_slug)
    dpi = int(request.args.get("dpi", "120"))
    target = request.args.get("target", "test")
    if target == "key":
        kp = _key_path(bqb.BASE_DIR / pdfname)
        if not kp:
            abort(404, "No key PDF")
        key = (bqb.EVENT.slug, kp.name)
        if key not in _pdf_cache:
            _pdf_cache[key] = fitz.open(str(kp))
            _pdf_cache_evict_excess()
        else:
            _pdf_cache.move_to_end(key)
        doc = _pdf_cache[key]
    else:
        doc = _open_pdf(pdfname)
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


@app.route("/event/<event_slug>/api/download/start", methods=["POST"])
def api_download_start(event_slug):
    """Kick off a background download_event run for this event. Returns a
    job_id the client polls via /api/download/<job_id>."""
    import threading
    import time
    import uuid
    if event_slug not in EVENTS:
        return jsonify({"error": f"unknown event: {event_slug}"}), 404
    # Reject if a job is already running for this event
    for jid, job in _DOWNLOAD_JOBS.items():
        if job.get("event") == event_slug and not job.get("finished"):
            return jsonify({"error": "a download is already in progress for this event",
                            "job_id": jid}), 409
    job_id = uuid.uuid4().hex[:12]
    job = {
        "event":      event_slug,
        "phase":      "Starting…",
        "log":        [],
        "done_count": 0,
        "total":      0,
        "started_at": time.time(),
        "elapsed_s":  0,
        "finished":   False,
        "success":    False,
    }
    _DOWNLOAD_JOBS[job_id] = job

    def _capture_print(line: str):
        # Capture each printed line into the job log
        line = (line or "").rstrip()
        if not line:
            return
        job["log"].append(line)
        if len(job["log"]) > 500:
            job["log"] = job["log"][-500:]
        # Cheap heuristics to update phase + counts
        if line.startswith("Total targets:"):
            try:
                job["total"] = int(line.split(":", 1)[1].strip())
                job["phase"] = "Downloading PDFs…"
            except Exception:
                pass
        elif "OK  " in line or "SKIP" in line or "FAIL" in line or "BOT" in line or "ERR" in line:
            job["done_count"] = min(job["done_count"] + 1, job["total"] or job["done_count"] + 1)
        elif line.startswith("[bot-bypass]"):
            job["phase"] = "Solving Anubis bot challenge (browser window will open)…"
        elif line.startswith("Done:"):
            job["phase"] = "Finished"

    def _run():
        try:
            # Patch print to also append to the job log
            import builtins
            real_print = builtins.print

            def captured_print(*args, **kwargs):
                msg = " ".join(str(a) for a in args)
                _capture_print(msg)
                real_print(*args, **kwargs)
            try:
                builtins.print = captured_print
                ok = download_event.download_all(event_slug, skip_existing=True, bypass_bot=True)
                job["success"] = bool(ok)
            finally:
                builtins.print = real_print
        except Exception as e:
            job["log"].append(f"FATAL: {e}")
            job["success"] = False
        finally:
            job["finished"] = True
            job["elapsed_s"] = int(time.time() - job["started_at"])

    # Stamp elapsed periodically via a tiny watcher thread
    def _watch():
        while not job["finished"]:
            job["elapsed_s"] = int(time.time() - job["started_at"])
            time.sleep(0.5)

    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_watch, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/event/<event_slug>/api/download/<job_id>")
def api_download_status(event_slug, job_id):
    job = _DOWNLOAD_JOBS.get(job_id)
    if not job or job.get("event") != event_slug:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(job)


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
    if "test_file" not in request.files:
        return jsonify({"error": "no test PDF provided"}), 400
    test_f = request.files["test_file"]
    test_raw = (test_f.filename or "").strip()
    if not test_raw or not test_raw.lower().endswith(".pdf"):
        return jsonify({"error": "test file must be a PDF"}), 400

    base_dir = bqb.BASE_DIR
    prefix = bqb.EVENT.filename_prefix
    base_dir.mkdir(parents=True, exist_ok=True)

    def normalized_name(raw: str, suffix: str) -> str:
        stem = secure_filename(Path(raw).stem) or "upload"
        if stem.lower().endswith(f"_{suffix}"):
            stem = stem[: -(len(suffix) + 1)]
        base = stem if stem.lower().startswith(prefix.lower()) else f"{prefix}_{stem}"
        name = f"{base}_{suffix}.pdf"
        n = 1
        while (base_dir / name).exists():
            name = f"{base}_{n}_{suffix}.pdf"
            n += 1
        return name

    test_name = normalized_name(test_raw, "test")
    test_dest = base_dir / test_name
    test_f.save(str(test_dest))
    if not pdf_safety.looks_like_pdf(test_dest):
        test_dest.unlink(missing_ok=True)
        return jsonify({"error": "test file isn't a valid PDF (bad header)"}), 400

    key_dest = None
    key_f = request.files.get("key_file")
    if key_f and (key_f.filename or "").strip():
        if not key_f.filename.lower().endswith(".pdf"):
            return jsonify({"error": "answer key must be a PDF"}), 400
        # Share the test file's exact (already de-duped) base so the
        # existing _key_path() string-replace lookup finds it automatically.
        key_dest = base_dir / test_name.replace("_test.pdf", "_key.pdf")
        key_f.save(str(key_dest))
        if not pdf_safety.looks_like_pdf(key_dest):
            key_dest.unlink(missing_ok=True)
            return jsonify({"error": "answer key isn't a valid PDF (bad header)"}), 400

    state = bqb._load_state()
    qs = process_pair(test_dest, key_dest, state, _vision_available())
    _compute_pages(test_name, qs)
    state.setdefault("questions", {})[test_name] = qs
    bqb._save_state(state)
    return jsonify({"ok": True, "pdf_name": test_name, "n_questions": len(qs),
                    "has_key": key_dest is not None})


@app.route("/event/<event_slug>/api/pdf/<pdfname>/reprocess", methods=["POST"])
def api_reprocess(event_slug, pdfname):
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
    qs = process_pair(test_pdf, _key_path(test_pdf), state, _vision_available())
    _compute_pages(pdfname, qs)
    state.setdefault("questions", {})[pdfname] = qs
    bqb._save_state(state)
    return jsonify({"ok": True, "n_questions": len(qs),
                    "discarded_annotations": bool(data.get("discard_annotations"))})


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
    if target == "key":
        kp = _key_path(bqb.BASE_DIR / pdfname)
        if not kp:
            return jsonify({"error": "no key PDF"}), 404
        key = (bqb.EVENT.slug, kp.name)
        if key not in _pdf_cache:
            _pdf_cache[key] = fitz.open(str(kp))
            _pdf_cache_evict_excess()
        else:
            _pdf_cache.move_to_end(key)
        doc = _pdf_cache[key]
    else:
        doc = _open_pdf(pdfname)
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
    if target == "key":
        kp = _key_path(bqb.BASE_DIR / pdfname)
        if not kp:
            return jsonify({"error": "no key PDF"}), 404
        key = (bqb.EVENT.slug, kp.name)
        if key not in _pdf_cache:
            _pdf_cache[key] = fitz.open(str(kp))
            _pdf_cache_evict_excess()
        else:
            _pdf_cache.move_to_end(key)
        doc = _pdf_cache[key]
    else:
        doc = _open_pdf(pdfname)
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
    if target == "key":
        kp = _key_path(bqb.BASE_DIR / pdfname)
        if not kp:
            return jsonify({"error": "no key PDF"}), 404
        key = (bqb.EVENT.slug, kp.name)
        if key not in _pdf_cache:
            _pdf_cache[key] = fitz.open(str(kp))
            _pdf_cache_evict_excess()
        else:
            _pdf_cache.move_to_end(key)
        doc = _pdf_cache[key]
    else:
        doc = _open_pdf(pdfname)
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
    for qs in state.get("questions", {}).values():
        all_q.extend(qs)
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
    for bucket, qs in qs_by_pdf.items():
        # "Recently edited" works off the per-bucket edited_at timestamp.
        bucket_edited_at = (manual.get(bucket) or {}).get("edited_at", "")
        for q in qs:
            qcopy = dict(q)
            qcopy["_bucket"] = bucket
            qcopy["_synthetic_bucket"] = bucket.startswith("_")  # generated/scraped
            qcopy["_has_image"] = bool(qcopy.get("images"))
            qcopy["_is_mcq"]    = bool(qcopy.get("choices"))
            qcopy["_edited_at"] = bucket_edited_at
            v = qcopy.get("validation") or {}
            qcopy["_validation_status"] = v.get("status") if v else None
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
    if target == "key":
        kp = _key_path(bqb.BASE_DIR / pdfname)
        if not kp:
            return jsonify({"error": "no key PDF"}), 404
        key = (bqb.EVENT.slug, kp.name)
        if key not in _pdf_cache:
            _pdf_cache[key] = fitz.open(str(kp))
            _pdf_cache_evict_excess()
        else:
            _pdf_cache.move_to_end(key)
        doc = _pdf_cache[key]
    else:
        doc = _open_pdf(pdfname)
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
        return Response(json.dumps(all_qs, ensure_ascii=False, indent=2),
                        mimetype="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename={bqb.EVENT.slug}.json"})
    if fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["number","topic","focus","text","answer","choices",
                    "source","year","division","bucket","validation_status","rationale"])
        for q in all_qs:
            v = q.get("validation") or {}
            choices_flat = " | ".join(f"{c.get('letter','?')}. {c.get('text','')}"
                                      for c in q.get("choices") or [])
            w.writerow([
                q.get("number",""), q.get("topic",""), q.get("focus",""),
                q.get("text",""), q.get("answer",""), choices_flat,
                q.get("source",""), q.get("year",""), q.get("division",""),
                q.get("_bucket",""), v.get("status",""), v.get("rationale",""),
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
        return _export_pdf(all_qs)
    return jsonify({"error": f"unsupported format: {fmt}"}), 400


def _export_pdf(all_qs: list[dict]) -> "Response":
    """Generate a printable PDF: questions front-to-back, answer key at the
    end. One question per logical block, page breaks honoured by reportlab's
    SimpleDocTemplate platypus flow."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether,
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

    n = 0  # global counter for cross-referencing with the answer key
    answer_lines: list[str] = []
    for topic in sorted(by_topic.keys()):
        story.append(Paragraph(_e(topic), h2))
        for q in by_topic[topic]:
            n += 1
            block = [
                Paragraph(f"<b>Q{n}.</b> {_e(q.get('text',''))}", body),
            ]
            for c in q.get("choices") or []:
                block.append(Paragraph(
                    f"<b>{_e(c.get('letter','?'))}.</b> {_e(c.get('text',''))}",
                    choice_style,
                ))
            meta_bits = []
            if q.get("source"):  meta_bits.append(_e(q["source"]))
            if q.get("focus"):   meta_bits.append(f"focus: {_e(q['focus'])}")
            if meta_bits:
                block.append(Paragraph(" · ".join(meta_bits), meta_style))
            block.append(Spacer(1, 6))
            story.append(KeepTogether(block))
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
        if (q.get("choices") or []):
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
    # Reuse the saved scioly.org cookies if we have them
    cookies = None
    try:
        from download_event import _load_cookies
        cookies = _load_cookies()
    except Exception:
        pass
    try:
        out = texts_mod.scrape_wiki(bqb.EVENT, cookies=cookies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "ok": True,
        "path": out.name,
        "size": out.stat().st_size,
        "url":  bqb.EVENT.wiki_url,
    })


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

TEXTBOOKS_DIR = REPO_ROOT / "textbooks"


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
    result = qgen.generate_questions(
        source_text=source_text,
        n=per_chunk_n,
        types=types,
        existing_questions=existing,
        max_chunks=max_chunks,
        keys=keys,
    )
    result["source"] = source_label
    result["existing_count"] = len(existing)
    return jsonify(result)


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

    # Layer 1 (exact): dedup by scio.ly UUID against prior scio.ly scrapes
    state = bqb._load_state()
    bucket_key = _scioly_bucket_key()
    existing_ids = {
        q.get("_scioly_id")
        for q in state.get("questions", {}).get(bucket_key, [])
        if q.get("_scioly_id")
    }
    # Layer 2 (fuzzy): dedup by text similarity against the WHOLE bank
    # (PDF-extracted, LLM-generated, and prior scio.ly scrapes combined)
    all_existing: list = []
    for qs in state.get("questions", {}).values():
        all_existing.extend(qs)

    try:
        result = scrape_scioly.scrape_questions(
            event_name=event_name,
            count=count,
            types=types,
            division=division,
            existing_scioly_ids=existing_ids,
            existing_questions=all_existing,
            focus=focus,
        )
    except Exception as e:
        return jsonify({"error": f"scrape failed: {e}",
                        "candidates": []}), 500

    questions = result["questions"]
    keys = _request_llm_keys()
    can_validate = validate and bool(llm_providers.available_providers(keys))

    # Optionally run each through the validator so the user can drop the
    # obviously-broken ones (missing context, wrong answer in the data).
    if can_validate:
        import time as _time
        for q in questions:
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
            _time.sleep(0.05)

    return jsonify({
        "ok":                  True,
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
    })


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

_COMMON_CSS = """
:root{
  --bg:#fff; --bg-alt:#f7f7f8; --bg-card:#fff; --bg-input:#fff;
  --fg:#222; --fg-soft:#444; --muted:#888;
  --line:#e2e2e2; --line-soft:#f0f0f0;
  --accent:#0066cc; --ok:#1f8a4d; --warn:#b86b00; --bad:#c0392b;
  --hover:#f0f0f0; --row-hover:#fafbfc;
  --header-bg:#fff;
}
*{box-sizing:border-box}
body{font:14px -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
  margin:0;color:var(--fg);background:var(--bg-alt)}
header{padding:10px 18px;background:var(--header-bg);
  border-bottom:1px solid var(--line);
  display:flex;gap:12px;align-items:center;position:sticky;top:0;z-index:10}
header h1{margin:0;font-size:15px;font-weight:600;flex:1;color:var(--fg)}
header a{color:var(--fg-soft)}
button{padding:5px 11px;border:1px solid var(--line);background:var(--bg-card);
  color:var(--fg);border-radius:4px;cursor:pointer;font-size:13px;line-height:1.4}
button:hover{background:var(--hover)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.primary:hover{filter:brightness(1.08)}
button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
button.ghost{background:transparent;border:none;color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
a{color:var(--accent);text-decoration:none}
.muted{color:var(--muted);font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;
  font-size:11px;font-weight:600;letter-spacing:.2px}
.badge.processed{background:#e3f4e9;color:var(--ok)}
.badge.fresh{background:#fde7e6;color:var(--bad)}
.badge.edited{background:#fff3d7;color:var(--warn)}
.banner{padding:8px 18px;background:#fff8e1;border-bottom:1px solid #f3d870;
  font-size:13px;color:#5a4a00}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--line);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .8s linear infinite;vertical-align:middle}
@keyframes spin{100%{transform:rotate(360deg)}}
.skeleton{background:linear-gradient(90deg, var(--line-soft) 25%,
  var(--line) 50%, var(--line-soft) 75%);background-size:200% 100%;
  animation:skel 1.4s infinite;border-radius:4px;color:transparent}
@keyframes skel{0%{background-position:200% 0}100%{background-position:-200% 0}}
.skeleton-row{height:22px;margin:6px 0}

/* Toast log */
.toast-host{position:fixed;bottom:16px;right:16px;z-index:200;
  display:flex;flex-direction:column;gap:8px;max-width:340px}
.toast{background:var(--bg-card);color:var(--fg);border:1px solid var(--line);
  border-left:3px solid var(--accent);box-shadow:0 4px 14px rgba(0,0,0,.18);
  padding:8px 12px;border-radius:6px;font-size:13px;
  animation:toastin .22s ease;opacity:1;transition:opacity .25s}
.toast.err{border-left-color:var(--bad)}
.toast.warn{border-left-color:var(--warn)}
.toast.ok{border-left-color:var(--ok)}
.toast.fade{opacity:0}
@keyframes toastin{from{transform:translateX(20px);opacity:0}
  to{transform:translateX(0);opacity:1}}
.toast-history{position:fixed;bottom:16px;right:16px;z-index:201;
  background:var(--bg-card);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.25);padding:10px 14px;
  max-height:60vh;width:380px;overflow-y:auto;display:none}
.toast-history.on{display:block}
.toast-history h4{margin:0 0 6px 0;font-size:13px;color:var(--fg)}
.toast-history .item{font-size:12px;padding:5px 0;border-bottom:1px solid var(--line-soft);
  color:var(--fg-soft)}
.toast-history .item .ts{color:var(--muted);font-size:10px;margin-right:6px;
  font-variant-numeric:tabular-nums}
.toast-history .item.err{color:var(--bad)}
.toast-history .empty{color:var(--muted);font-size:12px;padding:6px 0}
.history-btn{background:transparent;border:1px solid var(--line);font-size:11px;
  color:var(--muted);padding:2px 7px;border-radius:10px;cursor:pointer}
.history-btn:hover{background:var(--hover);color:var(--fg-soft)}
/* Running Anthropic-spend badge. Polls /api/usage every 30s. */
.cost-badge{display:inline-block;background:#fff8e1;color:#7a5a00;
  border:1px solid #f3d870;font-size:11px;padding:2px 8px;border-radius:10px;
  font-weight:600;font-variant-numeric:tabular-nums;cursor:default}
.cost-badge.over{background:#fde7e6;color:#a02020;border-color:#f3a0a0}

/* Floating action bar (sticky bottom-right) */
.floating-actions{position:fixed;bottom:18px;right:18px;z-index:150;
  display:flex;gap:6px;background:var(--bg-card);border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;box-shadow:0 4px 18px rgba(0,0,0,.22)}
.floating-actions button{font-size:13px}

/* LLM API key settings panel (floating gear button, bottom-left) */
#llm_settings_btn{position:fixed;bottom:18px;left:18px;z-index:150;
  font-size:12px;padding:6px 12px;border-radius:8px;
  box-shadow:0 4px 14px rgba(0,0,0,.18)}
#llm_settings_modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
  z-index:400;align-items:center;justify-content:center;padding:20px}
#llm_settings_modal .box{background:var(--bg-card);color:var(--fg);border-radius:8px;
  padding:18px 22px;width:420px;max-width:95vw;max-height:90vh;overflow:auto;
  box-shadow:0 12px 40px rgba(0,0,0,.45)}
#llm_settings_modal label{display:block;margin-bottom:10px;font-size:13px}
#llm_settings_modal input{display:block;width:100%;margin-top:4px;padding:7px 9px;
  border:1px solid var(--line);border-radius:4px;font:inherit;
  background:var(--bg-input);color:var(--fg)}
"""

# Common JS helpers injected into every page. Provides:
#  - toast(msg, kind?)        — append to the toast host + record in history
#  - setStatus(msg, kind?)    — back-compat shim that pipes through toast()
#  - hotkey(combo, handler)   — `Ctrl+S`, `Esc`, `/`, etc.
_COMMON_JS = r"""
// ---- toast / history ---------------------------------------------------
(function(){
  if(document.getElementById("toast-host")) return;
  const host = document.createElement("div");
  host.id = "toast-host"; host.className = "toast-host";
  document.body.appendChild(host);
  const hist = document.createElement("div");
  hist.id = "toast-history"; hist.className = "toast-history";
  hist.innerHTML = '<h4>Recent messages</h4><div id="toast-history-items"></div>';
  document.body.appendChild(hist);
})();
window._toastLog = [];
// Default to TEXT semantics so server-supplied strings can't inject HTML.
// Callers that need rich markup (e.g. inline <b>) pass {html: true} —
// they're responsible for escaping any interpolated user data themselves.
window.toast = function(msg, kind, opts){
  if(!msg) return;
  const host = document.getElementById("toast-host");
  if(!host) return;
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  if(opts && opts.html){
    el.innerHTML = msg;
  } else {
    el.textContent = msg;
  }
  host.appendChild(el);
  // record in history (keep last 30, plain-text)
  const txt = el.textContent.trim();
  if(txt){
    const now = new Date();
    const ts = now.toLocaleTimeString();
    window._toastLog.push({ts, msg: txt, kind: kind||""});
    if(window._toastLog.length > 30) window._toastLog.shift();
  }
  // auto-dismiss after 4.5s (errors stay 8s)
  const lifespan = (kind === "err") ? 8000 : 4500;
  setTimeout(() => { el.classList.add("fade"); }, lifespan - 250);
  setTimeout(() => { if(el.parentNode) el.parentNode.removeChild(el); }, lifespan);
};
function renderHistory(){
  const root = document.getElementById("toast-history-items");
  if(!root) return;
  if(!window._toastLog.length){
    root.innerHTML = '<div class="empty">No messages yet.</div>'; return;
  }
  root.innerHTML = window._toastLog.slice().reverse().map(e =>
    `<div class="item ${e.kind}"><span class="ts">${e.ts}</span>${
       (e.msg||"").replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`).join("");
}
window.toggleToastHistory = function(){
  const el = document.getElementById("toast-history");
  if(!el) return;
  const showing = el.classList.toggle("on");
  if(showing) renderHistory();
};
// Back-compat: the old setStatus() targeted #status. Keep that path working,
// but ALSO funnel into the toast log so the message is preserved.
//
// Default: TEXT semantics. Inline spinners and other markup need explicit
// opt-in via window.setStatusHtml(...) — auto-detection of "<" in msg as
// the HTML signal was tried but proved brittle (`<5` etc).
window.setStatus = function(msg, kind){
  const el = document.getElementById("status");
  if(el){
    el.textContent = msg || "";
    el.style.color = kind === "err" ? "var(--bad)" : "var(--muted)";
  }
  window.toast(msg, kind);
};
window.setStatusHtml = function(html, kind){
  const el = document.getElementById("status");
  if(el){
    el.innerHTML = html || "";
    el.style.color = kind === "err" ? "var(--bad)" : "var(--muted)";
  }
  window.toast(html, kind, {html: true});
};
// Tiny HTML-escape helper — used wherever a message wants to mix safe markup
// with untrusted server strings.
window.escHtml = function(s){
  return (s == null ? "" : String(s))
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
};
// ---- hotkey registration ----------------------------------------------
window._hotkeys = {};
window.hotkey = function(combo, handler, opts){
  window._hotkeys[combo.toLowerCase()] = {handler, opts: opts || {}};
};
document.addEventListener("keydown", function(e){
  // Ignore typing in inputs unless the hotkey is explicitly meta-modifier
  const tag = (e.target.tagName || "").toUpperCase();
  const inField = (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
                   || e.target.isContentEditable);
  const parts = [];
  if(e.ctrlKey || e.metaKey) parts.push("ctrl");
  if(e.shiftKey) parts.push("shift");
  if(e.altKey)   parts.push("alt");
  const k = (e.key || "").toLowerCase();
  parts.push(k === " " ? "space" : k);
  const combo = parts.join("+");
  const reg = window._hotkeys[combo];
  if(!reg) return;
  if(inField && !reg.opts.global) return;
  e.preventDefault();
  reg.handler(e);
});
// ---- Anthropic spend badge --------------------------------------------
// Polls /api/usage every 30s and updates the #cost-badge in the header. The
// goal isn't precise billing — it's an "uh oh" surface so an accidentally-
// running scrape becomes visible before the invoice arrives.
async function refreshCostBadge(){
  const el = document.getElementById("cost-badge");
  if(!el) return;
  try {
    const r = await fetch("/api/usage");
    const j = await r.json();
    const cost = Number(j.estimated_cost_usd || 0);
    el.textContent = "$" + cost.toFixed(cost >= 1 ? 2 : 3);
    el.title = (
      "Anthropic API spend this process\\n" +
      `Calls: ${j.calls}\\n` +
      `Input tokens: ${j.input_tokens.toLocaleString()}\\n` +
      `Output tokens: ${j.output_tokens.toLocaleString()}\\n` +
      `Rate: $${j.input_price_per_mtok}/MTok in, $${j.output_price_per_mtok}/MTok out`
    );
    el.classList.toggle("over", cost >= 5);
  } catch(e){ /* ignore — badge stays at the last known value */ }
}
refreshCostBadge();
setInterval(refreshCostBadge, 30000);
// ---- page title management --------------------------------------------
window._titleBase = document.title;
window.setPageTitlePrefix = function(prefix){
  document.title = (prefix ? prefix + " · " : "") + window._titleBase;
};

// ---- LLM API key settings -----------------------------------------------
// Lets the user supply their OWN API keys for Anthropic, OpenAI, Gemini,
// DeepSeek, and Mistral. Keys live ONLY in this browser's localStorage —
// never written to any file or sent anywhere except as the X-LLM-Keys
// header on this app's own same-origin /api/ requests, so the backend can
// use them (with automatic fallback to the next provider if the current
// one is out of credits, rate-limited, or invalid) instead of the server's
// own .env key.
const LLM_KEYS_STORAGE = "llm_api_keys";
const LLM_PROVIDERS = [
  {id: "anthropic", label: "Anthropic (Claude)"},
  {id: "openai",    label: "OpenAI (GPT)"},
  {id: "gemini",    label: "Google Gemini"},
  {id: "deepseek",  label: "DeepSeek"},
  {id: "mistral",   label: "Mistral"},
];
window.getLLMKeys = function(){
  try {
    const raw = JSON.parse(localStorage.getItem(LLM_KEYS_STORAGE) || "{}");
    const out = {};
    for(const p of LLM_PROVIDERS) if(raw[p.id]) out[p.id] = raw[p.id];
    return out;
  } catch(e){ return {}; }
};
window.setLLMKeys = function(keys){
  localStorage.setItem(LLM_KEYS_STORAGE, JSON.stringify(keys || {}));
};
(function(){
  if(document.getElementById("llm_settings_btn")) return;

  const btn = document.createElement("button");
  btn.id = "llm_settings_btn";
  btn.textContent = "⚙ LLM keys";
  btn.title = "Set your own API keys for Anthropic / OpenAI / Gemini / "
    + "DeepSeek / Mistral (stored only in this browser, never on the server)";
  document.body.appendChild(btn);

  const modal = document.createElement("div");
  modal.id = "llm_settings_modal";
  const rowsHtml = LLM_PROVIDERS.map(p => `
    <label>${escHtml(p.label)}
      <input type="password" data-llm-key="${p.id}" autocomplete="off">
    </label>`).join("");
  modal.innerHTML = `
    <div class="box">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <h3 style="margin:0;font-size:15px">LLM API keys</h3>
        <button id="llm_settings_close">✕ Close</button>
      </div>
      <p class="muted" style="margin:0 0 14px 0">
        Stored only in this browser (localStorage) — never written to any file
        on the server. If you set more than one, the app tries them in this
        order (Anthropic → OpenAI → Gemini → DeepSeek → Mistral)
        and automatically falls back to the next one if the current key is out
        of credits, rate-limited, or invalid.
      </p>
      ${rowsHtml}
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">
        <button id="llm_settings_clear">Clear all</button>
        <button id="llm_settings_save" class="primary">Save</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  function openModal(){
    const keys = getLLMKeys();
    modal.querySelectorAll("[data-llm-key]").forEach(inp => {
      inp.value = keys[inp.dataset.llmKey] || "";
    });
    modal.style.display = "flex";
  }
  function closeModal(){ modal.style.display = "none"; }
  btn.addEventListener("click", openModal);
  modal.querySelector("#llm_settings_close").addEventListener("click", closeModal);
  modal.addEventListener("click", e => { if(e.target === modal) closeModal(); });
  modal.querySelector("#llm_settings_save").addEventListener("click", () => {
    const keys = {};
    modal.querySelectorAll("[data-llm-key]").forEach(inp => {
      const v = inp.value.trim();
      if(v) keys[inp.dataset.llmKey] = v;
    });
    setLLMKeys(keys);
    closeModal();
    window.toast(
      Object.keys(keys).length
        ? `Saved ${Object.keys(keys).length} API key(s) to this browser.`
        : "No API keys saved — the app will use the server's own key, if any.",
      "ok",
    );
  });
  modal.querySelector("#llm_settings_clear").addEventListener("click", () => {
    if(!confirm("Remove all saved API keys from this browser?")) return;
    setLLMKeys({});
    modal.querySelectorAll("[data-llm-key]").forEach(inp => inp.value = "");
    window.toast("Cleared all saved API keys.", "ok");
  });
})();

// Auto-attach saved LLM keys to every same-origin /api/ request, so a key
// entered once in Settings reaches every current AND future LLM-backed
// endpoint without each call site needing to remember to do it.
(function(){
  const origFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    init = init || {};
    try {
      const url = typeof input === "string" ? input
        : (input && input.url) || "";
      if(url.indexOf("/api/") !== -1){
        const keys = getLLMKeys();
        if(Object.keys(keys).length){
          const baseHeaders = init.headers
            || (input && typeof input !== "string" && input.headers)
            || {};
          const headers = new Headers(baseHeaders);
          headers.set("X-LLM-Keys", JSON.stringify(keys));
          init = Object.assign({}, init, {headers});
        }
      }
    } catch(e){ /* never let key-attachment break a request */ }
    return origFetch(input, init);
  };
})();

// Auto-attach the CSRF double-submit-cookie token (see _check_csrf in
// review_app.py) to every mutating request, so no call site needs to
// remember to do it. GET/HEAD requests are never checked server-side, so
// they're skipped here too.
(function(){
  const origFetch = window.fetch.bind(window);
  function getCsrfToken(){
    const m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? m[1] : null;
  }
  window.fetch = function(input, init){
    init = init || {};
    try {
      const method = (init.method || (input && typeof input !== "string" && input.method) || "GET").toUpperCase();
      if(method !== "GET" && method !== "HEAD"){
        const token = getCsrfToken();
        if(token){
          const baseHeaders = init.headers
            || (input && typeof input !== "string" && input.headers)
            || {};
          const headers = new Headers(baseHeaders);
          headers.set("X-CSRF-Token", token);
          init = Object.assign({}, init, {headers});
        }
      }
    } catch(e){ /* never let CSRF-attachment break a request */ }
    return origFetch(input, init);
  };
})();
"""

# Shared across every page template via Jinja globals — see templates/*.html
# `{{ common_css|safe }}` / `{{ common_js|safe }}`.
app.jinja_env.globals["common_css"] = _COMMON_CSS
app.jinja_env.globals["common_js"] = _COMMON_JS


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
