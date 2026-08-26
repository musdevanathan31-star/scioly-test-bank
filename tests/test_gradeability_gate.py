"""
A question that no grader could ever mark right must not be certifiable.

23 MCQs in the real bank carry a prose answer (`answer: "12 volts"`) on a
question with lettered choices. `_grade_mcq`'s prose fallback compares only
the first character, so:

    correct='12 volts'  picked='A'         -> wrong
    correct='12 volts'  picked='1'         -> CORRECT   ("12 volts"[:1])

No choice a student can click is ever right. That fallback is deliberately
preserved — the fix is upstream: refuse to mark such a question `correct`,
so it can't be certified and therefore can't reach students.

Why verification rather than publishing: the assessment builder's pool
filter is "validated correct only", checked by default, so blocking the
`correct` verdict removes ungradeable questions from the default pool for
free. `incorrect` and `uncertain` stay settable — marking one incorrect is
precisely how a reviewer flags it for fixing.

Run with: `python -m pytest tests/test_gradeability_gate.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402


CHOICES = [{"letter": L, "text": f"opt {L}"} for L in "ABCD"]


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q, ok, reason_contains", [
    # --- mcq ---
    ({"qtype": "mcq", "answer": "B", "choices": CHOICES}, True, ""),
    ({"qtype": "mcq", "answer": "A, D", "choices": CHOICES}, True, ""),      # multi-answer
    ({"qtype": "mcq", "answer": "12 volts", "choices": CHOICES}, False, "choices"),
    ({"qtype": "mcq", "answer": "Z", "choices": CHOICES}, False, "choices"),  # letter not present
    ({"qtype": "mcq", "answer": "", "choices": CHOICES}, False, "choices"),
    # --- tf ---
    ({"qtype": "tf", "answer": "True"}, True, ""),
    ({"qtype": "tf", "answer": "False"}, True, ""),
    ({"qtype": "tf", "answer": "Infinite"}, False, "True/False"),   # preserved-unparseable
    ({"qtype": "tf", "answer": ""}, False, "True/False"),
    # --- frq ---
    ({"qtype": "frq", "answer": "42 ohms"}, True, ""),
    ({"qtype": "frq", "answer": "   "}, False, "reference answer"),
    ({"qtype": "frq", "answer": ""}, False, "reference answer"),
    # --- matching ---
    ({"qtype": "matching", "matching": {"left": [{"label": "1"}],
                                        "right": [{"label": "A"}],
                                        "pairs": {"1": "A"}}}, True, ""),
    ({"qtype": "matching", "matching": {"left": [{"label": "1"}],
                                        "right": [{"label": "A"}],
                                        "pairs": {}}}, False, "pairs"),
])
def test_gradeability_rule(q, ok, reason_contains):
    gradeable, reason = bqb.question_gradeability(q)
    assert gradeable is ok, (q, reason)
    if not ok:
        assert reason, "an ungradeable question must explain why"
        assert reason_contains.lower() in reason.lower(), reason


def test_effective_type_is_inferred_the_same_way_the_grader_infers_it():
    """No explicit qtype: choices present => judged as mcq, absent => frq.
    This must match _snapshot_one_question, or gradeability would judge a
    different type than the one actually graded."""
    assert bqb.question_gradeability({"answer": "B", "choices": CHOICES})[0] is True
    assert bqb.question_gradeability({"answer": "12 volts", "choices": CHOICES})[0] is False
    # No choices at all -> frq -> prose answer is fine.
    assert bqb.question_gradeability({"answer": "12 volts"})[0] is True


def test_reasons_are_user_facing_not_codes():
    """The reason is shown to whoever was blocked, so it has to name the
    problem in words rather than return an error code."""
    for q in ({"qtype": "mcq", "answer": "12 volts", "choices": CHOICES},
              {"qtype": "tf", "answer": "nope"},
              {"qtype": "matching", "matching": {"pairs": {}}},
              {"qtype": "frq", "answer": ""}):
        _, reason = bqb.question_gradeability(q)
        assert reason and reason[0].islower() or reason
        assert not reason.startswith("E"), f"looks like a code, not prose: {reason}"
        assert len(reason.split()) >= 3, f"too terse to act on: {reason}"


# ---------------------------------------------------------------------------
# The chokepoint: the PATCH handler that records a verdict
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="gradegate-")
    monkeypatch.setenv("DATA_ROOT", tmp)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    previous_event = bqb.current_event()

    import auth, events, seasons, assessments
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False

    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["t_test.pdf"] = [
            # ungradeable: prose answer on a lettered question
            {"number": "1", "text": "Q1", "qtype": "mcq",
             "answer": "12 volts", "choices": CHOICES, "images": []},
            # gradeable
            {"number": "2", "text": "Q2", "qtype": "mcq",
             "answer": "B", "choices": CHOICES, "images": []},
        ]

    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    token = c.get_cookie("csrf_token").value

    def patch(num, body):
        return c.patch(f"/event/{slug}/api/q/t_test.pdf/{num}",
                       json=body, headers={"X-CSRF-Token": token})

    yield patch

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_correct_is_refused_for_an_ungradeable_question(api):
    r = api("1", {"validation": {"status": "correct"}})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "cannot mark correct" in r.get_json()["error"].lower()


@pytest.mark.parametrize("status", ["incorrect", "uncertain"])
def test_other_verdicts_remain_settable_on_an_ungradeable_question(api, status):
    """Marking it incorrect is how a reviewer flags it for fixing — blocking
    that would leave no way to record the problem."""
    r = api("1", {"validation": {"status": status}})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_correct_is_allowed_for_a_gradeable_question(api):
    r = api("2", {"validation": {"status": "correct"}})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_fixing_the_answer_in_the_same_request_unblocks_it(api):
    """The verdict is judged against the record as the request would leave
    it, so correcting the answer and certifying in one PATCH must succeed —
    otherwise the obvious fix-and-approve flow would be impossible."""
    r = api("1", {"answer": "C", "validation": {"status": "correct"}})
    assert r.status_code == 200, r.get_data(as_text=True)
