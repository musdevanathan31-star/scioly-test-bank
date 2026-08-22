"""
Regression coverage for testing.migrate_legacy_responses(): the one-time,
idempotent backfill from the pre-redesign single-file test_responses.json
into the current per-(test_id, username) file layout (see testing.py's
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

import testing as testing_mod  # noqa: E402


def _seed_legacy_file(path: Path) -> None:
    path.write_text(json.dumps({
        "test1": {
            "alice": {
                "student_username": "alice", "test_id": "test1",
                "question_order": [1, 0], "answers": {"0": {"qtype": "mcq", "picked": "A"}},
                "auto_grade": {}, "manual_grade": {}, "status": "in_progress",
                "started_at": "2026-01-01T00:00:00", "last_saved_at": "2026-01-01T00:01:00",
                "submitted_at": None, "released": False, "released_at": None, "released_by": None,
            },
            "bob": {
                "student_username": "bob", "test_id": "test1",
                "question_order": [0, 1], "answers": {}, "auto_grade": {}, "manual_grade": {},
                "status": "in_progress", "started_at": "2026-01-01T00:00:00",
                "last_saved_at": "2026-01-01T00:00:00", "submitted_at": None,
                "released": False, "released_at": None, "released_by": None,
            },
        },
    }), encoding="utf-8")


def test_migrate_legacy_responses_backfills_and_renames(tmp_path, monkeypatch):
    legacy = tmp_path / "test_responses.json"
    monkeypatch.setattr(testing_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(testing_mod, "RESPONSES_DIR", tmp_path / "test_responses")
    _seed_legacy_file(legacy)

    migrated = testing_mod.migrate_legacy_responses()

    assert migrated == 2
    alice = testing_mod.get_response("test1", "alice")
    assert alice is not None
    assert alice.answers == {"0": {"qtype": "mcq", "picked": "A"}}
    bob = testing_mod.get_response("test1", "bob")
    assert bob is not None
    assert bob.answers == {}
    assert not legacy.exists(), "legacy file must not be left in place once migrated"
    assert (tmp_path / "test_responses.json.migrated").exists(), "legacy data must be renamed, not deleted"


def test_migrate_legacy_responses_is_idempotent(tmp_path, monkeypatch):
    legacy = tmp_path / "test_responses.json"
    monkeypatch.setattr(testing_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(testing_mod, "RESPONSES_DIR", tmp_path / "test_responses")
    _seed_legacy_file(legacy)

    first = testing_mod.migrate_legacy_responses()
    second = testing_mod.migrate_legacy_responses()

    assert first == 2
    assert second == 0, "a second run, after the legacy file was renamed away, must be a pure no-op"


def test_migrate_legacy_responses_noop_when_no_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(testing_mod, "_LEGACY_RESPONSES_FILE", tmp_path / "test_responses.json")
    monkeypatch.setattr(testing_mod, "RESPONSES_DIR", tmp_path / "test_responses")

    assert testing_mod.migrate_legacy_responses() == 0


def test_migrate_legacy_responses_skips_already_migrated_pairs(tmp_path, monkeypatch):
    """Simulates an interrupted first run: the legacy file is still present
    (the final rename never happened), but one pair was already written
    out to its new per-pair file. Re-running must resume — migrate only
    what's missing — not clobber the already-migrated file with stale
    legacy data."""
    legacy = tmp_path / "test_responses.json"
    monkeypatch.setattr(testing_mod, "_LEGACY_RESPONSES_FILE", legacy)
    monkeypatch.setattr(testing_mod, "RESPONSES_DIR", tmp_path / "test_responses")
    _seed_legacy_file(legacy)

    # Pre-create alice's new-layout file with DIFFERENT content than the
    # legacy blob has for her, so a wrongly-clobbering migration is
    # detectable rather than silently passing either way.
    alice_path = testing_mod._response_path("test1", "alice")
    alice_path.parent.mkdir(parents=True, exist_ok=True)
    alice_path.write_text(json.dumps({
        "student_username": "alice", "test_id": "test1", "question_order": [1, 0],
        "answers": {"0": {"qtype": "mcq", "picked": "Z"}}, "auto_grade": {}, "manual_grade": {},
        "status": "submitted", "started_at": "2026-01-01T00:00:00",
        "last_saved_at": "2026-01-01T00:05:00", "submitted_at": "2026-01-01T00:05:00",
        "released": False, "released_at": None, "released_by": None,
    }), encoding="utf-8")

    migrated = testing_mod.migrate_legacy_responses()

    assert migrated == 1, "only bob should be newly migrated — alice's file already existed"
    alice = testing_mod.get_response("test1", "alice")
    assert alice.status == "submitted", "pre-existing alice file must not be overwritten by legacy data"
    assert alice.answers == {"0": {"qtype": "mcq", "picked": "Z"}}
