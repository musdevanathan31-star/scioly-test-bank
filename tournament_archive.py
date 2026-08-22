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
import re
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
INDEX_SCHEMA_VERSION = 3   # bumped when duplicate detection was added

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


def _prune_duplicates_many(index: dict, gone: set) -> None:
    """Drop a whole batch of deleted paths from the duplicate groups in one
    pass. Doing it per file meant re-walking every group per deletion."""
    if not gone:
        return
    kept = []
    for g in index.get("duplicates") or []:
        paths = [p for p in g["paths"] if p not in gone]
        if len(paths) < 2:
            continue
        g["paths"] = paths
        g["wasted"] = g["size"] * (len(paths) - 1)
        kept.append(g)
    kept.sort(key=lambda g: g["wasted"], reverse=True)
    index["duplicates"] = kept


def index_remove_files(items: list) -> None:
    """Remove many files from the index with a single load and save.

    `items` is a list of `(rel, size)`. The per-file version re-parsed and
    re-serialised the whole index for every file, which turned a bulk
    duplicate sweep into one full index rewrite per deletion -- measured at
    14ms a file, so ~26s for 1800 files.
    """
    if not items:
        return
    index = _load_for_write()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    gone = set()
    for rel, size in items:
        gone.add(rel)
        parent = dirs.get(_parent_of(rel))
        if parent is not None:
            parent["n_files"] = max(0, parent.get("n_files", 0) - 1)
        _adjust_totals(dirs, rel, -1, -size)
    _prune_duplicates_many(index, gone)
    index["stale_duplicates"] = True
    save_index(index)


def _prune_duplicates(index: dict, gone: str, is_dir: bool) -> None:
    """Drop deleted paths from the duplicate groups.

    Exact, not a recompute: removing a path that no longer exists cannot
    invent a grouping. A group left with fewer than two copies is no longer
    a duplicate set and goes entirely. Without this the panel keeps offering
    files that are already in the trash, and acting on them reports failures.
    """
    groups = index.get("duplicates") or []
    prefix = gone.rstrip("/") + "/"
    kept = []
    for g in groups:
        paths = [p for p in g["paths"]
                 if not (p == gone or (is_dir and p.startswith(prefix)))]
        if len(paths) < 2:
            continue
        g["paths"] = paths
        g["wasted"] = g["size"] * (len(paths) - 1)
        kept.append(g)
    kept.sort(key=lambda g: g["wasted"], reverse=True)
    index["duplicates"] = kept


def _rekey_duplicates(index: dict, old_rel: str, new_rel: str) -> None:
    """Follow a rename or move, so groups keep pointing at real files."""
    prefix = old_rel.rstrip("/") + "/"
    for g in index.get("duplicates") or []:
        g["paths"] = [
            new_rel + p[len(old_rel):] if (p == old_rel or p.startswith(prefix)) else p
            for p in g["paths"]]


def index_move(old_rel: str, new_rel: str) -> None:
    """Re-key a subtree after a rename or move, and fix both ancestor chains.

    A rename inside one parent nets to zero on totals; a move between parents
    subtracts from the old chain and adds to the new. Doing both
    unconditionally handles either without a special case.
    """
    index = _load_for_write()
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
    _rekey_duplicates(index, old_rel, new_rel)
    index["stale_duplicates"] = True
    save_index(index)


def index_remove(rel: str) -> None:
    """Drop a subtree and subtract its totals from every ancestor."""
    index = _load_for_write()
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
    _prune_duplicates(index, rel, is_dir=True)
    index["stale_duplicates"] = True
    save_index(index)


def index_remove_file(rel: str, size: int) -> None:
    """Drop one file: its own directory's counts, and every ancestor's totals."""
    index = _load_for_write()
    if index is None:
        return
    dirs = index.get("dirs") or {}
    parent = dirs.get(_parent_of(rel))
    if parent is not None:
        parent["n_files"] = max(0, parent.get("n_files", 0) - 1)
    _adjust_totals(dirs, rel, -1, -size)
    _prune_duplicates(index, rel, is_dir=False)
    index["stale_duplicates"] = True
    save_index(index)


def index_add_dir(rel: str) -> None:
    """Register a newly created (necessarily empty) folder."""
    index = _load_for_write()
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

        for prefix_digest, prefix_paths in by_prefix.items():
            if len(prefix_paths) < 2:
                continue
            # A file smaller than the prefix window was already read whole,
            # so its prefix hash IS its full hash — no second pass needed.
            # Keep that digest: discarding it gave every small-file group an
            # empty id, and selecting one of them then matched all of them.
            if size <= _PARTIAL_BYTES:
                full_groups = {prefix_digest: prefix_paths}
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
                    # Size-qualified so two groups can never collide on a
                    # truncated digest. This is what the client selects by.
                    "id": f"{size}-{digest[:16]}",
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


