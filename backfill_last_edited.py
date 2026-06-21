"""
One-time, idempotent migration: backfill `lastEditedBy`/`lastEditedDateTime`
on every pre-existing question that doesn't have them yet (added when those
fields were introduced — see review_app.py's api_patch_question and
build_question_bank.py's apply_annotations).

Run once: `python backfill_last_edited.py`
Safe to re-run — only touches questions missing the field, so a second run
is a no-op.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import events

BACKFILL_USER = "srikanth"


def _save_state(state_file, state: dict) -> None:
    # Same atomic tempfile+os.replace pattern as every other state-writer
    # in this codebase (build_question_bank.py's _save_state, events.py's
    # _save_custom_events, auth.py's _save).
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, state_file)


def backfill_event(slug: str, ev) -> int:
    state_file = ev.state_file
    if not state_file.exists():
        return 0
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [SKIP] {slug}: could not read state file ({e})")
        return 0

    manual = state.get("manual", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = 0
    for bucket, questions in state.get("questions", {}).items():
        bucket_edited_at = (manual.get(bucket) or {}).get("edited_at")
        for q in questions:
            if q.get("lastEditedBy"):
                continue  # already backfilled or set by a real edit
            q["lastEditedBy"] = BACKFILL_USER
            q["lastEditedDateTime"] = bucket_edited_at or now
            touched += 1

    if touched:
        _save_state(state_file, state)
    return touched


def main() -> None:
    total = 0
    for slug, ev in sorted(events.EVENTS.items()):
        n = backfill_event(slug, ev)
        if n:
            print(f"  [{slug}] backfilled {n} question(s)")
        total += n
    print(f"\nDone. Backfilled {total} question(s) across {len(events.EVENTS)} event(s).")


if __name__ == "__main__":
    main()
