"""
Phase 3 of the tournament archive: renaming, moving, creating and deleting.

Two things are being pinned. The obvious one is that mutations do what they
say. The one that matters more is that the *index* keeps up: a rename
touches one subtree, and if patching it in place drifts from what a rebuild
would produce, every total on every page is quietly wrong from then on. So
several tests here compare the patched index against a full rebuild.

Run with: `python -m pytest tests/test_archive_ops.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def ops(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="aops-"))
    import events
    importlib.reload(events)
    import deletion
    importlib.reload(deletion)
    import tournament_archive as ta
    importlib.reload(ta)
    import archive_ops
    importlib.reload(archive_ops)

    root = ta.archive_root()

    def write(rel, size):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)

    write("Division B/Circuit Lab/2019/UF/test.pdf", 2000)
    write("Division B/Circuit Lab/2019/UF/key.pdf", 1000)
    write("Division B/Circuit Lab/2019/xz9##junk/scan.pdf", 500)
    write("Division B/Astronomy/2020/States/a.pdf", 300)
    write("_UnknownDivision/Anatomy/2020/Regionals/b.pdf", 100)
    (root / "Division B/Circuit Lab/2018").mkdir(parents=True, exist_ok=True)
    ta.save_index(ta.build_index())
    archive_ops._ta = ta
    return archive_ops


def _totals_match_a_rebuild(ta):
    """The patched index against what a fresh walk would produce."""
    patched = ta.load_index()["dirs"]
    rebuilt = ta.build_index(find_dupes=False)["dirs"]
    assert patched.keys() == rebuilt.keys(), (
        f"keys drifted: only-patched={sorted(set(patched) - set(rebuilt))} "
        f"only-rebuilt={sorted(set(rebuilt) - set(patched))}")
    for key, entry in rebuilt.items():
        for field in ("total_files", "total_bytes", "depth"):
            assert patched[key][field] == entry[field], f"{key}.{field}"


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

def test_a_preview_reports_real_counts(ops):
    plan = ops.preview_move("Division B/Circuit Lab", "_UnknownDivision")
    assert plan["files"] == 3
    assert plan["bytes"] == 3500
    assert plan["estimated"] is False


def test_a_preview_changes_nothing(ops):
    import tournament_archive as ta
    before = sorted(p.name for p in ta.archive_root().rglob("*"))
    ops.preview_rename("Division B/Circuit Lab", "Circuits Lab")
    ops.preview_delete("Division B/Astronomy")
    assert sorted(p.name for p in ta.archive_root().rglob("*")) == before


def test_a_folder_cannot_be_moved_inside_itself(ops):
    # shutil would relocate the destination along with the source and half-do
    # it before failing.
    with pytest.raises(ops.ArchiveOpError, match="inside itself"):
        ops.preview_move("Division B/Circuit Lab",
                         "Division B/Circuit Lab/2019")


def test_the_archive_root_cannot_be_renamed_moved_or_deleted(ops):
    for call in (lambda: ops.preview_rename("", "x"),
                 lambda: ops.preview_move("", "Division B"),
                 lambda: ops.preview_delete("")):
        with pytest.raises(ops.ArchiveOpError):
            call()


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "a:b", "a*b"])
def test_dangerous_names_are_refused(ops, bad):
    with pytest.raises(ops.ArchiveOpError):
        ops.check_name(bad)


def test_a_collision_is_refused_rather_than_merged(ops):
    # Silently merging two trees is unrecoverable without a file-by-file
    # audit of which came from where.
    with pytest.raises(ops.ArchiveOpError, match="already exists"):
        ops.preview_rename("Division B/Astronomy", "Circuit Lab")


# ---------------------------------------------------------------------------
# Mutations, and what they do to the index
# ---------------------------------------------------------------------------

def test_renaming_a_folder_rekeys_its_whole_subtree(ops):
    import tournament_archive as ta
    ops.rename("Division B/Circuit Lab/2019/xz9##junk", "UF Invitational 2")
    assert (ta.archive_root() /
            "Division B/Circuit Lab/2019/UF Invitational 2/scan.pdf").is_file()
    dirs = ta.load_index()["dirs"]
    assert "Division B/Circuit Lab/2019/UF Invitational 2" in dirs
    assert "Division B/Circuit Lab/2019/xz9##junk" not in dirs
    _totals_match_a_rebuild(ta)


def test_a_rename_within_one_parent_leaves_totals_alone(ops):
    import tournament_archive as ta
    before = ta.load_index()["dirs"]["Division B"]["total_bytes"]
    ops.rename("Division B/Circuit Lab", "Circuits Lab")
    assert ta.load_index()["dirs"]["Division B"]["total_bytes"] == before
    _totals_match_a_rebuild(ta)


def test_moving_a_folder_shifts_totals_between_both_chains(ops):
    import tournament_archive as ta
    dirs = ta.load_index()["dirs"]
    b_before = dirs["Division B"]["total_bytes"]
    u_before = dirs["_UnknownDivision"]["total_bytes"]
    plan = ops.move("Division B/Circuit Lab", "_UnknownDivision")
    dirs = ta.load_index()["dirs"]
    assert dirs["Division B"]["total_bytes"] == b_before - plan["bytes"]
    assert dirs["_UnknownDivision"]["total_bytes"] == u_before + plan["bytes"]
    _totals_match_a_rebuild(ta)


def test_moving_to_the_root_works(ops):
    import tournament_archive as ta
    ops.move("Division B/Astronomy", "")
    assert (ta.archive_root() / "Astronomy").is_dir()
    _totals_match_a_rebuild(ta)


def test_a_created_folder_is_immediately_browsable(ops):
    import tournament_archive as ta
    ops.create_folder("Division B/Circuit Lab", "2022")
    assert (ta.archive_root() / "Division B/Circuit Lab/2022").is_dir()
    listing = ta.list_dir("Division B/Circuit Lab")
    assert "2022" in [d["name"] for d in listing["subdirs"]]
    _totals_match_a_rebuild(ta)


def test_deleting_moves_to_the_shared_trash(ops):
    import tournament_archive as ta
    plan = ops.delete("Division B/Astronomy")
    assert not (ta.archive_root() / "Division B/Astronomy").exists()
    # Recoverable by design: organising means deleting junk, and a coach who
    # mis-clicks on 3GB should not need a backup restore.
    trashed = Path(plan["trash"])
    assert trashed.is_dir()
    assert any(trashed.rglob("a.pdf"))
    _totals_match_a_rebuild(ta)


def test_a_deleted_folders_bytes_leave_every_ancestor(ops):
    import tournament_archive as ta
    before = ta.load_index()["dirs"][""]["total_bytes"]
    plan = ops.delete("Division B/Astronomy")
    assert ta.load_index()["dirs"][""]["total_bytes"] == before - plan["bytes"]


def test_deleting_a_single_file_works(ops):
    import tournament_archive as ta
    plan = ops.delete("Division B/Circuit Lab/2019/UF/key.pdf")
    assert plan["is_file"] is True and plan["files"] == 1
    assert not (ta.archive_root() /
                "Division B/Circuit Lab/2019/UF/key.pdf").exists()
    _totals_match_a_rebuild(ta)


def test_the_trash_name_records_where_it_came_from(ops):
    # Basenames here are meaningless -- "test.pdf" a thousand times over --
    # so the trash folder has to say which one this was.
    plan = ops.delete("Division B/Circuit Lab/2019/UF")
    assert "Division B__Circuit Lab__2019__UF" in Path(plan["trash"]).name


def test_pruning_removes_empty_folders_only(ops):
    import tournament_archive as ta
    result = ops.delete_empty_folders()
    assert "Division B/Circuit Lab/2018" in result["removed"]
    # A folder holding files is not empty, whatever else is true of it.
    assert (ta.archive_root() / "Division B/Circuit Lab/2019/UF").is_dir()
    _totals_match_a_rebuild(ta)


def test_pruning_is_bottom_up(ops):
    import tournament_archive as ta
    (ta.archive_root() / "Division B/Empty/Deeper/Deepest").mkdir(parents=True)
    result = ops.delete_empty_folders()
    # A parent only becomes empty once its children are gone, so a top-down
    # pass would leave the outer shells behind.
    for rel in ("Division B/Empty", "Division B/Empty/Deeper",
                "Division B/Empty/Deeper/Deepest"):
        assert rel in result["removed"], rel
    assert not (ta.archive_root() / "Division B/Empty").exists()


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_every_mutation_is_logged_with_both_paths(ops):
    ops.rename("Division B/Astronomy", "Astro", by="coach1")
    ops.create_folder("Division B", "New", by="coach1")
    ops.delete("Division B/New", by="coach1")
    entries = ops.read_ops()
    assert [e["action"] for e in entries] == ["delete", "create", "rename"]
    rename_entry = entries[-1]
    assert rename_entry["src"] == "Division B/Astronomy"
    assert rename_entry["dest"] == "Division B/Astro"
    assert rename_entry["by"] == "coach1"


def test_a_failed_operation_is_not_logged(ops):
    with pytest.raises(ops.ArchiveOpError):
        ops.rename("Division B/Astronomy", "Circuit Lab")
    assert ops.read_ops() == []


def test_an_unwritable_log_does_not_undo_the_move(ops, monkeypatch):
    import tournament_archive as ta

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ops, "open", boom, raising=False)
    # Losing an audit line is bad; losing it *and* rolling back a move that
    # already happened on disk would be worse and impossible to reason about.
    ops.rename("Division B/Astronomy", "Astro")
    assert (ta.archive_root() / "Division B/Astro").is_dir()
