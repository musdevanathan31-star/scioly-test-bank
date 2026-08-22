"""
Phase 4 of the tournament archive: moving PDFs into an event's bank.

This replaces uploading through the web client, and its advantage is that
the archive path already encodes the metadata the upload form asks for:
`<Division>/<Event>/<Year>/<Tournament>/` gives year, division and submitter
without anyone retyping them. So the import prefills, and a coach corrects
rather than composes.

Files are **moved**, not copied. There is no duplication, the existing
pipeline picks them up with no further code involved, and the archive
shrinks as it is triaged — which is the whole point of the exercise.

The subtle part is collisions. Test and key are tied together by sharing an
exact filename stem (`_key_path()` and `_supplementary_docs()` both key off
it), so disambiguating a name cannot be done per file: bump the test and
leave the key alone and the pair silently stops being a pair. Names are
therefore resolved for a whole batch at once, against a single suffix that
clears every role in it.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from werkzeug.utils import secure_filename

import fitz

import events as events_mod
import tournament_archive as ta
import archive_ops

ROLES = ("test", "key", "supplementary", "notes")

DOC_EXTS = (".pdf", ".docx", ".doc", ".md", ".txt")
#: Scanned figures and diagrams filed next to a test. They cannot be a test
#: or a key (there is nothing to extract), but they belong with one, so they
#: import as supplementary and inherit the test's filename stem.
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff")

_YEAR_RE = re.compile(r"(19|20)\d{2}")
_DIVISION_RE = re.compile(r"\b(?:div(?:ision)?\s*)?([abc])\b", re.I)


class ImportError_(archive_ops.ArchiveOpError):
    """Refused — the caller asked for an import this module will not do."""


# ---------------------------------------------------------------------------
# What the path already tells us
# ---------------------------------------------------------------------------

def path_metadata(rel: str) -> dict:
    """Year, division and submitter inferred from an archive folder path.

    Best-effort by design. The convention is
    `<Division>/<Event>/<Year>/<Tournament>`, but the tree violates it
    freely, so every field falls back to the same placeholder the manual
    onboarding path uses rather than refusing to proceed.
    """
    parts = [p for p in (rel or "").split("/") if p]
    division = year = submitter = ""
    if parts:
        m = _DIVISION_RE.search(parts[0])
        if m and not parts[0].startswith("_"):
            division = m.group(1).lower()
    for part in parts:
        m = _YEAR_RE.search(part)
        if m:
            year = m.group(0)
            break
    if len(parts) >= 4:
        submitter = parts[3]
    elif len(parts) >= 3 and not _YEAR_RE.search(parts[-1]):
        # A tree missing its <Year> level still names the tournament last.
        submitter = parts[-1]
    return {"division": division or "x",
            "year": year or "unk",
            "submitter": _slugify(submitter) or "unknown"}


def _slugify(text: str) -> str:
    """Fold a folder name into something usable inside a filename.

    secure_filename() strips non-ASCII entirely, which on this corpus can
    collapse two distinct names to nothing at all — so an empty result is
    reported as empty and the caller substitutes a placeholder, rather than
    silently producing a name that collides with every other stripped one.
    """
    cleaned = secure_filename((text or "").strip().lower())
    cleaned = re.sub(r"[^a-z0-9]+", "", cleaned)
    return cleaned[:40]


def image_to_pdf(image: Path, dest: Path) -> None:
    """Wrap an image in a single-page PDF sized to the image.

    Supplementary material is discovered by globbing `<stem>_*.pdf` and
    opened with fitz, so a bare .png attached to a test is invisible to the
    viewer and would crash it if it were not. Converting on the way in means
    no viewer, route or template has to learn about image attachments — the
    same reasoning behind the .docx conversion on the manual scan path.

    The original is kept beside it. Nothing in this feature destroys a file,
    and the glob only picks up PDFs, so the image sits there inertly.
    """
    src = fitz.open(str(image))
    pdf_bytes = src.convert_to_pdf()
    out = fitz.open("pdf", pdf_bytes)
    out.save(str(dest))


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTS


def guess_role(filename: str) -> str:
    """test / key / supplementary from a filename, defaulting to test.

    Only a default for the picker — the coach chooses. Answer keys are the
    one thing worth catching automatically, because importing a key as a
    test puts the answers into the question bank as questions.
    """
    if is_image(filename):
        return "supplementary"
    stem = Path(filename).stem.lower()
    if re.search(r"\b(key|answer|answers|solutions?|soln)\b", stem) or \
            re.search(r"(key|answers|solutions)$", stem):
        return "key"
    if re.search(r"\b(rules?|notes?|reference|cheat|sheet)\b", stem):
        return "notes"
    return "test"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _event_or_raise(slug: str):
    ev = events_mod.EVENTS.get(slug)
    if ev is None or ev.archived:
        raise ImportError_(f"no such event: {slug}")
    return ev


def _target_name(prefix, year, division, submitter, role, ext) -> str:
    return f"{prefix}_{year}_{division}_{submitter}_{role}{ext}"


def plan_import(items: list, slug: str, meta: dict | None = None) -> dict:
    """Work out every destination name before moving anything.

    `items` is a list of `{"path": archive-relative, "role": ...}`. The whole
    batch shares one submitter suffix so that a test and its key stay a pair;
    see the module docstring.
    """
    ev = _event_or_raise(slug)
    if not items:
        raise ImportError_("nothing selected")

    resolved = []
    for item in items:
        rel = (item.get("path") or "").strip()
        role = (item.get("role") or "").strip().lower()
        if role not in ROLES:
            raise ImportError_(f"role must be one of {', '.join(ROLES)}")
        path = ta.safe_path(rel)
        if not path.is_file():
            raise ImportError_(f"no such file: {rel}")
        ext = path.suffix.lower()
        if ext not in DOC_EXTS and ext not in IMAGE_EXTS:
            raise ImportError_(
                f"{path.name}: only PDFs, documents and images import here")
        if role in ("test", "key") and ext not in (".pdf", ".docx", ".doc"):
            raise ImportError_(f"{path.name}: a {role} must be a PDF or Word document")
        if ext in IMAGE_EXTS and role != "supplementary":
            # An image cannot be a test or key -- there is nothing to extract
            # questions from -- but scanned figures and diagrams sitting next
            # to a test are worth keeping with it.
            raise ImportError_(
                f"{path.name}: an image can only be imported as supplementary")
        resolved.append({"rel": rel, "path": path, "role": role,
                         "name": path.name, "bytes": path.stat().st_size})

    base = dict(path_metadata(resolved[0]["rel"].rsplit("/", 1)[0]))
    base.update({k: v for k, v in (meta or {}).items() if v})
    year = _slugify(base["year"]) or "unk"
    division = _slugify(base["division"])[:1] or "x"
    submitter = _slugify(base["submitter"]) or "unknown"

    base_dir = ev.base_dir
    texts_dir = ev.texts_dir
    roles_needing_stem = {r["role"] for r in resolved
                          if r["role"] in ("test", "key", "supplementary")}

    # One suffix for the batch. Bumping per file would break the stem link
    # between a test and its key, which is the only thing tying them together.
    suffix, chosen = 0, submitter
    while True:
        chosen = submitter if suffix == 0 else f"{submitter}{suffix + 1}"
        clash = any(
            (base_dir / _target_name(ev.filename_prefix, year, division,
                                     chosen, role, ".pdf")).exists()
            or (base_dir / _target_name(ev.filename_prefix, year, division,
                                        chosen, role, ".docx")).exists()
            for role in roles_needing_stem)
        if not clash:
            break
        suffix += 1
        if suffix > 50:
            raise ImportError_("too many files already imported under that name")

    stem_prefix = f"{ev.filename_prefix}_{year}_{division}_{chosen}"
    plans, seen = [], set()
    for r in resolved:
        ext = r["path"].suffix.lower()
        if r["role"] == "notes":
            # Source *material*, not a document tied to one test — the same
            # category as anything uploaded to <event>/texts/.
            dest_name = secure_filename(r["name"]) or f"notes{ext}"
            dest = texts_dir / dest_name
            n = 1
            while dest.exists() or dest.name in seen:
                dest = texts_dir / f"{Path(dest_name).stem}_{n}{Path(dest_name).suffix}"
                n += 1
            dest_name = dest.name
            where = "texts"
        elif r["role"] == "supplementary":
            label = _slugify(Path(r["name"]).stem) or "sheet"
            dest_name = f"{stem_prefix}_{label}{ext}"
            where = "event"
            if ext in IMAGE_EXTS:
                # The image moves as-is and gains a PDF sibling that the
                # existing supplementary machinery can actually open.
                plans_extra = f"{stem_prefix}_{label}.pdf"
                if plans_extra in seen:
                    raise ImportError_(
                        f"two selected images would both become {plans_extra}")
                seen.add(plans_extra)
        else:
            dest_name = _target_name(ev.filename_prefix, year, division,
                                     chosen, r["role"], ext)
            where = "event"
        if dest_name in seen:
            raise ImportError_(
                f"two selected files would both become {dest_name} — "
                "import them separately or give one a different role")
        seen.add(dest_name)
        plans.append({"src": r["rel"], "name": r["name"], "role": r["role"],
                      "dest_name": dest_name, "where": where,
                      "as_pdf": (f"{stem_prefix}_{_slugify(Path(r['name']).stem) or 'sheet'}.pdf"
                                 if (r["role"] == "supplementary"
                                     and ext in IMAGE_EXTS) else None),
                      "bytes": r["bytes"]})

    return {
        "slug": slug, "event": ev.name,
        "year": year, "division": division, "submitter": chosen,
        "renamed_for_collision": suffix > 0,
        "files": plans,
        "total_bytes": sum(p["bytes"] for p in plans),
    }


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def run_import(items: list, slug: str, meta: dict | None = None,
               by: str = "") -> dict:
    """Move the planned files. Re-plans internally rather than trusting the
    client's preview: the tree can change between the two calls."""
    with archive_ops._lock:
        plan = plan_import(items, slug, meta)
        ev = _event_or_raise(slug)
        ev.base_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        for entry in plan["files"]:
            src = ta.safe_path(entry["src"])
            dest_dir = ev.texts_dir if entry["where"] == "texts" else ev.base_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / entry["dest_name"]
            if dest.exists():
                # Planned against a tree that has since changed. Stop rather
                # than overwrite: the file is still safely in the archive.
                raise ImportError_(f"{entry['dest_name']} appeared while importing")
            shutil.move(str(src), str(dest))
            if entry.get("as_pdf"):
                try:
                    image_to_pdf(dest, dest.parent / entry["as_pdf"])
                except Exception as e:              # noqa: BLE001
                    # The move already happened and the image is safely in
                    # the event directory; failing to wrap it is a degraded
                    # result, not a reason to abort the batch.
                    entry["warning"] = f"could not build a PDF view: {e}"
            ta.index_remove_file(entry["src"], entry["bytes"])
            moved.append({**entry, "dest": str(dest)})
            archive_ops.log_op("import", src=entry["src"],
                               dest=f"{slug}/{entry['dest_name']}",
                               role=entry["role"], by=by, bytes=entry["bytes"])
        plan["moved"] = moved
        return plan


