"""
The student-facing figure route is an allowlist, not a file server.

Students are deliberately blocked from `/event/<slug>/images/<fname>`
(_select_event 403s them) so practice-quiz/browse exposure can't leak
content bound for a future official test. That left them unable to load any
figure at all — including the diagram a grouped question set is built
around. `serve_assessment_image` is the narrow opening.

The property under test: entitlement to ONE assessment must never become a
directory listing of the whole event's images/. Only filenames that this
specific assessment's frozen snapshot references may be served, and the
allowlist must be derived from the snapshot rather than the live bank so
editing the bank mid-window can't change what a live test exposes.

Run with: `python -m pytest tests/test_assessment_images.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_app as ra          # noqa: E402
import assessments as am         # noqa: E402


def _assessment(snapshot, contexts=None):
    return am.Assessment(
        assessment_id="a1", window_id="w1", season_id="s1", event_slug="circuit_lab",
        snapshot=snapshot, snapshot_contexts=contexts or {},
    )


# ---------------------------------------------------------------------------
# _assessment_image_names — the allowlist itself
# ---------------------------------------------------------------------------

def test_collects_question_images():
    t = _assessment([{"number": "1", "images": ["a.png", "b.png"]}])
    assert ra._assessment_image_names(t) == {"a.png", "b.png"}


def test_collects_matching_cell_images():
    t = _assessment([{
        "number": "1", "qtype": "matching",
        "matching": {"left": [{"label": "1", "image": "L.png"}],
                     "right": [{"label": "A", "image": "R.png"}], "pairs": {}},
    }])
    assert ra._assessment_image_names(t) == {"L.png", "R.png"}


def test_collects_shared_context_images():
    """The motivating case: a circuit diagram attached to the shared context
    rather than to any one sub-question."""
    t = _assessment(
        [{"number": "1", "context_id": "ctx_1", "bucket": "b.pdf"}],
        {"b.pdf::ctx_1": {"id": "ctx_1", "text": "R1 and R2 in parallel",
                          "images": ["circuit.png"]}},
    )
    assert ra._assessment_image_names(t) == {"circuit.png"}


def test_collects_from_all_three_sources_at_once():
    t = _assessment(
        [{"number": "1", "images": ["q.png"], "context_id": "ctx_1", "bucket": "b.pdf",
          "qtype": "matching",
          "matching": {"left": [{"label": "1", "image": "L.png"}], "right": [], "pairs": {}}}],
        {"b.pdf::ctx_1": {"id": "ctx_1", "images": ["c.png"]}},
    )
    assert ra._assessment_image_names(t) == {"q.png", "L.png", "c.png"}


def test_empty_snapshot_allows_nothing():
    """A test with no figures must not become an open door."""
    assert ra._assessment_image_names(_assessment([])) == set()
    assert ra._assessment_image_names(_assessment(None)) == set()


def test_unreferenced_image_is_not_allowed():
    """The whole point: another question's figure, sitting in the same
    images/ directory, is not reachable through this assessment."""
    t = _assessment([{"number": "1", "images": ["mine.png"]}])
    names = ra._assessment_image_names(t)
    assert "someone_elses_secret_test_q9_pick_ab12cd34.png" not in names
    assert "mine.png" in names


def test_handles_missing_and_null_fields():
    """Snapshot entries are hand-editable JSON; absent/None fields must not
    raise on a route that runs for every image request."""
    t = _assessment(
        [{"number": "1"},
         {"number": "2", "images": None},
         {"number": "3", "matching": None},
         {"number": "4", "matching": {"left": None, "right": None}}],
        {"b::c": None},
    )
    assert ra._assessment_image_names(t) == set()


def test_traversal_style_name_is_not_in_allowlist():
    """Membership is checked before _safe_join, so a traversal attempt fails
    the allowlist first. (_safe_join is defense in depth, not the control.)"""
    t = _assessment([{"number": "1", "images": ["ok.png"]}])
    names = ra._assessment_image_names(t)
    for hostile in ("../auth_users.json", "..\\auth_users.json", "/etc/passwd"):
        assert hostile not in names
