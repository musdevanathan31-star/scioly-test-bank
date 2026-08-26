"""
Parse every page's inline JavaScript and fail on a syntax error.

Motivated by a real one that shipped: adding `await downloadServerExport(...)`
to the Browse export handler without making the handler `async` is a
SyntaxError, and a SyntaxError doesn't break the one feature — it aborts the
entire <script> block, so every button on that page silently stops working.
Python's tests can't see it, the template still renders 200, and the only
symptom is in the browser console.

Skipped when Node isn't installed, so it never blocks the server's
update-from-github validation run (which executes this suite as
qbank-deploy, on a box that has no reason to have Node).

Run with: `python -m pytest tests/test_page_js_syntax.py -q`
"""
from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node not installed — inline-JS syntax checking is skipped",
)

# <script> blocks with a src= are external files, not inline code.
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


@pytest.fixture(scope="module")
def rendered_pages(tmp_path_factory):
    """Every page a logged-in coach or student can reach, as HTML."""
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    tmp = tmp_path_factory.mktemp("jssweep")
    import os
    old_env = {k: os.environ.get(k) for k in ("DATA_ROOT", "FLASK_SECRET_KEY",
                                              "ALLOW_HARD_DELETE")}
    os.environ.update(DATA_ROOT=str(tmp), FLASK_SECRET_KEY="test",
                      ALLOW_HARD_DELETE="true")   # exercise the delete buttons too
    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    auth.create_user("stu1", "password123", "student")
    # A volunteer renders different branches of several pages -- the archive
    # hides its coach-only controls, and JS that reaches for them anyway
    # throws on the elements that are no longer there.
    auth.create_user("vol1", "password123", "volunteer", events=[slug])
    bqb.set_event(slug)
    # A real (tiny) PDF on disk, because the extract page resolves the file
    # before it renders — without one the route 404s and the sweep below
    # silently skips it. That is exactly what used to happen: the extract
    # page was never in `paths`, so the largest inline-JS surface in the app
    # (~3,300 lines, and the file most edits land in) was never syntax
    # checked despite this suite being treated as the gate for it.
    import fitz
    _doc = fitz.open()
    for _n in range(2):
        _pg = _doc.new_page()
        _pg.insert_text((72, 72), f"1. Sample question on page {_n + 1}")
    _doc.save(str(bqb.EVENT.base_dir / "s_test.pdf"))
    _doc.close()

    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["s_test.pdf"] = [
            {"number": "1", "text": "Q", "answer": "A", "qtype": "frq",
             "choices": [], "images": []}]
    seasons.create_season("2027", event_slugs=[slug], created_by="coach1")
    seasons.set_roster("2027", slug, ["stu1"])
    window = assessments.create_window("2027", "2027-01-01T09:00",
                                       "2099-01-01T11:00", [slug], label="W1")
    a = assessments.get_assessment_for(window.window_id, slug)
    assessments.update_assessment_kept(
        a.assessment_id, [{"bucket": "s_test.pdf", "number": "1", "max_points": 1}])
    assessments.publish_assessment(a.assessment_id, "coach1")

    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    paths = {
        "coach1": ["/", "/scores", "/assessments", "/club", "/admin/jobs", "/settings",
                   "/archive", "/archive/map",
                   f"/event/{slug}/", f"/event/{slug}/browse", f"/event/{slug}/sources",
                   f"/event/{slug}/quiz", f"/event/{slug}/jobs", f"/event/{slug}/scan",
                   f"/event/{slug}/extract/s_test.pdf",
                   f"/assessments/{a.assessment_id}/build",
                   f"/assessments/{a.assessment_id}/grade"],
        "stu1": ["/my-assessments", "/scores", "/settings"],
        "vol1": ["/archive", "/assessments", "/scores", "/settings",
                 f"/event/{slug}/", f"/event/{slug}/browse"],
    }
    pages = {}
    for who, page_paths in paths.items():
        c = review_app.app.test_client()
        c.post("/login", data={"username": who, "password": "password123"})
        for p in page_paths:
            r = c.get(p)
            if r.status_code == 200:
                pages[f"{who}:{p}"] = r.get_data(as_text=True)

    yield pages, tmp

    for k, v in old_env.items():
        if v is None:
            import os as _os
            _os.environ.pop(k, None)
        else:
            import os as _os
            _os.environ[k] = v
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_every_page_was_reachable(rendered_pages):
    pages, _tmp = rendered_pages
    # If a page 404s or 500s it silently drops out of the sweep, which would
    # make this file quietly stop covering it.
    assert len(pages) >= 26, sorted(pages)


