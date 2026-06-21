#!/usr/bin/env python3
"""Builds a human-readable git commit message for the extracted-data backup
by diffing lastEditedBy/lastEditedDateTime between the previously-committed
state and the freshly copied one, per event.

Usage: qbank-commit-message.py <old_dir> <new_dir> <event_slug> [<event_slug> ...]
Prints a commit message to stdout; exits 1 (nothing to print) if nothing
changed and exits 2 if this looks like the first-ever backup (no previous
committed state to diff against at all).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_LISTED = 8  # cap before falling back to a count summary


def load_state(d: str, slug: str) -> dict:
    p = Path(d) / slug / ".qbank_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def edit_stamps(state: dict) -> dict[str, tuple[str | None, str]]:
    """qnum -> (editor, lastEditedDateTime). Reprocessing the pipeline never
    touches these fields (only api_patch_question does), so this only ever
    reflects genuine human edits, not pipeline noise."""
    stamps: dict[str, tuple[str | None, str]] = {}
    for qs in state.get("questions", {}).values():
        for q in qs:
            if "lastEditedDateTime" in q:
                stamps[q["number"]] = (q.get("lastEditedBy"), q["lastEditedDateTime"])
    for ann in state.get("annotations", {}).values():
        for qnum, fo in ann.get("field_overrides", {}).items():
            if "lastEditedDateTime" in fo:
                stamps[qnum] = (fo.get("lastEditedBy"), fo["lastEditedDateTime"])
    return stamps


def changed_questions(old_dir: str, new_dir: str, slug: str):
    old = edit_stamps(load_state(old_dir, slug))
    new = edit_stamps(load_state(new_dir, slug))
    return [(q, by) for q, (by, ts) in new.items() if old.get(q, (None, ""))[1] != ts]


def summarize(slug: str, changed) -> str | None:
    if not changed:
        return None
    if len(changed) <= MAX_LISTED:
        by_editor: dict[str, list[str]] = {}
        for q, by in changed:
            by_editor.setdefault(by or "unknown", []).append(q)
        parts = [f"Q{','.join(qs)} edited by {by}" for by, qs in by_editor.items()]
        return f"{slug}: " + "; ".join(parts)
    editors = {by for _, by in changed}
    top_editor = max(editors, key=lambda b: sum(1 for _, x in changed if x == b))
    return f"{slug}: {len(changed)} questions edited (mostly by {top_editor})"


def main() -> None:
    old_dir, new_dir, *slugs = sys.argv[1:]
    old_root = Path(old_dir)
    if not old_root.exists() or not any(old_root.iterdir()):
        # Nothing committed yet — this is the first-ever backup.
        sys.exit(2)
    lines = [s for s in (summarize(slug, changed_questions(old_dir, new_dir, slug))
                         for slug in slugs) if s]
    if not lines:
        sys.exit(1)
    print(lines[0] if len(lines) == 1 else "Multiple events updated\n\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
