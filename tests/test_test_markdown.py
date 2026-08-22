"""
Coverage for testing.py's markdown rendering — the printable "Test" and
"Key" a coach downloads to administer a test on paper.

The properties that matter: a test copy must never leak answers, a key must
carry every answer, and the two must describe the same questions in the
same order. Everything else is formatting.

Run with: `python -m pytest tests/test_test_markdown.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import testing  # noqa: E402


def mcq(number="1", answer="B", points=1):
    return {
        "bucket": "b", "number": number, "qtype": "mcq", "max_points": points,
        "text": f"Question {number}?", "images": [],
        "choices": [{"letter": "A", "text": "wrong"}, {"letter": "B", "text": "right"}],
        "correct_answer": answer,
    }


def frq(number="2", answer="Because.", points=3):
    return {"bucket": "b", "number": number, "qtype": "frq", "max_points": points,
            "text": f"Explain {number}.", "images": [], "choices": [],
            "correct_answer": answer}


def matching(number="3"):
    return {
        "bucket": "b", "number": number, "qtype": "matching", "max_points": 2,
        "text": "Match them.", "images": [],
        "matching": {
            "left": [{"label": "1", "text": "ohm"}, {"label": "2", "text": "volt"}],
            "right": [{"label": "A", "text": "resistance"}, {"label": "B", "text": "potential"}],
            "pairs": {"1": "A", "2": "B"},
        },
    }


# ---------------------------------------------------------------------------
# The one property that really matters
# ---------------------------------------------------------------------------

def test_the_test_copy_never_contains_an_answer():
    snap = [mcq(answer="B"), frq(answer="SECRETANSWER"), matching()]
    out = testing.render_questions_markdown(snap, title="T", answers="none")
    assert "SECRETANSWER" not in out
    assert "Answer:" not in out
    assert "Answer key" not in out


def test_the_key_contains_every_answer():
    snap = [mcq(answer="B"), frq(answer="Because Ohm."), matching()]
    out = testing.render_questions_markdown(snap, title="T", answers="section")
    assert "Answer key" in out
    assert "Because Ohm." in out
    # Matching answers are rendered as label pairs.
    assert "1→A" in out and "2→B" in out


def test_section_layout_puts_the_key_after_every_question():
    snap = [mcq(number="1"), frq(number="2")]
    out = testing.render_questions_markdown(snap, title="T", answers="section")
    # The point of "section" over "inline": you can hand out the top half.
    assert out.index("Explain 2.") < out.index("Answer key")


def test_inline_layout_puts_each_answer_under_its_own_question():
    snap = [mcq(number="1", answer="B"), mcq(number="2", answer="A")]
    out = testing.render_questions_markdown(snap, title="T", answers="inline")
    first = out.index("Question 1?")
    second = out.index("Question 2?")
    assert first < out.index("**Answer:** B") < second
    assert "Answer key" not in out


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_questions_are_renumbered_sequentially_not_by_bank_number():
    # Bank numbers are per-PDF and arbitrary; a printed test must read 1..n.
    snap = [mcq(number="17"), mcq(number="4"), mcq(number="102")]
    out = testing.render_questions_markdown(snap, title="T", answers="none")
    assert "**1.**" in out and "**2.**" in out and "**3.**" in out


def test_points_are_pluralised_and_totalled():
    out = testing.render_questions_markdown([mcq(points=1), frq(points=3)],
                                            title="T", answers="none")
    assert "(1 pt)" in out
    assert "(3 pts)" in out
    assert "4 points total" in out


def test_free_response_leaves_writing_room_on_the_paper_copy():
    paper = testing.render_questions_markdown([frq()], title="T", answers="none")
    assert "\n" * testing._FRQ_ANSWER_LINES in paper


def test_inline_layout_replaces_writing_room_with_the_answer():
    # "section" deliberately keeps the writing room — its question half is
    # the same paper as the test copy, so a grader can compare line by line.
    # "inline" is the layout meant to be read rather than written on.
    inline = testing.render_questions_markdown([frq(answer="Because Ohm.")],
                                               title="T", answers="inline")
    assert "Because Ohm." in inline
    assert "\n" * testing._FRQ_ANSWER_LINES not in inline


def test_images_are_named_since_markdown_cannot_carry_them():
    q = mcq()
    q["images"] = ["fig1.png"]
    q["image_descriptions"] = {"fig1.png": "a circuit"}
    out = testing.render_questions_markdown([q], title="T", answers="none")
    assert "fig1.png" in out and "a circuit" in out


def test_a_shared_passage_prints_once_above_its_first_question():
    ctx = {"title": "Case study", "text": "A long passage."}
    q1, q2 = mcq(number="1"), mcq(number="2")
    for q in (q1, q2):
        q["context_id"] = "c1"
        q["_context"] = ctx
    out = testing.render_questions_markdown([q1, q2], title="T", answers="none")
    assert out.count("A long passage.") == 1
    assert out.index("A long passage.") < out.index("Question 1?")


def test_leading_markdown_characters_in_a_question_are_escaped():
    # A stem beginning "#" would otherwise render as a heading and swallow
    # the question numbering around it.
    q = mcq()
    q["text"] = "# of electrons in the outer shell?"
    out = testing.render_questions_markdown([q], title="T", answers="none")
    assert "\\#" in out


def test_title_and_subtitle_are_rendered():
    out = testing.render_questions_markdown([mcq()], title="Circuit Lab",
                                            subtitle="DRAFT", answers="none")
    assert out.startswith("# Circuit Lab")
    assert "DRAFT" in out


def test_unknown_layout_is_refused_rather_than_silently_omitting_answers():
    # Failing closed matters here: a typo that quietly produced a test copy
    # when a key was wanted would be discovered at grading time.
    with pytest.raises(ValueError, match="unknown answers layout"):
        testing.render_questions_markdown([mcq()], title="T", answers="keys")


def test_missing_key_is_stated_rather_than_left_blank():
    q = mcq(answer="")
    out = testing.render_questions_markdown([q], title="T", answers="section")
    assert "(no key recorded)" in out


def test_empty_snapshot_still_renders_a_valid_document():
    out = testing.render_questions_markdown([], title="Empty", answers="section")
    assert out.startswith("# Empty")
    assert "0 questions" in out


# ---------------------------------------------------------------------------
# Reuse detection for the Prepare-test pool (testing.used_question_keys)
# ---------------------------------------------------------------------------

def test_used_keys_span_kept_and_published_and_skip_the_current_test(tmp_path,
                                                                     monkeypatch):
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import seasons as seasons_mod, events as events_mod, testing as t
    for mod in (events_mod, seasons_mod, t):
        importlib.reload(mod)

    slug = sorted(events_mod.EVENTS)[0]
    seasons_mod.create_season("2027", event_slugs=[slug])
    w1 = t.create_window("2027", "2027-01-01T09:00", "2027-01-01T11:00", [slug])
    w2 = t.create_window("2027", "2027-02-01T09:00", "2027-02-01T11:00", [slug])
    t1 = t.get_test_for(w1.window_id, slug)
    t2 = t.get_test_for(w2.window_id, slug)

    t.update_test_kept(t1.test_id, [{"bucket": "b", "number": "5", "max_points": 1}])
    t.update_test_kept(t2.test_id, [{"bucket": "b", "number": "9", "max_points": 1}])

    # Building t2: q5 is used (by t1), q9 is t2's own and must not be hidden
    # from the person currently choosing it.
    used = t.used_question_keys("2027", exclude_test_id=t2.test_id)
    assert "b::5" in used
    assert "b::9" not in used

    # A different season shares nothing.
    seasons_mod.create_season("2028", event_slugs=[slug])
    assert t.used_question_keys("2028") == set()
