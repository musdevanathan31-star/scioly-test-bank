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
    # Reloaded together, always. ImportError_ subclasses ArchiveOpError, so
    # reloading one without the other leaves a subclass pointing at a base
    # that no longer exists by identity -- and `except ArchiveOpError` in the
    # routes then stops catching it.
    import archive_import
    importlib.reload(archive_import)

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


# ---------------------------------------------------------------------------
# Bulk duplicate removal
#
# Which copy survives is the whole problem. Deleting the wrong one is not
# destructive (everything goes to the trash) but it is degrading: the bytes
# are identical, so the path is the only remaining metadata, and keeping the
# copy under _UnknownEvent/xz9##/ throws away the only thing that said what
# the file was.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dups(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="adup-"))
    import events
    importlib.reload(events)
    import deletion
    importlib.reload(deletion)
    import tournament_archive as ta
    importlib.reload(ta)
    import archive_ops
    importlib.reload(archive_ops)
    import archive_import
    importlib.reload(archive_import)

    body = b"IDENTICAL-CONTENT" * 5000

    def write(rel, data=body):
        p = ta.archive_root() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    write("Division B/Circuit Lab/2019/UF Invitational/test.pdf")
    write("_UnknownDivision/_UnknownEvent/2019/xz9a8f7b6c5d4e3f2a1b0/copy.pdf")
    write("Division B/Circuit Lab/2019/Regionals/CircuitLab2019.pdf")
    write("Division C/Astronomy/2020/States/only.pdf", b"unique-content-here")
    ta.save_index(ta.build_index())
    return archive_ops, ta


def test_the_best_identified_copy_is_the_one_kept(dups):
    _ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    keeper = ta.choose_keeper(group["paths"])
    # Properly filed under a real division, event, year and tournament.
    assert keeper.startswith("Division B/Circuit Lab/2019/")
    assert "_Unknown" not in keeper


def test_a_gibberish_folder_never_wins_over_a_named_one(dups):
    _ops, ta = dups
    keeper = ta.choose_keeper([
        "_UnknownDivision/_UnknownEvent/2019/xz9a8f7b6c5d4e3f2a1b0/copy.pdf",
        "Division B/Circuit Lab/2019/UF Invitational/test.pdf"])
    assert keeper == "Division B/Circuit Lab/2019/UF Invitational/test.pdf"


def test_the_choice_is_stable_regardless_of_input_order(dups):
    _ops, ta = dups
    paths = ta.load_index()["duplicates"][0]["paths"]
    assert ta.choose_keeper(paths) == ta.choose_keeper(list(reversed(paths)))


def test_a_plan_always_keeps_exactly_one_copy(dups):
    _ops, ta = dups
    plan = ta.plan_dedupe(ta.load_index()["duplicates"])
    assert plan["groups"]
    for g in plan["groups"]:
        assert g["keep"] not in g["remove"]
        assert len(g["remove"]) >= 1


