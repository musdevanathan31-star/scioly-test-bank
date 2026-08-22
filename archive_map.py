"""
Phase 2 of the tournament archive: which archive folder belongs to which event.

Access control in this app is per event *slug*. An archive folder name is
not a slug — it is whatever a volunteer typed into Google Drive years ago,
so "Circuit Lab", "circuit-lab", "CircuitLab" and "Circuts Lab" all name the
same event. Requirement 6 (volunteers see only their own events' archive
content) is impossible without a deliberate mapping, and guessing from
folder names at request time would leak the rest of the corpus on a near
miss.

So the mapping is *stored*, not inferred. Normalised name matching only
suggests; a coach confirms. Anything unmapped stays coach-only, which makes
the failure mode "a volunteer sees too little" rather than "too much".

The file lives at the DATA_ROOT root and is backed up by the git mechanism
rather than restic: it is expensive curation work but tiny, and
`backup-bulk-data.sh` iterates `*/` — directories only — so a top-level JSON
file would otherwise be caught by neither.
"""
from __future__ import annotations

import json
import re
import threading

import events as events_mod
import tournament_archive as ta

MAP_FILE = "archive_event_map.json"
MAP_SCHEMA_VERSION = 1

#: Written rarely, read on every archive request.
_lock = threading.RLock()


def map_path():
    return events_mod.DATA_ROOT / MAP_FILE


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load_map() -> dict:
    """`{"<Division>/<Event folder>": "<slug>"}`. Empty when absent.

    A missing or corrupt file is not an error: it means nothing is mapped
    yet, and the archive is coach-only until it is. Failing the request
    instead would take the whole page down over derived curation data.
    """
    path = map_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != MAP_SCHEMA_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_map(entries: dict) -> None:
    path = map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"schema": MAP_SCHEMA_VERSION, "entries": entries}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
    tmp.replace(path)


def set_mapping(folder_key: str, slug: str | None) -> dict:
    """Map one `<Division>/<Event folder>` to a slug, or unmap it with None.

    Validated against live events rather than trusted: a slug that no longer
    exists would silently grant nobody access, which is a confusing way to
    fail. Unmapping is explicit so a coach can revoke a wrong assignment
    without hand-editing the file.
    """
    with _lock:
        entries = load_map()
        if slug is None or slug == "":
            entries.pop(folder_key, None)
        else:
            if slug not in events_mod.EVENTS:
                raise ValueError(f"no such event: {slug}")
            entries[folder_key] = slug
        save_map(entries)
        return entries


def set_many(pairs: dict) -> dict:
    """Apply a whole screenful at once.

    All-or-nothing on validation: a coach who mistypes one slug in a batch
    of forty should get told which one, not discover later that thirty-nine
    saved and one vanished.
    """
    bad = [slug for slug in pairs.values()
           if slug and slug not in events_mod.EVENTS]
    if bad:
        raise ValueError(f"no such event: {', '.join(sorted(set(bad)))}")
    with _lock:
        entries = load_map()
        for key, slug in pairs.items():
            if slug:
                entries[key] = slug
            else:
                entries.pop(key, None)
        save_map(entries)
        return entries


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

_NOISE = re.compile(r"[^a-z0-9]+")


def normalise(name: str) -> str:
    """Fold a folder name to something comparable with an event name.

    Deliberately aggressive: this only produces *suggestions*, which a coach
    reviews. A false suggestion costs one correction; a missed one costs
    typing the whole thing.
    """
    text = (name or "").lower()
    text = text.replace("&", " and ")
    text = _NOISE.sub(" ", text).strip()
    # Division suffixes are structure here, not part of the event name --
    # the division is already a level up in the tree.
    text = re.sub(r"\b(div(ision)?\s*[abc]|[abc])\b$", "", text).strip()
    return re.sub(r"\s+", " ", text)


def _candidate_names(ev) -> list:
    names = [ev.name, ev.slug, *(ev.event_match or ())]
    return [normalise(n) for n in names if n]


def suggest_slug(folder_name: str) -> str | None:
    """Best-guess slug for one event folder, or None.

    Exact normalised match first, then containment either way — "anatomy"
    should find "Anatomy & Physiology", and "Circuit Lab (B)" should find
    "Circuit Lab". Anything vaguer is left blank rather than guessed: a
    wrong mapping grants access to the wrong subtree, so the bar is high.
    """
    target = normalise(folder_name)
    if not target:
        return None
    for slug, ev in events_mod.EVENTS.items():
        if ev.archived:
            continue
        if target in _candidate_names(ev):
            return slug
    best = None
    for slug, ev in events_mod.EVENTS.items():
        if ev.archived:
            continue
        for cand in _candidate_names(ev):
            if not cand:
                continue
            if target.startswith(cand) or cand.startswith(target):
                # Prefer the longest agreement, so "anatomy" does not win
                # over "anatomy and physiology" for a folder named the latter.
                if best is None or len(cand) > best[1]:
                    best = (slug, len(cand))
    return best[0] if best else None


