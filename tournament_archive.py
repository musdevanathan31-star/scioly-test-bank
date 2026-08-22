"""The tournament archive: a large, untidy corpus waiting to be triaged.

Uploaded by scp to `<DATA_ROOT>/tournament_archive/`, nominally shaped

    <Division>/<Event>/<Year>/<Tournament>/<files…>

with `_UnknownDivision` / `_UnknownEvent` for what hasn't been identified.
See TODO_archive.md for the decisions behind this and the phase order.

**Depth is a convention, not a guarantee.** The whole reason this tool
exists is that the tree is currently wrong: files sit at the wrong level,
tournament names are gibberish, folders are empty. Every function here
treats the layout as a hint for labelling and never as something to rely
on — a file where a directory is expected must render, not raise.

**Why an index rather than walking on demand.** The corpus is tens of GB
across likely tens of thousands of files. Statting that per request would
make browsing unusable, so one walk builds a cached index and browsing
serves a single level from it. The index lives at
`<DATA_ROOT>/.archive_index.json`, dot-prefixed so both backup-bulk-data.sh
and migrate-data-root.sh skip it with their bare `*/` globs — it is derived
data, rebuildable at any time, and has no business in a snapshot.

The archive itself is deliberately excluded from the nightly restic backup
(see backup-bulk-data.sh): a second copy lives in Google Drive, and
importing a file MOVES it into `<DATA_ROOT>/<event>/`, which *is* backed up
— so the unprotected set shrinks as the archive is triaged.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from events import DATA_ROOT

#: Visible, not dot-prefixed: it is an scp target a human types, and its
#: exclusion from backups is stated explicitly in backup-bulk-data.sh
#: rather than implied by a leading dot.
ARCHIVE_DIRNAME = "tournament_archive"

#: Reserved so an event can never be registered with a colliding slug and
#: shadow the archive directory. events.py checks this.
RESERVED_SLUGS = frozenset({ARCHIVE_DIRNAME})

INDEX_FILE = DATA_ROOT / ".archive_index.json"
INDEX_SCHEMA_VERSION = 2   # bumped when duplicate detection was added

#: What each level of nesting means, for labelling only. Anything deeper is
#: "file" territory; anything shallower than its name suggests is still
#: rendered, just labelled by position.
LEVEL_NAMES = ("division", "event", "year", "tournament")

#: Skipped wholesale when indexing: OS litter that would otherwise dominate
#: the file counts and give a misleading picture of what is actually here.
IGNORED_NAMES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100",
    ".Trashes", "__MACOSX",
})

_index_lock = threading.Lock()


def archive_root() -> Path:
    return DATA_ROOT / ARCHIVE_DIRNAME


def exists() -> bool:
    return archive_root().is_dir()


def level_name(depth: int) -> str:
    """Label for a node `depth` levels below the archive root."""
    if 0 <= depth < len(LEVEL_NAMES):
        return LEVEL_NAMES[depth]
    return "folder"


def safe_path(rel: str) -> Path:
    """Resolve a client-supplied relative path inside the archive.

    Every path in this module arrives from a URL, so containment is the
    whole security story — the same reasoning as deletion.py's _contained.
    Verified after resolving, because a bare ".." survives naive string
    checks. An empty string means the root itself.
    """
    root = archive_root().resolve()
    candidate = (root / (rel or "")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes the archive: {rel!r}")
    return candidate


def rel_of(path: Path) -> str:
    """Archive-relative POSIX path, the form used in the index and URLs."""
    try:
        rel = path.resolve().relative_to(archive_root().resolve()).as_posix()
    except ValueError:
        return ""
    # relative_to() of a path against itself yields Path("."), whose
    # as_posix() is "." — not "". Left alone that shifts every depth by one
    # and makes a parent's child keys ("./X") disagree with the child's own
    # key ("X"), so no totals aggregate.
    return "" if rel == "." else rel


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class DirEntry:
    """One directory's summary. Files are counted and measured but not
    listed individually at index time — a tournament folder can hold
    hundreds, and the browse endpoint reads those from disk for the one
    directory being looked at rather than carrying every filename in a
    single JSON blob."""
    rel: str
    depth: int
    n_files: int = 0            # directly inside
    n_subdirs: int = 0
    total_files: int = 0        # including everything below
    total_bytes: int = 0
    mtime: float = 0.0
    subdirs: list = field(default_factory=list)   # names only


#: Named build steps, in order. The UI renders every one of them up front
#: so a long rebuild shows what is done, what is running and what has not
#: started — a single mutating status line cannot say that.
STEP_WALK = "walk"
STEP_COMPARE = "compare"
STEP_HASH = "hash"
STEP_SAVE = "save"

BUILD_STEPS = [
    (STEP_WALK, "Scan folders"),
    (STEP_COMPARE, "Compare files by size"),
    (STEP_HASH, "Hash duplicate candidates"),
    (STEP_SAVE, "Save index"),
]


def build_index(progress=None, should_cancel=None, find_dupes: bool = True) -> dict:
    """Walk the archive once and summarise every directory.

    Bottom-up, so a directory's totals can be assembled from children
    already computed rather than re-walking its subtree — the difference
    between linear and quadratic on a corpus this size.

    `progress(step_id, count, total)` reports which named step is running
    and how far it has got; `should_cancel` lets the caller interrupt it.
    Steps are named rather than free text so the UI can show all of them at
    once with the finished ones ticked, instead of one label mutating.
    """
    root = archive_root()
    if not root.is_dir():
        return {"schema": INDEX_SCHEMA_VERSION, "built_at": time.time(),
                "root_exists": False, "dirs": {}, "duplicates": []}

    dirs: dict[str, DirEntry] = {}
    by_size: dict[int, list] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("index build cancelled")
        dirnames[:] = [d for d in dirnames if d not in IGNORED_NAMES]
        filenames = [f for f in filenames if f not in IGNORED_NAMES]

        here = Path(dirpath)
        rel = rel_of(here)
        depth = 0 if not rel else len(rel.split("/"))

        n_bytes = 0
        for name in filenames:
            try:
                size = (here / name).stat().st_size
            except OSError:
                # A file that vanished mid-walk, or one we cannot stat, is
                # worth skipping rather than aborting the whole build.
                continue
            n_bytes += size
            # Sizes are free here and are stage 1 of duplicate detection —
            # collecting them now avoids a second full walk later.
            by_size.setdefault(size, []).append(
                f"{rel}/{name}" if rel else name)

        entry = DirEntry(rel=rel, depth=depth, n_files=len(filenames),
                         n_subdirs=len(dirnames), total_files=len(filenames),
                         total_bytes=n_bytes,
                         subdirs=sorted(dirnames, key=str.lower))
        try:
            entry.mtime = here.stat().st_mtime
        except OSError:
            pass
        # topdown=False guarantees children are already in `dirs`.
        for child in dirnames:
            child_rel = f"{rel}/{child}" if rel else child
            sub = dirs.get(child_rel)
            if sub is not None:
                entry.total_files += sub.total_files
                entry.total_bytes += sub.total_bytes
        dirs[rel] = entry

        seen += 1
        if progress is not None and seen % 250 == 0:
            progress(STEP_WALK, seen, None)

    if progress is not None:
        progress(STEP_WALK, seen, seen)

    duplicates = []
    if find_dupes:
        # Cheap and worth reporting on its own: it turns the vague "this
        # will hash things" into a number the coach can judge the wait by.
        n_candidates = sum(len(paths) for size, paths in by_size.items()
                           if size > 0 and len(paths) > 1)
        if progress is not None:
            progress(STEP_COMPARE, len(by_size), len(by_size))
            progress(STEP_HASH, 0, n_candidates)
        duplicates = find_duplicates(by_size, progress=(
            (lambda n: progress(STEP_HASH, n, n_candidates))
            if progress is not None else None), should_cancel=should_cancel)
        if progress is not None:
            progress(STEP_HASH, n_candidates, n_candidates)

    return {
        "schema": INDEX_SCHEMA_VERSION,
        "built_at": time.time(),
        "root_exists": True,
        "dirs": {rel: vars(e) for rel, e in dirs.items()},
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------------------
# Incremental index maintenance
#
# A rename touches one subtree. Re-walking 65GB to learn that would make
# every mutation cost a full rebuild, which is the difference between a tool
# a coach uses and one they avoid. So the index is patched in place.
#
# These are deliberately narrow: they maintain exactly the fields the browse
# view reads (keys, subdirs lists, and the totals that aggregate up the
# ancestor chain). Anything more subtle -- duplicate groups in particular --
# is left to go stale rather than half-maintained, and `stale_duplicates`
# says so, because a wrong duplicate group proposes deleting a file that is
# not a copy of anything.
# ---------------------------------------------------------------------------

def _ancestors(rel: str):
    """Every ancestor key of `rel`, nearest first, ending at the root ("")."""
    parts = [p for p in rel.split("/") if p]
    for i in range(len(parts) - 1, 0, -1):
        yield "/".join(parts[:i])
    yield ""


def _parent_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _adjust_totals(dirs: dict, start_rel: str, d_files: int, d_bytes: int) -> None:
    for anc in _ancestors(start_rel):
        entry = dirs.get(anc)
        if entry is None:
            continue
        entry["total_files"] = max(0, entry.get("total_files", 0) + d_files)
        entry["total_bytes"] = max(0, entry.get("total_bytes", 0) + d_bytes)


def _subtree_keys(dirs: dict, rel: str) -> list:
    prefix = rel + "/"
    return [k for k in dirs if k == rel or k.startswith(prefix)]


def index_move(old_rel: str, new_rel: str) -> None:
    """Re-key a subtree after a rename or move, and fix both ancestor chains.

    A rename inside one parent nets to zero on totals; a move between parents
    subtracts from the old chain and adds to the new. Doing both
    unconditionally handles either without a special case.
    """
    index = load_index()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    entry = dirs.get(old_rel)
    if entry is None:
        return
    files, size = entry.get("total_files", 0), entry.get("total_bytes", 0)

    moved = {}
    for key in _subtree_keys(dirs, old_rel):
        sub = dirs.pop(key)
        suffix = key[len(old_rel):]
        new_key = new_rel + suffix
        sub["rel"] = new_key
        sub["depth"] = len([p for p in new_key.split("/") if p])
        moved[new_key] = sub
    dirs.update(moved)

    old_parent, new_parent = _parent_of(old_rel), _parent_of(new_rel)
    old_name = old_rel.rsplit("/", 1)[-1]
    new_name = new_rel.rsplit("/", 1)[-1]
    src_parent = dirs.get(old_parent)
    if src_parent is not None and old_name in (src_parent.get("subdirs") or []):
        src_parent["subdirs"] = [n for n in src_parent["subdirs"] if n != old_name]
        src_parent["n_subdirs"] = len(src_parent["subdirs"])
    dst_parent = dirs.get(new_parent)
    if dst_parent is not None and new_name not in (dst_parent.get("subdirs") or []):
        dst_parent["subdirs"] = sorted(
            (dst_parent.get("subdirs") or []) + [new_name], key=str.lower)
        dst_parent["n_subdirs"] = len(dst_parent["subdirs"])

    _adjust_totals(dirs, old_rel, -files, -size)
    _adjust_totals(dirs, new_rel, files, size)
    index["stale_duplicates"] = True
    save_index(index)


def index_remove(rel: str) -> None:
    """Drop a subtree and subtract its totals from every ancestor."""
    index = load_index()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    entry = dirs.get(rel)
    if entry is None:
        return
    files, size = entry.get("total_files", 0), entry.get("total_bytes", 0)
    for key in _subtree_keys(dirs, rel):
        dirs.pop(key, None)
    parent = dirs.get(_parent_of(rel))
    name = rel.rsplit("/", 1)[-1]
    if parent is not None:
        parent["subdirs"] = [n for n in (parent.get("subdirs") or []) if n != name]
        parent["n_subdirs"] = len(parent["subdirs"])
    _adjust_totals(dirs, rel, -files, -size)
    index["stale_duplicates"] = True
    save_index(index)


def index_remove_file(rel: str, size: int) -> None:
    """Drop one file: its own directory's counts, and every ancestor's totals."""
    index = load_index()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    parent = dirs.get(_parent_of(rel))
    if parent is not None:
        parent["n_files"] = max(0, parent.get("n_files", 0) - 1)
    _adjust_totals(dirs, rel, -1, -size)
    index["stale_duplicates"] = True
    save_index(index)


def index_add_dir(rel: str) -> None:
    """Register a newly created (necessarily empty) folder."""
    index = load_index()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    if rel in dirs:
        return
    dirs[rel] = vars(DirEntry(
        rel=rel, depth=len([p for p in rel.split("/") if p])))
    parent = dirs.get(_parent_of(rel))
    name = rel.rsplit("/", 1)[-1]
    if parent is not None and name not in (parent.get("subdirs") or []):
        parent["subdirs"] = sorted(
            (parent.get("subdirs") or []) + [name], key=str.lower)
        parent["n_subdirs"] = len(parent["subdirs"])
    save_index(index)


# ---------------------------------------------------------------------------
# Duplicate detection
#
# Filenames are meaningless here — the same test turns up as "test.pdf",
# "CircuitLab2019.pdf" and "scan_0001.pdf" — so duplicates are identified by
# CONTENT.
#
# Hashing 65GB outright would take a very long time, and almost none of it
# needs reading: a file whose size is unique in the corpus cannot have a
# byte-identical twin. So this narrows in three stages, each cheaper than
# the one it feeds:
#
#   1. Group by exact size. Free — already statted during the walk. Any
#      group of one is finished, and that is the overwhelming majority.
#   2. Hash the first 64KB of each remaining candidate. Two files sharing a
#      size but differing at all usually differ early, so this eliminates
#      most survivors for one short read each.
#   3. Hash in full, but only within a group that still agrees. This is the
#      only stage that reads whole files, and by now it is reading almost
#      exclusively actual duplicates.
#
# Net effect: bytes read is roughly "the duplicated content plus 64KB per
# size collision", not "the whole archive".
#
# **What this does NOT find**: the same test scanned twice, or downloaded
# from two sources with different PDF metadata. Those are not byte-identical
# and no amount of hashing will pair them. Catching those needs text or
# perceptual comparison, which is a different job with a different error
# rate — see TODO_archive.md.
# ---------------------------------------------------------------------------

#: Enough to separate files that share a size but differ, without paying for
#: a full read. PDFs differ in their header/xref area far more often than not.
_PARTIAL_BYTES = 64 * 1024
_HASH_CHUNK = 1024 * 1024


def _hash_file(path: Path, limit: int | None = None) -> str | None:
    """SHA-256 of a file, or of its first `limit` bytes. None if unreadable —
    an unreadable file is skipped rather than aborting the whole scan."""
    h = hashlib.sha256()
    remaining = limit
    try:
        with open(path, "rb") as fh:
            while True:
                want = _HASH_CHUNK if remaining is None else min(_HASH_CHUNK, remaining)
                if want <= 0:
                    break
                chunk = fh.read(want)
                if not chunk:
                    break
                h.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(by_size: dict, progress=None, should_cancel=None) -> list:
    """Groups of byte-identical files, largest wasted space first.

    `by_size` maps size -> [relative paths]. Returns a list of
    {"size", "hash", "paths", "wasted"} where `wasted` is the space that
    would be reclaimed by keeping exactly one copy.
    """
    groups: list = []
    hashed = 0

    # Zero-byte files all share a size and would otherwise form one enormous
    # bogus "duplicate" group. They are junk to be deleted on their own
    # merits (requirement 4), not duplicates of each other.
    candidates = [(size, paths) for size, paths in by_size.items()
                  if size > 0 and len(paths) > 1]
    # Biggest first: if a scan is interrupted, the groups worth the most
    # space are the ones already found.
    candidates.sort(key=lambda kv: kv[0] * len(kv[1]), reverse=True)

    for size, paths in candidates:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("duplicate scan cancelled")

        # Stage 2: cheap prefix hash.
        by_prefix: dict = {}
        for rel in paths:
            digest = _hash_file(archive_root() / rel, limit=_PARTIAL_BYTES)
            hashed += 1
            if progress is not None and hashed % 200 == 0:
                progress(hashed)
            if digest is not None:
                by_prefix.setdefault(digest, []).append(rel)

        for prefix_paths in by_prefix.values():
            if len(prefix_paths) < 2:
                continue
            # A file smaller than the prefix window was already read whole,
            # so its prefix hash IS its full hash — no second pass needed.
            if size <= _PARTIAL_BYTES:
                full_groups = {"": prefix_paths}
            else:
                full_groups = {}
                for rel in prefix_paths:
                    digest = _hash_file(archive_root() / rel)
                    hashed += 1
                    if progress is not None and hashed % 50 == 0:
                        progress(hashed)
                    if digest is not None:
                        full_groups.setdefault(digest, []).append(rel)
            for digest, same in full_groups.items():
                if len(same) < 2:
                    continue
                groups.append({
                    "size": size,
                    "hash": digest[:16],
                    "paths": sorted(same),
                    "wasted": size * (len(same) - 1),
                })

    groups.sort(key=lambda g: g["wasted"], reverse=True)
    return groups


def duplicate_summary(index: dict | None = None) -> dict:
    idx = index if index is not None else load_index()
    groups = (idx or {}).get("duplicates") or []
    return {
        "groups": len(groups),
        "files": sum(len(g["paths"]) for g in groups),
        "reclaimable_bytes": sum(g["wasted"] for g in groups),
    }


def duplicate_groups(limit: int = 100, offset: int = 0) -> dict:
    idx = load_index() or {}
    groups = idx.get("duplicates") or []
    # The summary is nested, not spread. Spreading it put its own "groups"
    # key -- an integer count -- on top of the list of groups, so the client
    # received a number where it expected an array.
    return {
        "total": len(groups),
        "offset": offset,
        "groups": groups[offset:offset + limit],
        "summary": duplicate_summary(idx),
    }


def _duplicate_lookup(index: dict | None = None) -> dict:
    """rel path -> group hash, so a listing can mark which files are copies
    without carrying the whole duplicate table into every browse call."""
    idx = index if index is not None else load_index()
    out = {}
    for g in (idx or {}).get("duplicates") or []:
        for rel in g["paths"]:
            out[rel] = g["hash"]
    return out


def save_index(index: dict) -> None:
    with _index_lock:
        tmp = INDEX_FILE.with_suffix(INDEX_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        os.replace(tmp, INDEX_FILE)


def load_index() -> dict | None:
    """The cached index, or None when it is absent or from an older schema.

    A schema mismatch returns None rather than attempting a migration:
    this is derived data that rebuilds from the filesystem in one pass, so
    a rebuild is always cheaper and safer than a conversion.
    """
    with _index_lock:
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    if data.get("schema") != INDEX_SCHEMA_VERSION:
        return None
    return data


def index_age_seconds() -> float | None:
    idx = load_index()
    if not idx:
        return None
    return max(0.0, time.time() - float(idx.get("built_at") or 0))


def summary() -> dict:
    """Top-level numbers for the archive page header."""
    idx = load_index()
    if not idx:
        return {"indexed": False, "root_exists": exists()}
    root = idx["dirs"].get("", {})
    return {
        "indexed": True,
        "root_exists": bool(idx.get("root_exists")),
        "built_at": idx.get("built_at"),
        "age_seconds": index_age_seconds(),
        "n_dirs": len(idx["dirs"]),
        "total_files": root.get("total_files", 0),
        "total_bytes": root.get("total_bytes", 0),
        "duplicates": duplicate_summary(idx),
    }


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------

def list_dir(rel: str) -> dict:
    """One level: subdirectories (from the index, with their totals) and
    files (read from disk, since the index deliberately doesn't carry
    filenames).

    Subdirectory names are always read from disk so the view reflects
    reality; the index only supplies each one's recursive totals, and a
    folder it has not seen yet is marked `indexed: false` rather than
    reported as empty.
    """
    path = safe_path(rel)
    if not path.is_dir():
        raise FileNotFoundError(rel)

    idx = load_index() or {"dirs": {}}
    dup_lookup = _duplicate_lookup(idx)
    entry = idx["dirs"].get(rel_of(path))
    depth = 0 if not rel_of(path) else len(rel_of(path).split("/"))

    # Names always come from disk, never from the index: one iterdir() on
    # the directory being viewed is cheap, and taking names from the index
    # meant a folder created since the last build was invisible — which is
    # precisely the "upload, then browse" case this page exists for. The
    # index supplies the recursive totals, which are the part that actually
    # needs one.
    subdir_names = sorted(
        (p.name for p in path.iterdir()
         if p.is_dir() and p.name not in IGNORED_NAMES), key=str.lower)

    subdirs = []
    for name in subdir_names:
        child_rel = f"{rel_of(path)}/{name}" if rel_of(path) else name
        child = idx["dirs"].get(child_rel) or {}
        subdirs.append({
            "name": name,
            "rel": child_rel,
            "level": level_name(depth),
            "total_files": child.get("total_files"),
            "total_bytes": child.get("total_bytes"),
            "n_subdirs": child.get("n_subdirs"),
            "indexed": bool(child),
        })

    files = []
    try:
        for p in sorted(path.iterdir(), key=lambda x: x.name.lower()):
            if p.name in IGNORED_NAMES or not p.is_file():
                continue
            try:
                st = p.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = None, None
            frel = rel_of(p)
            files.append({"name": p.name, "rel": frel, "size": size,
                          "mtime": mtime, "ext": p.suffix.lower(),
                          # Which copies exist is answered by the duplicates
                          # view; here it is only worth knowing THAT a file
                          # has one, so the listing stays small.
                          "dup": dup_lookup.get(frel)})
    except OSError as e:
        raise FileNotFoundError(str(e))

    return {
        "rel": rel_of(path),
        "depth": depth,
        "level": level_name(depth - 1) if depth else "root",
        "child_level": level_name(depth),
        "subdirs": subdirs,
        "files": files,
        "stale": entry is None,
    }


def breadcrumbs(rel: str) -> list:
    """Ancestors of `rel`, root first, for the browse header."""
    out = [{"name": ARCHIVE_DIRNAME, "rel": ""}]
    parts = [p for p in (rel or "").split("/") if p]
    for i, part in enumerate(parts):
        out.append({"name": part, "rel": "/".join(parts[:i + 1])})
    return out


# ---------------------------------------------------------------------------
# Background rebuild
#
# Deliberately not on jobs.py's queue. That queue is keyed by event slug and
# writes its history into <DATA_ROOT>/<slug>/.qbank_jobs/ — the archive is
# not an event, and borrowing a pseudo-slug would create a bogus event
# directory and put archive jobs in the way of extraction work. It is also
# the wrong queue semantically: a reindex is idempotent and safe to run
# alongside anything else, whereas that queue exists to serialise work that
# is not.
# ---------------------------------------------------------------------------

def _fresh_steps() -> list:
    return [{"id": sid, "label": label, "status": "pending",
             "count": 0, "total": None} for sid, label in BUILD_STEPS]


_build_state: dict = {"running": False, "started_at": None,
                      "finished_at": None, "error": None,
                      "cancelled": False, "steps": _fresh_steps()}
_build_lock = threading.Lock()
_cancel_flag = threading.Event()


def _snapshot() -> dict:
    """Caller must hold _build_lock. Steps are copied because the worker
    mutates them in place and the JSON encoder runs outside the lock."""
    state = dict(_build_state)
    state["steps"] = [dict(st) for st in _build_state["steps"]]
    return state


def build_status() -> dict:
    with _build_lock:
        return _snapshot()


def cancel_build() -> dict:
    """Ask a running build to stop at its next checkpoint.

    Cooperative rather than forced: the walk and the hasher both check the
    flag, so cancelling costs at most one directory or one file. The
    existing index is left exactly as it was -- a half-written index is
    worse than a stale one, because nothing downstream can tell it is
    partial.
    """
    _cancel_flag.set()
    return build_status()


def start_build() -> dict:
    """Kick off a rebuild unless one is already going. Returns the state.

    Re-entrant by design: the page polls this and a second click while a
    build runs should join the existing one rather than start a rival walk
    over tens of thousands of directories.
    """
    with _build_lock:
        if _build_state["running"]:
            return _snapshot()
        _cancel_flag.clear()
        _build_state.update(running=True, started_at=time.time(),
                            finished_at=None, error=None, cancelled=False,
                            steps=_fresh_steps())

    def _mark(step_id, status, count=None, total=None):
        """Set one step's status, and close out any earlier step still shown
        as running -- progress only ever moves forward, so reaching step N
        is proof that N-1 finished."""
        with _build_lock:
            reached = False
            for st in _build_state["steps"]:
                if st["id"] == step_id:
                    reached = True
                    st["status"] = status
                    if count is not None:
                        st["count"] = count
                    if total is not None:
                        st["total"] = total
                elif not reached and st["status"] == "running":
                    st["status"] = "done"

    def progress(step_id, count, total=None):
        _mark(step_id, "running", count, total)

    def _run():
        try:
            index = build_index(progress=progress,
                                should_cancel=_cancel_flag.is_set)
            _mark(STEP_SAVE, "running")
            save_index(index)
            _mark(STEP_SAVE, "done")
        except InterruptedError:
            with _build_lock:
                _build_state["cancelled"] = True
                for st in _build_state["steps"]:
                    if st["status"] in ("running", "pending"):
                        st["status"] = "cancelled"
        except Exception as e:                      # noqa: BLE001
            with _build_lock:
                _build_state["error"] = str(e)[:300]
                for st in _build_state["steps"]:
                    if st["status"] == "running":
                        st["status"] = "failed"
                    elif st["status"] == "pending":
                        st["status"] = "skipped"
        finally:
            with _build_lock:
                _build_state["running"] = False
                _build_state["finished_at"] = time.time()

    threading.Thread(target=_run, name="archive-index", daemon=True).start()
    return build_status()      # outside the lock, so this one is safe
