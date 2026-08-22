"""
Phase 2 of the tournament archive: folder -> event mapping, and the volunteer
scoping that depends on it.

The security property under test is one-directional. Failing to grant access
is a nuisance; granting it wrongly hands a volunteer someone else's archive.
So the tests lean on the deny side: unmapped is invisible, near-miss names
are never auto-applied, and a parent listing must not name branches the
viewer cannot enter — folder names alone disclose the shape of the corpus.

Run with: `python -m pytest tests/test_archive_map.py -q`
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class FakeUser:
    role: str
    events: tuple = ()
    username: str = "u"


@pytest.fixture()
def am(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", tempfile.mkdtemp(prefix="amap-"))
    import events
    importlib.reload(events)
    import tournament_archive as ta
    importlib.reload(ta)
    import archive_map
    importlib.reload(archive_map)

    root = ta.archive_root()

    def write(rel, size=100):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)

    write("Division B/Circuit Lab/2019/UF/test.pdf")
    write("Division C/Circuit Lab/2019/States/test.pdf")
    write("Division B/Anatomy and Physiology/2020/Regionals/a.pdf")
    write("Division C/_UnknownEvent/2021/States/b.pdf")
    write("_UnknownDivision/Astronomy/2020/Invitational/c.pdf")
    ta.save_index(ta.build_index())
    return archive_map


@pytest.fixture()
def slugs(am):
    import events
    return sorted(events.EVENTS)


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def test_normalising_folds_the_ways_people_actually_type_event_names(am):
    assert am.normalise("Circuit Lab") == am.normalise("circuit-lab")
    assert am.normalise("CircuitLab") != ""
    assert am.normalise("Anatomy & Physiology") == "anatomy and physiology"
    # The division is already a level up in the tree, so a suffix is noise.
    assert am.normalise("Circuit Lab B") == "circuit lab"


def test_a_suggestion_is_only_a_suggestion(am):
    # Nothing is mapped until a coach saves, however confident the match.
    rows = am.event_folders()
    suggested = [r for r in rows if r["suggestion"]]
    assert suggested, "expected at least one confident name match"
    assert all(r["slug"] is None for r in rows)
    assert am.load_map() == {}


def test_an_unrecognisable_folder_gets_no_suggestion(am):
    # A wrong mapping grants access to the wrong subtree, so a vague match
    # must yield nothing rather than a plausible-looking guess.
    assert am.suggest_slug("_UnknownEvent") is None
    assert am.suggest_slug("xz9##garbage") is None
    assert am.suggest_slug("") is None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_a_mapping_to_a_nonexistent_event_is_refused(am):
    with pytest.raises(ValueError, match="no such event"):
        am.set_mapping("Division B/Circuit Lab", "not_a_real_event")
    assert am.load_map() == {}


def test_a_batch_with_one_bad_slug_saves_nothing(am, slugs):
    # Thirty-nine saved and one silently dropped is the worst outcome: the
    # coach has no way to tell which row did not take.
    with pytest.raises(ValueError, match="no such event"):
        am.set_many({"Division B/Circuit Lab": slugs[0],
                     "Division C/Circuit Lab": "bogus"})
    assert am.load_map() == {}


def test_a_mapping_can_be_revoked(am, slugs):
    am.set_mapping("Division B/Circuit Lab", slugs[0])
    assert am.load_map() == {"Division B/Circuit Lab": slugs[0]}
    am.set_mapping("Division B/Circuit Lab", None)
    assert am.load_map() == {}


def test_a_corrupt_map_reads_as_empty_rather_than_failing(am):
    am.map_path().write_text("{not json", encoding="utf-8")
    # Curation data should not be able to take the archive page down; empty
    # means coach-only, which is the safe direction.
    assert am.load_map() == {}


def test_an_older_schema_is_discarded_not_migrated(am, slugs):
    am.set_mapping("Division B/Circuit Lab", slugs[0])
    import json
    data = json.loads(am.map_path().read_text(encoding="utf-8"))
    data["schema"] = am.MAP_SCHEMA_VERSION - 1
    am.map_path().write_text(json.dumps(data), encoding="utf-8")
    assert am.load_map() == {}


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_a_coach_sees_everything_including_the_unmapped_backlog(am):
    coach = FakeUser("coach")
    assert am.can_access(coach, "Division C/_UnknownEvent/2021")
    assert am.can_access(coach, "_UnknownDivision/Astronomy")
    assert am.visible_children(coach, "", ["Division B", "Division C"]) == [
        "Division B", "Division C"]


def test_a_volunteer_sees_nothing_until_something_is_mapped(am):
    vol = FakeUser("volunteer", events=("circuit_lab",))
    assert not am.can_access(vol, "Division B/Circuit Lab/2019")
    assert am.visible_children(vol, "", ["Division B", "Division C"]) == []


def test_a_volunteer_sees_only_their_own_mapped_subtree(am, slugs):
    mine, theirs = slugs[0], slugs[1]
    am.set_many({"Division B/Circuit Lab": mine,
                 "Division B/Anatomy and Physiology": theirs})
    vol = FakeUser("volunteer", events=(mine,))
    assert am.can_access(vol, "Division B/Circuit Lab/2019/UF")
    assert not am.can_access(vol, "Division B/Anatomy and Physiology/2020")
    assert am.visible_children(
        vol, "Division B", ["Circuit Lab", "Anatomy and Physiology"]) == ["Circuit Lab"]


def test_a_division_is_hidden_when_it_holds_nothing_for_this_volunteer(am, slugs):
    # Listing a division the volunteer cannot enter still tells them the
    # archive has one, which is the disclosure this scoping exists to stop.
    am.set_many({"Division B/Circuit Lab": slugs[0]})
    vol = FakeUser("volunteer", events=(slugs[0],))
    assert am.visible_children(
        vol, "", ["Division B", "Division C", "_UnknownDivision"]) == ["Division B"]


def test_mapping_governs_everything_below_it(am, slugs):
    am.set_many({"Division B/Circuit Lab": slugs[0]})
    vol = FakeUser("volunteer", events=(slugs[0],))
    assert am.can_access(vol, "Division B/Circuit Lab/2019/UF")
    assert am.slug_for_path("Division B/Circuit Lab/2019/UF/test.pdf") == slugs[0]


def test_the_root_and_division_levels_belong_to_nobody(am, slugs):
    am.set_many({"Division B/Circuit Lab": slugs[0]})
    assert am.slug_for_path("") is None
    assert am.slug_for_path("Division B") is None
    vol = FakeUser("volunteer", events=(slugs[0],))
    # They can still *traverse* to reach their own subtree...
    assert am.visible_children(vol, "", ["Division B"]) == ["Division B"]
    # ...but the division itself is not theirs, so loose files there are not.
    assert not am.can_access(vol, "Division B")


def test_revoking_an_event_removes_archive_access(am, slugs):
    am.set_many({"Division B/Circuit Lab": slugs[0]})
    still_theirs = FakeUser("volunteer", events=(slugs[0],))
    no_longer = FakeUser("volunteer", events=())
    assert am.can_access(still_theirs, "Division B/Circuit Lab/2019")
    assert not am.can_access(no_longer, "Division B/Circuit Lab/2019")


def test_the_same_event_under_two_divisions_maps_separately(am, slugs):
    # The bulk shortcut sets both, but they remain independent keys so an
    # inconsistently-named folder can still be corrected on its own.
    am.set_many({"Division B/Circuit Lab": slugs[0]})
    vol = FakeUser("volunteer", events=(slugs[0],))
    assert am.can_access(vol, "Division B/Circuit Lab/2019")
    assert not am.can_access(vol, "Division C/Circuit Lab/2019")
    groups = am.folders_by_name()
    key = am.normalise("Circuit Lab")
    assert sorted(groups[key]) == ["Division B/Circuit Lab", "Division C/Circuit Lab"]