def test_removing_duplicates_leaves_one_copy_on_disk(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    result = ops.remove_duplicates([group["id"]], by="coach1")
    survivors = [p for p in group["paths"]
                 if (ta.archive_root() / p).exists()]
    assert len(survivors) == 1
    assert survivors[0] == result["kept"][0]
    assert result["count"] == len(group["paths"]) - 1


def test_removed_duplicates_go_to_the_trash(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    ops.remove_duplicates([group["id"]])
    trashed = list(ops.trash_dir().rglob("*.pdf"))
    assert len(trashed) == len(group["paths"]) - 1


def test_scoping_to_a_folder_never_deletes_outside_it(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    outside = "_UnknownDivision/_UnknownEvent/2019/xz9a8f7b6c5d4e3f2a1b0/copy.pdf"
    ops.remove_duplicates([group["id"]], scope="Division B")
    # Cleaning up "this folder" must not reach out and delete a file
    # somewhere the coach is not looking.
    assert (ta.archive_root() / outside).exists()


def test_a_group_with_one_local_copy_has_nothing_to_remove(dups):
    _ops, ta = dups
    groups = ta.groups_under("Division C")
    assert groups == []


def test_groups_under_a_folder_only_count_local_copies(dups):
    _ops, ta = dups
    groups = ta.groups_under("Division B")
    assert len(groups) == 1
    assert all(p.startswith("Division B/") for p in groups[0]["paths"])
    assert len(groups[0]["paths"]) == 2


def test_an_unknown_id_touches_nothing_and_says_so(dups):
    ops, ta = dups
    before = len(list(ta.archive_root().rglob("*.pdf")))
    # It used to return count 0 and look like a success, which is precisely
    # how a sweep that matched nothing became indistinguishable from one
    # that worked.
    with pytest.raises(ops.ArchiveOpError):
        ops.remove_duplicates(["deadbeefdeadbeef"])
    assert len(list(ta.archive_root().rglob("*.pdf"))) == before


def test_bulk_removal_is_logged_in_full(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    result = ops.remove_duplicates([group["id"]], by="coach1")
    entry = ops.read_ops()[0]
    # One entry for the batch rather than one per file -- but it names every
    # path on both sides, because a destructive sweep is exactly what an
    # audit trail is for and a sample would not let anyone reconstruct it.
    assert entry["action"] == "dedupe"
    assert entry["by"] == "coach1"
    assert sorted(entry["paths"]) == sorted(result["removed"])
    assert entry["kept"] == result["kept"]
    assert entry["removed"] == len(result["removed"])


def test_a_bulk_sweep_keeps_the_original_paths_in_the_trash(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    result = ops.remove_duplicates([group["id"]])
    # Basenames collide constantly here, so flattening a batch into one
    # directory would lose files to overwrites. Each keeps its archive path.
    for rel in result["removed"]:
        assert (Path(result["trash"]) / rel).is_file(), rel


# ---------------------------------------------------------------------------
# Tournament-name suggestions
# ---------------------------------------------------------------------------

def test_existing_tournament_names_are_offered(ops):
    names = [r["name"] for r in ops.tournament_names()]
    assert "UF" in names and "States" in names


def test_the_common_spelling_ranks_first(ops):
    import tournament_archive as ta
    for rel in ("Division B/Astronomy/2021/States", "_UnknownDivision/Anatomy/2021/States"):
        (ta.archive_root() / rel).mkdir(parents=True, exist_ok=True)
    ta.save_index(ta.build_index())
    # Standardisation is the point: steer towards the spelling already in use.
    rows = ops.tournament_names("st")
    assert rows[0]["name"] == "States"
    assert rows[0]["count"] >= 2


def test_a_query_filters_the_suggestions(ops):
    assert all("uf" in r["name"].lower() for r in ops.tournament_names("uf"))


def test_each_duplicate_group_has_its_own_id(dups):
    _ops, ta = dups
    # Small files skip the second hashing pass, and an earlier version reused
    # the empty string as their digest -- so every small group shared one id
    # and selecting any of them matched all of them.
    small = b"tiny"
    (ta.archive_root() / "Division B/Circuit Lab/2019/UF").mkdir(
        parents=True, exist_ok=True)
    for rel in ("Division B/Circuit Lab/2019/UF/s1.pdf",
                "Division B/Circuit Lab/2019/UF/s2.pdf"):
        (ta.archive_root() / rel).write_bytes(small)
    for rel in ("Division B/Circuit Lab/2019/UF/t1.pdf",
                "Division B/Circuit Lab/2019/UF/t2.pdf"):
        (ta.archive_root() / rel).write_bytes(b"othr")
    ta.save_index(ta.build_index())
    groups = ta.load_index()["duplicates"]
    ids = [g["id"] for g in groups]
    assert all(ids), "a group with no id cannot be selected safely"
    assert len(set(ids)) == len(ids), ids


def test_selecting_one_small_group_removes_only_that_group(dups):
    ops, ta = dups
    (ta.archive_root() / "Division B/Circuit Lab/2019/UF").mkdir(
        parents=True, exist_ok=True)
    for rel in ("Division B/Circuit Lab/2019/UF/s1.pdf",
                "Division B/Circuit Lab/2019/UF/s2.pdf"):
        (ta.archive_root() / rel).write_bytes(b"tiny")
    for rel in ("Division B/Circuit Lab/2019/UF/t1.pdf",
                "Division B/Circuit Lab/2019/UF/t2.pdf"):
        (ta.archive_root() / rel).write_bytes(b"othr")
    ta.save_index(ta.build_index())
    target = next(g for g in ta.load_index()["duplicates"] if g["size"] == 4
                  and any(p.endswith("s1.pdf") for p in g["paths"]))
    ops.remove_duplicates([target["id"]])
    # The other four-byte group must be untouched.
    assert (ta.archive_root() / "Division B/Circuit Lab/2019/UF/t1.pdf").exists()
    assert (ta.archive_root() / "Division B/Circuit Lab/2019/UF/t2.pdf").exists()


def test_removing_duplicates_clears_them_from_the_panel(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    ops.remove_duplicates([group["id"]])
    # One copy survives, so the set is no longer a duplicate set at all.
    # Leaving it listed makes the panel offer files that are in the trash,
    # and acting on them reports failures.
    remaining = [g for g in ta.load_index()["duplicates"]
                 if g["id"] == group["id"]]
    assert remaining == []


def test_deleting_one_copy_shrinks_its_group(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    assert len(group["paths"]) == 3
    ops.delete(group["paths"][0])
    after = next(g for g in ta.load_index()["duplicates"] if g["id"] == group["id"])
    assert len(after["paths"]) == 2
    assert after["wasted"] == after["size"]


def test_deleting_a_folder_prunes_the_copies_inside_it(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    folder = "_UnknownDivision"
    inside = [p for p in group["paths"] if p.startswith(folder + "/")]
    assert inside
    ops.delete(folder)
    after = next(g for g in ta.load_index()["duplicates"] if g["id"] == group["id"])
    assert not any(p.startswith(folder + "/") for p in after["paths"])


def test_renaming_follows_the_duplicate_paths(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    ops.rename("Division B/Circuit Lab", "Circuits Lab")
    after = next(g for g in ta.load_index()["duplicates"] if g["id"] == group["id"])
    # A group pointing at the old path would offer files that cannot be found.
    for p in after["paths"]:
        assert (ta.archive_root() / p).exists(), p


def test_a_bulk_sweep_updates_the_index_once(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    saves = []
    real = ta.save_index
    try:
        ta.save_index = lambda idx: (saves.append(1), real(idx))[1]
        ops.remove_duplicates([group["id"]])
    finally:
        ta.save_index = real
    # One rewrite for the batch. Per-file index maintenance re-parsed and
    # re-serialised the whole index for every deletion -- 14ms each, which
    # made a 1800-file sweep take 26 seconds of apparently-dead page.
    assert saves == [1], f"{len(saves)} index rewrites for one sweep"


def test_a_missing_file_does_not_abandon_the_batch(dups):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]
    plan = ta.plan_dedupe([group])
    doomed = plan["groups"][0]["remove"]
    assert len(doomed) >= 2
    # Something removed it between the index build and now.
    (ta.archive_root() / doomed[0]).unlink()
    result = ops.remove_duplicates([group["id"]])
    assert len(result["failed"]) == 1
    assert result["count"] == len(doomed) - 1
    # The keeper still survives, which is the invariant that matters.
    assert (ta.archive_root() / result["kept"][0]).is_file()


def test_a_sweep_that_matches_nothing_is_an_error_not_a_success(dups):
    ops, _ta = dups
    # The reported symptom: remove appeared to succeed, the count did not
    # move, and a rebuild proved nothing had been deleted. "Removed 0" must
    # never be indistinguishable from a sweep that worked.
    with pytest.raises(ops.ArchiveOpError, match="are in the current index"):
        ops.remove_duplicates(["9999-deadbeefdeadbeef"])


def test_a_stale_id_from_a_rebuilt_index_is_reported(dups):
    ops, ta = dups
    stale = ta.load_index()["duplicates"][0]["id"]
    # Rebuild underneath the open page, changing the groups.
    for rel in ("Division B/Circuit Lab/2019/UF Invitational/test.pdf",
                "_UnknownDivision/_UnknownEvent/2019/xz9a8f7b6c5d4e3f2a1b0/copy.pdf"):
        (ta.archive_root() / rel).unlink()
    ta.save_index(ta.build_index())
    with pytest.raises(ops.ArchiveOpError, match="reload the page"):
        ops.remove_duplicates([stale])


def test_a_sweep_where_every_file_fails_raises(dups, monkeypatch):
    ops, ta = dups
    group = ta.load_index()["duplicates"][0]

    def refuse(*a, **kw):
        raise OSError("Permission denied")

    monkeypatch.setattr(ops.shutil, "move", refuse)
    with pytest.raises(ops.ArchiveOpError, match="Permission denied"):
        ops.remove_duplicates([group["id"]])
    # And nothing was quietly dropped from the index on the way out.
    assert ta.load_index()["duplicates"][0]["paths"] == group["paths"]
