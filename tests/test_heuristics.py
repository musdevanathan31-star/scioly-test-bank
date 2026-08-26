"""
Regression baseline for the heuristics that drive question extraction and dedup.

These tests pin down today's observable behaviour for:
  - split_choices              (MC option detection + stem split)
  - split_choices_by_lines     (multi-line fallback splitter)
  - _strip_points              (point-marker stripper + NFKC normalisation)
  - classify_topic             (keyword-weighted topic assignment)
  - _section_suffix            (duplicate-question alpha-suffix generator)
  - qgen.is_duplicate          (Jaccard 3-gram dedup threshold)

Run with: `python -m pytest tests/ -q` (no external deps).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402
import qgen                         # noqa: E402

bqb.set_event("circuit_lab")


# ---------------------------------------------------------------------------
# split_choices
# ---------------------------------------------------------------------------

def test_split_choices_clean_abcd():
    text = "What is V? a. 1 V b. 2 V c. 3 V d. 4 V"
    stem, choices = bqb.split_choices(text)
    assert stem.startswith("What is V?")
    assert [c["letter"] for c in choices] == ["A", "B", "C", "D"]
    assert choices[0]["text"] == "1 V"


def test_split_choices_no_question_hint_still_works_when_clean_A():
    # Completion-style stem (no "?" or "which/what") — the post-fix branch
    # should still accept when letters[0] == "A".
    text = ("If an solid conducting sphere is given a positive net charge, "
            "the electrostatic potential of the conductor is "
            "a. Constant b. Entirely zero c. Zero on the surface "
            "d. Largest on the surface e. Largest at the center")
    stem, choices = bqb.split_choices(text)
    assert len(choices) == 5
    assert choices[0]["letter"] == "A"
    assert choices[0]["text"] == "Constant"


def test_split_choices_rejects_unit_C_after_digit():
    # Pre-fix bug: "5 C." inside the question stem matched _MC_OPTION as a
    # phantom "C." option, which broke the strictly-ascending letter check.
    text = "Q has 5 C of charge. a. yes b. no"
    stem, choices = bqb.split_choices(text)
    assert [c["letter"] for c in choices] == ["A", "B"]


def test_split_choices_not_multiple_choice():
    text = "Compute the resistance of the circuit. Show your work."
    stem, choices = bqb.split_choices(text)
    assert choices == []
    assert stem == text


def test_split_choices_by_lines_multiline_fallback():
    raw = """\
