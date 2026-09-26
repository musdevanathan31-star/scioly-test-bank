"""
Question-bundle import (bundle_import.py + the /sources/bundle/* routes).

A bundle is the zip QUESTION_EXPORT_PROMPT.md asks other Claude chats to
produce: manifest.json + images/. Covered here:
  - field mapping per qtype (numerical -> frq with import_meta, multi-letter
    MCQ answers, re-lettered choices, T/F normalisation, difficulty, topics)
  - zip safety: a hostile member path, SVG, mislabelled image bytes, size caps
  - stage = preview only (bank untouched); import = background job that
    writes questions, images and shared contexts, then deletes the upload
  - re-importing the same bundle only yields duplicates
  - a cancelled import leaves the bank and image dir as they were

Run with: `python -m pytest tests/test_bundle_import.py -q`
"""
from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_question_bank as bqb  # noqa: E402
import bundle_import  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


def _q(i, **kw):
    """A question whose text shares almost nothing with any other _q(i)."""
    words = ["capacitor", "inductor", "resistor", "diode", "battery", "transistor",
             "voltmeter", "ammeter", "oscilloscope", "transformer", "thermistor", "fuse"]
    base = {
        "id": f"q{i:04d}",
        "topic": "",
        "qtype": "frq",
        "text": f"Explain what happens to the {words[i % len(words)]} number {i} "
                f"when {words[(i * 5 + 3) % len(words)]} stage {i * 7} is doubled.",
        "choices": [],
        "answer": "it doubles",
        "justification": "worked solution",
        "difficulty": None,
        "images": [],
    }
    base.update(kw)
    return base


def _zip(manifest, images=None, extra=None, prefix=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "manifest.json", json.dumps(manifest))
        for name, data in (images or {}).items():
            zf.writestr(prefix + "images/" + name, data)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _manifest(questions, contexts=None, event="Circuit Lab"):
    return {"bundle_version": "1.0", "event": event, "season": "2027",
            "generated_at": "2026-09-11T00:00:00Z",
            "contexts": contexts or [], "questions": questions}


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bundle-")
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

    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    token = c.get_cookie("csrf_token").value

    class Env:
        pass
    e = Env()
    e.c, e.slug, e.csrf, e.app = c, slug, token, review_app
    e.topics = list(bqb.EVENT.topics)
    e.bucket = f"_generated_{slug}.pdf"
    yield e

    for mod in (events, bqb, auth, seasons, assessments):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def _stage(e, data, name="bundle.zip"):
    return e.c.post(f"/event/{e.slug}/api/sources/bundle/stage",
                    data={"file": (io.BytesIO(data), name)},
                    headers={"X-CSRF-Token": e.csrf},
                    content_type="multipart/form-data")


def _import(e, token, mark_validated=False):
    return e.c.post(f"/event/{e.slug}/api/sources/bundle/import",
                    json={"token": token, "mark_validated": mark_validated},
                    headers={"X-CSRF-Token": e.csrf})


def _wait(e, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = e.c.get(f"/event/{e.slug}/api/jobs/{job_id}").get_json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    raise AssertionError("job didn't finish")


def _bucket(e):
    bqb.set_event(e.slug)
    return bqb._load_state().get("questions", {}).get(e.bucket, [])


def _convert(bq, topics=("Alpha Topic", "Beta Topic"), images=None):
    return bundle_import.convert_question(
        bq, digest="deadbeef", topics=list(topics), season="2027",
        source_label="Imported · test", image_names=images or {},
        classify=lambda text: "Other / General")


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer, expected", [
    ("4.2 m/s", {"value": 4.2, "unit": "m/s"}),
    ("-1.5", {"value": -1.5, "unit": ""}),
    ("1,200 J", {"value": 1200.0, "unit": "J"}),
    ("3.0e8 m/s", {"value": 3.0e8, "unit": "m/s"}),
    ("3.0 × 10^8 m/s", {"value": 3.0e8, "unit": "m/s"}),
    ("2.5 × 10⁻³ A", {"value": 2.5e-3, "unit": "A"}),
    ("$4.2\\ \\text{m/s}$", {"value": 4.2, "unit": "m/s"}),
    ("about 12 J", {"value": 12.0, "unit": "J"}),
    ("twelve volts", None),
    ("4 because the current doubles when the resistance halves", None),
    ("", None),
])
def test_parse_numeric_answer(answer, expected):
    got = bundle_import.parse_numeric_answer(answer)
    if expected is None:
        assert got is None
    else:
        assert got["unit"] == expected["unit"]
        assert got["value"] == pytest.approx(expected["value"])


