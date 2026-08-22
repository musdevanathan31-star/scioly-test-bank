"""
Phase 1 of the tournament archive: the index and read-only browsing.

The corpus this indexes is deliberately untidy — that is the reason the
tool exists — so the tests use a tree that violates the nominal
<Division>/<Event>/<Year>/<Tournament> shape in the ways the real upload
will: a file at the wrong depth, an empty folder, gibberish names, unknown
markers, and OS litter. Anything here that raises on those has misread the
job.

Run with: `python -m pytest tests/test_tournament_archive.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def archive(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="arch-"))
    import events
    importlib.reload(events)
    import tournament_archive as ta
    importlib.reload(ta)

    root = ta.archive_root()

    def write(rel, size):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)

    write("Division B/Circuit Lab/2019/UF Invitational/test.pdf", 2000)
    write("Division B/Circuit Lab/2019/UF Invitational/key.pdf", 1500)
    write("Division B/Circuit Lab/2019/xz9##garbage/scan.pdf", 500)
    write("Division C/_UnknownEvent/2021/States/a.pdf", 100)
    write("_UnknownDivision/Anatomy/2020/Regionals/b.pdf", 300)
    # A file where a <Year> folder should be — the tree really is like this.
    write("Division B/Circuit Lab/stray.pdf", 42)
    (root / "Division B/Circuit Lab/2018").mkdir(parents=True, exist_ok=True)
    write("Division B/.DS_Store", 6)
    return ta


def test_totals_aggregate_up_the_tree(archive):
    archive.save_index(archive.build_index())
    s = archive.summary()
    # Six real files; the .DS_Store is litter and must not be counted.
    assert s["total_files"] == 6
    assert s["total_bytes"] == 2000 + 1500 + 500 + 100 + 300 + 42
    top = archive.list_dir("")
    by_name = {d["name"]: d for d in top["subdirs"]}
    assert by_name["Division B"]["total_files"] == 4
    assert by_name["Division C"]["total_files"] == 1
    assert by_name["_UnknownDivision"]["total_files"] == 1


def test_the_root_is_depth_zero(archive):
    # relative_to() against itself yields Path("."), which if left alone
    # shifts every depth by one and stops parent/child keys matching.
    assert archive.rel_of(archive.archive_root()) == ""
    top = archive.list_dir("")
    assert top["depth"] == 0
    assert top["level"] == "root"
    assert top["child_level"] == "division"


def test_levels_are_labelled_by_position(archive):
    archive.save_index(archive.build_index())
    assert archive.list_dir("Division B")["child_level"] == "event"
    assert archive.list_dir("Division B/Circuit Lab")["child_level"] == "year"
    assert archive.list_dir("Division B/Circuit Lab/2019")["child_level"] == "tournament"
    # Deeper than the convention describes: still browsable, just unnamed.
    assert archive.level_name(9) == "folder"


def test_a_file_at_the_wrong_depth_is_still_listed(archive):
    archive.save_index(archive.build_index())
    listing = archive.list_dir("Division B/Circuit Lab")
    assert [f["name"] for f in listing["files"]] == ["stray.pdf"]


def test_an_empty_folder_renders_rather_than_raising(archive):
    archive.save_index(archive.build_index())
    listing = archive.list_dir("Division B/Circuit Lab/2018")
    assert listing["subdirs"] == [] and listing["files"] == []


def test_os_litter_is_ignored(archive):
    archive.save_index(archive.build_index())
    names = [f["name"] for f in archive.list_dir("Division B")["files"]]
    assert ".DS_Store" not in names


@pytest.mark.parametrize("bad", ["..", "../..", "Division B/../../outside",
                                 "/etc/passwd"])
def test_paths_cannot_escape_the_archive(archive, bad):
    # Every path here arrives from a URL, so containment is the whole
    # security story.
    with pytest.raises(ValueError):
        archive.safe_path(bad)


def test_browsing_works_before_any_index_exists(archive):
    # A coach who uploads and immediately opens the page should see their
    # folders, just without totals — not an error.
    listing = archive.list_dir("")
    assert {d["name"] for d in listing["subdirs"]} == {
        "Division B", "Division C", "_UnknownDivision"}
    assert all(d["indexed"] is False for d in listing["subdirs"])
    assert listing["stale"] is True


def test_a_folder_created_after_indexing_is_still_browsable(archive):
    archive.save_index(archive.build_index())
    (archive.archive_root() / "Division B/Brand New").mkdir(parents=True)
    listing = archive.list_dir("Division B")
    fresh = [d for d in listing["subdirs"] if d["name"] == "Brand New"]
    assert fresh and fresh[0]["indexed"] is False
    assert archive.list_dir("Division B/Brand New")["files"] == []


def test_an_index_from_an_older_schema_is_discarded_not_migrated(archive):
    idx = archive.build_index()
    idx["schema"] = archive.INDEX_SCHEMA_VERSION - 1
    archive.save_index(idx)
    # Derived data: a rebuild is always cheaper and safer than a conversion.
    assert archive.load_index() is None


def test_breadcrumbs_lead_back_to_the_root(archive):
    crumbs = archive.breadcrumbs("Division B/Circuit Lab/2019")
    assert [c["name"] for c in crumbs] == [
        archive.ARCHIVE_DIRNAME, "Division B", "Circuit Lab", "2019"]
    assert crumbs[0]["rel"] == ""
    assert crumbs[-1]["rel"] == "Division B/Circuit Lab/2019"


def test_indexing_a_missing_archive_is_not_an_error(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="noarch-"))
    import events
    importlib.reload(events)
    import tournament_archive as ta
    importlib.reload(ta)
    idx = ta.build_index()
    assert idx["root_exists"] is False
    assert ta.summary()["root_exists"] is False


def test_the_archive_directory_name_cannot_be_taken_by_an_event(archive):
    import events
    with pytest.raises(ValueError, match="reserved"):
        events.add_custom_event(archive.ARCHIVE_DIRNAME, "Not An Event")