# ---------------------------------------------------------------------------
# Bulk duplicate removal
#
# Choosing which copy to keep is the whole problem. Deleting the wrong one
# is not destructive here (everything goes to the trash) but it is
# *degrading*: keeping the copy under `_UnknownEvent/xz9##/` and deleting the
# one under `Division B/Circuit Lab/2019/UF Invitational/` throws away the
# only thing that said what the file was. The bytes are identical; the paths
# are not, and the path is the metadata.
#
# So the keeper is the best-identified copy, and the ranking says so
# explicitly rather than falling out of sort order.
# ---------------------------------------------------------------------------

_UNKNOWN_MARKERS = ("_unknowndivision", "_unknownevent", "_unknown", "_unsorted",
                    "_misc", "_inbox", "_new", "_todo")

#: A folder name that carries no information about the tournament: a stripped
#: Drive URL, a hash, "copy of ...", "untitled", and similar.
_NOISE_NAME = re.compile(
    r"^(untitled|new folder|copy|copy of .*|folder\d*|\d+|[0-9a-f]{8,}|"
    r"[a-z0-9_-]{20,})$", re.I)


def _keeper_rank(rel: str) -> tuple:
    """Sort key: lower is a better copy to keep.

    Ordered by how much the path tells you about the file, then by
    shallowness (a file parked at the top is less filed than one sitting in
    its tournament folder), then lexicographically so the choice is stable
    and reproducible rather than dependent on walk order.
    """
    parts = [p for p in rel.split("/") if p]
    folders = [p.lower() for p in parts[:-1]]
    unknown = sum(1 for f in folders
                  if any(f.startswith(m) for m in _UNKNOWN_MARKERS))
    noisy = sum(1 for f in folders if _NOISE_NAME.match(f))
    # A copy at the conventional depth (Division/Event/Year/Tournament/file)
    # is properly filed; anything shallower is loose.
    depth_penalty = abs(len(parts) - 5)
    return (unknown, noisy, depth_penalty, len(rel), rel)


def choose_keeper(paths: list) -> str:
    return sorted(paths, key=_keeper_rank)[0]


def plan_dedupe(groups: list) -> dict:
    """For each group, which copy survives and which go.

    Never returns a group with nothing kept: the invariant this whole
    feature rests on is that removing duplicates removes *copies*, never the
    last instance of a file's contents.
    """
    plans = []
    for g in groups:
        paths = list(g.get("paths") or [])
        if len(paths) < 2:
            continue
        keep = choose_keeper(paths)
        remove = [p for p in paths if p != keep]
        plans.append({"hash": g.get("hash"), "size": g.get("size", 0),
                      "keep": keep, "remove": remove,
                      "reclaimed": g.get("size", 0) * len(remove)})
    return {"groups": plans,
            "files": sum(len(p["remove"]) for p in plans),
            "reclaimed_bytes": sum(p["reclaimed"] for p in plans)}


def groups_under(rel: str = "", limit: int | None = None,
                 offset: int = 0) -> list:
    """Duplicate groups with at least two copies inside `rel`.

    Copies *outside* the folder are excluded from the group rather than the
    group being dropped: cleaning up "this folder" should not reach out and
    delete a file somewhere the coach is not looking. A group left with one
    local copy has nothing to remove and disappears.
    """
    index = load_index() or {}
    all_groups = index.get("duplicates") or []
    if not rel:
        # A copy: sorting below would otherwise reorder the cached index's
        # own list in place.
        scoped = list(all_groups)
    else:
        prefix = rel.rstrip("/") + "/"
        scoped = []
        for g in all_groups:
            local = [p for p in g["paths"] if p.startswith(prefix)]
            if len(local) > 1:
                scoped.append({**g, "paths": local,
                               "wasted": g["size"] * (len(local) - 1)})
    scoped.sort(key=lambda g: g["wasted"], reverse=True)
    if limit is None:
        return scoped
    return scoped[offset:offset + limit]


def groups_by_hash(ids: list) -> list:
    """Look groups up by the id the client was shown.

    The client sends back ids rather than paths so the server re-derives what
    to delete from its own index. A client that sent paths could ask for the
    deletion of every copy of something.
    """
    wanted = {i for i in (ids or []) if i}
    index = load_index() or {}
    return [g for g in (index.get("duplicates") or [])
            if g.get("id") and g["id"] in wanted]


def _duplicate_lookup(index: dict | None = None) -> dict:
    """rel path -> group hash, so a listing can mark which files are copies
    without carrying the whole duplicate table into every browse call.

    Memoised beside the parsed index: rebuilding it per listing is cheap
    next to parsing, but it is derived from exactly the same data and
    invalidates on exactly the same key.
    """
    if index is None:
        with _index_lock:
            cached = _index_cache["dup_lookup"]
            if cached is not None:
                return cached
        idx = load_index()
        out = _build_dup_lookup(idx)
        with _index_lock:
            _index_cache["dup_lookup"] = out
        return out
    return _build_dup_lookup(index)