def test_numerical_becomes_a_real_numerical_question():
    q, issues = _convert(_q(1, qtype="numerical", answer="4.20 m/s", topic="Alpha Topic"))
    assert q["qtype"] == "numerical"
    assert q["numeric"]["unit"] == "m/s" and q["numeric"]["quantity"] == "velocity"
    assert q["numeric"]["sig_figs"] == 3
    assert q["answer"] == "4.20 m/s"
    assert "qtype" not in q["import_meta"]
    assert q["choices"] == []
    assert not issues
    assert bqb.question_gradeability(q) == (True, "")


def test_numerical_explicit_unit_fields_win():
    q, _ = _convert(_q(1, qtype="numerical", answer="4.2 N m", unit="N m",
                       quantity="torque", sig_figs=2))
    assert q["numeric"]["quantity"] == "torque" and q["numeric"]["value"] == 4.2
    assert q["numeric"]["sig_figs"] == 2


def test_unparseable_numerical_falls_back_to_frq_but_is_remembered():
    q, issues = _convert(_q(1, qtype="numerical", answer="about four metres per second"))
    assert q["qtype"] == "frq"
    assert q["import_meta"]["qtype"] == "numerical"
    assert any("imported as frq" in i for i in issues)


def test_multi_letter_mcq_answer_is_kept():
    ch = [{"letter": L, "text": f"opt {L}"} for L in "ABCD"]
    q, _ = _convert(_q(2, qtype="mcq", choices=ch, answer="A, C"))
    assert q["qtype"] == "mcq"
    assert q["answer"] == "A, C"
    assert bqb.question_gradeability(q) == (True, "")


def test_choices_are_relettered_and_the_answer_follows():
    ch = [{"letter": "b", "text": "first"}, {"letter": "c", "text": "second"},
          {"letter": "d", "text": "third"}]
    q, _ = _convert(_q(3, qtype="mcq", choices=ch, answer="c"))
    assert [c["letter"] for c in q["choices"]] == ["A", "B", "C"]
    assert q["answer"] == "B"


def test_tf_answer_is_normalised():
    q, issues = _convert(_q(4, qtype="tf", answer="true"))
    assert q["qtype"] == "tf" and q["answer"] == "True" and q["choices"] == []
    assert not issues


@pytest.mark.parametrize("raw, stored, has_issue", [
    (None, None, False),
    (0.45, 0.45, False),
    (1.4, 1.0, True),
    ("hard", None, True),
])
def test_difficulty(raw, stored, has_issue):
    q, issues = _convert(_q(5, difficulty=raw))
    if stored is None:
        assert "difficulty" not in q
    else:
        assert q["difficulty"] == pytest.approx(stored)
    assert any("difficulty" in i for i in issues) is has_issue


def test_topic_matching():
    q, _ = _convert(_q(6, topic="beta topic"))
    assert q["topic"] == "Beta Topic" and "topic" not in q["import_meta"]
    q, _ = _convert(_q(7, topic="Something Else Entirely"))
    assert q["topic"] == "Other / General"
    assert q["import_meta"]["topic"] == "Something Else Entirely"


def test_classifier_sees_the_bundle_topic_name():
    """Live finding: an Ohm's-law stem mentioning "voltage" classified as
    AC Circuits on its own; with the bundle's topic name in front it lands in
    Basic Electrical Concepts. The name must reach the classifier."""
    seen = []
    bundle_import.convert_question(
        _q(10, topic="Ohm's Law", text="A 4 ohm resistor carries 3 A. What voltage is across it?"),
        digest="deadbeef", topics=["Alpha Topic"], season="2027",
        source_label="x", image_names={}, classify=lambda t: seen.append(t) or "Alpha Topic")
    assert seen and seen[0].startswith("Ohm's Law ")


def test_justification_becomes_the_explanation_and_context_is_prefixed():
    q, _ = _convert(_q(8, justification="because", context_id="ctx1"))
    assert q["explanation"] == "because"
    assert q["validation"]["status"] == "uncertain"    # provenance only
    assert q["validation"]["rationale"] == ""
    assert q["context_id"] == "impdeadbeef_ctx1"


def test_missing_text_is_rejected():
    q, issues = _convert(_q(9, text="   "))
    assert q is None and issues


# ---------------------------------------------------------------------------
# Reading the upload
# ---------------------------------------------------------------------------

