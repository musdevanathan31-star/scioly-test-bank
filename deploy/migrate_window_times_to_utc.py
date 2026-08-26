#!/usr/bin/env python3
"""One-off, idempotent migration: rewrite naive local wall-clock
opens_at/closes_at values (in assessment_windows.json, and every
overrides[username] inside assessments.json) to offset-aware UTC ISO
strings.

Why this exists: opens_at/closes_at were entered through an
<input type="datetime-local">, which yields naive text like
"2026-08-26T18:00" meaning 6pm in the COACH's own local zone. Nothing
converted it before it hit storage, and assessments.is_window_open /
is_window_past then read a naive value as if it were already UTC — so a
window a coach set to close at 6pm local actually closed hours earlier on
the server's UTC clock. The app-side fix (assessments_dashboard.html)
now converts local -> UTC in the browser at entry time, but every row
written before that fix is still naive text sitting in these two files.
This script rewrites those existing rows in place, using a real IANA zone
(so a season that spans a DST change is handled correctly — a single
fixed offset would be wrong for half the season) rather than guessing.

Usage:
    python deploy/migrate_window_times_to_utc.py --tz America/New_York
    python deploy/migrate_window_times_to_utc.py --tz America/New_York --dry-run

Run this ONCE per existing instance, after deploying the browser-side fix
and before (or right after) coaches start entering new windows again --
see MIGRATION.md. Safe to re-run: any value that already carries an
offset or "Z" is left byte-identical, so a second run is a no-op.

Honors DATA_ROOT exactly like assessments.py, so it finds the same files
review_app.py reads/writes on this instance.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("DATA_ROOT") or REPO_ROOT)
WINDOWS_FILE = DATA_ROOT / "assessment_windows.json"
ASSESSMENTS_FILE = DATA_ROOT / "assessments.json"


def convert(value: str, tz: ZoneInfo) -> tuple[str, bool]:
    """Returns (new_value, changed). A value that already parses with a
    tzinfo (an offset or trailing "Z") is returned unchanged — that's what
    makes re-running this safe. An empty/unparseable value is left alone
    too (nothing safe to do with it here)."""
    if not value:
        return value, False
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value, False
    if dt.tzinfo is not None:
        return value, False
    aware = dt.replace(tzinfo=tz)
    return aware.astimezone(ZoneInfo("UTC")).isoformat(), True


def migrate_windows(tz: ZoneInfo, dry_run: bool) -> int:
    if not WINDOWS_FILE.exists():
        return 0
    raw = json.loads(WINDOWS_FILE.read_text(encoding="utf-8"))
    changed = 0
    for window_id, w in raw.items():
        for field in ("opens_at", "closes_at"):
            old = w.get(field, "")
            new, did_change = convert(old, tz)
            if did_change:
                print(f"  window {window_id} .{field}: {old!r} -> {new!r}")
                if not dry_run:
                    w[field] = new
                changed += 1
    if changed and not dry_run:
        _write_atomic(WINDOWS_FILE, raw)
    return changed


def migrate_assessments(tz: ZoneInfo, dry_run: bool) -> int:
    if not ASSESSMENTS_FILE.exists():
        return 0
    raw = json.loads(ASSESSMENTS_FILE.read_text(encoding="utf-8"))
    changed = 0
    for assessment_id, t in raw.items():
        overrides = t.get("overrides") or {}
        for username, ov in overrides.items():
            for field in ("opens_at", "closes_at"):
                old = ov.get(field, "")
                new, did_change = convert(old, tz)
                if did_change:
                    print(f"  assessment {assessment_id} override[{username}].{field}: {old!r} -> {new!r}")
                    if not dry_run:
                        ov[field] = new
                    changed += 1
    if changed and not dry_run:
        _write_atomic(ASSESSMENTS_FILE, raw)
    return changed


def _write_atomic(path: Path, data) -> None:
    """Back up the existing file, then write the new content the same way
    assessments.py's _save_json_unlocked does (tempfile + os.replace) so a
    crash mid-write can never leave a truncated/corrupt file on disk."""
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    print(f"  (backed up {path.name} -> {backup.name})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tz", required=True,
                         help="IANA zone name the naive values were originally entered in, e.g. America/New_York")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would change without writing anything")
    args = parser.parse_args()

    try:
        tz = ZoneInfo(args.tz)
    except ZoneInfoNotFoundError:
        print(f"error: unknown IANA zone name {args.tz!r}", file=sys.stderr)
        return 2

    print(f"Migrating naive local times ({args.tz}) to UTC-aware ISO in {DATA_ROOT} "
          f"{'(dry run — nothing will be written)' if args.dry_run else ''}")
    print(f"windows file: {WINDOWS_FILE}")
    n_windows = migrate_windows(tz, args.dry_run)
    print(f"assessments file: {ASSESSMENTS_FILE}")
    n_overrides = migrate_assessments(tz, args.dry_run)

    total = n_windows + n_overrides
    if total == 0:
        print("Nothing to migrate — every stored value is already offset-aware (or files are empty/missing).")
    else:
        verb = "Would convert" if args.dry_run else "Converted"
        print(f"{verb} {n_windows} window field(s) and {n_overrides} override field(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
