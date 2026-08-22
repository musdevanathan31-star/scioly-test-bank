"""
Regression coverage for assessments.migrate_legacy_responses(): the one-time,
idempotent backfill from the pre-redesign single-file assessment_responses.json
into the current per-(assessment_id, username) file layout (see assessments.py's
module docstring and migrate_legacy_responses()'s own docstring for why —
short version: the old single-file/single-lock design made answer-save
latency grow super-linearly with concurrent students; loadtest_students.py
proved it against production before this redesign).

Run with: `python -m pytest tests/test_response_migration.py -q`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import assessments as assessments_mod  # noqa: E402


def _seed_legacy_file(path: Path) -> None:
    path.write_text(json.dumps({
        "test1": {
            "alice": {
                "student_username": "alice", "assessment_id": "test1",
                "question_order": [1, 0], "answers": {"0": {"qtype": "mcq", "picked": "A"}},
                "auto_grade": {}, "manual_grade": {}, "status": "in_progress",
                "started_at": "2026-01-01T00:00:00", "last_saved_at": "2026-01-01T00:01:00",
                "submitted_at": None, "released": False, "released_at": None, "released_by": None,
            },
            "bob": {
                "student_username": "bob", "assessment_id": "test1",
                "question_order": [0, 1], "answers": {}, "auto_grade": {}, "manual_grade": {},
                "status": "in_progress", "started_at": "2026-01-01T00:00:00",
                "last_saved_at": "2026-01-01T00:00:00", "submitted_at": None,
                "released": False, "released_at": None, "released_by": None,
            },
        },
    }), encoding="utf-8")


def test_migrate_legacy_responses_backfills_and_renames(tmp_path, monkeypatch):
    legacy = tmp_path / "assessment_responses.json"
    monkeypatch.setattr(assessments_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(assessments_mod, "RESPONSES_DIR", tmp_path / "assessment_responses")
    _seed_legacy_file(legacy)

    migrated = assessments_mod.migrate_legacy_responses()

    assert migrated == 2
    alice = assessments_mod.get_response("test1", "alice")
    assert alice is not None
    assert alice.answers == {"0": {"qtype": "mcq", "picked": "A"}}
    bob = assessments_mod.get_response("test1", "bob")
    assert bob is not None
    assert bob.answers == {}
    assert not legacy.exists(), "legacy file must not be left in place once migrated"
    assert (tmp_path / "assessment_responses.json.migrated").exists(), "legacy data must be renamed, not deleted"


def test_migrate_legacy_responses_is_idempotent(tmp_path, monkeypatch):
    legacy = tmp_path / "assessment_responses.json"
    monkeypatch.setattr(assessments_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(assessments_mod, "RESPONSES_DIR", tmp_path / "assessment_responses")
    _seed_legacy_file(legacy)

    first = assessments_mod.migrate_legacy_responses()
    second = assessments_mod.migrate_legacy_responses()

    assert first == 2
    assert second == 0, "a second run, after the legacy file was renamed away, must be a pure no-op"


def test_migrate_legacy_responses_noop_when_no_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(assessments_mod, "_LEGACY_RESPONSES_FILE", tmp_path / "assessment_responses.json")
    monkeypatch.setattr(assessments_mod, "RESPONSES_DIR", tmp_path / "assessment_responses")

    assert assessments_mod.migrate_legacy_responses() == 0


def test_migrate_legacy_responses_skips_already_migrated_pairs(tmp_path, monkeypatch):
    """Simulates an interrupted first run: the legacy file is still present
    (the final rename never happened), but one pair was already written
    out to its new per-pair file. Re-running must resume — migrate only
    what's missing — not clobber the already-migrated file with stale
    legacy data."""
    legacy = tmp_path / "assessment_responses.json"
    monkeypatch.setattr(assessments_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(assessments_mod, "RESPONSES_DIR", tmp_path / "assessment_responses")
    _seed_legacy_file(legacy)

    # Pre-create alice's new-layout file with DIFFERENT content than the
    # legacy blob has for her, so a wrongly-clobbering migration is
    # detectable rather than silently passing either way.
    alice_path = assessments_mod._response_path("test1", "alice")
    alice_path.parent.mkdir(parents=True, exist_ok=True)
    alice_path.write_text(json.dumps({
        "student_username": "alice", "assessment_id": "test1", "question_order": [1, 0],
        "answers": {"0": {"qtype": "mcq", "picked": "Z"}}, "auto_grade": {}, "manual_grade": {},
        "status": "submitted", "started_at": "2026-01-01T00:00:00",
        "last_saved_at": "2026-01-01T00:05:00", "submitted_at": "2026-01-01T00:05:00",
        "released": False, "released_at": None, "released_by": None,
    }), encoding="utf-8")

    migrated = assessments_mod.migrate_legacy_responses()

    assert migrated == 1, "only bob should be newly migrated — alice's file already existed"
    alice = assessments_mod.get_response("test1", "alice")
    assert alice.status == "submitted", "pre-existing alice file must not be overwritten by legacy data"
    assert alice.answers == {"0": {"qtype": "mcq", "picked": "Z"}}


# ---------------------------------------------------------------------------
# Test -> Assessment storage rename
# ---------------------------------------------------------------------------

def test_rename_migration_moves_legacy_storage_and_is_idempotent(tmp_path, monkeypatch):
    """A pre-rename instance has test_windows.json / tests.json /
    test_responses/. Startup must adopt them under the new names, or the
    server comes up looking like every season was deleted."""
    import importlib, json
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import assessments as a
    importlib.reload(a)

    (tmp_path / "test_windows.json").write_text('{"w1": {}}', encoding="utf-8")
    (tmp_path / "tests.json").write_text('{"t1": {}}', encoding="utf-8")
    old_dir = tmp_path / "test_responses"
    (old_dir / "t1").mkdir(parents=True)
    (old_dir / "t1" / "stu1.json").write_text('{"answers": {}}', encoding="utf-8")

    moved = a.migrate_test_to_assessment_names()
    assert len(moved) == 3, moved
    assert (tmp_path / "assessment_windows.json").exists()
    assert (tmp_path / "assessments.json").exists()
    assert (tmp_path / "assessment_responses" / "t1" / "stu1.json").exists()
    assert not (tmp_path / "test_windows.json").exists()

    # Second run is a no-op, not an error — startup runs it every boot.
    assert a.migrate_test_to_assessment_names() == []


def test_rename_migration_refuses_to_overwrite_an_existing_new_file(tmp_path, monkeypatch):
    # Both names present means someone has run old and new code against the
    # same DATA_ROOT. Picking a winner automatically is how a season of
    # responses disappears, so it must refuse and say so.
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import assessments as a
    importlib.reload(a)

    (tmp_path / "tests.json").write_text('{"old": {}}', encoding="utf-8")
    (tmp_path / "assessments.json").write_text('{"new": {}}', encoding="utf-8")

    moved = a.migrate_test_to_assessment_names()
    assert any("SKIPPED" in m for m in moved), moved
    assert (tmp_path / "tests.json").read_text(encoding="utf-8") == '{"old": {}}'
    assert (tmp_path / "assessments.json").read_text(encoding="utf-8") == '{"new": {}}'


def test_legacy_single_file_constant_still_uses_its_real_on_disk_name(tmp_path, monkeypatch):
    # Regression guard for a bug introduced by the rename itself: the
    # pre-per-file responses blob is called test_responses.json on disk and
    # always was. Renaming the constant would point the backfill at a file
    # that has never existed and silently strand every old response.
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import assessments as a
    importlib.reload(a)
    assert a._LEGACY_RESPONSES_FILE.name == "test_responses.json"
