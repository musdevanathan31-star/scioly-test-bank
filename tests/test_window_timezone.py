"""
Regression coverage for the "extended window still shows as past" bug.

A coach extended a published test's window; the student still saw it as
past and couldn't take it. Root cause: opens_at/closes_at came from an
<input type="datetime-local">, which yields NAIVE local wall-clock text
("2026-08-26T18:00" — 6pm in the coach's own zone). Nothing converted it,
and assessments.is_window_open/is_window_past then did

    if closes.tzinfo is None:
        closes = closes.replace(tzinfo=timezone.utc)

silently reinterpreting that local wall time as if it were already UTC.
On a UTC-4 machine, a window extended to 18:00 local was read as
18:00 UTC == 14:00 local, so it looked closed hours before it should have
— worse, extending it in the afternoon made it look already-past
immediately, which is exactly the reported symptom.

The fix: store absolute instants. The browser converts local -> UTC at
entry (new Date(...).toISOString()), so stored values are offset-aware
ISO strings. This file proves:
  - is_window_open/is_window_past behave correctly around the boundary
    for offset-aware stored values (the normal path going forward)
  - the exact reported bug, as a regression: a window stored as a UTC
    instant that is open in the evening in the coach's own local zone is
    reported open, not past
  - the naive-value fallback still exists (an unmigrated instance keeps
    functioning) but is legacy — covered here only to show it still
    round-trips, not endorsed as correct for new data
  - deploy/migrate_window_times_to_utc.py converts naive stored values
    using a real IANA zone (DST-correct, unlike a fixed offset), is
    idempotent, migrates assessments.json overrides as well as
    assessment_windows.json, and never touches an already-aware value

Run with: `python -m pytest tests/test_window_timezone.py -q`
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import assessments as am   # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_window_times_to_utc", REPO_ROOT / "deploy" / "migrate_window_times_to_utc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _window(opens_at: str, closes_at: str) -> am.AssessmentWindow:
    return am.AssessmentWindow(window_id="w1", season_id="s1", opens_at=opens_at, closes_at=closes_at)


def _assessment(overrides: dict | None = None) -> am.Assessment:
    return am.Assessment(assessment_id="a1", window_id="w1", season_id="s1", event_slug="circuit_lab",
                          overrides=overrides or {})


# ---------------------------------------------------------------------------
# is_window_open / is_window_past with offset-aware stored values
# ---------------------------------------------------------------------------

def test_open_window_offset_aware_reports_open_at_the_boundary():
    now = datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc)
    w = _window((now - timedelta(hours=1)).isoformat(), (now + timedelta(hours=1)).isoformat())
    t = _assessment()
    assert am.is_window_open(t, w, "stu1", now=now) is True
    assert am.is_window_past(t, w, "stu1", now=now) is False


def test_window_closed_exactly_one_second_past_close_is_past_not_open():
    closes = datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc)
    w = _window((closes - timedelta(hours=1)).isoformat(), closes.isoformat())
    t = _assessment()
    just_before = closes - timedelta(seconds=1)
    just_after = closes + timedelta(seconds=1)
    assert am.is_window_open(t, w, "stu1", now=just_before) is True
    assert am.is_window_open(t, w, "stu1", now=just_after) is False
    assert am.is_window_past(t, w, "stu1", now=just_before) is False
    assert am.is_window_past(t, w, "stu1", now=just_after) is True


def test_offset_aware_value_with_non_utc_offset_is_read_correctly():
    # -04:00 offset (US Eastern, summer) rather than "Z" — fromisoformat
    # must resolve this to the same absolute instant either way.
    w = _window("2026-08-26T10:00:00-04:00", "2026-08-26T18:00:00-04:00")
    t = _assessment()
    # 18:00-04:00 == 22:00 UTC
    just_open = datetime(2026, 8, 26, 21, 59, 59, tzinfo=timezone.utc)
    just_closed = datetime(2026, 8, 26, 22, 0, 1, tzinfo=timezone.utc)
    assert am.is_window_open(t, w, "stu1", now=just_open) is True
    assert am.is_window_open(t, w, "stu1", now=just_closed) is False


# ---------------------------------------------------------------------------
# The reported bug, as a regression
# ---------------------------------------------------------------------------

def test_coach_extends_window_to_evening_local_time_student_sees_it_open():
    """The exact report: a coach extends a published test's window to
    6pm in their own (US Eastern, UTC-4) local time. Stored correctly (as
    the browser now does — an absolute UTC instant), the student must see
    it as open during that local evening, not as already past."""
    eastern = ZoneInfo("America/New_York")
    coach_local_close = datetime(2026, 8, 26, 18, 0, 0, tzinfo=eastern)
    coach_local_open = datetime(2026, 8, 26, 9, 0, 0, tzinfo=eastern)
    w = _window(coach_local_open.astimezone(timezone.utc).isoformat(),
                coach_local_close.astimezone(timezone.utc).isoformat())
    t = _assessment()
    # A moment during that evening, in the coach's own local time —
    # 5:30pm Eastern, before the 6pm local close.
    now_local = datetime(2026, 8, 26, 17, 30, 0, tzinfo=eastern)
    now_utc = now_local.astimezone(timezone.utc)
    assert am.is_window_open(t, w, "stu1", now=now_utc) is True
    assert am.is_window_past(t, w, "stu1", now=now_utc) is False


# ---------------------------------------------------------------------------
# Legacy naive fallback — still functions, but is not the contract for new data
# ---------------------------------------------------------------------------

def test_naive_stored_value_still_falls_back_to_being_read_as_utc():
    """This is the LEGACY path (see is_window_open's comment) — kept only
    so an unmigrated instance doesn't crash. Explicitly NOT the behavior
    new data should ever hit; deploy/migrate_window_times_to_utc.py exists
    to eliminate naive values from storage."""
    w = _window("2026-08-26T10:00:00", "2026-08-26T18:00:00")
    t = _assessment()
    now = datetime(2026, 8, 26, 17, 0, 0, tzinfo=timezone.utc)   # naive "18:00" read as 18:00 UTC
    assert am.is_window_open(t, w, "stu1", now=now) is True


# ---------------------------------------------------------------------------
# Migration script
# ---------------------------------------------------------------------------

def _write_windows(path: Path, windows: dict) -> None:
    path.write_text(json.dumps(windows), encoding="utf-8")


def _write_assessments(path: Path, assessments_: dict) -> None:
    path.write_text(json.dumps(assessments_), encoding="utf-8")


def test_migration_converts_naive_window_value_using_given_zone(tmp_path, monkeypatch):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_windows(mod.WINDOWS_FILE, {
        "w1": {"window_id": "w1", "opens_at": "2026-01-15T09:00:00", "closes_at": "2026-01-15T17:00:00"},
    })

    changed = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    assert changed == 2

    raw = json.loads(mod.WINDOWS_FILE.read_text(encoding="utf-8"))
    # January is EST (UTC-5): 09:00 local -> 14:00 UTC.
    opens = datetime.fromisoformat(raw["w1"]["opens_at"])
    assert opens.tzinfo is not None
    assert opens.astimezone(timezone.utc) == datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)


def test_migration_is_idempotent(tmp_path, monkeypatch):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_windows(mod.WINDOWS_FILE, {
        "w1": {"window_id": "w1", "opens_at": "2026-01-15T09:00:00", "closes_at": "2026-01-15T17:00:00"},
    })

    first = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    assert first == 2
    after_first = mod.WINDOWS_FILE.read_text(encoding="utf-8")

    second = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    assert second == 0
    after_second = mod.WINDOWS_FILE.read_text(encoding="utf-8")
    assert after_first == after_second


def test_migration_handles_each_side_of_a_dst_transition_differently(tmp_path, monkeypatch):
    """A fixed-offset implementation would get one of these two rows
    wrong — US Eastern's DST-in transition (spring forward) is 2026-03-08.
    The row just before it is EST (UTC-5); the row just after is EDT
    (UTC-4). A single hardcoded offset cannot produce both correctly."""
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_windows(mod.WINDOWS_FILE, {
        "before": {"window_id": "before", "opens_at": "2026-03-01T12:00:00", "closes_at": "2026-03-01T13:00:00"},
        "after": {"window_id": "after", "opens_at": "2026-03-15T12:00:00", "closes_at": "2026-03-15T13:00:00"},
    })

    changed = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    assert changed == 4

    raw = json.loads(mod.WINDOWS_FILE.read_text(encoding="utf-8"))
    before_utc = datetime.fromisoformat(raw["before"]["opens_at"]).astimezone(timezone.utc)
    after_utc = datetime.fromisoformat(raw["after"]["opens_at"]).astimezone(timezone.utc)
    # EST (UTC-5): 12:00 local -> 17:00 UTC.
    assert before_utc == datetime(2026, 3, 1, 17, 0, 0, tzinfo=timezone.utc)
    # EDT (UTC-4): 12:00 local -> 16:00 UTC.
    assert after_utc == datetime(2026, 3, 15, 16, 0, 0, tzinfo=timezone.utc)


def test_migration_converts_overrides_inside_assessments_json(tmp_path, monkeypatch):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_assessments(mod.ASSESSMENTS_FILE, {
        "a1": {
            "assessment_id": "a1",
            "overrides": {
                "stu1": {"opens_at": "2026-01-15T09:00:00", "closes_at": "2026-01-15T17:00:00",
                         "granted_by": "coach1", "granted_at": "2026-01-14T00:00:00Z", "reason": "sick day"},
            },
        },
    })

    changed = mod.migrate_assessments(ZoneInfo("America/New_York"), dry_run=False)
    assert changed == 2

    raw = json.loads(mod.ASSESSMENTS_FILE.read_text(encoding="utf-8"))
    ov = raw["a1"]["overrides"]["stu1"]
    opens = datetime.fromisoformat(ov["opens_at"])
    assert opens.tzinfo is not None
    assert opens.astimezone(timezone.utc) == datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
    # granted_at already carries "Z" — not a target field, must be untouched.
    assert ov["granted_at"] == "2026-01-14T00:00:00Z"
    # Non-time fields are preserved as-is.
    assert ov["granted_by"] == "coach1"
    assert ov["reason"] == "sick day"


def test_migration_leaves_already_aware_value_byte_identical(tmp_path, monkeypatch):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    aware_value = "2026-01-15T14:00:00+00:00"
    _write_windows(mod.WINDOWS_FILE, {
        "w1": {"window_id": "w1", "opens_at": aware_value, "closes_at": "2026-01-15T22:00:00Z"},
    })

    changed = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    assert changed == 0

    raw = json.loads(mod.WINDOWS_FILE.read_text(encoding="utf-8"))
    assert raw["w1"]["opens_at"] == aware_value
    assert raw["w1"]["closes_at"] == "2026-01-15T22:00:00Z"


def test_migration_dry_run_prints_but_does_not_write(tmp_path, monkeypatch, capsys):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_windows(mod.WINDOWS_FILE, {
        "w1": {"window_id": "w1", "opens_at": "2026-01-15T09:00:00", "closes_at": "2026-01-15T17:00:00"},
    })
    before = mod.WINDOWS_FILE.read_text(encoding="utf-8")

    changed = mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=True)
    assert changed == 2
    assert mod.WINDOWS_FILE.read_text(encoding="utf-8") == before


def test_migration_backs_up_files_it_edits(tmp_path, monkeypatch):
    mod = _load_migration_module()
    monkeypatch.setattr(mod, "WINDOWS_FILE", tmp_path / "assessment_windows.json")
    monkeypatch.setattr(mod, "ASSESSMENTS_FILE", tmp_path / "assessments.json")
    _write_windows(mod.WINDOWS_FILE, {
        "w1": {"window_id": "w1", "opens_at": "2026-01-15T09:00:00", "closes_at": "2026-01-15T17:00:00"},
    })

    mod.migrate_windows(ZoneInfo("America/New_York"), dry_run=False)
    backup = mod.WINDOWS_FILE.with_suffix(".json.bak")
    assert backup.exists()
    backup_raw = json.loads(backup.read_text(encoding="utf-8"))
    assert backup_raw["w1"]["opens_at"] == "2026-01-15T09:00:00"


def test_migration_unknown_zone_name_errors_cleanly():
    mod = _load_migration_module()
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "deploy" / "migrate_window_times_to_utc.py"),
         "--tz", "Not/A_Real_Zone", "--dry-run"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "unknown IANA zone" in result.stderr