def _open(tmp_path, data, name="b.zip"):
    p = tmp_path / name
    p.write_bytes(data)
    return bundle_import.open_bundle(p)


def test_bare_manifest_is_accepted(tmp_path):
    b = _open(tmp_path, json.dumps(_manifest([_q(1)])).encode(), "manifest.json")
    assert not b.is_zip and len(b.manifest["questions"]) == 1


def test_manifest_inside_a_zipped_folder_is_found(tmp_path):
    b = _open(tmp_path, _zip(_manifest([_q(1, images=["f.png"])]), {"f.png": PNG},
                             prefix="export/"))
    data, why = b.read_image("f.png")
    assert data == PNG, why


def test_zip_without_manifest_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(bundle_import.BundleError, match="manifest"):
        _open(tmp_path, buf.getvalue())


def test_not_json_is_refused(tmp_path):
    with pytest.raises(bundle_import.BundleError):
        _open(tmp_path, b"this is not json", "manifest.json")


def test_svg_and_mislabelled_images_are_refused(tmp_path):
    b = _open(tmp_path, _zip(_manifest([_q(1)]),
                             {"a.svg": b"<svg onload='x'/>", "b.png": JPEG, "c.jpg": JPEG}))
    assert b.read_image("a.svg")[0] is None
    data, why = b.read_image("b.png")
    assert data is None and "PNG" in why
    assert b.read_image("c.jpg")[0] == JPEG


def test_oversized_image_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle_import, "MAX_IMAGE_BYTES", 10)
    b = _open(tmp_path, _zip(_manifest([_q(1)]), {"big.png": PNG}))
    data, why = b.read_image("big.png")
    assert data is None and "larger" in why


def test_total_image_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle_import, "MAX_TOTAL_IMAGE_BYTES", 100)
    with pytest.raises(bundle_import.BundleError, match="images total"):
        _open(tmp_path, _zip(_manifest([_q(1)]), {"a.png": PNG, "b.png": PNG}))


def test_hostile_member_path_is_written_under_a_server_name():
    name = bundle_import.stored_image_name("deadbeef", "../../../etc/evil.png")
    assert name == "imp_deadbeef_evil.png"
    assert "/" not in name and "\\" not in name


# ---------------------------------------------------------------------------
# Routes + job
# ---------------------------------------------------------------------------

def _full_bundle(e):
    ch = [{"letter": L, "text": f"choice {L}"} for L in "ABCD"]
    qs = [
        _q(1, qtype="mcq", choices=ch, answer="B", topic=e.topics[0],
           images=["q1.png"], difficulty=0.3),
        _q(2, qtype="tf", answer="False", topic=e.topics[0]),
        _q(3, qtype="numerical", answer="4.2 m/s", topic="Not A Real Topic"),
        _q(4, qtype="frq", answer="", context_id="ctx1"),                 # ungradeable
        _q(5, qtype="frq", answer="x", context_id="ctx1", images=["missing.png"]),
        {"id": "q0006", "qtype": "frq", "text": ""},                          # invalid
    ]
    ctx = [{"id": "ctx1", "title": "Shared table", "text": "Table 1 ...",
            "images": ["ctx1.png"]}]
    return _zip(_manifest(qs, ctx), {"q1.png": PNG, "ctx1.png": PNG,
                                     "../../escape.png": PNG})


def test_stage_previews_without_writing(env):
    r = _stage(env, _full_bundle(env))
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    p = j["preview"]
    assert p["total"] == 6 and p["importable"] == 5
    assert len(p["invalid"]) == 1
    assert "Not A Real Topic" in p["remapped_topics"]
    assert any(ip["image"] == "missing.png" for ip in p["image_problems"])
    assert _bucket(env) == []
    assert not any((bqb.EVENT.image_dir).glob("imp_*")) if bqb.EVENT.image_dir.exists() else True
    assert (bqb.EVENT.base_dir / ".imports" / f"{j['token']}.bundle").exists()


def test_stage_rejects_a_bad_file(env):
    r = _stage(env, b"garbage", "x.zip")
    assert r.status_code == 400
    assert not list((bqb.EVENT.base_dir / ".imports").glob("*.bundle"))


