"""
Multi-correct-answer MCQ support.

Two things drove this: (1) the review card now sets an MCQ's answer by
ticking choices instead of typing a letter, and (2) scanning every
.qbank_state.json found two real MCQs already in the bank with more than one
correct letter ("A, D, E" and "A, B") -- both were being silently mis-graded
by code that only looked at the first character of the answer string.

Covers:
  - text_utils.parse_answer_letters() -- the one place the "A, D, E" parsing
    rule lives, shared by every grading/publishing path.
  - assessments._grade_mcq() -- now set-based, all-or-nothing for multi.
  - assessments._snapshot_one_question() -- the select_multiple flag, set
    only for genuine multi-answer MCQs.
  - A regression pin that the student take payload still never leaks
    correct_answer, even now that select_multiple rides along with it.

Run with: `python -m pytest tests/test_mcq_multi_answer.py -q`
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from text_utils import parse_answer_letters   # noqa: E402
import assessments as am                       # noqa: E402

PAST = "2020-01-01T00:00:00"
FUTURE = "2099-01-01T00:00:00"

CHOICES = [{"letter": "A", "text": "one"}, {"letter": "B", "text": "two"},
           {"letter": "C", "text": "three"}, {"letter": "D", "text": "four"}]


# ---------------------------------------------------------------------------
# parse_answer_letters
# ---------------------------------------------------------------------------

def test_parse_single_letter():
    assert parse_answer_letters("A", CHOICES) == {"A"}


def test_parse_multi_letter_with_spaces():
    assert parse_answer_letters("A, B", CHOICES) == {"A", "B"}


def test_parse_multi_letter_no_spaces():
    assert parse_answer_letters("A,B", CHOICES) == {"A", "B"}


def test_parse_lowercase():
    assert parse_answer_letters("a, d, e", [{"letter": "A"}, {"letter": "D"}, {"letter": "E"}]) == {"A", "D", "E"}


def test_parse_prose_returns_empty():
    assert parse_answer_letters("12 volts", CHOICES) == set()


def test_parse_letter_with_no_matching_choice_excluded():
    # "E" isn't among CHOICES' letters (A-D) -- dropped, "A" kept.
    assert parse_answer_letters("A, E", CHOICES) == {"A"}


def test_parse_empty_and_none():
    assert parse_answer_letters("", CHOICES) == set()
    assert parse_answer_letters(None, CHOICES) == set()


def test_parse_no_choices_list():
    # No choices to validate against -- every letter is dropped, same as a
    # stale/unmatched letter would be.
    assert parse_answer_letters("A", []) == set()
    assert parse_answer_letters("A", None) == set()


# ---------------------------------------------------------------------------
# assessments._grade_mcq — single-letter must match today's behavior exactly
# ---------------------------------------------------------------------------

def test_grade_mcq_single_letter_correct():
    g = am._grade_mcq("A", "A", CHOICES)
    assert g["correct"] is True
    assert g["points_earned"] == 1.0
    assert g["points_possible"] == 1.0


def test_grade_mcq_single_letter_incorrect():
    g = am._grade_mcq("B", "A", CHOICES)
    assert g["correct"] is False
    assert g["points_earned"] == 0.0


def test_grade_mcq_single_letter_no_pick():
    g = am._grade_mcq(None, "A", CHOICES)
    assert g["correct"] is False


def test_grade_mcq_multi_all_correct():
    g = am._grade_mcq("A, D", "A, D", CHOICES)
    assert g["correct"] is True
    assert g["points_earned"] == 1.0


def test_grade_mcq_multi_all_correct_any_order_and_spacing():
    g = am._grade_mcq("D,A", "A, D", CHOICES)
    assert g["correct"] is True


def test_grade_mcq_multi_partial_scores_zero():
    # Picked only A of A+D -- all-or-nothing, no partial credit for MCQ
    # (unlike matching, which does partial-credit per pair).
    g = am._grade_mcq("A", "A, D", CHOICES)
    assert g["correct"] is False
    assert g["points_earned"] == 0.0


def test_grade_mcq_multi_extra_wrong_pick_scores_zero():
    g = am._grade_mcq("A, D, B", "A, D", CHOICES)
    assert g["correct"] is False
    assert g["points_earned"] == 0.0


def test_grade_mcq_respects_max_points():
    g = am._grade_mcq("A", "A", CHOICES, max_points=2.0)
    assert g["correct"] is True
    assert g["points_earned"] == 2.0
    assert g["points_possible"] == 2.0
    g2 = am._grade_mcq("B", "A", CHOICES, max_points=2.0)
    assert g2["points_earned"] == 0.0
    assert g2["points_possible"] == 2.0


def test_grade_mcq_prose_answer_path_unchanged():
    # Prose (non-letter) correct_answer falls back to the ORIGINAL
    # first-character compare, bug and all -- these 23 real questions must
    # keep grading exactly as they did before this change.
    prose = "12 volts"
    # Old formula: picked.strip().upper() == correct_raw.upper()[:1] == "1"
    g_match = am._grade_mcq("1", prose, CHOICES)
    assert g_match["correct"] is True
    g_no_match = am._grade_mcq("12 volts", prose, CHOICES)
    assert g_no_match["correct"] is False


# ---------------------------------------------------------------------------
# assessments._snapshot_one_question — select_multiple flag
# ---------------------------------------------------------------------------

def _q(qtype, **kw):
    base = {"number": "1", "text": "stem", "qtype": qtype}
    base.update(kw)
    return base


def test_snapshot_sets_select_multiple_for_genuine_multi_mcq():
    q = _q("mcq", choices=CHOICES, answer="A, D")
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert entry["select_multiple"] is True


def test_snapshot_does_not_set_select_multiple_for_single_answer_mcq():
    q = _q("mcq", choices=CHOICES, answer="A")
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert entry["select_multiple"] is False


def test_snapshot_does_not_set_select_multiple_for_prose_mcq():
    q = _q("mcq", choices=CHOICES, answer="12 volts")
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert entry["select_multiple"] is False


def test_snapshot_never_sets_select_multiple_for_tf():
    q = _q("tf", choices=[], answer="True")
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert "select_multiple" not in entry


def test_snapshot_never_sets_select_multiple_for_frq():
    q = _q("frq", choices=[], answer="some free text")
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert "select_multiple" not in entry


def test_snapshot_never_sets_select_multiple_for_matching():
    q = _q("matching", matching={"left": [], "right": [], "pairs": {}})
    entry = am._snapshot_one_question(q, "bucket", 1.0)
    assert "select_multiple" not in entry


# ---------------------------------------------------------------------------
# Regression: correct_answer must still be stripped from the student take
# payload, even now that select_multiple rides along in the same dict.
# ---------------------------------------------------------------------------

def _patch_files(monkeypatch, tmp_path):
    import auth
    import seasons
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "auth_users.json")
    monkeypatch.setattr(seasons, "SEASONS_FILE", tmp_path / "seasons.json")
    monkeypatch.setattr(seasons, "ROSTERS_FILE", tmp_path / "season_rosters.json")
    monkeypatch.setattr(am, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(am, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    monkeypatch.setattr(am, "RESPONSES_DIR", tmp_path / "assessment_responses")


def test_take_payload_strips_correct_answer_but_keeps_select_multiple(tmp_path, monkeypatch):
    import auth
    import seasons

    _patch_files(monkeypatch, tmp_path)
    auth.create_user("student1", "password123", role="student", events=[])
    seasons.create_season("2027", event_slugs=["circuit_lab"])
    seasons.set_roster("2027", "circuit_lab", ["student1"])

    window = am.create_window(season_id="2027", opens_at=PAST, closes_at=FUTURE,
                               event_slugs=["circuit_lab"], label="w1")
    test = am.get_assessment_for(window.window_id, "circuit_lab")
    snapshot = [{
        "bucket": "circuit_lab", "number": "1", "qtype": "mcq", "text": "A multi-answer question",
        "max_points": 1.0, "images": [], "image_descriptions": {}, "context_id": None,
        "choices": CHOICES, "correct_answer": "A, D", "select_multiple": True,
    }]
    with am._assessments_transaction() as ts:
        ts[test.assessment_id] = replace(ts[test.assessment_id], status="live", snapshot=snapshot)

    import review_app
    review_app.app.testing = True
    with review_app.app.test_client() as c:
        r = c.post("/login", data={"username": "student1", "password": "password123"})
        assert r.status_code == 302

        r = c.get(f"/api/my-assessments/{test.assessment_id}/take")
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["questions"]) == 1
        q_out = body["questions"][0]
        assert "correct_answer" not in q_out, \
            "correct_answer leaked to the student payload"
        assert "source_question_ref" not in q_out
        assert q_out.get("select_multiple") is True, \
            "select_multiple must still pass through the sanitizer"
        assert q_out["choices"] == CHOICES
