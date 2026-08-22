"""
Phase 3 of the tournament archive: renaming, moving, creating and deleting.

Everything here changes the filesystem, so everything here previews first.
A mis-scoped move across 65GB has no undo button, and the counts that make a
preview meaningful ("moves 412 files, 3.1 GB") are already in the index — so
the preview costs nothing and there is no excuse for skipping it.

Three rules shape the module:

- **Nothing is destroyed.** Deletes move to `<DATA_ROOT>/.deleted/`, the same
  trash the rest of the app uses. That is why this does not sit behind
  `ALLOW_HARD_DELETE`: organising inherently means deleting junk, and gating
  it on that flag would mean leaving user/season/event deletion switched on
  for the whole triage effort.
- **Every mutation is logged** to an append-only JSONL file, with the paths
  before and after. It is an audit trail now and the prerequisite for undo
  later; neither can be reconstructed after the fact.
- **The index is patched, not rebuilt.** Re-walking the corpus after each
  rename would make the tool unusable at this size.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import events as events_mod
import tournament_archive as ta

OPS_LOG = "archive_ops.jsonl"

#: Serialises mutations against each other. Reads are not blocked: a listing
#: racing a rename shows a stale name, which the next refresh fixes.
_lock = threading.RLock()


class ArchiveOpError(Exception):
    """Refused — the caller asked for something this module will not do."""


def ops_log_path():
    return events_mod.DATA_ROOT / OPS_LOG


def log_op(action: str, **fields) -> None:
    """Append one record. Never raises: losing an audit line must not undo a
    move that already happened on disk."""
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "action": action, **fields}
    try:
        path = ops_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_ops(limit: int = 100) -> list:
    """The most recent operations, newest first."""
    path = ops_log_path()
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: Rejected outright in a new or renamed folder name. Path separators and
#: traversal are the security cases; the rest are names that make a tree
#: painful to work with over scp and rsync.
_BAD_NAME_CHARS = set('/\\:*?"<>|\0')


def check_name(name: str) -> str:
    name = (name or "").strip().strip(".")
    if not name:
        raise ArchiveOpError("name cannot be empty")
    if name in (".", ".."):
        raise ArchiveOpError("name cannot be . or ..")
    if set(name) & _BAD_NAME_CHARS:
        raise ArchiveOpError(r'name cannot contain / \ : * ? " < > |')
    if len(name) > 120:
        raise ArchiveOpError("name is too long (120 characters max)")
    return name


def _entry(rel: str) -> dict | None:
    index = ta.load_index() or {}
    return (index.get("dirs") or {}).get(rel)


def _counts(rel: str, path: Path) -> dict:
    """How much a folder holds. From the index when it knows, from a walk
    when it does not — a folder created since the last build is empty or
    nearly so, which is cheap to count and wrong to report as zero."""
    entry = _entry(rel)
    if entry is not None:
        return {"files": entry.get("total_files", 0),
                "bytes": entry.get("total_bytes", 0), "estimated": False}
    files = total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            if name in ta.IGNORED_NAMES:
                continue
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
            files += 1
    return {"files": files, "bytes": total, "estimated": True}


def _resolve_dir(rel: str) -> Path:
    path = ta.safe_path(rel)
    if not path.is_dir():
        raise ArchiveOpError(f"no such folder: {rel or '/'}")
    return path


def _resolve_file(rel: str) -> Path:
    path = ta.safe_path(rel)
    if not path.is_file():
        raise ArchiveOpError(f"no such file: {rel}")
    return path


def _is_within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# Previews — what would happen, without touching anything
# ---------------------------------------------------------------------------

def preview_rename(rel: str, new_name: str) -> dict:
    if not rel:
        raise ArchiveOpError("the archive root cannot be renamed")
    path = _resolve_dir(rel)
    name = check_name(new_name)
    parent = ta._parent_of(rel)
    new_rel = f"{parent}/{name}" if parent else name
    if new_rel != rel and ta.safe_path(new_rel).exists():
        raise ArchiveOpError(f"{name} already exists here")
    return {"action": "rename", "src": rel, "dest": new_rel,
            "unchanged": new_rel == rel, **_counts(rel, path)}


def preview_move(src: str, dest_parent: str) -> dict:
    if not src:
        raise ArchiveOpError("the archive root cannot be moved")
    path = _resolve_dir(src)
    _resolve_dir(dest_parent) if dest_parent else ta.archive_root()
    if _is_within(dest_parent, src):
        # Moving a folder into its own subtree would relocate the destination
        # along with it. shutil would half-do it before failing.
        raise ArchiveOpError("a folder cannot be moved inside itself")
    name = src.rsplit("/", 1)[-1]
    new_rel = f"{dest_parent}/{name}" if dest_parent else name
    if new_rel == src:
        raise ArchiveOpError("that folder is already there")
    if ta.safe_path(new_rel).exists():
        raise ArchiveOpError(f"{name} already exists in the destination")
    return {"action": "move", "src": src, "dest": new_rel,
            **_counts(src, path)}


def preview_delete(rel: str) -> dict:
    if not rel:
        raise ArchiveOpError("the archive root cannot be deleted")
    path = ta.safe_path(rel)
    if path.is_file():
        return {"action": "delete", "src": rel, "is_file": True,
                "files": 1, "bytes": path.stat().st_size, "estimated": False}
    if not path.is_dir():
        raise ArchiveOpError(f"no such folder: {rel}")
    return {"action": "delete", "src": rel, "is_file": False,
            **_counts(rel, path)}


def preview_create(parent: str, name: str) -> dict:
    if parent:
        _resolve_dir(parent)
    name = check_name(name)
    new_rel = f"{parent}/{name}" if parent else name
    if ta.safe_path(new_rel).exists():
        raise ArchiveOpError(f"{name} already exists here")
    return {"action": "create", "dest": new_rel, "files": 0, "bytes": 0,
            "estimated": False}


PREVIEWS = {"rename": preview_rename, "move": preview_move,
            "delete": preview_delete, "create": preview_create}


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def rename(rel: str, new_name: str, by: str = "") -> dict:
    with _lock:
        plan = preview_rename(rel, new_name)
        if plan["unchanged"]:
            return plan
        src, dest = ta.safe_path(rel), ta.safe_path(plan["dest"])
        src.rename(dest)
        ta.index_move(rel, plan["dest"])
        log_op("rename", src=rel, dest=plan["dest"], by=by,
               files=plan["files"], bytes=plan["bytes"])
        return plan


def move(src: str, dest_parent: str, by: str = "") -> dict:
    with _lock:
        plan = preview_move(src, dest_parent)
        src_path, dest_path = ta.safe_path(src), ta.safe_path(plan["dest"])
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # shutil.move rather than rename: the archive is inside DATA_ROOT so
        # this is normally one filesystem, but a DATA_ROOT spanning a mount
        # point would make rename() fail with EXDEV where a copy succeeds.
        shutil.move(str(src_path), str(dest_path))
        ta.index_move(src, plan["dest"])
        log_op("move", src=src, dest=plan["dest"], by=by,
               files=plan["files"], bytes=plan["bytes"])
        return plan


def create_folder(parent: str, name: str, by: str = "") -> dict:
    with _lock:
        plan = preview_create(parent, name)
        ta.safe_path(plan["dest"]).mkdir(parents=True)
        ta.index_add_dir(plan["dest"])
        log_op("create", dest=plan["dest"], by=by)
        return plan


def delete(rel: str, by: str = "") -> dict:
    """Move to the shared trash. Recoverable by design — see module docstring."""
    with _lock:
        plan = preview_delete(rel)
        path = ta.safe_path(rel)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # The archive-relative path is kept in the trash folder name because
        # basenames here are meaningless ("test.pdf" a thousand times over)
        # and a coach restoring something needs to know which one it was.
        label = rel.replace("/", "__")[:80]
        dest_dir = trash_dir() / f"archive-{label}-{stamp}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        if plan["is_file"]:
            ta.index_remove_file(rel, plan["bytes"])
        else:
            ta.index_remove(rel)
        log_op("delete", src=rel, trash=str(dest_dir), by=by,
               files=plan["files"], bytes=plan["bytes"])
        plan["trash"] = str(dest_dir)
        return plan


def trash_dir() -> Path:
    """The same trash the rest of the app uses, so there is one place to look."""
    import deletion
    return deletion.trash_dir()


def delete_empty_folders(rel: str = "", by: str = "") -> dict:
    """Remove every empty folder under `rel`, bottom-up (requirement 4).

    Genuinely empty only -- a folder holding nothing but OS litter still has
    files in it, and deciding that .DS_Store is worthless is a different
    judgement than deciding a folder is. Removed outright rather than
    trashed: an empty directory has no content to recover.
    """
    with _lock:
        root = _resolve_dir(rel) if rel else ta.archive_root()
        removed = []
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            path = Path(dirpath)
            if path == root:
                continue
            try:
                if not any(path.iterdir()):
                    path.rmdir()
                    removed.append(ta.rel_of(path))
            except OSError:
                continue
        for gone in removed:
            ta.index_remove(gone)
        if removed:
            log_op("prune_empty", src=rel, by=by, removed=len(removed),
                   paths=removed[:50])
        return {"action": "prune_empty", "removed": removed,
                "count": len(removed)}
