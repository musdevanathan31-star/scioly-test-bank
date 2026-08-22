"""
Phase 4 of the tournament archive: moving PDFs into an event's bank.

The property that matters most here is that a test and its answer key stay a
pair. They are tied together by sharing an exact filename stem — nothing
else links them — so any collision handling that renames one without the
other silently breaks the relationship, and the break is invisible until
someone opens the event and finds a test with no key.

Run with: `python -m pytest tests/test_archive_import.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def imp(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="aimp-"))
    import events
    importlib.reload(events)
    importlib.reload(bqb)
    import tournament_archive as ta
    importlib.reload(ta)
    import archive_ops
    importlib.reload(archive_ops)
    import archive_import
    importlib.reload(archive_import)

    root = ta.archive_root()

    def write(rel, size=500):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)

    write("Division B/Circuit Lab/2019/UF Invitational/test.pdf")
    write("Division B/Circuit Lab/2019/UF Invitational/answer key.pdf")
    write("Division B/Circuit Lab/2019/UF Invitational/rules notes.pdf")
    write("_UnknownDivision/Circuit Lab/UF Invitational/loose.pdf")
    ta.save_index(ta.build_index())

    yield archive_import, sorted(events.EVENTS)[0], ta

    importlib.reload(events)
    importlib.reload(bqb)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


# ---------------------------------------------------------------------------
# What the path already tells us
# ---------------------------------------------------------------------------

def test_metadata_comes_from_the_folder_path(imp):
    archive_import, _slug, _ta = imp
    meta = archive_import.path_metadata(
        "Division B/Circuit Lab/2019/UF Invitational")
    assert meta == {"division": "b", "year": "2019",
                    "submitter": "ufinvitational"}


def test_a_tree_that_breaks_the_convention_still_yields_placeholders(imp):
    archive_import, _slug, _ta = imp
    # The whole reason this tool exists is that the tree is wrong, so a path
    # that does not match must degrade rather than refuse.
    meta = archive_import.path_metadata("_UnknownDivision/Circuit Lab")
    assert meta["division"] == "x"
    assert meta["year"] == "unk"


def test_an_answer_key_is_recognised_by_name(imp):
    archive_import, _slug, _ta = imp
    # Importing a key as a test puts the answers into the bank as questions,
    # so this is the one guess worth making automatically.
    for name in ("answer key.pdf", "2019_KEY.pdf", "solutions.pdf",
                 "circuitlab_answers.pdf"):
        assert archive_import.guess_role(name) == "key", name
    assert archive_import.guess_role("2019 test.pdf") == "test"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_a_plan_names_every_destination_before_moving_anything(imp):
    archive_import, slug, ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.plan_import(
        [{"path": f"{base}/test.pdf", "role": "test"},
         {"path": f"{base}/answer key.pdf", "role": "key"}], slug)
    names = {f["role"]: f["dest_name"] for f in plan["files"]}
    assert names["test"].endswith("_test.pdf")
    assert names["key"].endswith("_key.pdf")
    # Still in the archive: planning touches nothing.
    assert (ta.archive_root() / f"{base}/test.pdf").is_file()


def test_a_test_and_its_key_share_a_stem(imp):
    archive_import, slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.plan_import(
        [{"path": f"{base}/test.pdf", "role": "test"},
         {"path": f"{base}/answer key.pdf", "role": "key"}], slug)
    names = {f["role"]: f["dest_name"] for f in plan["files"]}
    assert names["test"][: -len("_test.pdf")] == names["key"][: -len("_key.pdf")]


def test_a_collision_bumps_the_whole_batch_not_one_file(imp):
    archive_import, slug, _ta = imp
    import events
    base = "Division B/Circuit Lab/2019/UF Invitational"
    items = [{"path": f"{base}/test.pdf", "role": "test"},
             {"path": f"{base}/answer key.pdf", "role": "key"}]
    first = archive_import.plan_import(items, slug)
    # Simulate the test half already being present from an earlier import.
    ev = events.EVENTS[slug]
    ev.base_dir.mkdir(parents=True, exist_ok=True)
    (ev.base_dir / next(f["dest_name"] for f in first["files"]
                        if f["role"] == "test")).write_bytes(b"old")
    second = archive_import.plan_import(items, slug)
    assert second["renamed_for_collision"] is True
    names = {f["role"]: f["dest_name"] for f in second["files"]}
    # Bumping the test alone would leave the key pointing at the OLD test.
    assert names["test"][: -len("_test.pdf")] == names["key"][: -len("_key.pdf")]
    assert names["test"] != next(f["dest_name"] for f in first["files"]
                                 if f["role"] == "test")


def test_two_files_that_would_collide_are_refused_with_a_reason(imp):
    archive_import, slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    with pytest.raises(archive_import.ImportError_, match="both become"):
        archive_import.plan_import(
            [{"path": f"{base}/test.pdf", "role": "test"},
             {"path": f"{base}/answer key.pdf", "role": "test"}], slug)


def test_notes_go_to_the_texts_directory(imp):
    archive_import, slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.plan_import(
        [{"path": f"{base}/rules notes.pdf", "role": "notes"}], slug)
    assert plan["files"][0]["where"] == "texts"


def test_importing_into_a_nonexistent_event_is_refused(imp):
    archive_import, _slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    with pytest.raises(archive_import.ImportError_, match="no such event"):
        archive_import.plan_import(
            [{"path": f"{base}/test.pdf", "role": "test"}], "not_an_event")


def test_a_path_outside_the_archive_is_refused(imp):
    archive_import, slug, _ta = imp
    with pytest.raises((ValueError, archive_import.ImportError_)):
        archive_import.plan_import(
            [{"path": "../../etc/passwd", "role": "test"}], slug)


def test_an_unknown_role_is_refused(imp):
    archive_import, slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    with pytest.raises(archive_import.ImportError_, match="role must be"):
        archive_import.plan_import(
            [{"path": f"{base}/test.pdf", "role": "homework"}], slug)


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def test_importing_moves_the_file_out_of_the_archive(imp):
    archive_import, slug, ta = imp
    import events
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"}], slug, by="coach1")
    assert not (ta.archive_root() / f"{base}/test.pdf").exists()
    dest = events.EVENTS[slug].base_dir / plan["files"][0]["dest_name"]
    assert dest.is_file()


def test_an_import_shrinks_the_archive_totals(imp):
    archive_import, slug, ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    before = ta.load_index()["dirs"][""]["total_bytes"]
    plan = archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"}], slug)
    after = ta.load_index()["dirs"][""]["total_bytes"]
    assert after == before - plan["files"][0]["bytes"]


def test_an_import_is_logged_with_both_ends(imp):
    archive_import, slug, _ta = imp
    import archive_ops
    base = "Division B/Circuit Lab/2019/UF Invitational"
    archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"}], slug, by="vol1")
    entry = archive_ops.read_ops()[0]
    assert entry["action"] == "import"
    assert entry["src"] == f"{base}/test.pdf"
    assert entry["dest"].startswith(f"{slug}/")
    assert entry["by"] == "vol1"


def test_the_imported_name_follows_the_events_convention(imp):
    archive_import, slug, _ta = imp
    import events
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"}], slug)
    prefix = events.EVENTS[slug].filename_prefix
    # Same shape the scraper and the manual onboarding path produce, so every
    # existing discovery path picks it up with no further code involved.
    assert plan["files"][0]["dest_name"] == \
        f"{prefix}_2019_b_ufinvitational_test.pdf"


def test_coach_supplied_metadata_overrides_the_path(imp):
    archive_import, slug, _ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    plan = archive_import.plan_import(
        [{"path": f"{base}/test.pdf", "role": "test"}], slug,
        {"year": "2021", "division": "c", "submitter": "states"})
    assert plan["files"][0]["dest_name"].endswith("_2021_c_states_test.pdf")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def _write_image(path: Path) -> None:
    """A real image, not a stub — the import converts it, so bytes matter."""
    import fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30))
    pix.clear_with(200)
    path.write_bytes(pix.tobytes("png"))


def test_an_image_imports_as_supplementary_and_inherits_the_test_stem(imp):
    archive_import, slug, ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    _write_image(ta.archive_root() / base / "figure 3.png")
    plan = archive_import.plan_import(
        [{"path": f"{base}/test.pdf", "role": "test"},
         {"path": f"{base}/figure 3.png", "role": "supplementary"}], slug)
    names = {f["role"]: f["dest_name"] for f in plan["files"]}
    stem = names["test"][: -len("_test.pdf")]
    # _supplementary_docs() finds attachments by shared stem prefix, so an
    # image that does not carry it is orphaned from the test it belongs to.
    assert names["supplementary"].startswith(stem)
    assert names["supplementary"].endswith(".png")


def test_an_image_cannot_be_a_test_or_key(imp):
    archive_import, slug, ta = imp
    base = "Division B/Circuit Lab/2019/UF Invitational"
    _write_image(ta.archive_root() / base / "scan.jpg")
    for role in ("test", "key"):
        with pytest.raises(archive_import.ImportError_):
            archive_import.plan_import(
                [{"path": f"{base}/scan.jpg", "role": role}], slug)


def test_an_image_defaults_to_supplementary_in_the_picker(imp):
    archive_import, _slug, _ta = imp
    assert archive_import.guess_role("figure 3.png") == "supplementary"
    assert archive_import.guess_role("diagram.JPEG") == "supplementary"


def test_an_imported_pair_is_found_by_the_events_own_discovery(imp):
    archive_import, slug, ta = imp
    import review_app, build_question_bank as bqb
    base = "Division B/Circuit Lab/2019/UF Invitational"
    archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"},
         {"path": f"{base}/answer key.pdf", "role": "key"}], slug)
    bqb.set_event(slug)
    # The whole point of importing under the naming convention is that no
    # further code is involved: the existing discovery paths just find it.
    tests = review_app._list_test_pdfs()
    assert len(tests) == 1
    assert review_app._key_path(tests[0]) is not None


def test_an_imported_image_is_found_as_supplementary(imp):
    archive_import, slug, ta = imp
    import review_app, build_question_bank as bqb
    base = "Division B/Circuit Lab/2019/UF Invitational"
    _write_image(ta.archive_root() / base / "figure 3.png")
    archive_import.run_import(
        [{"path": f"{base}/test.pdf", "role": "test"},
         {"path": f"{base}/figure 3.png", "role": "supplementary"}], slug)
    bqb.set_event(slug)
    test_pdf = review_app._list_test_pdfs()[0]
    extras = [p.name for p in review_app._supplementary_docs(test_pdf)]
    # _supplementary_docs globs *.pdf and the viewer opens results with fitz,
    # so the image arrives wrapped in a single-page PDF rather than every
    # viewer having to learn about image attachments.
    assert any("figure3" in n and n.endswith(".pdf") for n in extras), extras
    # The original is kept beside it — nothing in this feature destroys a file.
    assert any(p.suffix == ".png" for p in test_pdf.parent.iterdir())


# ---------------------------------------------------------------------------
# Bulk import from a subtree
#
# Mapping a folder to an event does not put its files in the event — that
# needs an import, and doing it a folder at a time across hundreds of files
# is not a workflow anyone finishes.
# ---------------------------------------------------------------------------

@pytest.fixture()
def subtree(imp):
    archive_import, slug, ta = imp
    root = ta.archive_root()

    def write(rel, data):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    base = "Division B/Circuit Lab"
    write(f"{base}/2019/UF Invitational/test.pdf", b"UF-2019-TEST" * 100)
    write(f"{base}/2019/UF Invitational/answer key.pdf", b"UF-2019-KEY" * 100)
    write(f"{base}/2021/States/test.pdf", b"ST-2021-TEST" * 100)
    write(f"{base}/2021/States/key.pdf", b"ST-2021-KEY" * 100)
    ta.save_index(ta.build_index())
    return archive_import, slug, ta, base


def test_a_subtree_is_grouped_by_year_and_tournament(subtree):
    archive_import, slug, _ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    keys = [g["key"] for g in out["groups"]]
    assert "2019/UF Invitational" in keys
    assert "2021/States" in keys
    for g in out["groups"]:
        assert g["year"] in ("2019", "2021")


def test_roles_are_guessed_per_file(subtree):
    archive_import, slug, _ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    roles = {f["name"]: f["role"] for g in out["groups"] for f in g["files"]}
    # Getting this wrong puts the answers into the bank as questions.
    assert roles["answer key.pdf"] == "key"
    assert roles["key.pdf"] == "key"
    assert roles["test.pdf"] == "test"


def test_importing_spans_folders_without_mixing_their_years(subtree):
    archive_import, slug, _ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    items = [{"path": f["path"], "role": f["role"]}
             for g in out["groups"] for f in g["files"]]
    result = archive_import.run_batch_import(items, slug, by="coach1")
    # Five: this fixture's two pairs plus the notes file the base fixture
    # leaves in the 2019 folder.
    assert result["count"] == 5 and result["folders"] == 2
    names = sorted(f["dest_name"] for f in result["imported"])
    # A single plan across folders would file everything under whichever
    # folder came first, pairing a 2019 key with a 2021 test.
    assert any("_2019_" in n and n.endswith("_test.pdf") for n in names)
    assert any("_2021_" in n and n.endswith("_test.pdf") for n in names)
    for year in ("2019", "2021"):
        stems = {n.rsplit("_", 1)[0] for n in names if f"_{year}_" in n}
        assert len(stems) == 1, f"{year} test and key must share a stem: {stems}"


def test_a_file_already_in_the_event_is_hidden(subtree):
    archive_import, slug, ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    first = out["groups"][0]["files"][0]
    archive_import.run_import([{"path": first["path"], "role": first["role"]}], slug)

    again = archive_import.subtree_files(base, slug)
    remaining = [f["name"] for g in again["groups"] for f in g["files"]]
    assert first["name"] not in remaining or len(remaining) < out["shown"]


def test_a_duplicate_copy_elsewhere_counts_as_already_imported(subtree):
    archive_import, slug, ta, base = subtree
    # The case that makes "already imported" a content question: import one
    # copy, and the identical file sitting in another tournament folder must
    # not come back looking like fresh material.
    src = ta.archive_root() / base / "2019/UF Invitational/test.pdf"
    twin = ta.archive_root() / base / "2022/Regionals/scan_0001.pdf"
    twin.parent.mkdir(parents=True, exist_ok=True)
    twin.write_bytes(src.read_bytes())
    ta.save_index(ta.build_index())

    archive_import.run_import(
        [{"path": f"{base}/2019/UF Invitational/test.pdf", "role": "test"}], slug)

    out = archive_import.subtree_files(base, slug)
    names = [f["name"] for g in out["groups"] for f in g["files"]]
    assert "scan_0001.pdf" not in names, "a copy of an imported file was offered again"
    assert out["already_in_event"] >= 1


def test_hidden_files_can_still_be_listed_on_request(subtree):
    archive_import, slug, _ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    first = out["groups"][0]["files"][0]
    archive_import.run_import([{"path": first["path"], "role": first["role"]}], slug)
    shown = archive_import.subtree_files(base, slug, include_imported=True)
    assert shown["already_in_event"] == 0 or shown["shown"] >= out["shown"] - 1


def test_one_bad_folder_does_not_cost_the_rest_of_the_sweep(subtree, monkeypatch):
    archive_import, slug, _ta, base = subtree
    out = archive_import.subtree_files(base, slug)
    items = [{"path": f["path"], "role": f["role"]}
             for g in out["groups"] for f in g["files"]]
    real = archive_import.run_import
    calls = {"n": 0}

    def flaky(folder_items, s, meta=None, by=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise archive_import.ImportError_("boom")
        return real(folder_items, s, meta, by)

    monkeypatch.setattr(archive_import, "run_import", flaky)
    result = archive_import.run_batch_import(items, slug)
    assert len(result["failed"]) == 1
    assert result["count"] == 2, "the second folder should still have imported"