def event_folders() -> list:
    """Every `<Division>/<Event folder>` in the archive, with its mapping.

    Read from the index rather than the disk: this is a whole-tree question
    and the index exists precisely so those do not re-walk 65GB. Depth 2 is
    the convention; a tree that violates it simply contributes fewer rows,
    which is honest — an unmapped folder is coach-only anyway.
    """
    index = ta.load_index() or {}
    dirs = index.get("dirs") or {}
    entries = load_map()
    rows = []
    for rel, entry in dirs.items():
        if not rel or entry.get("depth") != 2:
            continue
        division, _, folder = rel.partition("/")
        mapped = entries.get(rel)
        rows.append({
            "key": rel,
            "division": division,
            "folder": folder,
            # Normalised here, not in the browser: the folding rules are a
            # server concern and the client should not re-derive them.
            "name_key": normalise(folder),
            "slug": mapped,
            "suggestion": None if mapped else suggest_slug(folder),
            "total_files": entry.get("total_files", 0),
            "total_bytes": entry.get("total_bytes", 0),
        })
    rows.sort(key=lambda r: (r["division"].lower(), r["folder"].lower()))
    return rows


def folders_by_name() -> dict:
    """Event-folder name -> the keys using it, for the "same slug for every
    division" shortcut. The name is normalised so "Circuit Lab" and
    "circuit-lab" are offered together."""
    out: dict = {}
    for row in event_folders():
        out.setdefault(normalise(row["folder"]), []).append(row["key"])
    return out


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def slug_for_path(rel: str) -> str | None:
    """The event slug governing an archive path, or None if unmapped.

    Mapping is at `<Division>/<Event>`; everything below inherits it. The
    root and division levels are above any mapping and so belong to nobody.
    """
    parts = [p for p in (rel or "").split("/") if p]
    if len(parts) < 2:
        return None
    return load_map().get(f"{parts[0]}/{parts[1]}")


def can_access(user, rel: str) -> bool:
    """Whether `user` may see this archive path.

    Coaches see everything, including the unmapped and `_UnknownEvent`
    backlog — triage is a coach job. A volunteer sees a path only when it
    resolves to an event they already hold. Unmapped means no.
    """
    if getattr(user, "role", None) == "coach":
        return True
    slug = slug_for_path(rel)
    return bool(slug) and slug in (getattr(user, "events", None) or ())


def can_traverse(user, rel: str) -> bool:
    """Whether `user` may *open* this folder, as opposed to see its files.

    Distinct from can_access() on purpose. A volunteer's own subtree sits
    below a division that belongs to nobody, so requiring content access to
    list a folder would make their own events unreachable. Traversal is
    granted when some accessible key lies at or below this path; the listing
    is then filtered, and files directly inside a folder they cannot access
    are dropped separately.
    """
    if getattr(user, "role", None) == "coach":
        return True
    if not rel:
        return True
    if can_access(user, rel):
        return True
    prefix = rel.rstrip("/") + "/"
    return any(key == rel or key.startswith(prefix)
               for key in accessible_keys(user))


def visible_children(user, rel: str, names: list) -> list:
    """Filter one listing's subdirectory names for `user`.

    Applied at the parent, not the child: a volunteer standing at the root
    or at a division must see only the branches leading to their own events,
    otherwise the folder names alone disclose the shape of the corpus.
    """
    if getattr(user, "role", None) == "coach":
        return names
    allowed = accessible_keys(user)
    out = []
    for name in names:
        child = f"{rel}/{name}" if rel else name
        depth = len([p for p in child.split("/") if p])
        if depth == 1:
            if any(key.startswith(f"{name}/") for key in allowed):
                out.append(name)
        elif depth == 2:
            if child in allowed:
                out.append(name)
        else:
            if can_access(user, child):
                out.append(name)
    return out


def accessible_keys(user) -> set:
    """The `<Division>/<Event>` keys a volunteer holds."""
    if getattr(user, "role", None) == "coach":
        return {key for key in load_map()}
    holds = set(getattr(user, "events", None) or ())
    return {key for key, slug in load_map().items() if slug in holds}
