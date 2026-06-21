"""
Snapshot/restore for destructive PDF reprocesses, plus an operator-only CLI
for actual disk cleanup.

The web app never permanently deletes anything (see review_app.py's
reprocess route and auth.py/events.py's disable/archive flags). For
Reprocess's "wipe annotations" / "manual mode", that means snapshotting a
PDF's annotations/manual/questions state to `<event>/.archive/<pdfname>/
<timestamp>.json` before the wipe, so it can be restored later. No retention
limit — this is small JSON, not PDFs, and pruning it automatically would
itself be a deletion the app performs on the user's behalf. Real cleanup is
this module's CLI, run directly on the server, never through the web app.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_write_lock = threading.Lock()

_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z\.json$")


def _archive_dir(event, pdfname: str) -> Path:
    return event.base_dir / ".archive" / pdfname


def snapshot_pdf_state(event, pdfname: str, state: dict) -> Path:
    """Save a timestamped copy of one PDF's annotations/manual/questions
    before a destructive reprocess wipes them. Atomic tempfile+os.replace,
    same pattern as build_question_bank.py's _save_state."""
    snapshot = {
        "pdfname": pdfname,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "annotations": state.get("annotations", {}).get(pdfname),
        "manual": state.get("manual", {}).get(pdfname),
        "questions": state.get("questions", {}).get(pdfname),
    }
    d = _archive_dir(event, pdfname)
    with _write_lock:
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = d / f"{ts}.json"
        n = 1
        while dest.exists():
            dest = d / f"{ts}-{n}.json"
            n += 1
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, dest)
    return dest


def list_snapshots(event, pdfname: str) -> list[dict]:
    """Newest first. Each entry: {"file", "saved_at"}."""
    d = _archive_dir(event, pdfname)
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.json"):
        try:
            saved_at = json.loads(f.read_text(encoding="utf-8")).get("saved_at")
        except Exception:
            saved_at = None
        out.append({"file": f.name, "saved_at": saved_at})
    out.sort(key=lambda e: e["file"], reverse=True)
    return out


def load_snapshot(event, pdfname: str, filename: str) -> dict:
    """Raises FileNotFoundError / ValueError on a bad filename — callers
    should catch and turn into a 404/400."""
    if not re.match(r"^\d{8}T\d{6}Z(-\d+)?\.json$", filename):
        raise ValueError(f"not a valid snapshot filename: {filename!r}")
    f = _archive_dir(event, pdfname) / filename
    if not f.exists():
        raise FileNotFoundError(filename)
    return json.loads(f.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Operator-only CLI — actual disk cleanup, never exposed via the web app.
# ---------------------------------------------------------------------------

def _purge_snapshots_older_than(days: int, repo_root: Path) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for archive_dir in repo_root.glob("*/.archive/*"):
        if not archive_dir.is_dir():
            continue
        for f in archive_dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
    return removed


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Operator-only archive cleanup. Never run this from the web app.")
    parser.add_argument("--purge-snapshots-older-than-days", type=int, metavar="N",
                         help="Permanently delete reprocess snapshots older than N days.")
    args = parser.parse_args()
    repo_root = Path(__file__).parent
    if args.purge_snapshots_older_than_days is not None:
        n = _purge_snapshots_older_than(args.purge_snapshots_older_than_days, repo_root)
        print(f"Removed {n} snapshot file(s) older than "
              f"{args.purge_snapshots_older_than_days} days.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
