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

**Phase 2 — mapping and volunteer scoping.** *Done.* `archive_event_map.json`
maps `<Division>/<Event folder>` to an app slug, assigned by a coach at
`/archive/map` with suggestions from normalised name matching against
`events.EVENTS`. Suggestions never auto-apply — a wrong mapping grants access
to the wrong subtree, so a coach confirms. "Apply to all N divisions" handles
the common case; keys stay independent so an inconsistently-named folder can
still be corrected alone.

Volunteer access derives from it, and **traversal is distinct from access**:
a volunteer's subtree sits below a division that belongs to nobody, so
requiring content access to *list* a folder would make their own events
unreachable. They may pass through a division only when something below it
is theirs, and a parent listing names only the branches they can enter —
folder names alone would otherwise disclose the shape of the corpus. Barred
paths return 404, not 403, for the same reason. Unmapped and `_UnknownEvent`
stay coach-only.

Import is open to volunteers (they already have this power via web upload);
mapping, indexing and duplicate review stay coach-only. Pure metadata; still
no file moves.

**Phase 3 — mutations.** *Done.* Rename, move, create, delete and prune-empty,
each previewing the real counts ("moves 412 files, 3.1GB") from the index
before applying — the numbers are already there, so acting on a guess is
never necessary. Path containment validated server-side on every request,
the way `deletion.py` does it, on the write routes as well as the read one.

Coach-only, and deliberately **not** behind `ALLOW_HARD_DELETE`: organising
inherently means deleting junk, and gating it on that flag would mean
leaving user/season/event deletion switched on for the whole triage effort.
Deletes go to the shared `<DATA_ROOT>/.deleted/` trash instead, in a folder
named for the archive-relative path — basenames here are meaningless
("test.pdf", a thousand times over), so a coach restoring something needs to
know which one it was.

Every mutation appends to `archive_ops.jsonl` with both paths and the user.
That is an audit trail now and the prerequisite for undo later; neither can
be reconstructed after the fact. Logging never raises — losing an audit line
must not roll back a move that already happened on disk.

**The index is patched, not rebuilt.** Re-walking 65GB after each rename
would make the tool unusable at this size. The patch maintains keys, subdir
lists and the totals up both ancestor chains; tests compare the patched
index against a full rebuild, because silent drift here makes every total on
every page wrong from then on. Duplicate groups are deliberately *not*
maintained — a half-updated group would propose deleting a file that is not
a copy of anything — so `stale_duplicates` marks them for the next rebuild.

**Phase 4 — import into an event.** *Done.* From any folder, tick PDFs,
confirm the event and role, and they **move** into the event directory under
the naming convention. The archive path already encodes year, division and
tournament, so that metadata prefills instead of being typed — the main
advantage over the web upload it replaces — and the Phase 2 mapping usually
supplies the destination event too, making it a confirmation rather than a
choice.

Open to volunteers for their own events, unlike the Phase 3 mutations:
importing is the same power they already have through the web upload, just
sourced differently, and it is the main way 65GB actually gets triaged.
**Both ends are checked** — they must be able to see the source path *and*
hold the destination event.

Collision handling is load-bearing. A test and its key are tied together
only by sharing an exact filename stem, so renaming one without the other
silently breaks the pair, invisibly, until someone opens the event and finds
a test with no key. Names are therefore resolved for a whole batch against a
single suffix that clears every role in it. Two selected files that would
land on the same name are refused with a reason rather than silently
overwritten.

An answer key is guessed from its filename because getting that wrong puts
the answers into the question bank *as questions*; everything else defaults
to `test` and the user chooses.

## Follow-on work (done)

**PDF preview.** Tournament folders are frequently a stripped Drive URL, so
the only way to know what a file is is to look inside it — which makes the
viewer a prerequisite for renaming a folder and for judging which copy of a
duplicate to keep, not a convenience. Rendered through the same two caches
the event viewer uses, under a `.archive` scope (archive files belong to no
event, and the key uses the full path because basenames repeat across the
corpus). Opened via `pdf_safety`: this is the least-trusted content in the
system, and a malformed file is reported so a coach can delete it rather
than failing the request.

**Rename with type-ahead.** Names already used elsewhere in the archive are
offered as you type, ranked by how many folders use each, and an exact match
is confirmed. Standardisation is the point: seeing "UF Invitational" used 40
times steers towards it instead of adding a forty-first spelling.

**Bulk duplicate removal**, per-folder and from the duplicates panel
(tick / select-all-on-page / clear). One copy of each set always survives:
the best-identified one, preferring a real Division/Event/Year/Tournament
path over `_Unknown…` or a gibberish folder name. The bytes are identical,
so the **path is the only metadata left** — deleting the wrong copy is not
destructive (everything is trashed) but it is degrading. The plan shows
which copy survives before anything happens, and selection travels as a
group id rather than paths so a client cannot ask for every copy to go.

**Images import** as supplementary only — there is nothing to extract
questions from — and gain a single-page PDF sibling, because
`_supplementary_docs()` globs `*.pdf` and the viewer fitz-opens what it
finds. Converting on the way in means no viewer, route or template has to
learn about image attachments. The original is kept beside it.

## Known risks

- **Duplicate groups go stale after any mutation.** `stale_duplicates` marks
  them; a rebuild clears it. They are deliberately not patched in place,
  because a half-updated group would propose deleting a file that is not a
  copy of anything.
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
