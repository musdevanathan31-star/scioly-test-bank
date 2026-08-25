"""
Coverage for the True/False question type (qtype: "tf").

Design (see the task's spec, not relitigated here): tf is a distinct qtype
with choices: [] and answer normalized to the literal string "True" or
"False" — most Sci-Oly T/F items print no lettered options at all, so
modeling this as a synthetic 2-choice MCQ would fabricate choices that leak
into every export/browse/compare view. Follows the qtype=="matching"
precedent: its own storage shape, its own renderer branch, its own grader.

Run with: `python -m pytest tests/test_true_false.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb      # noqa: E402
import assessments as assessments_mod  # noqa: E402

bqb.set_event("circuit_lab")


# ---------------------------------------------------------------------------
# _normalize_tf_answer
# ---------------------------------------------------------------------------

def test_normalize_tf_answer_bare_letters():
    assert bqb._normalize_tf_answer("T") == "True"
    assert bqb._normalize_tf_answer("t") == "True"
    assert bqb._normalize_tf_answer("f") == "False"
    assert bqb._normalize_tf_answer("F") == "False"


def test_normalize_tf_answer_words_any_case():
    assert bqb._normalize_tf_answer("TRUE") == "True"
    assert bqb._normalize_tf_answer("true") == "True"
    assert bqb._normalize_tf_answer("False") == "False"
    assert bqb._normalize_tf_answer("FALSE") == "False"


def test_normalize_tf_answer_whitespace_and_punctuation():
    assert bqb._normalize_tf_answer("  True  ") == "True"
    assert bqb._normalize_tf_answer("F.") == "False"
    assert bqb._normalize_tf_answer("T,") == "True"


def test_normalize_tf_answer_junk_returns_none():
    assert bqb._normalize_tf_answer("") is None
    assert bqb._normalize_tf_answer("maybe") is None
    assert bqb._normalize_tf_answer("42") is None
    assert bqb._normalize_tf_answer(None) is None
    assert bqb._normalize_tf_answer("Truely") is None


# ---------------------------------------------------------------------------
# _looks_like_tf
# ---------------------------------------------------------------------------

def test_looks_like_tf_cue_in_stem():
    assert bqb._looks_like_tf("True or False: Ohm's law applies to all resistors.", [])
    assert bqb._looks_like_tf("T/F: current leads voltage in a capacitor.", [])
    assert bqb._looks_like_tf("This is a true/false question about diodes.", [])


def test_looks_like_tf_lettered_true_false_choices():
    choices = [{"letter": "A", "text": "True"}, {"letter": "B", "text": "False"}]
    assert bqb._looks_like_tf("Ohm's law applies to all resistors.", choices)


def test_looks_like_tf_negative_plain_mcq():
    choices = [{"letter": "A", "text": "1 ohm"}, {"letter": "B", "text": "2 ohm"}]
    assert not bqb._looks_like_tf("What is the resistance?", choices)


def test_looks_like_tf_negative_plain_frq():
    assert not bqb._looks_like_tf("Explain why current flows through a resistor.", [])


def test_looks_like_tf_cue_does_not_override_real_choices():
    """A stem cue alone must not win against real, non-True/False options.
    An ordinary MCQ that merely contains the phrase "true or false" would
    otherwise be tagged tf and have its choices cleared by
    _finalize_tf_answers() — silently destroying the extracted options."""
    choices = [{"letter": "A", "text": "Only I"}, {"letter": "B", "text": "Only II"},
               {"letter": "C", "text": "Both"}, {"letter": "D", "text": "Neither"}]
    stem = "Determine whether each statement below is true or false, then choose the best option."
    assert not bqb._looks_like_tf(stem, choices)


def test_finalize_tf_preserves_choices_of_untagged_mcq():
    """End-to-end guard on the same case: the question never gets tagged, so
    its choices survive process_pair()'s _finalize_tf_answers() pass."""
    choices = [{"letter": "A", "text": "Only I"}, {"letter": "B", "text": "Only II"},
               {"letter": "C", "text": "Both"}, {"letter": "D", "text": "Neither"}]
    stem = "Determine whether each statement below is true or false, then choose the best option."
    q = {"number": "7", "text": stem, "choices": list(choices), "answer": "C"}
    if bqb._looks_like_tf(stem, choices):
        q["qtype"] = "tf"
    bqb._finalize_tf_answers([q])
    assert q.get("qtype") != "tf"
    assert q["choices"] == choices
    assert q["answer"] == "C"