# ---------------------------------------------------------------------------
# Bulk import from a subtree
#
# Importing 122 files a folder at a time is not a workflow anyone finishes.
# This flattens a whole mapped event subtree into one list grouped by year
# and tournament, guesses each file's role, and hides what is already in the
# event.
#
# "Already imported" has to be judged by CONTENT. Import moves files, so
# anything imported has left the archive -- but this corpus is full of
# duplicates, so the same test usually sits in several tournament folders,
# and the remaining copies would otherwise look like fresh material every
# time. Matching on size then hash means a second copy of something already
# imported is recognised however it happens to be named.
# ---------------------------------------------------------------------------

def _event_files_by_size(ev) -> dict:
    """size -> [paths] for everything already in the event. No hashing.

    Sizes are free (one stat each) and rule out almost everything: an
    archive file whose size matches nothing in the event cannot be a copy of
    anything in it. Hashing happens later, lazily, only for the sizes that
    actually collide.
    """
    out: dict = {}
    for folder in (ev.base_dir, ev.texts_dir):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            try:
                if p.is_file():
                    out.setdefault(p.stat().st_size, []).append(p)
            except OSError:
                continue
    return out


def _already_checker(ev):
    """Returns is_already(path, size) -> bool, hashing as little as possible.

    The event side of a colliding size is hashed once and cached; the
    archive file is hashed only when its size collides at all.
    """
    by_size = _event_files_by_size(ev)
    cache: dict = {}

    def hashes_for(size: int) -> set:
        if size not in cache:
            digests = set()
            for p in by_size.get(size, ()):
                d = ta._hash_file(p)
                if d:
                    digests.add(d)
            cache[size] = digests
        return cache[size]

    def is_already(path, size: int) -> bool:
        if size not in by_size:
            return False
        digest = ta._hash_file(path)
        return bool(digest and digest in hashes_for(size))

    return is_already


