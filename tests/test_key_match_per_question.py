"""
Answer-key matching is judged per question number, not per document.

A paper whose sections each restart at 1 is deduped by extract_questions to
1 / 1b / 1c, and extract_answers' dict collapses the key's repeats the same
way — so matching by number alone could staple a later section's answer onto
an earlier section's question. Empty beats wrong, so those are skipped.

The ambiguity is per number though. The guard used to be all-or-nothing: one
stray duplicate anywhere and the entire document lost every answer it had.
Measured across this repo's corpus, that cost 28 test PDFs *all* of their
answers while 279 unambiguous ones sat there matchable.

The rule now: a number is safe exactly when its base occurs once in the PDF.
If "12b" exists then "12" is part of a restarted run and BOTH are skipped —
the bare one is no less ambiguous than its suffixed sibling.

Run with: `python -m pytest tests/test_key_match_per_question.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402


def _assign(questions, answers):
    """The assignment step of process_pair, in isolation.

    Mirrors the production loop exactly; kept here rather than exercising the
    whole PDF pipeline so the rule can be tested without fixture PDFs (event
    PDFs are gitignored and absent from a fresh clone).
    """
    import re
    from collections import defaultdict
    base = lambda n: re.sub(r"[a-z]+$", "", str(n))
    counts = defaultdict(int)
    for q in questions:
        counts[base(q["number"])] += 1
    for q in questions:
        if q.get("qtype") == "matching":
            continue
        if counts[base(q["number"])] > 1:
            continue
        q["answer"] = answers.get(q["number"], "")
    return questions


def test_unambiguous_numbers_get_their_answers():
    qs = [{"number": "1"}, {"number": "2"}, {"number": "3"}]
    _assign(qs, {"1": "A", "2": "B", "3": "C"})
    assert [q["answer"] for q in qs] == ["A", "B", "C"]


def test_a_restarted_run_is_skipped_on_both_sides():
    """"1" and "1b" are equally ambiguous — neither may take the key's "1"."""
    qs = [{"number": "1"}, {"number": "1b"}, {"number": "2"}]
    _assign(qs, {"1": "A", "1b": "Z", "2": "B"})
    assert qs[0].get("answer", "") == "", "bare 1 must not take the key's 1"
    assert qs[1].get("answer", "") == "", "1b must not take an answer either"
    assert qs[2]["answer"] == "B", "an unrelated number is unaffected"


def test_one_stray_duplicate_no_longer_costs_the_whole_document():
    """The regression this fixes: 49 questions losing every answer because
    four of them came from a restarted section."""
    qs = [{"number": str(n)} for n in range(1, 46)]
    qs += [{"number": "12b"}, {"number": "13b"}, {"number": "14b"}, {"number": "15b"}]
    answers = {str(n): "A" for n in range(1, 46)}
    _assign(qs, answers)
    answered = sum(1 for q in qs if q.get("answer"))
    # 45 numbered 1..45, minus the four whose bases (12,13,14,15) are shared.
    assert answered == 41, answered
    for q in qs:
        if q["number"] in ("12", "13", "14", "15") or q["number"].endswith("b"):
            assert q.get("answer", "") == "", q["number"]


def test_matching_questions_are_left_alone():
    """Their pairs come from extract_matching_answers; `answer` stays empty."""
    qs = [{"number": "1", "qtype": "matching"}, {"number": "2"}]
    _assign(qs, {"1": "A", "2": "B"})
    assert qs[0].get("answer", "") == ""
    assert qs[1]["answer"] == "B"


def test_a_number_with_no_key_entry_gets_an_empty_answer_not_a_crash():
    qs = [{"number": "7"}]
    _assign(qs, {})
    assert qs[0]["answer"] == ""


def test_multi_letter_suffixes_share_a_base():
    """Deduping goes past 'b' — 1/1b/1c/1d all belong to the same run."""
    qs = [{"number": "1"}, {"number": "1b"}, {"number": "1c"}, {"number": "1d"}]
    _assign(qs, {"1": "A"})
    assert all(q.get("answer", "") == "" for q in qs)


def test_the_production_loop_matches_this_rule():
    """Guards against the copy above drifting from process_pair: the real
    source must still key on a base-count, not on a document-wide flag."""
    src = Path(bqb.__file__).read_text(encoding="utf-8")
    assert "base_counts" in src, "process_pair no longer uses per-number counts"
    assert "has_dupes = any(" not in src, (
        "the all-or-nothing document-wide guard is back")
