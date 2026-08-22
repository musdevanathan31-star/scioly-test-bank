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
    bqb.set_event(slug)
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
                   f"/event/{slug}/", f"/event/{slug}/browse", f"/event/{slug}/sources",
                   f"/event/{slug}/quiz", f"/event/{slug}/jobs", f"/event/{slug}/scan",
                   f"/assessments/{a.assessment_id}/build",
                   f"/assessments/{a.assessment_id}/grade"],
        "stu1": ["/my-assessments", "/scores", "/settings"],
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
    assert len(pages) >= 17, sorted(pages)


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