def _build_dup_lookup(idx: dict | None) -> dict:
    out = {}
    for g in (idx or {}).get("duplicates") or []:
        for rel in g["paths"]:
            out[rel] = g["hash"]
    return out


# ---------------------------------------------------------------------------
# In-process cache
#
# Every browse click, and every poll of /api/archive/status, used to re-read
# and re-parse the whole index from disk. json.loads holds the GIL and the
# app runs --workers 1 --threads 8, so that parse is not merely this
# request's cost -- it stalls every other thread for its duration. Measured
# at roughly 17ms per megabyte of index.
#
# One process means one cache with no coherence problem. The key is the
# file's identity (mtime_ns and size), so a rebuild or a patched index
# re-parses on the next read without anyone having to remember to invalidate.
# ---------------------------------------------------------------------------

_index_cache: dict = {"key": None, "data": None, "dup_lookup": None}


def _index_key():
    try:
        st = INDEX_FILE.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def save_index(index: dict) -> None:
    with _index_lock:
        tmp = INDEX_FILE.with_suffix(INDEX_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        os.replace(tmp, INDEX_FILE)
        # Seed the cache from what we already hold rather than making the
        # next reader parse back what this writer just serialised -- but
        # only when it would survive a read. Seeding unconditionally let a
        # wrong-schema index be served straight back out of memory, skipping
        # the check that exists to discard it.
        if index.get("schema") == INDEX_SCHEMA_VERSION:
            _index_cache.update(key=_index_key(), data=index, dup_lookup=None)
        else:
            _index_cache.update(key=None, data=None, dup_lookup=None)


def load_index() -> dict | None:
    """The index, or None when it is absent or from an older schema.

    **Read-only.** The returned dict is shared with every other caller, so
    anything that intends to modify it must use `_load_for_write()`.

    A schema mismatch returns None rather than attempting a migration:
    this is derived data that rebuilds from the filesystem in one pass, so
    a rebuild is always cheaper and safer than a conversion.
    """
    with _index_lock:
        key = _index_key()
        if key is not None and key == _index_cache["key"]:
            return _index_cache["data"]
        data = _parse_index()
        _index_cache.update(key=key, data=data, dup_lookup=None)
        return data


def _parse_index() -> dict | None:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("schema") != INDEX_SCHEMA_VERSION:
        return None
    return data


def _load_for_write() -> dict | None:
    """A private copy for mutators, so patching it cannot corrupt the copy
    other threads are reading from. They call save_index() when done, which
    re-seeds the cache."""
    with _index_lock:
        return _parse_index()


def index_age_seconds(index: dict | None = None) -> float | None:
    idx = index if index is not None else load_index()
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
        "age_seconds": index_age_seconds(idx),
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
    # rel_of() resolves the path, so compute it once instead of per use.
    here_rel = rel_of(path)
    entry = idx["dirs"].get(here_rel)
    depth = 0 if not here_rel else len(here_rel.split("/"))

    # Names always come from disk, never from the index: one iterdir() on
    # the directory being viewed is cheap, and taking names from the index
    # meant a folder created since the last build was invisible — which is
    # precisely the "upload, then browse" case this page exists for. The
    # index supplies the recursive totals, which are the part that actually
    # needs one.
    # One scandir pass, not two iterdir passes plus a stat per entry.
    # os.DirEntry caches its type and stat from the directory read itself,
    # so this is a single syscall's worth of work where the previous version
    # paid one per file -- which measured as the dominant cost of a listing,
    # ahead of parsing the index.
    dir_entries, file_entries = [], []
    try:
        with os.scandir(path) as it:
            for de in it:
                if de.name in IGNORED_NAMES:
                    continue
                try:
                    if de.is_dir():
                        dir_entries.append(de)
                    elif de.is_file():
                        file_entries.append(de)
                except OSError:
                    continue
    except OSError as e:
        raise FileNotFoundError(str(e))

    subdir_names = sorted((de.name for de in dir_entries), key=str.lower)

    subdirs = []
    for name in subdir_names:
        child_rel = f"{here_rel}/{name}" if here_rel else name
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
    here = here_rel
    for de in sorted(file_entries, key=lambda d: d.name.lower()):
        try:
            st = de.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = None, None
        # Built by string join rather than rel_of(): the parent is already
        # known, and resolving every file's path against the archive root
        # was a syscall per entry for an answer we already had.
        frel = f"{here}/{de.name}" if here else de.name
        files.append({"name": de.name, "rel": frel, "size": size,
                      "mtime": mtime, "ext": Path(de.name).suffix.lower(),
                      # Which copies exist is answered by the duplicates
                      # view; here it is only worth knowing THAT a file
                      # has one, so the listing stays small.
                      "dup": dup_lookup.get(frel)})

    return {
        "rel": here_rel,
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
