"""
season_admin.py (operator CLI: reset a season, stage a week of practice
tests) plus the pieces it added elsewhere: count answers and the unit
superscript fix in units.py, bundle_import's keep_topics/dedup/level
options, events.extend_event_topics.

Run with: `python -m pytest tests/test_season_admin.py -q`
"""
from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import units  # noqa: E402


# ---------------------------------------------------------------------------
# units: counts + superscripts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer, unit, quantity", [
    ("8.97 g/cm³", "g/cm³", "density"),
    ("14.2 m³/s", "m³/s", "flow_rate"),
    ("3.2 × 10⁻⁶ m³", "m³", "volume"),
    ("3.0 × 10⁻⁴ m²", "m²", "area"),
    ("24 runs", "runs", "count"),
    ("2.0 × 10^10 electrons", "electrons", "count"),
    ("200 per 100,000", "per 100,000", "count"),
])
def test_imported_answers_parse(answer, unit, quantity):
    k = units.key_from_answer_text(answer)
    assert k is not None, answer
    assert (k["unit"], k["quantity"]) == (unit, quantity)


@pytest.mark.parametrize("answer", ["4 because it doubles", "twelve volts", "4 m/s/s/s/s x y z"])
def test_prose_is_not_a_count(answer):
    assert units.key_from_answer_text(answer) is None


def test_count_grading():
    k = units.key_from_answer_text("48 chromatids")
    assert units.grade(k, "48", "")["status"] == "correct"            # bare number is complete
    assert units.grade(k, "48", "Chromatid")["status"] == "correct"   # label, any case/plural
    assert units.grade(k, "48", "cells")["status"] == "wrong_dimension"
    assert units.grade(k, "48", "kg")["status"] == "wrong_dimension"
    assert units.grade(k, "47", "chromatids")["status"] == "wrong_value"
    assert units.grade(units.key_from_answer_text("200 per 100,000"), "200", "per 100000")["status"] == "correct"


def test_editors_need_count_chosen_explicitly():
    with pytest.raises(units.UnitError):
        units.make_key("24", "runs")                   # typo'd unit is an error in an editor…
    assert units.make_key("24", "runs", "count")["quantity"] == "count"   # …unless Count is picked
    assert "Count" in units.check_unit("runs", None)["message"]
    assert units.check_unit("runs", "count")["ok"]
    assert not units.check_unit("kg", "count")["ok"]


# ---------------------------------------------------------------------------
# Fixture: a data root with junk to clean and an old season to wipe
# ---------------------------------------------------------------------------

def _bundle_zip(event, questions):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"bundle_version": "1.0", "event": event, "season": "2027",
                                                  "contexts": [], "questions": questions}))
        zf.writestr("README.txt", "x")
    return buf.getvalue()


def _q(i, **kw):
    q = {"id": f"q{i:04d}", "topic": "Plant Cells", "qtype": "mcq", "level": 1, "chapter": "01.02",
         "text": f"Which organelle number {i} performs photosynthesis in leaf tissue sample {i * 3}?",
         "choices": [{"letter": L, "text": f"opt {L}"} for L in "ABCD"], "answer": "B",
         "justification": "because", "difficulty": 0.3, "images": [], "context_id": None}
    q.update(kw)
    return q