# ---------------------------------------------------------------------------
# extract_questions() — cued stem gets tagged qtype="tf"
# ---------------------------------------------------------------------------

def test_extract_questions_tags_cued_stem_as_tf():
    pages = ["1. True or False: Ohm's law applies to all resistors. ____"]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert len(qs) == 1
    assert qs[0]["qtype"] == "tf"
    assert qs[0]["choices"] == []


def test_extract_questions_leaves_ordinary_stem_alone():
    pages = ["1. What is the unit of resistance? A. Ohm B. Volt C. Amp D. Watt"]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert len(qs) == 1
    assert qs[0].get("qtype") is None
    assert len(qs[0]["choices"]) == 4


def test_extract_questions_tags_lettered_true_false_as_tf():
    pages = ["1. Ohm's law applies to all resistors. A. True B. False"]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert len(qs) == 1
    assert qs[0]["qtype"] == "tf"
    # extract_questions() itself doesn't know the answer yet (that's merged
    # later in process_pair) — choices stay populated here so the eventual
    # letter->word mapping in _finalize_tf_answers() has something to map.
    assert [c["letter"] for c in qs[0]["choices"]] == ["A", "B"]


# ---------------------------------------------------------------------------
# _finalize_tf_answers() — process_pair()'s post-answer-merge normalization
# ---------------------------------------------------------------------------

def test_finalize_tf_answers_maps_printed_letter_to_word_and_clears_choices():
    questions = [{
        "number": "1", "qtype": "tf", "answer": "A",
        "choices": [{"letter": "A", "text": "True"}, {"letter": "B", "text": "False"}],
    }]
    bqb._finalize_tf_answers(questions)
    assert questions[0]["answer"] == "True"
    assert questions[0]["choices"] == []


def test_finalize_tf_answers_maps_letter_b_to_false():
    questions = [{
        "number": "1", "qtype": "tf", "answer": "B",
        "choices": [{"letter": "A", "text": "True"}, {"letter": "B", "text": "False"}],
    }]
    bqb._finalize_tf_answers(questions)
    assert questions[0]["answer"] == "False"


def test_finalize_tf_answers_normalizes_bare_word_answer():
    questions = [{"number": "1", "qtype": "tf", "answer": "false", "choices": []}]
    bqb._finalize_tf_answers(questions)
    assert questions[0]["answer"] == "False"


def test_finalize_tf_answers_unparseable_answer_left_untouched():
    # An unparseable key is a review-UI problem, not a reason to destroy data.
    questions = [{"number": "1", "qtype": "tf", "answer": "see key page 3", "choices": []}]
    bqb._finalize_tf_answers(questions)
    assert questions[0]["answer"] == "see key page 3"


def test_finalize_tf_answers_ignores_non_tf_questions():
    questions = [{"number": "1", "qtype": "mcq", "answer": "A",
                  "choices": [{"letter": "A", "text": "1 ohm"}]}]
    bqb._finalize_tf_answers(questions)
    assert questions[0]["answer"] == "A"
    assert questions[0]["choices"] == [{"letter": "A", "text": "1 ohm"}]


# ---------------------------------------------------------------------------
# assessments._snapshot_one_question — preserves explicit tf + correct_answer
# ---------------------------------------------------------------------------

