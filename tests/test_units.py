"""
Numerical questions: units.py (parsing, keys, grading), the JS mirror of its
pure helpers (common_ui.py's window.Units), and every server path a
numerical question passes through — gradeability, annotation replay,
PATCH, the state migration, qgen/bundle promotion, assessment snapshot /
take-page sanitising / auto-grading, and the /api/units/* routes.

Run with: `python -m pytest tests/test_units.py -q`
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402
import units  # noqa: E402


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, value", [
    ("4.2", 4.2), ("-1.5e3", -1500.0), ("3.0 × 10^8", 3.0e8), ("2.5×10⁻³", 2.5e-3),
    ("1,200", 1200.0), ("3/4", 0.75), ("−2", -2.0), (".5", 0.5), ("6.02 x 10^(23)", 6.02e23),
    ("$1.5 \\times 10^{-3}$", 1.5e-3),
])
def test_parse_value(text, value):
    assert units.parse_value(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["", "abc", "4.2.1", "1/0", "1e999", "x" * 100])
def test_parse_value_rejects(text):
    with pytest.raises(units.UnitError):
        units.parse_value(text)


@pytest.mark.parametrize("text, n", [
    ("4.20", 3), ("0.0042", 2), ("1200", 4), ("1200.", 4), ("10", 2), ("3.0×10^8", 2), ("0", 1),
    ("0.00", 3), ("2.5×10⁻³", 2), ("1,200", 4), ("100.0", 4), ("7", 1), ("3/4", None),
])
def test_sig_figs_of(text, n):
    assert units.sig_figs_of(text) == n


def test_every_catalog_unit_parses_and_fits_its_quantity():
    for name, (_label, _dim, us) in units.QUANTITIES.items():
        for u in us:
            assert units.unit_matches_quantity(u, name), (name, u)


@pytest.mark.parametrize("unit, quantity", [
    ("m/s", "velocity"), ("km/h", "velocity"), ("kΩ", "resistance"), ("kohm", "resistance"),
    ("µF", "capacitance"), ("uF", "capacitance"), ("N·m", "torque"), ("J", "energy"),
    ("°C", "temperature"), ("%", "fraction"), ("C", "charge"), ("mA", "current"),
    ("kg/s", units.OTHER), ("nonsense-unit", units.OTHER),
])
def test_infer_quantity(unit, quantity):
    assert units.infer_quantity(unit) == quantity


def test_au_is_the_astronomical_unit():
    assert units.infer_quantity("AU") == "length"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_make_key_defaults():
    k = units.make_key("4.20", "m/s")
    assert k == {"value": 4.2, "value_text": "4.20", "unit": "m/s", "quantity": "velocity", "sig_figs": 3}
    assert units.format_key(k) == "4.20 m/s"
    assert units.format_key(units.make_key("75", "%")) == "75%"


@pytest.mark.parametrize("args, fragment", [
    (("4.2", "furlongz"), "recognises"),
    (("4.2", "s", "velocity"), "isn't a unit of velocity"),
    (("four", "m/s"), "isn't a number"),
    (("4.2", "m/s", "velocity", 0), "between 1 and 15"),
    (("4.2", "m/s", "warp"), "unknown quantity"),
])
def test_make_key_rejects(args, fragment):
    with pytest.raises(units.UnitError, match=fragment):
        units.make_key(*args)


def test_key_problem_and_gradeability():
    assert units.key_problem(units.make_key("4.2", "m/s")) == ""
    assert "numerical" in units.key_problem(None)
    bad = {"value_text": "4.2", "unit": "s", "quantity": "velocity", "sig_figs": 2}
    ok, reason = bqb.question_gradeability({"qtype": "numerical", "numeric": bad})
    assert not ok and "velocity" in reason
    assert bqb.question_gradeability({"qtype": "numerical",
                                      "numeric": units.make_key("4.2", "m/s")}) == (True, "")


def test_key_from_answer_text():
    assert units.key_from_answer_text("10 ms")["quantity"] == "time"
    assert units.key_from_answer_text("≈ 3.0 × 10^8 m/s")["value"] == pytest.approx(3e8)
    assert units.key_from_answer_text("0.75")["quantity"] == "fraction"
    assert units.key_from_answer_text("twelve volts") is None
    assert units.key_from_answer_text("4 because it doubles") is None


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

KEY = units.make_key("4.20", "m/s")          # accepts 4.195 .. 4.205 m/s


@pytest.mark.parametrize("value, unit, status, credit", [
    ("4.20", "m/s", "correct", 1.0),
    ("4.2", "m/s", "correct", 1.0),           # fewer digits written is fine
    ("4.2049", "m/s", "correct", 1.0),
    ("4.195", "m/s", "correct", 1.0),         # boundary is inclusive
    ("4.21", "m/s", "wrong_value", 0.0),
    ("15.12", "km/h", "correct", 1.0),        # unit conversion
    ("420", "cm/s", "correct", 1.0),
    ("0.0042", "km/s", "correct", 1.0),
    ("4.2", "", "no_unit", units.NO_UNIT_CREDIT),
    ("9.9", "", "wrong_value", 0.0),          # no unit AND wrong number
    ("4.2", "s", "wrong_dimension", 0.0),
    ("4.2", "furlongz", "unparseable", 0.0),
    ("fast", "m/s", "unparseable", 0.0),
    ("", "", "blank", 0.0),
])
def test_grade(value, unit, status, credit):
    v = units.grade(KEY, value, unit)
    assert v["status"] == status, v
    assert v["credit"] == credit
    assert v["expected"] == "4.20 m/s"


def test_grade_temperature_offsets():
    k = units.make_key("25", "°C")
    assert units.grade(k, "298.15", "K")["status"] == "correct"
    assert units.grade(k, "77", "°F")["status"] == "correct"
    assert units.grade(k, "25", "K")["status"] == "wrong_value"


def test_grade_dimensionless_accepts_a_bare_number_either_way():
    k = units.make_key("75", "%")
    assert units.grade(k, "75", "")["status"] == "correct"
    assert units.grade(k, "0.75", "")["status"] == "correct"
    assert units.grade(k, "3/4", "")["status"] == "correct"
    assert units.grade(k, "0.75", "")["credit"] == 1.0      # no partial-credit penalty
    assert units.grade(k, "0.5", "")["status"] == "wrong_value"


def test_grade_electrical_prefixes():
    assert units.grade(units.make_key("1.5", "kΩ"), "1500", "ohm")["status"] == "correct"
    assert units.grade(units.make_key("4.7", "µF"), "4700", "nF")["status"] == "correct"
    assert units.grade(units.make_key("12", "mA"), "0.012", "A")["status"] == "correct"


def test_tolerance_follows_sig_figs():
    assert units.tolerance(units.make_key("4.20", "m")) == pytest.approx(0.005)
    assert units.tolerance(units.make_key("4.2", "m")) == pytest.approx(0.05)
    assert units.tolerance(units.make_key("1200", "m")) == pytest.approx(0.5)
    assert units.tolerance(units.make_key("10", "ms")) == pytest.approx(0.5)     # 9.5 .. 10.5
    assert units.tolerance(units.make_key("0.00", "V")) == pytest.approx(0.005)
    loose = units.make_key("4.20", "m/s", sig_figs=2)
    assert units.grade(loose, "4.24", "m/s")["status"] == "correct"


def test_check_unit_messages():
    assert units.check_unit("km/h", "velocity") == {"ok": True, "message": ""}
    assert units.check_unit("", "velocity")["message"] == "no unit given — this answer needs one"
    assert "isn't a unit of velocity" in units.check_unit("kg", "velocity")["message"]
    assert units.check_unit("", "fraction")["ok"]


def test_broken_key_grades_zero_not_crash():
    v = units.grade({"value_text": "x", "unit": "zz"}, "1", "m")
    assert v["credit"] == 0.0


# ---------------------------------------------------------------------------
# JS mirror (common_ui.py window.Units)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node not installed")
def test_js_helpers_match_python():
    import common_ui
    src = common_ui.COMMON_JS
    start = src.index("window.Units = (function(){")
    end = src.index("})();", start) + len("})();")
    iife = src[start:end]
    samples = ["4.20", "0.0042", "1200", "1200.", "10", "3.0×10^8", "0", "0.00", "2.5×10⁻³",
               "1,200", "100.0", "7", "3/4", "abc"]
    keys = [units.make_key("4.20", "m/s"), units.make_key("75", "%"), units.make_key("12", "")]
    script = ("const window = {};\n" + iife + "\n"
              f"const S = {json.dumps(samples, ensure_ascii=False)};\n"
              f"const K = {json.dumps(keys, ensure_ascii=False)};\n"
              "console.log(JSON.stringify({sf: S.map(s => window.Units.sigFigs(s)),"
              " fk: K.map(k => window.Units.formatKey(k)),"
              " sp: window.Units.splitAnswer('4.2 m/s')}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    got = json.loads(out)
    assert got["sf"] == [units.sig_figs_of(s) for s in samples]
    assert got["fk"] == [units.format_key(k) for k in keys]
    assert got["sp"] == {"value_text": "4.2", "unit": "m/s"}


# ---------------------------------------------------------------------------
# Server paths
# ---------------------------------------------------------------------------

def test_apply_annotations_normalises_a_typed_key():
    q = {"number": "1", "text": "How fast?", "qtype": "frq", "answer": "", "choices": [], "images": []}
    ann = {"field_overrides": {"1": {"qtype": "numerical",
                                      "numeric": {"value_text": "4.20", "unit": "km/h", "quantity": "",
                                                  "sig_figs": None, "value": 999}}}}
    [out] = bqb.apply_annotations([q], ann)
    assert out["numeric"]["value"] == 4.2            # client's value ignored
    assert out["numeric"]["quantity"] == "velocity"
    assert out["numeric"]["sig_figs"] == 3
    assert out["answer"] == "4.20 km/h"


def test_migration_promotes_imported_numericals():
    state = {"_schema_version": 3, "questions": {"b": [
        {"number": "1", "qtype": "frq", "answer": "10 ms", "import_meta": {"qtype": "numerical"}},
        {"number": "2", "qtype": "frq", "answer": "about ten", "import_meta": {"qtype": "numerical"}},
        {"number": "3", "qtype": "frq", "answer": "10 ms"},
    ]}}
    bqb._migrate_state(state)
    q1, q2, q3 = state["questions"]["b"]
    assert q1["qtype"] == "numerical" and q1["numeric"]["unit"] == "ms"
    assert q2["qtype"] == "frq" and "numeric" not in q2
    assert q3["qtype"] == "frq"
    assert q1["numeric"]["sig_figs"] == 2                  # "10" counts both digits
    assert state["_schema_version"] == bqb.STATE_SCHEMA_VERSION


def test_migration_upgrades_old_whole_number_sig_figs():
    old_default = {"value": 10.0, "value_text": "10", "unit": "ms", "quantity": "time", "sig_figs": 1}
    hand_set = {"value": 1200.0, "value_text": "1200", "unit": "m", "quantity": "length", "sig_figs": 3}
    decimal = {"value": 4.2, "value_text": "4.20", "unit": "m/s", "quantity": "velocity", "sig_figs": 3}
    state = {"_schema_version": 4, "questions": {"b": [
        {"number": "1", "qtype": "numerical", "numeric": dict(old_default)},
        {"number": "2", "qtype": "numerical", "numeric": dict(hand_set)},
        {"number": "3", "qtype": "numerical", "numeric": dict(decimal)},
    ]}}
    bqb._migrate_state(state)
    a, b, c = (q["numeric"]["sig_figs"] for q in state["questions"]["b"])
    assert (a, b, c) == (2, 3, 3)    # default upgraded; hand-set and decimals untouched


def test_qgen_numerical_candidate():
    import qgen
    q = qgen.candidate_to_question({"type": "numerical", "text": "t", "answer": "4.2 m/s"}, "7", "x")
    assert q["qtype"] == "numerical" and q["numeric"]["quantity"] == "velocity"
    q = qgen.candidate_to_question({"type": "numerical", "text": "t", "answer": "fast"}, "7", "x")
    assert "qtype" not in q


def test_markdown_shows_key_and_sig_figs():
    lines: list[str] = []
    q = {"source": "s", "number": "1", "text": "How fast?", "qtype": "numerical",
         "numeric": units.make_key("4.20", "m/s"), "answer": "4.20 m/s", "choices": [], "images": []}
    bqb._render_question_block(lines, q, 1)
    body = "\n".join(lines)
    assert "**Numerical answer:** ______" in body
    assert "**Answer:** 4.20 m/s (3 s.f.)" in body


def test_assessment_snapshot_and_grading():
    import assessments
    q = {"number": "5", "text": "How fast?", "qtype": "numerical",
         "numeric": units.make_key("4.20", "m/s"), "answer": "4.20 m/s"}
    entry = assessments._snapshot_one_question(q, "b.pdf", 2.0)
    assert entry["quantity"] == "velocity"
    assert entry["correct_numeric"]["value"] == 4.2 and entry["choices"] == []
    g = assessments._grade_numerical(entry["correct_numeric"], "15.12", "km/h", 2.0)
    assert g["correct"] and g["points_earned"] == 2.0
    g = assessments._grade_numerical(entry["correct_numeric"], "4.2", "", 2.0)
    assert not g["correct"] and g["points_earned"] == 1.0 and g["numeric"]["status"] == "no_unit"
    md = "\n".join(assessments._render_question(entry, 1, include_answers=True))
    assert "(include units)" in md and "4.20 m/s (3 significant figures)" in md
    student = "\n".join(assessments._render_question(entry, 1, include_answers=False))
    assert "4.20" not in student


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="units-")
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
    auth.create_user("stu1", "password123", "student")
    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    bqb.set_event(slug)
    with bqb._state_transaction() as st:
        st.setdefault("questions", {})["t.pdf"] = [
            {"number": "1", "text": "How fast is it going?", "qtype": "frq",
             "answer": "4.20 m/s", "choices": [], "images": []},
        ]
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    token = c.get_cookie("csrf_token").value
    yield c, slug, token, review_app
    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _q1(slug):
    bqb.set_event(slug)
    return bqb._load_state()["questions"]["t.pdf"][0]


def test_patch_switch_to_numerical_prefills_from_answer(client):
    c, slug, token, _ = client
    r = c.patch(f"/event/{slug}/api/q/t.pdf/1", json={"qtype": "numerical"},
                headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    q = _q1(slug)
    assert q["qtype"] == "numerical" and q["numeric"]["quantity"] == "velocity"


def test_patch_numeric_validates_and_rederives_answer(client):
    c, slug, token, _ = client
    r = c.patch(f"/event/{slug}/api/q/t.pdf/1",
                json={"numeric": {"value_text": "15.1", "unit": "km/h", "quantity": None, "sig_figs": None}},
                headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.get_data(as_text=True)
    q = _q1(slug)
    assert q["answer"] == "15.1 km/h" and q["numeric"]["sig_figs"] == 3 and q["qtype"] == "numerical"
    r = c.patch(f"/event/{slug}/api/q/t.pdf/1",
                json={"numeric": {"value_text": "15", "unit": "s", "quantity": "velocity"}},
                headers={"X-CSRF-Token": token})
    assert r.status_code == 400 and "velocity" in r.get_json()["error"]
    assert _q1(slug)["answer"] == "15.1 km/h"          # bad request changed nothing


def test_unit_routes(client):
    c, slug, token, _ = client
    cat = c.get("/api/units/catalog").get_json()
    assert any(q["name"] == "velocity" for q in cat["quantities"])
    r = c.post("/api/units/check", json={"unit": "s", "quantity": "velocity"},
               headers={"X-CSRF-Token": token}).get_json()
    assert r["ok"] is False
    r = c.post("/api/units/check", json={"unit": "kΩ", "value": "4.70", "describe": True},
               headers={"X-CSRF-Token": token}).get_json()
    assert r["ok"] and r["quantities"][0] == "resistance" and r["sig_figs"] == 3
    r = c.post("/api/units/grade", json={"key": units.make_key("4.20", "m/s"), "value": "420", "unit": "cm/s"},
               headers={"X-CSRF-Token": token}).get_json()
    assert r["status"] == "correct"


def test_students_cannot_use_the_grade_route(client):
    c, slug, token, review_app = client
    s = review_app.app.test_client()
    s.post("/login", data={"username": "stu1", "password": "password123"})
    tok = s.get_cookie("csrf_token").value
    r = s.post("/api/units/grade", json={"key": units.make_key("1", "m"), "value": "1", "unit": "m"},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 403
    # ...but the unit check (no answer involved) is open to them.
    r = s.post("/api/units/check", json={"unit": "m/s"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200


def test_take_page_never_receives_the_key():
    """api_take_assessment's sanitiser strips correct_numeric along with
    correct_answer (source-level check: the strip list is one tuple)."""
    src = Path(__file__).resolve().parent.parent.joinpath("review_app.py").read_text(encoding="utf-8")
    m = re.search(r'if k not in \(([^)]*)\)\}', src)
    assert m and '"correct_numeric"' in m.group(1) and '"correct_answer"' in m.group(1)