@pytest.fixture()
def env(monkeypatch, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DATA_ROOT", str(root))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import build_question_bank as bqb
    previous_event = bqb.current_event()
    import auth, events, seasons, assessments, deletion, bundle_import
    for mod in (events, bqb, auth, seasons, assessments, deletion, bundle_import):
        importlib.reload(mod)
    for slug, name, topics in (("circuit_lab", "Circuit Lab", ["Ohm's Law"]),
                               ("anatomy_physiology", "Anatomy & Physiology", ["Bones"])):
        if slug not in events.EVENTS:              # circuit_lab ships seeded
            events.add_custom_event(slug, name, topics=topics)
    auth.create_user("stu1", "password123", "student")

    bqb.set_event("circuit_lab")
    with bqb._state_transaction() as st:
        st["questions"] = {
            "circuitlab_2020_b_x_test.pdf": [{"number": "1", "text": "pdf q", "answer": "A",
                                             "choices": [], "topic": "Ohm's Law"}],
            "_generated_circuit_lab.pdf": [
                {"number": "2", "text": "good", "qtype": "frq", "answer": "5 V", "topic": "Ohm's Law"},
                {"number": "3", "text": "no answer", "qtype": "frq", "answer": "", "topic": "Ohm's Law"},
                {"number": "4", "text": "no topic", "qtype": "frq", "answer": "x", "topic": "Other / General"},
            ],
        }
        st["annotations"] = {"circuitlab_2020_b_x_test.pdf": {"field_overrides": {}}}

    seasons.create_season("old", event_slugs=["circuit_lab"], created_by="t")
    seasons.set_roster("old", "circuit_lab", ["stu1"])
    w = assessments.create_window("old", "2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00",
                                  ["circuit_lab"], label="Old")

    week = tmp_path / "week.zip"
    with zipfile.ZipFile(week, "w") as zf:
        zf.writestr("Circuit_Lab_2027_export.zip", _bundle_zip("Circuit Lab", [
            _q(1, topic="Foundations"), _q(2, qtype="numerical", choices=[], answer="24 runs"),
            _q(3, qtype="tf", choices=[], answer="True")]))
        zf.writestr("Anatomy_and_Physiology_2027_export.zip", _bundle_zip("Anatomy and Physiology", [_q(4)]))
        zf.writestr("Botany_2027_export.zip", _bundle_zip("Botany", [_q(5), _q(6)]))

    import season_admin
    importlib.reload(season_admin)

    def run(*argv):
        season_admin.main(["--data-root", str(root), *argv])

    yield run, week, root, bqb
    for mod in (events, bqb, auth, seasons, assessments, deletion, bundle_import):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


STAGE = ["--season", "2027", "--label", "Week 1", "--date", "2026-09-30", "--start", "12:00",
         "--end", "16:00", "--tz", "America/New_York", "--go-live"]


def test_dry_runs_change_nothing(env, capsys):
    run, week, root, bqb = env
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    run("inspect", str(week))
    run("reset", "--season", "2027", "--events-from", str(week), "--copy-rosters-from", "old")
    out = capsys.readouterr().out
    assert "Anatomy and Physiology" in out and "→ anatomy_physiology" in out   # "and" vs "&"
    assert "botany" in out and "NEW" in out
    assert "Dry run" in out
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before


def test_reset_then_stage_week(env, capsys):
    run, week, root, bqb = env
    import seasons, assessments, events
    run("reset", "--season", "2027", "--events-from", str(week), "--copy-rosters-from", "old", "--apply")

    bqb.set_event("circuit_lab")
    st = bqb._load_state()
    assert "circuitlab_2020_b_x_test.pdf" not in st["questions"]
    assert "circuitlab_2020_b_x_test.pdf" not in st["annotations"]
    assert [q["number"] for q in st["questions"]["_generated_circuit_lab.pdf"]] == ["2"]
    assert list(seasons.load_seasons()) == ["2027"]
    assert seasons.load_seasons()["2027"].is_current
    assert assessments.load_windows() == {} or all(w.season_id == "2027" for w in assessments.load_windows().values())
    assert seasons.get_roster("2027", "circuit_lab") == ["stu1"]
    assert "botany" in events.EVENTS
    assert list(root.joinpath(".season_admin_backups").glob("*-reset.tar.gz"))

    run("stage-week", str(week), *STAGE, "--apply")
    out = capsys.readouterr().out
    [w] = assessments.windows_for_season("2027")
    assert (w.opens_at, w.closes_at) == ("2026-09-30T16:00:00+00:00", "2026-09-30T20:00:00+00:00")
    t = assessments.get_assessment_for(w.window_id, "circuit_lab")
    assert t.status == "live"
    assert [q["qtype"] for q in t.snapshot] == ["mcq", "numerical", "tf"]         # bundle order
    assert t.snapshot[1]["correct_numeric"]["quantity"] == "count"
    assert "Foundations" in events.EVENTS["circuit_lab"].topics                  # topic added
    bqb.set_event("circuit_lab")
    imported = [q for q in bqb._load_state()["questions"]["_generated_circuit_lab.pdf"]
                if q.get("import_meta")]
    assert imported[0]["topic"] == "Foundations"                                 # kept verbatim
    assert imported[0]["import_meta"]["level"] == 1 and imported[0]["import_meta"]["chapter"] == "01.02"
    assert imported[0]["validation"]["status"] == "correct"
    assert assessments.get_assessment_for(w.window_id, "botany").status == "live"
    assert "no students rostered" in out

    # Running it again is a no-op: same questions reused, tests left alone.
    run("stage-week", str(week), *STAGE, "--apply")
    out = capsys.readouterr().out
    assert "0 new,   3 already imported" in out and "already live" in out
    assert len(assessments.windows_for_season("2027")) == 1


def test_conflicting_window_refused_before_any_write(env):
    run, week, root, bqb = env
    run("reset", "--season", "2027", "--events-from", str(week), "--apply")
    run("stage-week", str(week), *STAGE, "--apply")
    backups = len(list(root.joinpath(".season_admin_backups").iterdir()))
    with pytest.raises(SystemExit, match="different times"):
        run("stage-week", str(week), *[a if a != "12:00" else "13:00" for a in STAGE], "--apply")
    assert len(list(root.joinpath(".season_admin_backups").iterdir())) == backups


def test_instance_guards(tmp_path, monkeypatch):
    import season_admin
    # main() loads the instance's .env into os.environ; registering DATA_ROOT
    # with monkeypatch first makes sure it's put back for later tests.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    conf = tmp_path / "instances.conf"
    app = tmp_path / "app"
    app.mkdir()
    (tmp_path / ".env").write_text(f"DATA_ROOT={tmp_path / 'data'}\n")
    conf.write_text(f"# c\nncms:NCMS:{app}:qbank.service:someone-else:5000\n")
    with pytest.raises(SystemExit, match="sudo -u someone-else"):
        season_admin.main(["--instance", "ncms", "--conf", str(conf), "inspect"])
    monkeypatch.setattr(season_admin, "service_active", lambda svc: True)
    monkeypatch.setattr(season_admin.getpass, "getuser", lambda: "someone-else")
    with pytest.raises(SystemExit, match="Stop it first"):
        season_admin.main(["--instance", "ncms", "--conf", str(conf), "reset", "--season", "x",
                           "--events-from", "nowhere.zip", "--apply"])
    with pytest.raises(SystemExit, match="no instance"):
        season_admin.main(["--instance", "chs", "--conf", str(conf), "inspect"])


# ---------------------------------------------------------------------------
# accounts: student + volunteer logins from a JSON file
# ---------------------------------------------------------------------------

def _accounts_file(tmp_path, **doc):
    body = {"format": "scioly-accounts/1", "season": "2027", "students": [], "volunteers": []}
    body.update(doc)
    p = tmp_path / f"accounts{len(list(tmp_path.glob('accounts*.json')))}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return str(p)


@pytest.fixture()
def season_2027(env, monkeypatch):
    run, week, root, bqb = env
    monkeypatch.setenv("SCHOOL_NAME", "NCMS")
    run("reset", "--season", "2027", "--events-from", str(week), "--apply")
    return env


def test_accounts_create_roster_and_rerun(season_2027, tmp_path, capsys):
    run, week, root, bqb = season_2027
    import auth, seasons
    f = _accounts_file(tmp_path, students=[
        {"display_name": "Jane Doe", "events": ["Anatomy & Physiology", "circuit lab"]},
        {"display_name": "Sam Lee", "username": "saml", "events": ["botany"]},
    ], volunteers=[{"display_name": "Pat Doe", "username": "patd", "events": ["Anatomy and Physiology"]}])

    before = {p: p.read_bytes() for p in root.glob("*.json")}
    run("accounts", f)                                            # dry run
    assert "Dry run" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in root.glob("*.json")} == before

    run("accounts", f, "--apply")
    out = capsys.readouterr().out
    jane = auth.get_user("janedoe")
    assert jane.role == "student" and jane.display_name == "Jane Doe" and jane.must_change_password
    assert auth.verify_login("janedoe", "ncms2027janedoe") is not None
    assert auth.get_user("patd").events == ("anatomy_physiology",)
    assert auth.get_user("patd").role == "volunteer"
    assert seasons.get_roster("2027", "anatomy_physiology") == ["janedoe"]
    assert seasons.get_roster("2027", "circuit_lab") == ["janedoe"]    # reset didn't copy rosters
    assert seasons.get_roster("2027", "botany") == ["saml"]
    [creds] = root.joinpath(".season_admin_backups").glob("*-accounts-credentials.csv")
    assert "ncms2027saml" in creds.read_text(encoding="utf-8")
    assert "Starting passwords for 3" in out

    # Same file again: finds janedoe (no janedoe2), touches nothing.
    auth.change_own_password("janedoe", "ncms2027janedoe", "janeschoice1")
    run("accounts", f, "--apply")
    out = capsys.readouterr().out
    assert "janedoe2" not in out and auth.get_user("janedoe2") is None
    assert auth.verify_login("janedoe", "janeschoice1") is not None
    assert "Starting passwords" not in out


