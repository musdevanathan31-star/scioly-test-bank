# Tournament archive — plan

A ~65GB collection of previous tournament tests, uploaded by scp, to be
triaged into the question bank through a coach-only interface.

On disk:

```
<DATA_ROOT>/tournament_archive/<Division>/<Event>/<Year>/<Tournament>/<files…>
```

`_UnknownDivision` and `_UnknownEvent` mark what hasn't been identified yet.
`<Tournament>` is frequently gibberish. Depth is a *convention*, not a
guarantee — real uploads will violate it, and the code must survive that.

## Requirements

1. Rename a `<Tournament>` node.
2. Move files and folders to the correct location.
3. Create missing `<Year>` / `<Event>` / `<Tournament>` nodes.
4. Delete empty folders and junk files.
5. Import PDFs into an event's bank from the archive, instead of uploading
   through the web client.
6. Volunteers see only the archive content for events they already have
   access to.

Four more the implementation needs regardless:

7. **An index.** 65GB is likely tens of thousands of files; walking the tree
   per request is not viable. Browse one level at a time from a cached index.
8. **An event-folder → slug mapping.** Requirement 6 is impossible without
   it: access control is per-slug, and an archive folder name is not one.
9. **Preview before every mutation**, matching `deletion.py`. A mis-scoped
   move across 65GB has no undo button.
10. **Serialisation.** Bulk moves are slow and `jobs.py` runs one job at a
    time, so organising competes with extraction.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Import | **Move** into `<DATA_ROOT>/<slug>/` | No duplication; the existing pipeline works unchanged; the archive shrinks as it is triaged |
| Restic backup | **Excluded** | A second copy lives in Google Drive. Exposure covers only un-triaged files, and shrinks to nothing as they are imported |
| `migrate-data-root.sh` | **Not** excluded | On a host move, a local rsync beats re-downloading 65GB from Drive |
| Mapping file | Backed up via the **git** mechanism | Curation work, expensive to redo, tiny. `backup-bulk-data.sh` iterates `*/` — directories only — so a top-level JSON file would otherwise be backed up by neither mechanism |
| Volunteer scope | Mapped subtrees only | Unmapped and `_UnknownEvent` are coach-only; triage is a coach job |
| Destructive ops | Coach-only, own gate | Organising inherently means deleting junk; requiring `ALLOW_HARD_DELETE` would mean leaving user/season/event deletion live throughout. Deletes go to `<DATA_ROOT>/.deleted/`, all actions logged |

## Phases

**Phase 1 — index, duplicate detection, and read-only browse.** `tournament_archive.py` builds an
index at `<DATA_ROOT>/.archive_index.json` (dot-prefixed: derived data, stays
out of backups), refreshed incrementally by directory mtime and rebuilt as a
job. Coach-only `/archive` page browsing one level at a time. Read-only —
nothing can move or delete a file yet, which is the point: it proves the
index survives 65GB before anything risky is built on it.

The rebuild reports named steps rather than a single mutating status line,
and is cancellable — both because a first walk over 65GB has no known
duration. It runs on its own thread rather than `jobs.py`'s queue: that
queue is keyed by event slug, so the archive would need a pseudo-slug that
creates a bogus event directory, and it runs strictly one job at a time, so
a long reindex would block all question extraction. A reindex is idempotent
and safe to run alongside anything else, which is the opposite of what that
queue exists to enforce.

Duplicate detection runs as part of the same build, identifying copies by
**content** since filenames in this corpus are meaningless — the same test
appears as `test.pdf`, `CircuitLab2019.pdf` and `scan_0001.pdf`. Hashing
65GB outright would be punishing, so it narrows in three stages: group by
size (free, already statted); hash the first 64KB of anything sharing a
size; hash in full only within a group that still agrees. Measured on a
synthetic 172MB corpus with a realistic duplicate rate, this read **8.5%**
of the bytes.

It finds *byte-identical* files only. The same test scanned twice, or
downloaded from two sources with different PDF metadata, will not be paired
— that needs text or perceptual comparison, a different job with a
different error rate, and is not planned.

**Phase 2 — mapping and volunteer scoping.** `archive_event_map.json` maps
`<Division>/<Event folder>` to an app slug, assigned by a coach with
suggestions from normalised name matching against `events.EVENTS`. Volunteer
access derives from it. Pure metadata; still no file moves.

**Phase 3 — mutations.** Rename, move, create, delete, each previewing the
real counts ("moves 412 files, 3.1GB") before applying. Bulk moves run as
jobs so they report progress. Path containment validated server-side on
every request, the way `deletion.py` does it.

**Phase 4 — import into an event.** From a `<Tournament>` node, pick PDFs,
choose a role, and they move into the event directory under the naming
convention, reusing `api_scan_rename`'s logic. The archive path already
encodes year, division and tournament, so that metadata prefills instead of
being typed — the main advantage over the web upload it replaces.

## Known risks

- **Non-ASCII filenames are already present.** One existing PDF is
  `circuitlab_2019_c_ssss-utf-8u+6211u+662f_test.pdf` — mojibake from an
  earlier import. A corpus this size will have more, and
  `secure_filename()` strips non-ASCII entirely, so two distinct names can
  collapse into one. Import must check for collisions rather than trust the
  sanitised name.
- **Index build time** on 65GB is unknown until measured. Phase 1 exists
  partly to find out.
- **The pending server migration** now has 65GB more to rsync. Sequence the
  migration before the upload, or budget for it.
- **Depth assumptions.** Files at the wrong depth, or directories where
  files are expected, must render rather than raise — the whole point of the
  tool is that the tree is currently wrong.