What does this do?
a. choice one
b. choice two
c. choice three
d. choice four
"""
    stem, choices = bqb.split_choices_by_lines(raw)
    assert stem == "What does this do?"
    assert len(choices) == 4
    assert choices[2]["letter"] == "C" and choices[2]["text"] == "choice three"


def test_split_choices_by_lines_no_markers_positional():
    raw = "alpha\nbeta\ngamma\ndelta"
    stem, choices = bqb.split_choices_by_lines(raw)
    assert stem == ""
    assert [c["letter"] for c in choices] == ["A", "B", "C", "D"]
    assert choices[2]["text"] == "gamma"


# ---------------------------------------------------------------------------
# _strip_points + NFKC normalisation
# ---------------------------------------------------------------------------

def test_strip_points_removes_marker():
    assert bqb._strip_points("(2 points) Find R") == "Find R"
    assert bqb._strip_points("[3pts] each") == "each"


def test_strip_points_normalises_nbsp():
    # NBSP (U+00A0) and ZWS (U+200B) shouldn't survive normalisation.
    out = bqb._strip_points("a b​c")
    assert " " not in out
    assert "​" not in out
    assert "abc" in out.replace(" ", "")


# ---------------------------------------------------------------------------
# classify_topic (event-specific keyword scoring)
# ---------------------------------------------------------------------------

def test_classify_topic_capacitors():
    assert bqb.classify_topic("compute the RC time constant for the capacitor") == "Capacitors"


def test_classify_topic_kirchhoff():
    assert bqb.classify_topic("apply KVL around the closed loop") == "Kirchhoff's Laws"


def test_classify_topic_other_when_unknown():
    assert bqb.classify_topic("the quick brown fox") == "Other / General"


# ---------------------------------------------------------------------------
# _section_suffix
# ---------------------------------------------------------------------------

def test_section_suffix_basic():
    assert bqb._section_suffix(1) == "b"
    assert bqb._section_suffix(2) == "c"
    assert bqb._section_suffix(9) == "j"


def test_section_suffix_overflow():
    assert bqb._section_suffix(25) == "z"
    assert bqb._section_suffix(26) == "aa"
    assert bqb._section_suffix(27) == "ab"
    assert bqb._section_suffix(51) == "az"
    assert bqb._section_suffix(52) == "ba"


# ---------------------------------------------------------------------------
# qgen.is_duplicate (Jaccard 3-gram threshold 0.4)
# ---------------------------------------------------------------------------

def test_is_duplicate_identical():
    bank = [{"text": "Find the voltage across the capacitor.", "number": "1"}]
    is_dup, matched = qgen.is_duplicate(
        {"text": "Find the voltage across the capacitor."}, bank)
    assert is_dup
    assert matched == "1"


def test_is_duplicate_different():
    bank = [{"text": "Find the voltage across the capacitor.", "number": "1"}]
    is_dup, _ = qgen.is_duplicate(
        {"text": "Compute the magnetic flux through the coil."}, bank)
    assert not is_dup


def test_is_duplicate_paraphrase_threshold():
    bank = [{"text": "The capacitor stores charge.", "number": "1"}]
    is_dup, _ = qgen.is_duplicate(
        {"text": "A resistor dissipates heat."}, bank)
    assert not is_dup


# ---------------------------------------------------------------------------
# Event.filename_prefix derivation
# ---------------------------------------------------------------------------

def test_filename_prefix_derives_from_event_match():
    from events import Event
    e = Event(slug="disease_detectives", name="Disease Detectives",
              event_match=("disease detectives",),
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "diseasedetectives"


def test_filename_prefix_explicit_override_wins():
    from events import Event
    e = Event(slug="anatomy_physiology", name="Anatomy & Physiology",
              event_match=("anatomy",), filename_prefix="anatomy",
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "anatomy"


def test_filename_prefix_falls_back_to_slug_when_no_event_match():
    from events import Event
    e = Event(slug="my_local_event", name="My Local Event",
              event_match=(),
              topics=("Other / General",), topic_keywords={})
    assert e.filename_prefix == "my_local_event"


# ---------------------------------------------------------------------------
# Matching-question detection/splitting
# ---------------------------------------------------------------------------

def test_looks_like_matching_positive():
    body = ("Match each term in Column A to its definition in Column B. "
             "1. Resistor 2. Capacitor 3. Inductor 4. Diode 5. Transistor "
             "A. Stores charge B. Limits current C. One-way valve "
             "D. Amplifies signal E. Stores magnetic energy")
    assert bqb._looks_like_matching(body)


def test_looks_like_matching_negative_plain_frq():
    body = "Explain why current flows through a resistor when a voltage is applied."
    assert not bqb._looks_like_matching(body)


def test_looks_like_matching_negative_plain_mc():
    body = "What is the unit of resistance? A. Ohm B. Volt C. Amp D. Watt"
    assert not bqb._looks_like_matching(body)


def test_split_column_items_numeric_with_markers():
    raw = "1. Resistor\n2. Capacitor\n3. Inductor"
    items = bqb.split_column_items(raw, "numeric")
    assert [it["label"] for it in items] == ["1", "2", "3"]
    assert items[1]["text"] == "Capacitor"
    assert all(it["image"] is None for it in items)


def test_split_column_items_alpha_with_markers():
    raw = "A. Limits current\nB. Stores charge\nC. Stores magnetic energy"
    items = bqb.split_column_items(raw, "alpha")
    assert [it["label"] for it in items] == ["A", "B", "C"]
    assert items[2]["text"] == "Stores magnetic energy"


def test_split_column_items_no_markers_positional_fallback():
    raw = "Resistor\nCapacitor\nInductor"
    items = bqb.split_column_items(raw, "numeric")
    assert [it["label"] for it in items] == ["1", "2", "3"]
    assert [it["text"] for it in items] == ["Resistor", "Capacitor", "Inductor"]


def test_split_column_items_no_ceiling_unlike_mc_choices():
    # Matching columns commonly run well past split_choices's 5-item cap.
    raw = "\n".join(f"{i}. item {i}" for i in range(1, 9))
    items = bqb.split_column_items(raw, "numeric")
    assert len(items) == 8


def test_split_column_items_empty_input():
    assert bqb.split_column_items("", "numeric") == []


def test_split_column_items_strips_unconditional_leading_placeholder():
    # Real specimen (disease_detectives bakingsoda test, p.2): the left
    # column is a blank-for-the-student's-answer + bare term, no marker at
    # all — the placeholder must be stripped even with nothing to anchor to.
    raw = "___ Eradication\n___ Vector\n___ Isolation"
    items = bqb.split_column_items(raw, "numeric")
    assert [it["label"] for it in items] == ["1", "2", "3"]
    assert [it["text"] for it in items] == ["Eradication", "Vector", "Isolation"]


def test_split_column_items_merges_marker_alone_on_own_line():
    # Real specimen (same PDF): several markers sit alone on their own
    # line, with the entry's text starting only on the next line(s).
    raw = "f.\nCipher catches a cold after staying at the\nhospital.\nj.\nA mosquito"
    items = bqb.split_column_items(raw, "alpha")
    assert [it["label"] for it in items] == ["F", "J"]
    assert items[0]["text"] == "Cipher catches a cold after staying at the hospital."
    assert items[1]["text"] == "A mosquito"


def test_split_column_items_accepts_letter_marker_on_declared_numeric_side():
    # Real specimen (disease_detectives vert1ran test, p.4): the LEFT
    # column is lettered, violating the old left=numeric assumption — the
    # letter marker must still be recognized and stripped, not silently
    # treated as unmarked text with a positional "1" label.
    raw = "a. Wearing a mask on the subway\nb. Staphylococcus aureus"
    items = bqb.split_column_items(raw, "numeric")
    assert [it["label"] for it in items] == ["A", "B"]
    assert items[0]["text"] == "Wearing a mask on the subway"


def test_split_column_items_no_marker_column_rows_stay_separate():
    # A column with no printed labels at all (every row genuinely separate)
    # must not be merged just because rows are adjacent — regression guard
    # for the marker-based-only merge tier.
    raw = "Influenza\nTuberculosis\nAthlete's foot"
    items = bqb.split_column_items(raw, "alpha")
    assert [it["text"] for it in items] == ["Influenza", "Tuberculosis", "Athlete's foot"]
    assert [it["label"] for it in items] == ["A", "B", "C"]


def test_split_column_items_multiline_wrap_merges_into_one_item():
    # Real specimen (vert1ran): a long definition wraps 3 physical lines
    # with the marker on the first — must merge into one item, not three.
    raw = ("a. more than expected cases in a particular area or population\n"
           "during a particular period, affecting a significant proportion of\n"
           "a community\n"
           "b. an inanimate object that can transmit infectious agents")
    items = bqb.split_column_items(raw, "numeric")
    assert len(items) == 2
    assert items[0]["label"] == "A"
    assert items[0]["text"] == (
        "more than expected cases in a particular area or population "
        "during a particular period, affecting a significant proportion of "
        "a community")


def test_group_continuation_rows_marker_per_row():
    groups = bqb._group_continuation_rows(["1. a", "2. b", "3. c"])
    assert groups == [[0], [1], [2]]


def test_group_continuation_rows_merges_unmarked_onto_previous_marked():
    groups = bqb._group_continuation_rows(["f.", "continues here", "more text"])
    assert groups == [[0, 1, 2]]


def test_group_continuation_rows_no_marker_at_all_stays_separate():
    groups = bqb._group_continuation_rows(["term one", "term two", "term three"])
    assert groups == [[0], [1], [2]]


# Real-PDF fixtures: row text extracted verbatim (via fitz) from each
# column's drag-captured region on the two specimens that originally
# exposed issues 1/2/4 — disease_detectives bakingsoda test p.2 (unlabeled
# left column with answer-blank placeholders, lettered right column with
# several marker-alone-on-its-own-line and multi-line entries) and
# vert1ran test p.4 (lettered LEFT column violating the old left=numeric
# assumption, unlabeled right column, multi-line left entries).

_BAKINGSODA_LEFT_RAW = """\
___ Eradication
___ Vector
___ Isolation
___ Quarantine
___ Public health approach
___ Pathogenicity
___ Virulence
___ Prophylaxis
___ Disease
___ Noscomial
___ Syndromic surveillance
___ Infectivity
___ Agent
"""

_BAKINGSODA_RIGHT_RAW = """\
a. Wearing a mask on the subway to avoid
catching a cold
b. Staphylococcus aureus
c. Used to separate and restrict the movement
of people who are healthy but who may
have been exposed to an infectious disease
to see if they develop illness.
d. The property of causing disease following
infection
e. A local hospital monitors reports of
patients coming in for sore throats.
f.
Cipher catches a cold after staying at the
hospital.
g. Any harmful deviation from the normal
structural or functional state of an
organism.
h. The property of establishing infection after
exposure
i.
The property of causing severe disease
j.
A mosquito
k. Separates sick people with a contagious
disease from those who are not sick.
l.
Funds are directed to an area few in
healthcare resources so citizens can stay
healthy
m. Rinderpest no longer exists naturally in the
world
"""

_VERT1RAN_LEFT_RAW = """\
a. more than expected cases in a particular area or population
during a particular period, affecting a significant proportion of
a community
b. an inanimate object that can transmit infectious agents
c. an aggregation of cases in a defined area during a particular
period
d. disease, or any departure from a healthy state
e. separation of healthy people who have been potentially
exposed to infectious disease
f. the frequency with which new cases occur within a
population over a particular period
g. a factor whose presence or absence is necessary in the
occurrence of an adverse health outcome.
h. the probability an event will occur
i. a range of manifestations the disease process can take
j. period between exposure and onset of symptoms of disease
(typically non-infectious)
k. an agent's ability to cause disease following infection
l. a factor that brings about change in health conditions or
other characteristics
m. the first case or instance of a patient coming to the
attention of health authorities
n. an event that occurs infrequently and irregularly
o. a living intermediary that carries an agent from a reservoir to
a susceptible host
"""

_VERT1RAN_RIGHT_RAW = """\
agent
cluster
determinant
epidemic
fomite
incidence
latency period
morbidity
pathogenicity
quarantine
risk
spectrum of illness
vector
zoonosis
outlier
"""


def test_split_column_items_bakingsoda_left_column_real_fixture():
    items = bqb.split_column_items(_BAKINGSODA_LEFT_RAW, "numeric")
    assert len(items) == 13
    assert items[0] == {"label": "1", "text": "Eradication", "image": None}
    assert items[-1] == {"label": "13", "text": "Agent", "image": None}
    assert all("_" not in it["text"] for it in items)


def test_split_column_items_bakingsoda_right_column_real_fixture():
    items = bqb.split_column_items(_BAKINGSODA_RIGHT_RAW, "alpha")
    assert len(items) == 13
    assert [it["label"] for it in items] == [chr(ord("A") + i) for i in range(13)]
    # The two marker-alone-on-its-own-line entries must be fully merged,
    # not truncated at the marker or split into extra spurious items.
    assert items[5]["text"] == "Cipher catches a cold after staying at the hospital."
    assert items[9]["text"] == "A mosquito"
    # A 3-line wrapped entry must merge into one item too.
    assert items[2]["text"] == (
        "Used to separate and restrict the movement of people who are "
        "healthy but who may have been exposed to an infectious disease "
        "to see if they develop illness.")


def test_split_column_items_strips_bidi_control_chars_real_fixture():
    # Real specimen (disease_detectives bakingsoda 2026 test, p.2): PyMuPDF
    # extracts every text run wrapped in invisible Unicode bidi override
    # marks (U+202D LRO before, U+202C PDF after) — confirmed via the
    # debug_extraction.py tool against the live PDF. Before normalize_unicode()
    # was wired into split_column_items(), _PLACEHOLDER_BLANK_RE's "^_{2,}"
    # anchor silently failed to match (U+202D sits before the "_"), leaving
    # every item's text polluted with both the placeholder and the bidi marks.
    # Built with chr() rather than embedding literal invisible characters in
    # this source file (fragile — editors/tools can silently drop them).
    LRO, PDF_MARK = chr(0x202D), chr(0x202C)
    raw = "\n".join(
        f"{LRO}___ {term}{PDF_MARK}" for term in ("Eradication", "Vector", "Isolation")
    )
    assert chr(0x202D) in raw  # sanity: the test fixture actually contains the bug trigger
    items = bqb.split_column_items(raw, "numeric")
    assert items == [
        {"label": "1", "text": "Eradication", "image": None},
        {"label": "2", "text": "Vector", "image": None},
        {"label": "3", "text": "Isolation", "image": None},
    ]


def test_split_column_items_vert1ran_left_column_real_fixture():
    items = bqb.split_column_items(_VERT1RAN_LEFT_RAW, "numeric")
    assert len(items) == 15
    # Left column is lettered, not numbered — must be recognized and
    # uppercased despite label_charset="numeric" (the old hardcoded
    # left=numeric assumption is exactly what broke this real PDF).
    assert [it["label"] for it in items] == [chr(ord("A") + i) for i in range(15)]
    assert items[0]["text"] == (
        "more than expected cases in a particular area or population "
        "during a particular period, affecting a significant proportion of "
        "a community")


def test_split_column_items_vert1ran_right_column_real_fixture():
    items = bqb.split_column_items(_VERT1RAN_RIGHT_RAW, "alpha")
    assert len(items) == 15
    assert [it["text"] for it in items] == [
        "agent", "cluster", "determinant", "epidemic", "fomite", "incidence",
        "latency period", "morbidity", "pathogenicity", "quarantine", "risk",
        "spectrum of illness", "vector", "zoonosis", "outlier",
    ]


def test_parse_matching_key_line_exact_length():
    pairs = bqb._parse_matching_key_line("A,C,B", ["1", "2", "3"])
    assert pairs == {"1": "A", "2": "C", "3": "B"}


def test_parse_matching_key_line_rejects_length_mismatch():
    assert bqb._parse_matching_key_line("A,C", ["1", "2", "3"]) is None


def test_parse_matching_key_line_rejects_no_letters():
    assert bqb._parse_matching_key_line("see diagram", ["1", "2"]) is None


# ---------------------------------------------------------------------------
# apply_annotations threading qtype/matching through field_overrides and
# the "added" question-defaults path (Part 1 of the matching-question plan)
# ---------------------------------------------------------------------------

def _matching_question(number="5"):
    return {
        "number": number, "topic": "Other / General", "text": "Match each item.",
        "choices": [], "answer": "", "images": [],
        "source": "2024 Div-C: test", "year": "2024", "division": "C",
        "qtype": "matching",
        "matching": {
            "left":  [{"label": "1", "text": "Resistor", "image": None}],
            "right": [{"label": "A", "text": "Limits current", "image": None}],
            "pairs": {"1": "A"},
        },
    }


def test_apply_annotations_field_override_persists_matching_edit():
    questions = [_matching_question()]
    edited = {**_matching_question()["matching"]}
    edited["pairs"] = {}   # simulate the reviewer clearing the answer key
    ann = {"field_overrides": {"5": {"matching": edited, "qtype": "matching"}}}
    out = bqb.apply_annotations(questions, ann)
    assert out[0]["qtype"] == "matching"
    assert out[0]["matching"]["pairs"] == {}


def test_apply_annotations_added_question_keeps_matching_fields():
    added = _matching_question(number="7")
    ann = {"added": [added]}
    out = bqb.apply_annotations([], ann)
    assert len(out) == 1
    assert out[0]["qtype"] == "matching"
    assert out[0]["matching"]["pairs"] == {"1": "A"}


def test_apply_annotations_added_question_defaults_qtype_to_frq():
    # A plain (non-matching) manually-added question shouldn't pick up a
    # stray qtype/matching from the defaulting logic.
    added = {"number": "9", "text": "Free response", "choices": [], "answer": "x"}
    out = bqb.apply_annotations([], {"added": [added]})
    assert out[0]["qtype"] == "frq"
    assert out[0]["matching"] is None


# ---------------------------------------------------------------------------
# Markdown export renders a matching question's two columns + answer key
# instead of silently dropping it (Part 1.3 export audit)
# ---------------------------------------------------------------------------

def test_render_matching_block_renders_table_and_pairs():
    lines: list[str] = []
    bqb._render_matching_block(lines, _matching_question()["matching"])
    rendered = "\n".join(lines)
    assert "Resistor" in rendered
    assert "Limits current" in rendered
    assert "1→A" in rendered


def test_render_matching_block_empty_shell_is_noop():
    lines: list[str] = []
    bqb._render_matching_block(lines, {"left": [], "right": [], "pairs": {}})
    assert lines == []


# ---------------------------------------------------------------------------
# extract_questions() — the answer-key latch requires real corroboration,
# not just a KEY_INDICATORS phrase appearing anywhere on the page (see the
# fix comment in extract_questions() for the full rationale).
# ---------------------------------------------------------------------------

def test_extract_questions_instructions_mentioning_answer_sheet_do_not_latch():
    # "answer sheet" is test-instruction language ("record your answers on
    # the answer sheet provided"), not evidence of an actual key page. It is
    # no longer even in KEY_INDICATORS, so this must never end extraction.
    pages = [
        "Please record your answers on the answer sheet provided.\n"
        "1. This is a real question with plenty of substantive text here.",
        "2. A second real question that also has enough substantive text.",
    ]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert [q["number"] for q in qs] == ["1", "2"]


def test_extract_questions_key_phrase_without_structural_evidence_does_not_latch():
    # A KEY_INDICATORS phrase ("solutions") shows up mid-document inside a
    # sentence, but the page it's on doesn't structurally look like a key
    # (no run of short "N. <answer>" lines) — must not latch.
    pages = [
        "1. A real question with plenty of substantive text right here.",
        "The solutions to some of these derivations are shown above in "
        "the appendix for reference.\n"
        "2. Another real question with plenty of substantive text here.",
        "3. A third real question with plenty of substantive text here too.",
    ]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert [q["number"] for q in qs] == ["1", "2", "3"]


def test_extract_questions_genuine_key_page_mid_document_still_latches():
    # A genuine key page (KEY_INDICATORS phrase + several short numbered
    # lines) appearing after page 1 must still end extraction.
    pages = [
        "1. A real question with plenty of substantive text right here.",
        "Answer Key\n1. B\n2. C\n3. A\n4. D\n5. B",
        "6. This question lives after the key page and must be dropped.",
    ]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert [q["number"] for q in qs] == ["1"]


def test_extract_questions_latch_never_fires_on_page_one():
    # Page 1 looks exactly like a key page (KEY_INDICATORS phrase + several
    # short numbered lines) — an answer key never opens a test PDF, so this
    # must not latch, and real content on later pages must still come out.
    pages = [
        "Answer Key\n1. B\n2. C\n3. A\n4. D\n5. B",
        "10. A real question living on page two with substantive text here.",
    ]
    qs = bqb.extract_questions(pages, source="src", year="2027", division="B")
    assert [q["number"] for q in qs] == ["10"]


# ---------------------------------------------------------------------------
# associate_images() — a resource-dictionary xref that get_images() reports
# on a page but that was never actually drawn there (get_image_rects()
# returns []) must not be saved for that page.
# ---------------------------------------------------------------------------

class _FakeRect:
    def __init__(self, y0: float) -> None:
        self.y0 = y0


class _FakePage:
    """Minimal stand-in for fitz.Page covering only what associate_images()
    touches when there are no text blocks and use_vision=False."""

    def __init__(self, rects: list) -> None:
        self._rects = rects

    def get_images(self, full: bool = True):
        return [(4242, 0, 0, 0, 0, 0, 0, 0, 0, 0)]

    def get_image_rects(self, xref: int):
        return self._rects

    def get_text(self, kind: str):
        return []


def test_associate_images_skips_xref_not_actually_drawn_on_page():
    calls: list[int] = []

    def fake_save_image(doc, xref, src_slug, pno, idx):
        calls.append(xref)
        return f"img_{xref}.png"

    orig = bqb._save_image
    bqb._save_image = fake_save_image
    try:
        page = _FakePage(rects=[])   # declared but never drawn on this page
        bqb.associate_images([page], [], "src", False, {})
    finally:
        bqb._save_image = orig
    assert calls == []


def test_associate_images_saves_xref_actually_drawn_on_page():
    calls: list[int] = []

    def fake_save_image(doc, xref, src_slug, pno, idx):
        calls.append(xref)
        return f"img_{xref}.png"

    orig = bqb._save_image
    bqb._save_image = fake_save_image
    try:
        page = _FakePage(rects=[_FakeRect(y0=10.0)])   # actually drawn here
        bqb.associate_images([page], [], "src", False, {})
    finally:
        bqb._save_image = orig
    assert calls == [4242]