def test_accounts_errors_block_everything(season_2027, tmp_path, capsys):
    run, week, root, bqb = season_2027
    import auth
    f = _accounts_file(tmp_path, students=[
        {"display_name": "Ok Kid", "events": ["botany"]},
        {"display_name": "Bad Event", "events": ["Underwater Basket Weaving"]},
        {"display_name": "Clash", "username": "stu1x", "events": []},
        {"display_name": "Clash Again", "username": "stu1x", "events": []},
    ], volunteers=[{"display_name": "Wrong Role", "username": "stu1", "events": []}])
    with pytest.raises(SystemExit):
        run("accounts", f, "--apply")
    out = capsys.readouterr().out
    assert "unknown event 'Underwater Basket Weaving'" in out
    assert "more than once" in out and "already exists as a student" in out
    assert auth.get_user("okkid") is None


def test_accounts_replace_and_window_assignment(season_2027, tmp_path):
    run, week, root, bqb = season_2027
    import auth, seasons, assessments
    run("stage-week", str(week), *STAGE, "--apply")
    run("accounts", _accounts_file(tmp_path, students=[
        {"display_name": "A Kid", "username": "akid", "events": ["botany", "circuit_lab"]}],
        volunteers=[{"display_name": "V One", "username": "vone", "events": ["botany"]}]), "--apply")
    run("accounts", _accounts_file(tmp_path, students=[
        {"display_name": "B Kid", "username": "bkid", "events": ["botany"]}],
        volunteers=[{"display_name": "V One", "username": "vone", "events": ["circuit_lab"]}]),
        "--apply", "--replace", "--assign-windows")
    assert seasons.get_roster("2027", "botany") == ["bkid"]
    assert seasons.get_roster("2027", "circuit_lab") == []
    assert auth.get_user("vone").events == ("circuit_lab",)
    [w] = assessments.windows_for_season("2027")
    assert w.assignments.get("circuit_lab") == ["vone"] and not w.assignments.get("botany")


def test_accounts_reset_passwords(season_2027, tmp_path):
    run, week, root, bqb = season_2027
    import auth
    f = _accounts_file(tmp_path, students=[{"display_name": "Forgetful", "username": "forg",
                                            "events": ["botany"]}])
    run("accounts", f, "--apply")
    auth.change_own_password("forg", "ncms2027forg", "forgotthis1")
    run("accounts", f, "--apply", "--reset-passwords")
    user = auth.get_user("forg")
    assert user.must_change_password and auth.verify_login("forg", "ncms2027forg") is not None