def test_snapshot_one_question_preserves_tf():
    q = {"number": "3", "qtype": "tf", "text": "Ohm's law applies to all resistors.",
         "choices": [], "answer": "True"}
    entry = assessments_mod._snapshot_one_question(q, bucket="testpdf.pdf", max_points=1)
    assert entry["qtype"] == "tf"
    assert entry["choices"] == []
    assert entry["correct_answer"] == "True"


# ---------------------------------------------------------------------------
# assessments._grade_tf
# ---------------------------------------------------------------------------

def test_grade_tf_correct():
    result = assessments_mod._grade_tf("True", "True")
    assert result["correct"] is True
    assert result["points_earned"] == 1.0
    assert result["points_possible"] == 1.0


def test_grade_tf_incorrect():
    result = assessments_mod._grade_tf("True", "False")
    assert result["correct"] is False
    assert result["points_earned"] == 0.0


def test_grade_tf_case_and_letter_insensitive():
    assert assessments_mod._grade_tf("t", "True")["correct"] is True
    assert assessments_mod._grade_tf("false", "F")["correct"] is True


def test_grade_tf_blank_answer():
    result = assessments_mod._grade_tf(None, "True")
    assert result["correct"] is False
    assert result["points_earned"] == 0.0


def test_grade_tf_respects_max_points():
    result = assessments_mod._grade_tf("True", "True", max_points=2.0)
    assert result["points_earned"] == 2.0
    assert result["points_possible"] == 2.0


# ---------------------------------------------------------------------------
# submit_response() / assessment_grading_complete() — tf auto-grades,
# doesn't block on manual grading
# ---------------------------------------------------------------------------

def _patch_files(monkeypatch, tmp_path):
    monkeypatch.setattr(assessments_mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(assessments_mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    monkeypatch.setattr(assessments_mod, "RESPONSES_DIR", tmp_path / "assessment_responses")


def _tf_snapshot():
    return [{"bucket": "testpdf.pdf", "number": "1", "qtype": "tf",
              "text": "Ohm's law applies to all resistors.", "max_points": 1,
              "choices": [], "correct_answer": "True"}]


def test_submit_response_writes_auto_grade_for_tf(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    snapshot = _tf_snapshot()
    assessments_mod.start_or_get_response("assess1", "student1", snapshot)
    assessments_mod.save_answer("assess1", "student1", "1", {"qtype": "tf", "picked": "True"})
    resp = assessments_mod.submit_response("assess1", "student1", snapshot)
    assert resp.status == "submitted"
    assert "1" in resp.auto_grade
    assert resp.auto_grade["1"]["correct"] is True
    assert resp.auto_grade["1"]["points_earned"] == 1.0


def test_submit_response_tf_wrong_answer_scores_zero(tmp_path, monkeypatch):
    _patch_files(monkeypatch, tmp_path)
    snapshot = _tf_snapshot()
    assessments_mod.start_or_get_response("assess2", "student1", snapshot)
    assessments_mod.save_answer("assess2", "student1", "1", {"qtype": "tf", "picked": "False"})
    resp = assessments_mod.submit_response("assess2", "student1", snapshot)
    assert resp.auto_grade["1"]["correct"] is False
    assert resp.auto_grade["1"]["points_earned"] == 0.0


def test_assessment_grading_complete_ignores_tf(tmp_path, monkeypatch):
    # No manual grade ever recorded for the tf question — grading must
    # still be reported complete (tf is auto-graded, not FRQ manual-graded).
    _patch_files(monkeypatch, tmp_path)
    snapshot = _tf_snapshot()
    assessments_mod.start_or_get_response("assess3", "student1", snapshot)
    assessments_mod.save_answer("assess3", "student1", "1", {"qtype": "tf", "picked": "True"})
    assessments_mod.submit_response("assess3", "student1", snapshot)
    assert assessments_mod.assessment_grading_complete("assess3", snapshot) is True
