"""
Guard against the class of bug that made student test-taking impossible
from the day it shipped: templates/assessment_take.html (then test_take.html) called `fetch()` with
`${APP_ROOT}` four times, but APP_ROOT was defined only inside
_user_badge.html — and the test-taking page is the one page that renders
no header, so it never included it. Every fetch threw "APP_ROOT is not
defined" and the page could not load a test at all.

Nothing caught it because it is invisible to Python: both files are
individually fine, and the contract between them lives only in whether one
happens to include the other. This test makes that contract explicit — any
template that *uses* APP_ROOT must also be able to *see* it.

Run with: `python -m pytest tests/test_template_app_root.py -q`
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
DEFINER = "_app_root.html"

# `{% include "x.html" %}` / `{% include 'x.html' %}`
_INCLUDE = re.compile(r"{%-?\s*include\s+[\"']([^\"']+)[\"']")
# A template defining it inline instead of via the partial (the admin app's
# dashboard does this; it's a separate app with its own base template).
_DEFINES = re.compile(r"APP_ROOT\s*=")
# Any use at all, including inside a JS template literal.
_USES = re.compile(r"APP_ROOT")


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _includes_of(path: Path) -> set[str]:
    return set(_INCLUDE.findall(path.read_text(encoding="utf-8")))


def _can_see_app_root(path: Path, depth: int = 0) -> bool:
    """True if this template defines APP_ROOT or (transitively) includes
    something that does."""
    if depth > 5:                      # cycle guard; nesting here is 1 deep
        return False
    text = path.read_text(encoding="utf-8")
    if path.name == DEFINER or _DEFINES.search(text):
        return True
    for inc in _INCLUDE.findall(text):
        child = TEMPLATES / inc
        if child.exists() and _can_see_app_root(child, depth + 1):
            return True
    return False


def test_the_definer_partial_exists():
    assert (TEMPLATES / DEFINER).exists(), (
        f"templates/{DEFINER} is the single definition site for APP_ROOT"
    )


def test_every_template_using_app_root_can_see_it():
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        if not _USES.search(text):
            continue
        if not _can_see_app_root(path):
            offenders.append(path.relative_to(TEMPLATES).as_posix())
    assert not offenders, (
        "these templates reference APP_ROOT but neither define it nor include "
        f"{DEFINER} (directly or transitively): {offenders}. "
        f'Add {{% include "{DEFINER}" %}} above the script that uses it.'
    )


def test_assessment_take_specifically_can_see_app_root():
    # Named explicitly because this is the page the bug actually broke, and
    # a student hitting it is the least likely person to be able to report
    # a useful console error.
    assert _can_see_app_root(TEMPLATES / "assessment_take.html")


def test_definition_is_idempotent_not_a_const():
    # A second <script> re-declaring `const APP_ROOT` is a SyntaxError that
    # takes down the whole page, so the definition must tolerate being
    # included twice.
    text = (TEMPLATES / DEFINER).read_text(encoding="utf-8")
    assert "window.APP_ROOT" in text
    assert not re.search(r"\bconst\s+APP_ROOT", text)