def test_import_job_writes_questions_images_and_contexts(env):
    token = _stage(env, _full_bundle(env)).get_json()["token"]
    r = _import(env, token, mark_validated=True)
    assert r.status_code == 200, r.get_data(as_text=True)
    job = _wait(env, r.get_json()["job_id"])
    assert job["status"] == "succeeded", job
    res = job["result"]
    assert res["added"] == 5

    qs = _bucket(env)
    assert len(qs) == 5
    by_id = {q["import_meta"]["id"]: q for q in qs}
    nums = [int(q["number"]) for q in qs]
    assert len(set(nums)) == 5

    q1 = by_id["q0001"]
    assert q1["validation"]["status"] == "correct"
    assert q1["difficulty"] == pytest.approx(0.3)
    assert len(q1["images"]) == 1 and q1["images"][0].startswith("imp_")
    assert (bqb.EVENT.image_dir / q1["images"][0]).read_bytes() == PNG

    assert by_id["q0003"]["qtype"] == "numerical"
    assert by_id["q0003"]["numeric"]["unit"] == "m/s"
    # Ungradeable (no answer) is imported but not certified.
    assert by_id["q0004"]["validation"].get("status") != "correct"
    assert any(s["number"] == by_id["q0004"]["number"]
               for s in res["skipped_validation_ungradeable"])

    ctxs = bqb._all_contexts()
    key = f"{env.bucket}::{by_id['q0004']['context_id']}"
    assert key in ctxs and ctxs[key]["title"] == "Shared table"
    assert (bqb.EVENT.image_dir / ctxs[key]["images"][0]).exists()

    # Only server-named files landed in the image dir; nothing escaped it.
    assert all(p.name.startswith("imp_") for p in bqb.EVENT.image_dir.iterdir())
    assert not (bqb.EVENT.base_dir.parent / "escape.png").exists()

    # The upload is gone, so the token can't be replayed.
    assert not list((bqb.EVENT.base_dir / ".imports").glob(f"{token}.*"))
    assert _import(env, token).status_code == 404

    # The job's progress was reported against a total.
    assert job["total"] and job["done_count"] == job["total"]


def test_reimporting_the_same_bundle_only_finds_duplicates(env):
    data = _full_bundle(env)
    job = _wait(env, _import(env, _stage(env, data).get_json()["token"]).get_json()["job_id"])
    assert job["result"]["added"] == 5
    images_before = sorted(p.name for p in bqb.EVENT.image_dir.iterdir())

    j = _stage(env, data).get_json()
    assert j["preview"]["importable"] == 0
    # The same file again: recognised by its digest, not by fuzzy matching.
    assert len(j["preview"]["already_imported"]) == 5
    assert j["preview"]["duplicates"] == []
    job2 = _wait(env, _import(env, j["token"]).get_json()["job_id"])
    assert job2["status"] == "succeeded" and job2["result"]["added"] == 0
    assert len(_bucket(env)) == 5
    # The first import's images are still there (not "cleaned up" as unused).
    assert sorted(p.name for p in bqb.EVENT.image_dir.iterdir()) == images_before


def test_same_token_cannot_be_queued_twice(env):
    token = _stage(env, _full_bundle(env)).get_json()["token"]
    first = _import(env, token)
    second = _import(env, token)
    assert first.status_code == 200
    assert second.status_code in (404, 409)
    _wait(env, first.get_json()["job_id"])


def test_cancel_leaves_bank_and_images_untouched(env, tmp_path):
    from jobs import JobCancelled
    path = tmp_path / "b.zip"
    path.write_bytes(_full_bundle(env))
    bundle = bundle_import.open_bundle(path)
    calls = {"n": 0}

    def should_cancel():
        # Let image checking + copying finish, cancel during the questions.
        calls["n"] += 1
        return calls["n"] > 8

    bqb.set_event(env.slug)
    with pytest.raises(JobCancelled):
        bundle_import.run_import(bundle, filename="b.zip", bucket=env.bucket,
                                 mark_validated=False,
                                 next_number=env.app._next_global_q_number,
                                 should_cancel=should_cancel,
                                 on_progress=lambda **kw: None)
    assert _bucket(env) == []
    assert "annotations" not in bqb._load_state() or \
        env.bucket not in bqb._load_state()["annotations"]
    img_dir = bqb.EVENT.image_dir
    assert not (img_dir.exists() and any(img_dir.glob("imp_*")))


def test_bundle_panel_is_on_the_sources_page(env):
    html = env.c.get(f"/event/{env.slug}/sources").get_data(as_text=True)
    for el in ('id="bundle_panel"', 'id="bnd_overlay"', "bundleImportJob:"):
        assert el in html
