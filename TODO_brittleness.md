# Codebase Brittleness & Fragility — TODO

A walkthrough of places the current code is more fragile than it should be, grouped by impact. Items are written so you can ask me to take any of them on as a follow-up turn.

Legend: **🔴 high** (real bug waiting to happen), **🟡 medium** (fine for solo localhost dev, breaks under concurrency or growth), **🟢 low** (cleanup / future-proofing).

Everything raised in the original brittleness sweep is **✅ DONE**, including both items that were previously deferred as "optional next sprint" (ContextVar event scope, full Jinja2 file migration). That sweep is preserved below for context/history. A second wave of concurrency issues surfaced later, once the app was carrying real production load from a season-long testing workflow — see **"Second wave" below** for those.

---

## State & concurrency

- **✅ DONE — `set_event()` no longer mutates module globals.** `build_question_bank.py` binds the active `Event` in a `contextvars.ContextVar`; each request/thread gets its own isolated view. A PEP 562 module `__getattr__` keeps `bqb.EVENT`/`BASE_DIR`/etc. working for external callers, and an internal `_ev()` helper replaced every bare in-module reference. The `_event_switch_lock` band-aid that used to serialise requests in `review_app.py` is gone — multi-threaded WSGI (gunicorn, waitress with threads) is now safe. Verified with a direct multi-thread isolation test.

- **✅ DONE — `.qbank_state.json` write locking.** Atomic write via tempfile + `os.replace`, with a per-event `threading.RLock` around the read-modify-write cycle. See `_state_lock()` / `_save_state()` in `build_question_bank.py`.

- **✅ DONE — `apply_annotations()` no longer mutates input.** Defensive copy at function entry.

- **✅ DONE — `_pdf_cache` is now LRU-bounded (max 16) and keyed by `(event_slug, pdf_name)`** so switching events can't return a stale doc handle. `_select_event` evicts on switch implicitly via the composite key.

---

## File I/O & filesystem

- **✅ DONE — Upload size cap (50 MB) via `MAX_CONTENT_LENGTH`** and **`werkzeug.utils.secure_filename`** sanitisation in `/api/sources/upload`.

- **✅ DONE — `.scioly_cookies.json` expiry handling.** `download_event.py` tracks `using_saved_cookies` and deletes the stale cookie file on detected mid-batch expiry, with a clear log line instead of looking like a generic network error.

- **🟢 No file locking on `events_custom.json`.** Two registrations at once could lose one. Unlikely in practice; not worth the complexity.

---

## Parsing & extraction edge cases

- **✅ DONE — `_strip_points` now NFKC-normalises and strips zero-width characters** before regex matching. Lives in `text_utils.py` to avoid the import-cycle risk.

- **✅ DONE — `split_choices` stem-hint vocabulary extended** (`_STEM_QUESTION_HINT`) to catch more legitimate phrasings, with `_MC_OPTION` split into three alternatives (parenthesized / lowercase / uppercase+digit-guard) so the extension doesn't break the unit-suffix guard.

- **✅ DONE — Multi-section dedup suffix now uses bijective base-26** (`_section_suffix(n)`), going `b, c, ..., z, aa, ab, ..., az, ba, ...` instead of capping at `j`.

- **✅ DONE — Cover-page detection** rewritten to require structural confirmation (`short_page`/`form_like` signals) alongside marker strings, instead of trusting "Score:"-style markers alone.

- **✅ DONE — Answer-key parsing** gets a Haiku-vision OCR pass on multi-section key PDFs (env-gated via `VISION_KEY_OCR`) to recover answers that `has_dupes` would otherwise skip entirely.

---

## LLM integration

- **✅ DONE — Anthropic SDK is constructed with `max_retries=4`** so 429/5xx are retried automatically. Additionally, `validate_answer` retries once with a stricter "JSON only" follow-up when the first response isn't parseable.

- **✅ DONE — `_parse_json` has a 5-step repair ladder** (curly-quote normalisation → fences → LaTeX/backslash + unescaped-quote + control-char repair → outermost {…} trim → trailing-comma fix → single-quoted strings). The backslash/quote step (`repair_json_text`) fixes the two most common real-world breaks in hand-typed/LLM-exported question banks:
  - Unescaped LaTeX commands (`\Delta`, `\sqrt`, `\theta`, `\frac{...}`, …) — most are an outright `JSONDecodeError` patched by doubling the backslash at the reported error position in a retry loop; a handful (`\theta`, `\frac`, `\nu`, `\rho`, `\beta`, …) start with a letter that's *also* a valid single-char JSON escape (`t/f/n/r/b`) so they don't raise, they silently corrupt — those are matched by name via `_LATEX_AMBIGUOUS_RE` before the first parse attempt.
  - Unescaped literal quotes inside a string value (units like `5"`, quoted text) that close the string early — the decoder then errors expecting a delimiter instead of more text; patched by escaping the nearest preceding unescaped quote and retrying. Interleaved in the same loop as the backslash fixes since real files have both error types at different points.
  - Validated end-to-end against a real 363KB hand-authored LaTeX-heavy question bank (356 candidates, 246 backslash fixes + 2 quote fixes) that failed to parse at all beforehand — content was never read/inspected, only error positions and aggregate counts.
  - The import-generated-questions endpoint (`/event/<slug>/api/sources/import-generated`) now reads the raw POST body and runs it through this ladder instead of relying on Flask's strict `request.get_json()`, which used to hard-reject anything not already valid JSON before app code ever ran.

