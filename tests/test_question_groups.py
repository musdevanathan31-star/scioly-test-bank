"""
Coverage for the shared-context "question groups" hardening pass:

  - assessments._grouped_shuffle_order  (group-preserving assessment shuffle,
    B3 — a graded test must never serve a grouped sub-question out of order
    or split a group across included/excluded, unlike quiz.html's opt-in
    "keep groups together" checkbox)
  - a context image round-trips through build_question_bank._all_contexts()
    (B1 — POST /api/context-image writes the PNG; the filename lives in
    annotations[bucket].contexts[i].images same as any other context field)
  - review_app._VALID_QTYPES rejection (B5 — a bogus qtype must not be
    persisted through api_patch_question's PATCH logic)

Run with: `python -m pytest tests/test_question_groups.py -q`
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb              # noqa: E402
import assessments as assessments_mod          # noqa: E402

bqb.set_event("circuit_lab")


def _fake_event(tmp_path):
    """Isolates bqb's active-event state to a scratch directory, the same
    trick tests/test_concurrency.py uses for build_question_bank.py's own
    tests — bqb._ev()/__getattr__ both resolve through _get_current(), so
    monkeypatching that one function redirects every _state_transaction()/
    _load_state() call regardless of what set_event() does elsewhere."""
    return SimpleNamespace(slug="qgroups_test", state_file=tmp_path / "state.json")


# ---------------------------------------------------------------------------
# assessments._grouped_shuffle_order
# ---------------------------------------------------------------------------

def _grouped_snapshot():
    """6 items: group A = indices 0,1 (bucket p, ctx c1); a singleton at 2;
    group C = indices 3,4 (bucket p, ctx c2); a singleton at 5."""
    return [
        {"bucket": "p", "context_id": "c1"},
        {"bucket": "p", "context_id": "c1"},
        {"bucket": "p", "context_id": None},
        {"bucket": "p", "context_id": "c2"},
        {"bucket": "p", "context_id": "c2"},
        {"bucket": "p", "context_id": None},
    ]


def test_grouped_shuffle_keeps_group_members_contiguous_and_in_order():
    snapshot = _grouped_snapshot()
    for seed in range(300):
        order = assessments_mod._grouped_shuffle_order(snapshot, random.Random(seed))
        assert len(order) == len(snapshot)
        assert sorted(order) == list(range(len(snapshot))), "every index must appear exactly once"
        pos = {idx: i for i, idx in enumerate(order)}
        # Group A (0,1): must be adjacent, and 0 must precede 1 (snapshot order preserved).
        assert pos[1] == pos[0] + 1, f"seed {seed}: group A split or reordered: {order}"
        # Group C (3,4): same.
        assert pos[4] == pos[3] + 1, f"seed {seed}: group C split or reordered: {order}"


def test_grouped_shuffle_singletons_still_get_shuffled():
    # 8 singleton items (no context_id) — over many seeds the resulting
    # order must not always be the identity order.
    snapshot = [{"bucket": "p", "context_id": None} for _ in range(8)]
    identity = list(range(8))
    saw_non_identity = False
    for seed in range(50):
        order = assessments_mod._grouped_shuffle_order(snapshot, random.Random(seed))
        assert sorted(order) == identity
        if order != identity:
            saw_non_identity = True
    assert saw_non_identity, "singleton items were never actually shuffled across 50 seeds"


def test_grouped_shuffle_two_groups_never_interleave():
    # Group A = 0,1 ; Group B = 2,3 — the only two valid final orders are
    # A-then-B or B-then-A, each internally in original order.
    snapshot = [
        {"bucket": "p", "context_id": "gA"},
        {"bucket": "p", "context_id": "gA"},
        {"bucket": "p", "context_id": "gB"},
        {"bucket": "p", "context_id": "gB"},
    ]
    valid = ([0, 1, 2, 3], [2, 3, 0, 1])
    for seed in range(300):
        order = assessments_mod._grouped_shuffle_order(snapshot, random.Random(seed))
        assert order in valid, f"seed {seed}: groups interleaved: {order}"


def test_grouped_shuffle_with_no_contexts_behaves_like_plain_shuffle():
    snapshot = [{"bucket": "p", "context_id": None} for _ in range(10)]
    order = assessments_mod._grouped_shuffle_order(snapshot, random.Random(7))
    assert len(order) == 10
    assert sorted(order) == list(range(10))


# ---------------------------------------------------------------------------
# Context image round-trip (B1) — write into annotations, read back via
# build_question_bank._all_contexts()
# ---------------------------------------------------------------------------

def test_context_image_round_trips_through_all_contexts(tmp_path, monkeypatch):
    fake_event = _fake_event(tmp_path)
    monkeypatch.setattr(bqb, "_get_current", lambda: fake_event)

    with bqb._state_transaction() as state:
        state.setdefault("annotations", {})["some_test.pdf"] = {
            "contexts": [
                {"id": "ctx_1", "title": "Circuit for Q5-Q6", "text": "",
                 "images": ["some_test_q_ctx_deadbeef.png"], "pages": [3]},
            ],
        }

    contexts = bqb._all_contexts()
    assert "some_test.pdf::ctx_1" in contexts
    assert contexts["some_test.pdf::ctx_1"]["images"] == ["some_test_q_ctx_deadbeef.png"]


# ---------------------------------------------------------------------------
# _VALID_QTYPES rejection (B5)
# ---------------------------------------------------------------------------

def test_bogus_qtype_is_not_persisted_via_patch_question(tmp_path, monkeypatch):
    import review_app

    fake_event = _fake_event(tmp_path)
    monkeypatch.setattr(bqb, "_get_current", lambda: fake_event)

    with bqb._state_transaction() as state:
        state.setdefault("questions", {})["bucket.pdf"] = [
            {"number": "1", "qtype": "mcq",
             "choices": [{"letter": "A", "text": "x"}, {"letter": "B", "text": "y"}],
             "text": "stem", "answer": "A"},
        ]

    with review_app.app.test_request_context(
        "/event/circuit_lab/api/q/bucket.pdf/1", method="PATCH",
        json={"qtype": "essay_freeform_bogus"},
    ):
        review_app.api_patch_question("circuit_lab", "bucket.pdf", "1")

    state = bqb._load_state()
    q = state["questions"]["bucket.pdf"][0]
    assert q["qtype"] == "mcq", f"bogus qtype must not overwrite the existing valid one, got {q.get('qtype')!r}"


def test_valid_qtype_is_persisted_via_patch_question(tmp_path, monkeypatch):
    import review_app

    fake_event = _fake_event(tmp_path)
    monkeypatch.setattr(bqb, "_get_current", lambda: fake_event)

    with bqb._state_transaction() as state:
        state.setdefault("questions", {})["bucket.pdf"] = [
            {"number": "1", "qtype": "mcq",
             "choices": [{"letter": "A", "text": "x"}, {"letter": "B", "text": "y"}],
             "text": "stem", "answer": "A"},
        ]

    from flask import g
    with review_app.app.test_request_context(
        "/event/circuit_lab/api/q/bucket.pdf/1", method="PATCH",
        json={"qtype": "frq"},
    ):
        g.user = SimpleNamespace(username="tester", role="coach", can_access=lambda slug: True)
        review_app.api_patch_question("circuit_lab", "bucket.pdf", "1")

    state = bqb._load_state()
    q = state["questions"]["bucket.pdf"][0]
    assert q["qtype"] == "frq"


def test_valid_qtypes_constant_matches_documented_set():
    import review_app
    assert review_app._VALID_QTYPES == {"mcq", "frq", "tf", "matching"}