def test_the_extract_page_is_actually_covered(rendered_pages):
    """Named explicitly because it is the biggest inline-JS surface in the
    app and the one most edits land in, yet it was absent from the sweep
    entirely until now — the route needs a real PDF on disk to render, and
    the collector skips anything that doesn't return 200, so its absence
    was invisible. A bare count assertion would not have caught that.
    """
    pages, _tmp = rendered_pages
    key = next((k for k in pages if "/extract/" in k), None)
    assert key, f"extract page missing from the sweep: {sorted(pages)}"
    html = pages[key]
    scripts = _INLINE_SCRIPT.findall(html)
    assert scripts, "extract page rendered but carried no inline <script>"
    assert sum(len(s) for s in scripts) > 20000, (
        "extract page's inline JS is suspiciously small — did it render a "
        "login redirect or an error page instead of the real page?")


def test_no_page_has_a_javascript_syntax_error(rendered_pages):
    pages, tmp = rendered_pages
    failures = []
    for name, html in sorted(pages.items()):
        scripts = _INLINE_SCRIPT.findall(html)
        if not scripts:
            continue
        target = tmp / "page.js"
        target.write_text("\n;\n".join(scripts), encoding="utf-8")
        result = subprocess.run(["node", "--check", str(target)],
                                capture_output=True, text=True)
        if result.returncode:
            detail = next((ln for ln in result.stderr.splitlines()
                           if "Error" in ln), result.stderr.strip()[:200])
            failures.append(f"{name}: {detail}")
    assert not failures, "inline JS failed to parse:\n  " + "\n  ".join(failures)


# ---------------------------------------------------------------------------
# Undefined helpers
#
# `node --check` parses; it does not resolve names. A page calling esc()
# without defining it parses perfectly and then throws
# "ReferenceError: esc is not defined" at runtime, aborting whatever handler
# touched it. That shipped on /archive, where the helper every other
# template defines locally had simply been left out.
#
# Static rather than executed: running these scripts would need a DOM. So
# this collects bare call sites -- name( with no dot in front -- and
# requires each one to be declared somewhere in the same page's bundle, or
# to be a browser/language builtin.
# ---------------------------------------------------------------------------

#: Followed by "(" in normal code without being function calls.
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "await", "of", "in", "do", "else", "delete", "void", "yield",
    "throw", "case", "instanceof", "with", "async", "get", "set",
    "constructor", "super", "this", "import", "export", "default",
}

#: Globals the browser or the language provides.
_BUILTINS = {
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "parseInt", "parseFloat", "isNaN", "isFinite", "alert", "confirm", "prompt",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "Array", "Object", "String", "Number", "Boolean", "Math", "JSON", "Date",
    "Promise", "RegExp", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "Error",
    "URL", "URLSearchParams", "FormData", "Blob", "File", "FileReader",
    "atob", "btoa", "structuredClone", "queueMicrotask", "requestAnimationFrame",
    "Image", "Audio", "AbortController", "IntersectionObserver",
    "MutationObserver", "ResizeObserver", "CustomEvent", "Event", "Intl",
    "BigInt", "Proxy", "Reflect", "TextEncoder", "TextDecoder", "print",
    "matchMedia", "getComputedStyle", "scrollTo", "open", "close", "Headers",
    "Request", "Response", "AbortSignal", "Notification", "WebSocket",
}