- **✅ DONE — Token-cost tracking.** `_track_usage`/`get_usage_stats` tally input/output tokens and estimated cost server-side; `/api/usage` + header cost badge (auto-refreshes every 30s).

- **✅ DONE — Vision cache keys namespaced by call-type** (`assoc_p<N>`, `ocr_p<N>`, `key_ocr_p<N>`) so a future caller can't silently collide with an existing one. The legacy `p<N>` → `assoc_p<N>` migration shim has since been dropped now that all state files are upgraded.

- **✅ DONE — `VISION_MODEL`/`DIAGRAM_MODEL` are env-configurable** (`ANTHROPIC_VISION_MODEL`, `ANTHROPIC_DIAGRAM_MODEL`), no code change needed when Anthropic ships a new model.

---

## Web-UI fragility

- **✅ DONE — Full Jinja2 file migration.** All 6 page templates (`events`, `event_index`, `browse`, `review`, `sources`, `quiz`) now live in `templates/*.html` and render via Flask's `render_template()` with default autoescaping — no more `__PLACEHOLDER__` string substitution. `_render()` and the `_COMMON_*` string-injection loop are gone; `_COMMON_CSS`/`_COMMON_JS` are now exposed as Jinja globals (`common_css`/`common_js`) and included via `{{ ... |safe }}` in each template. The events-landing row markup moved from hand-built escaped HTML strings to a real `{% for r in rows %}` loop. As a side effect this also fixed a real bug: the quiz page was missing from the old common-JS injection loop, so it never got toasts/hotkeys/theme — every template now references `common_js` directly, so that gap can't recur.
  - Default Jinja delimiters (`{{ }}` / `{% %}`) were verified safe against the embedded JS/CSS: a full-text scan found zero literal `{{`/`{%`/`{#` sequences inside the 6 templates (the only literal `{{ }}` in the codebase are genanki Mustache placeholders in the unrelated Anki-export code), so no custom `Environment` delimiter remap was needed.
  - Migration risk that was caught and fixed: extracting the literal Python source bytes preserved Python string-escape sequences (e.g. `\\s` meant for Python's own triple-quoted-string parser to collapse to `\s`) that Jinja, unlike Python, doesn't unescape. Caught via `node --check` on every extracted `<script>` block; fixed by re-decoding each template through Python's own triple-quoted-string parser once, then verified clean.

- **✅ DONE — HTML-in-status-bar (`setStatus`).** `setStatusHtml()`/`escHtml()` added; `window.toast()` defaults to `textContent`, with an explicit `{html:true}` opt-in only where deliberate.

- **✅ DONE — Busy-state `try/finally` audit.** Error reporting verified to not drop silently outside the busy-state reset.

- **🟢 No CSRF.** Fine for localhost; would block production deployment. Not actioned — out of scope until there's an actual multi-user deployment target.

---

## Pipeline & schema

- **✅ DONE — State carries `_schema_version` and forward-migrates on load.** `STATE_SCHEMA_VERSION` + `_migrate_state()` in `build_question_bank.py`.

- **✅ DONE — `process_pair` refuses to run on buckets whose name starts with `_`** (the synthetic-bucket convention is now enforced, not just convention).

- **✅ DONE — Q-number collisions across synthetic buckets.** `_next_global_q_number(state)` guarantees uniqueness across all buckets, not just within one.

- **✅ DONE — `_strip_points` moved to `text_utils.py`,** breaking the import-cycle risk between `build_question_bank`, `scrape_scioly`, `qgen`, `texts`.

---

## Operational

- **✅ DONE — Structured logging.** `logging` configured (`log = logging.getLogger("scioly.bqb")`) in place of bare `print()`.

- **✅ DONE — pytest suite covering the heuristics.** `tests/test_heuristics.py` pins down today's behaviour for `split_choices`, `split_choices_by_lines`, `_strip_points`, `classify_topic`, `_section_suffix`, `qgen.is_duplicate`, and `Event.filename_prefix` derivation. 19 tests, sub-second: `python -m pytest tests/ -q`.

- **✅ DONE — Hardcoded model strings.** Both vision and diagram models are env-configurable (see above).

- **✅ DONE — Playwright headless bypass mode.** `--bypass-bot-headless` CLI flag / `headless` kwarg on `acquire_anubis_cookies`/`download_all` for unattended CI runs.

- **✅ DONE — Graceful shutdown.** `install_graceful_shutdown()` drains in-flight state writes on Ctrl-C so a save mid-flight finishes instead of being abandoned.

- **✅ DONE — Version-pinning of deps.** `requirements.txt` pins minimum versions for PyMuPDF, Flask, Werkzeug, anthropic, playwright, markdownify, beautifulsoup4, requests, pytest.

---

## Second wave — concurrency under real load

The original sweep was written for solo localhost use. Once the season-long testing workflow put many students on the server at once — and once `loadtest_students.py` existed to actually measure that instead of guessing — two more lock-contention bugs surfaced. Both are the same shape as the first-wave items above: a lock held across more work than it needed to, serializing requests that had no reason to wait on each other.

- **✅ DONE — Per-response storage no longer serializes every student on every assessment.** `assessments.py` used to keep every student's response, for every assessment ever administered, in one combined file behind one `threading.RLock`. Every autosave — fired on each MCQ click or matching pick, with no debounce — read, merged into, and rewrote that entire file. A production run of `loadtest_students.py` measured the consequence directly: p50 save latency went from 0.14s at 40 concurrent students to 2.36s at 160, growing super-linearly rather than the roughly-linear growth simple queuing would predict, because the cost of one save scaled with the size of *all* historical response data, not just that student's own. Redesigned to one file per `(assessment_id, username)` pair under `assessment_responses/`, each with its own lock, so concurrent saves from different students — or the same student on different assessments — never contend. An idempotent, startup-time migration backfills any pre-redesign data. Re-measure with `loadtest_students.py` after any further storage change; it is the tool that will tell you if a "small" change reintroduced this class of bug.
- **✅ DONE — Password changes no longer stall every request on the instance.** `auth.py`'s `change_own_password` held the single global user-file lock across both `scrypt` calls (verify old password, hash new one — roughly 80ms each), and every authenticated request takes that same lock to read the user list. A burst of students changing passwords therefore stalled page loads and answer-saves for everyone, not just the students changing passwords — measured at p50 0.76s / p95 1.35s under 8 concurrent password changes. Fixed by hashing both passwords *before* taking the lock, so the lock covers only the swap; `scrypt` releases the GIL, so the hashing itself now overlaps across the thread pool instead of queuing behind the lock. This reopens a narrower race (two concurrent changes could both verify against the same starting hash), closed with a compare-and-swap: the swap re-checks the stored hash is still the one it verified against and refuses rather than silently overwriting if not. After the fix: p50 0.32s / p95 0.33s under the same load, and the answer-save path holds at 0.02s in a mixed run with password changes in flight.

Both were found by measuring, not by reading the code and guessing — worth keeping in mind before declaring a third wave complete on inspection alone.

## Status

Nothing remains open from the original sweep or the second wave above. The two items that were previously deferred to an "optional next sprint" — the ContextVar-based event scope (B#1) and the full Jinja2 template-file migration (B#2) — have both landed, verified via `python -m pytest tests/ -q` and the Playwright smoke test (`python test_webapp.py`) after each change. The test suite has grown substantially since; run `python -m pytest tests/ -q` for the current count rather than trusting a number recorded here.

This file is not re-audited on a schedule — it reflects what's been found and fixed, not a claim that nothing else is fragile. Treat "nothing remains open" as "nothing *known* remains open," and if you're about to add concurrent load to a new code path, consider measuring it with `loadtest_students.py` before assuming the existing locking is fine.

- **✅ DONE — Removed remaining `circuit_lab` hardcoded defaults from the first-test-case era.** The `_current_event` ContextVar in `build_question_bank.py` no longer has an implicit `default=get_event("circuit_lab")` — every entry point (`review_app._select_event`, `build_question_bank.main()`, tests) must call `set_event(slug)` explicitly, and an unset access now raises a clear `RuntimeError` instead of silently resolving to Circuit Lab. The `--event` CLI flag is now `required=True` in both `build_question_bank.py` and `download_event.py` (was `default="circuit_lab"`). Misleading module/CLI docstrings calling the tool "the Circuit Lab question bank/review UI" were genericized. The fully-superseded one-off scripts `download_circuit_lab.py` and `check_qa.py` were deleted (replaced by `download_event.py --event <slug>`). Left untouched, by design: the `circuit_lab` entry in `events.py`'s `EVENTS` registry (a real, permanent built-in event), and the `circuit_lab` references in `tests/test_heuristics.py` / `test_webapp.py` (deliberate, commented test-fixture choices, confirmed with the user to keep as-is).