def subtree_files(rel: str, slug: str, include_imported: bool = False) -> dict:
    """Every importable file under `rel`, grouped by year and tournament.

    Groups are keyed by the path between the subtree root and the file, which
    is `<Year>/<Tournament>` when the tree follows the convention and
    whatever it actually is when it does not -- this has to render a corpus
    that violates the convention freely, so it groups by real structure
    rather than assuming a depth.
    """
    ev = _event_or_raise(slug)
    root = ta.safe_path(rel)
    if not root.is_dir():
        raise ImportError_(f"no such folder: {rel or '/'}")

    is_already = _already_checker(ev)
    groups: dict = {}
    n_total = n_hidden = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ta.IGNORED_NAMES]
        here = Path(dirpath)
        for name in sorted(filenames, key=str.lower):
            if name in ta.IGNORED_NAMES:
                continue
            ext = Path(name).suffix.lower()
            if ext not in DOC_EXTS and ext not in IMAGE_EXTS:
                continue
            path = here / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            n_total += 1

            already = is_already(path, size)
            if already and not include_imported:
                n_hidden += 1
                continue

            frel = ta.rel_of(path)
            group_key = here.relative_to(root).as_posix()
            if group_key == ".":
                group_key = ""
            meta = path_metadata(frel.rsplit("/", 1)[0])
            g = groups.setdefault(group_key, {
                "key": group_key,
                "label": group_key or "(loose files)",
                "year": meta["year"],
                "tournament": meta["submitter"],
                "files": [],
            })
            g["files"].append({
                "path": frel, "name": name, "size": size,
                "role": guess_role(name), "already": already,
            })

    ordered = sorted(groups.values(), key=lambda g: g["key"].lower())
    return {
        "slug": slug, "event": ev.name, "root": rel,
        "groups": ordered,
        "total": n_total,
        "shown": sum(len(g["files"]) for g in ordered),
        "already_in_event": n_hidden,
    }


def run_batch_import(items: list, slug: str, by: str = "") -> dict:
    """Import files spanning several tournament folders.

    Runs one plan per source folder rather than one for the batch. Year,
    division and tournament come from the path, and the filename stem that
    ties a test to its key is derived from them -- so a single plan across
    folders would file everything under whichever folder happened to be
    first, and silently pair a 2019 key with a 2021 test.
    """
    by_folder: dict = {}
    for item in items:
        rel = (item.get("path") or "").strip()
        if not rel:
            raise ImportError_("an item is missing its path")
        by_folder.setdefault(rel.rsplit("/", 1)[0], []).append(item)

    imported, failures = [], []
    for folder in sorted(by_folder):
        try:
            plan = run_import(by_folder[folder], slug, by=by)
            imported.extend(plan["files"])
        except (ImportError_, OSError, ValueError) as e:
            # One bad folder must not cost the rest of a 122-file sweep.
            failures.append({"folder": folder, "error": str(e)})
    return {"imported": imported, "failed": failures,
            "count": len(imported), "folders": len(by_folder)}