#: Provided by <script src=> tags, which this sweep deliberately does not
#: read -- they are third-party files, not code we wrote.
_EXTERNAL = {"renderMathInElement", "katex", "MathJax"}

_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_DECL = re.compile(
    r"(?:function\s*\*?\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|class\s+([A-Za-z_$][\w$]*))")
#: Callback parameters: a name declared only as an argument, e.g.
#: `function openJobProgress(slug, jobId, onDone)`, then invoked as onDone().
#: Over-approximates slightly by also matching `if (x) {`, which is harmless.
_SIGNATURE = re.compile(r"\(([^()]*)\)\s*(?:=>|\{)")
_ARROW = re.compile(r"([A-Za-z_$][\w$]*)\s*=>")
#: Object-method shorthand -- `onDone(cb){ ... }` is a definition that
#: looks exactly like a call.
_METHOD = re.compile(r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{")
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*")
#: Destructured lists, extra declarators, and assignment to a bare global.
_LOOSE_DECL = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:=[^=]|,)")


def _strip_comments_and_strings(js: str) -> str:
    """Blank out comments and string bodies, keeping code positions.

    Prose is full of "word (" — a comment saying "brittle (see below)" would
    otherwise read as a call to brittle(). Template literals keep their
    ${...} contents, which really are code.
    """
    out = []
    i, n = 0, len(js)
    #: A "/" starts a regex only where a value is expected. Without this,
    #: a pattern like /"/g is read as the start of a string and swallows
    #: everything up to the next quote -- which silently hid whole function
    #: declarations and made defined helpers look undefined.
    prev_significant = ""
    while i < n:
        ch = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt not in "/*" and (
                prev_significant == "" or prev_significant in "(,=:[!&|?{};+-*%~^<>"):
            i += 1
            in_class = False
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == "[":
                    in_class = True
                elif js[i] == "]":
                    in_class = False
                elif js[i] == "/" and not in_class:
                    break
                elif js[i] == "\n":
                    break
                i += 1
            i += 1
            while i < n and js[i] in "gimsuyd":     # flags
                i += 1
            out.append(" 0 ")
            prev_significant = "0"
            continue
        if not ch.isspace():
            prev_significant = ch
        if ch == "/" and nxt == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n and not (js[i] == "*" and js[i + 1:i + 2] == "/"):
                i += 1
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n and js[i] != quote:
                i += 2 if js[i] == "\\" else 1
            i += 1
            out.append('""')
            continue
        if ch == "`":
            # Walk the literal, emitting only what is inside ${ }.
            i += 1
            while i < n and js[i] != "`":
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == "$" and js[i + 1:i + 2] == "{":
                    i += 2
                    depth = 1
                    start = i
                    while i < n and depth:
                        if js[i] == "{":
                            depth += 1
                        elif js[i] == "}":
                            depth -= 1
                        i += 1
                    out.append(" " + _strip_comments_and_strings(js[start:i - 1]) + " ")
                    continue
                i += 1
            i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_no_page_calls_an_undefined_helper(rendered_pages):
    pages, _tmp = rendered_pages
    failures = []
    for name, html in sorted(pages.items()):
        bundle = "\n;\n".join(_INLINE_SCRIPT.findall(html))
        if not bundle:
            continue
        bundle = _strip_comments_and_strings(bundle)
        declared = {m for groups in _DECL.findall(bundle) for m in groups if m}
        declared |= set(_LOOSE_DECL.findall(bundle))
        declared |= set(_ARROW.findall(bundle))
        declared |= set(_METHOD.findall(bundle))
        for params in _SIGNATURE.findall(bundle):
            for part in params.split(","):
                m = _IDENT.match(part.strip().lstrip(".{[ "))
                if m:
                    declared.add(m.group(0))
        called = set(_CALL.findall(bundle))
        missing = sorted(called - declared - _BUILTINS - _KEYWORDS - _EXTERNAL)
        if missing:
            failures.append(f"{name}: {', '.join(missing)}")
    assert not failures, (
        "pages call helpers that are never defined:\n  " + "\n  ".join(failures))
