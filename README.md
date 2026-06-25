# NCMS Sci-Oly Question Banks

A Science Olympiad test-prep tool. It downloads past test PDFs from scioly.org, extracts questions topic-by-topic, attaches diagrams to the right question, validates answers with an LLM, and produces a browsable markdown question bank per event. A Flask review UI lets you correct anything the pipeline got wrong; corrections persist through re-runs.

Adding another event is a single entry in `events.py` (see [Adding a new event](#adding-a-new-event)).

> **Desktop browsers only.** The web UI is built for desktop-sized viewports (≥ 1200 px). Mobile and tablet layouts are explicitly out of scope; the review page assumes the PDF-pane / question-pane split, drag-to-capture uses a mouse, and the browse page sidebar is always visible. Use Chrome / Edge / Firefox / Safari on a real computer.

**New to this app?** [`HOWTO.md`](HOWTO.md) is a task-oriented guide organized by role (coach/volunteer) — "I want to do X" rather than "how does X work." This README covers setup, configuration, and deployment/operations; `spec.md` covers internals and architecture rationale.

## Contents

- [Configured events](#configured-events)
- [Rotating foci](#rotating-foci)
- [Suggested slugs for other Sci-Oly events](#suggested-slugs-for-other-sci-oly-events)
- [What you get (per event)](#what-you-get-per-event)
- [Quick start](#quick-start)
- [Files](#files)
- [Workflow](#workflow)
- [The cache & annotations](#the-cache--annotations)
- [Cost notes](#cost-notes)
- [Configuration](#configuration)
  - [Separating app code from data (`DATA_ROOT`)](#separating-app-code-from-data-data_root)
- [Authentication & roles](#authentication--roles)
- [Deploying (shared, multi-user instance)](#deploying-shared-multi-user-instance)
- [Production deployment (current state)](#production-deployment-current-state)
  - [Admin app](#admin-app)
- [Maintaining the server](#maintaining-the-server)
  - [Migrating an instance's data to a new `DATA_ROOT`](#migrating-an-instances-data-to-a-new-data_root-one-time-as-needed)
- [Security hardening](#security-hardening)
- [CLI reference](#cli-reference)
- [Adding a new event](#adding-a-new-event)

## Configured events

| `--event <slug>` | Display name | Source-filename prefix | Topic taxonomy |
|---|---|---|---|
| `circuit_lab` | Circuit Lab | `circuitlab_*.pdf` | 11 topics (Ohm's Law, Series & Parallel, Kirchhoff, RLC, semiconductors, …) |
| `thermodynamics` | Thermodynamics | `thermodynamics_*.pdf` | 12 topics (Heat Transfer, Gas Laws, Carnot Engine, Entropy, …) |

These two ship pre-seeded with a full topic/keyword taxonomy out of the box, but they're ordinary events from there on — editable and archivable from the landing page exactly like anything registered through the UI.

The slug is the value to pass to `--event` on every CLI tool. To see what's registered:

```
python -c "from events import EVENTS; [print(s, '|', e.name) for s, e in EVENTS.items()]"
```

## Rotating foci

Several Sci-Oly events rotate sub-topics per year — Anatomy & Physiology cycles through body systems (Endocrine / Nervous / Sense Organs / Cardiovascular / Muscular / Skeletal / Integumentary / Excretory / Immune / Lymphatic), Dynamic Planet alternates (Oceanography / Glaciers / Tectonics / Earthquakes & Volcanoes), Water Quality picks (Freshwater / Saltwater) per year, Materials Science assigns a focus (Nanomaterials / Polymers / Ceramics / Metals).

The pipeline supports this via an optional `foci` field on each event:

- **Register foci** when creating the event: comma-separated values like `"Endocrine, Nervous, Sense Organs"` in the "+ Register a new event" form.
- **scio.ly scrape**: the Sources page shows a **Focus** dropdown when the event has foci. Pick one and the scraper rewrites the scio.ly event name to match their `"<Event> - <Focus>"` naming convention (e.g. `"Anatomy - Endocrine"`) and tags every returned question with the chosen focus.
- **Review UI**: every question card gets a focus dropdown next to the topic dropdown. Edit it like any other field; "+ New focus…" lets you add ones the event config didn't anticipate. Edits go into `field_overrides` and survive reprocess.
- **Markdown output**: if the event has foci declared AND any question has one set, `question_bank.md` groups by focus first (as top-level `# Focus` headings), then by topic within each (as `## Topic` headings). Otherwise it falls back to the flat topic-only layout used for non-rotating events.

Events worth registering with foci:

| Event | Suggested foci |
|---|---|
| Anatomy & Physiology | Endocrine, Nervous, Sense Organs, Cardiovascular, Muscular, Skeletal, Integumentary, Excretory, Immune, Lymphatic, Respiratory, Digestive |
| Dynamic Planet | Oceanography, Glaciers, Tectonics, Earthquakes & Volcanoes |
| Water Quality | Freshwater, Saltwater |
| Materials Science | Nanomaterials, Polymers, Ceramics, Metals |
| Disease Detectives | Population, Environmental, Food |

You don't have to declare every possible focus up-front — register the ones you care about today and add more via the "+ New focus…" option later.

## Suggested slugs for other Sci-Oly events

These aren't shipped yet, but `scioly_tests.json` already contains the metadata. Add a single entry in `events.py` to enable any of them (see [Adding a new event](#adding-a-new-event)).

**Best fits** — large corpora and clean topic axes; the pipeline shines here.

| Suggested slug | Event | PDFs available | Filename prefix to use |
|---|---|---:|---|
| `anatomy_physiology` | Anatomy & Physiology | 61 | `anatomy` ¹ |
| `codebusters` | Codebusters | 47 | `codebusters` |
| `disease_detectives` | Disease Detectives | 45 | `diseasedetectives` |
| `dynamic_planet` | Dynamic Planet | 38 | `dynamicplanet` |
| `chemistry_lab` | Chemistry Lab | 34 | `chemistrylab` |
| `astronomy` | Astronomy | 31 | `astronomy` |
| `forensics` | Forensics | 29 | `forensics` |
| `fermi_questions` | Fermi Questions | 27 | `fermiquestions` |
| `optics` | Optics | 25 | `optics` |
| `rocks_minerals` | Rocks & Minerals | 25 | `rocksminerals` |

¹ Anatomy & Physiology PDFs are uploaded under **two** prefixes on scioly.org: `anatomy_*` (68 files) and `anatomyphysiology_*` (51 files). Pick one for `filename_prefix` and you'll get roughly half the corpus; the other half will download but won't be picked up by the pipeline's filename glob until you switch the prefix. A simple workaround is to rename the files on disk to share one prefix; a cleaner fix would be to extend `Event.filename_prefix` to a tuple — file an issue if you need this.

**Also viable** — 10–25 PDFs each, mostly knowledge tests with usable topic structure.

| Suggested slug | Event | PDFs |
|---|---|---:|
| `microbe_mission` | Microbe Mission | 24 |
| `experimental_design` | Experimental Design | 17 |
| `ecology` | Ecology | 21 |
| `herpetology` | Herpetology | 21 |
| `hovercraft` | Hovercraft (written portion) | 21 |
| `fossils` | Fossils | 18 |
| `remote_sensing` | Remote Sensing | 20 |
| `ornithology` | Ornithology | 19 |
| `materials_science` | Materials Science | 17 |
| `crime_busters` | Crime Busters | 16 |
| `invasive_species` | Invasive Species | 13 |
| `heredity` | Heredity | 14 |
| `designer_genes` | Designer Genes | 12 |
| `protein_modeling` | Protein Modeling | 12 |
| `hydrogeology` | Hydrogeology | 10 |
| `sounds_of_music` | Sounds of Music (acoustics) | 10 |
| `water_quality` | Water Quality | 9 |
| `meteorology` | Meteorology | 5 |
| `reach_for_the_stars` | Reach for the Stars (Div B Astronomy) | 5 |
| `solar_system` | Solar System | 5 |

**Won't work** — pure construction/build events. `scioly_tests.json` has 0 PDFs for these because there's no written test to extract. Skip:

Towers · Boomilever · Wright Stuff · Mousetrap Vehicle · Gravity Vehicle · Helicopters · Electric Vehicle · Bottle Rocket · Battery Buggy · Robot Arm · Trajectory · Rollercoaster · Mission Possible · Bridge events.

> **Filename prefix tip:** the prefix is the lowercase event name with spaces and punctuation stripped, matching what's before the first underscore in scioly.org URLs (e.g. `chemistrylab_2022_c_uflorida_test.pdf` → prefix `chemistrylab`). To check before configuring:
>
> ```
> python -c "import json; data=json.load(open('scioly_tests.json'));\
> from collections import Counter;\
> p=Counter(d['test_link'].split('/')[-1].split('_')[0] for d in data if 'test_link' in d and 'YOUR_EVENT_NAME'.lower() in d['event'].lower());\
> print(p.most_common())"
> ```

## What you get (per event)

- `<event>/` — all downloaded PDFs (test + key files)
- `<event>/question_bank.md` — a topic-organised question bank
- `<event>/images/` — extracted diagrams
- `<event>/texts/` — wiki dump + user-supplied source PDFs (for LLM question generation)
- `<event>/.qbank_state.json` — pipeline cache + user annotations + LLM verdicts + generated questions

## Quick start

```
# 1. Install deps
pip install pymupdf requests flask anthropic playwright \
            markdownify beautifulsoup4
playwright install chromium    # one-time browser download for bot-bypass

# 2. (Optional) put your Anthropic key in .env so vision OCR, math capture,
#    answer validation, and LLM question generation work. The pipeline still
#    runs without it (degraded — no vision, no LLM features).
cp .env.example .env   # then set ANTHROPIC_API_KEY=sk-ant-...

# 3. Download the source PDFs for the event you want
python download_event.py --event circuit_lab
python download_event.py --event thermodynamics

# 4. Build the question bank (caches by PDF)
python build_question_bank.py --event circuit_lab
python build_question_bank.py --event thermodynamics --rebuild   # start fresh
python build_question_bank.py --event circuit_lab --text-only    # skip vision

# 5. Open the review UI — single server handles every event
python review_app.py
# → http://127.0.0.1:5000/   (event landing page)
```

The pipeline always reads the latest state from each event's `.qbank_state.json`, so the CLI and the review UI feed each other.

### scioly.org bot challenge — handled automatically

scioly.org uses Anubis JavaScript proof-of-work bot protection. The first time `download_event.py` hits a challenge page, it launches a real Chromium via Playwright, lets the PoW finish (5–30 seconds), captures the cookies into `.scioly_cookies.json`, then resumes the downloads using `requests`. Subsequent runs reuse the saved cookies until they expire.

Prerequisite (one-time):
```
pip install playwright
playwright install chromium
```

To force a fresh bypass (e.g. cookies expired):
```
python download_event.py --event <slug> --reauth
```

#### Running on a headless server (no X11, no Playwright)

Playwright/Chromium needs a baseline of shared libraries that Playwright's
`--with-deps` installer doesn't reliably cover outside Ubuntu/Debian (e.g.
RHEL). Rather than fighting that on the server, never run Playwright there
at all:

1. On any machine that *can* run Playwright (your dev laptop, anywhere),
   solve the bot challenge once: `python download_event.py --event <slug> --reauth --bypass-bot-headless`. This writes a fresh `.scioly_cookies.json`.
2. `scp` that one file to the server, into the app's working directory.
3. Downloads on the server now work via `requests` alone — Playwright is
   never imported, so it doesn't need to be installed there at all.

Cookies last ~7 days. The header badge (coach login, "🍪 scioly.org cookies
expire in …") warns once the saved cookie is within 48 hours of expiring,
so the next `scp` happens before a download run starts failing mid-batch.

## Files

| File | Purpose |
|---|---|
| `events.py` | Event registry: per-event topics, keywords, paths, filename prefix, wiki page |
| `download_event.py` | Generic PDF downloader; takes `--event <slug>` (required). Auto-bypasses scioly.org's Anubis bot challenge via Playwright. |
| `build_question_bank.py` | The extraction pipeline (CLI). Run after downloading. `--event <slug>` (required). |
| `texts.py` | Scrapes the scioly.org wiki for an event into markdown; converts user-supplied source PDFs to markdown |
| `doc_convert.py` | Normalizes `.docx`/`.doc` test/key files to PDF via headless LibreOffice (`soffice`) so the rest of the pipeline only ever deals with PDFs |
| `qgen.py` | LLM (Haiku) question generation from source texts; Jaccard-based dedup against the existing bank |
| `scrape_scioly.py` | Pulls public questions from scio.ly/practice's JSON API; normalizes them into the canonical Question shape |
| `review_app.py` | Flask review UI — single server, multi-event, with a Generate page (sources + LLM generation) per event |
| `common_ui.py` | Shared CSS/JS (design tokens, modal/badge/toolbar components, `confirmModal()`, job-progress modal) — imported by both `review_app.py` and `admin_app.py` so the two Flask processes render an identical look without duplicating the stylesheet |
| `jobs.py` | Background-job queue for long-running operations (reprocess, scio.ly scrape/download, LLM generation, wiki scrape) — see "Background jobs" below |
| `templates/*.html` | Jinja2 page templates for the review UI (`events`, `event_index`, `browse`, `review`, `sources`, `quiz`, `event_jobs`, `admin_jobs`, `event_scan`, `settings`) |
| `text_utils.py` | Shared text-normalization helpers (`strip_points`) used by the pipeline, scraper, and generator without an import cycle |
| `scioly_tests.json` | Pre-scraped metadata for **all** Science Olympiad tests, 887 entries |
| `.env.example` | Template for the Anthropic API key |
| `.scioly_cookies.json` | Cached Anubis cookies (auto-managed; delete to force a fresh bot-bypass) |
| `events_custom.json` | Auto-generated registry of every registered event, including the Circuit Lab/Thermodynamics defaults (their curated keyword data lives in `events.py` and is seeded in here on first run, but the live registry entry — and any edits/archiving — lives here) |
| `TODO_brittleness.md` | Self-audit of fragility / brittleness in the codebase with a suggested refactor ordering |
| `TODO_ux.md` | Self-audit of UX rough edges with suggestions grouped by effort (⚡/🛠/🏗) |
| `spec.md` | Technical spec — data shapes, pipeline stages, annotation/validation/generation schemas |

## Workflow

1. **Download** — `download_event.py --event <slug>` pulls every test and key PDF for the chosen event from scioly.org. It uses a Chrome User-Agent and auto-bypasses Anubis bot protection via Playwright on the first challenge (saving cookies to `.scioly_cookies.json` for subsequent runs — see [running on a headless server](#running-on-a-headless-server-no-x11-no-playwright) if Playwright can't be installed where this runs). Already have a test on disk? The event index page (`/event/<slug>/`) has a **+ Upload test** button that opens a small form with three file slots — drop in a test (required), its answer key (optional), and a figures/supplementary document (optional, e.g. a separate `_sheet`/`_notes` file referenced by the test) — each accepting a **PDF, `.docx`, or `.doc`**. It's saved with the correct `_test.pdf`/`_key.pdf`/`_figures.pdf` naming, converted to PDF via headless LibreOffice if it wasn't one already, and the test+key are run through the extraction pipeline immediately (the figures file is never extracted — it's pure browsing material for the review page's target toggle, see below). No separate Reprocess step needed.

   **Files copied directly onto the server** (e.g. `scp`'d in from another machine where you're assembling question banks) aren't picked up by any of the above on their own unless they already match the `{prefix}_{year}_{division}_{submitter}_test.pdf` naming convention. The event's **Scan files** page (`/event/<slug>/scan`) finds them: a **Ready to process** bucket for already-conforming files never run through extraction (one-click **Process all**), a **Needs conversion** bucket for `.docx`/`.doc` files awaiting LibreOffice conversion, and an **Unrecognized** bucket for anything else — each gets a small inline form (best-effort year/division guessed from the filename, editable) to rename it as a Test, Key, or supplementary document, after which it's indistinguishable from anything scioly.org-sourced. This is an on-demand page, not a background scan — reload it (or click Refresh) after dropping in new files.

2. **Extract** — `build_question_bank.py` runs three stages per PDF:
   - PyMuPDF text extraction → topic-classified questions, choices, answers
   - Spatial image association → each circuit diagram is attached to the question it sits closest to on the page
   - (Optional) Haiku vision pass to OCR image-only PDFs and to refine image-to-question assignments when the heuristic is ambiguous

3. **Browse** (`/event/<slug>/browse`) — an event-wide question explorer that aggregates every question across every bucket (PDF-extracted, LLM-generated, scio.ly-scraped) into one searchable, filterable view. Filter by topic, focus, source, **bucket**, validation status, question type (MCQ / FRQ / **Matching**), has-image; sort by Q#, topic, source, length, or validation. Filter state is persisted in the URL, so reload and back-button work and links are shareable. Search box is hotkeyed to `/`; image lightbox on click; sidebar shows live counts of topics/sources/validation.

   **Every card is directly editable, no Edit click required** — Topic, Focus, Stem, Choices, and Answer are live fields right on the card (topic select keeps the deterministic colour-hash background so it's still recognisable at a glance). Changes autosave ~600ms after you stop typing (no Save button) and a small status line shows "Saving…" → "Saved HH:MM:SS". An **"↺ Undo"** button appears after each autosave and reverts that question's fields back to what they were just before your most recent batch of edits (single-level — a *new* edit replaces the undo point). Every card also shows **"Last edited by `<user>` · `<timestamp>`"**, stamped server-side from the logged-in session on every save (never client-supplied).

   **Validation, both AI and manual** — an **"🤖 AI Validate"** button calls the same Haiku check used elsewhere and persists the verdict immediately (Browse previously couldn't persist this at all). A **Validation** dropdown next to it (Correct/Incorrect/Uncertain/unset) lets a human set or override the status directly — whichever happens most recently simply wins, so a human can always correct a stale or wrong AI verdict, and re-running AI Validate can likewise override a human's. The badge shows who set it (`(ai)`/`(human)`).

   PDF-sourced questions keep a persistent **"Open source ↗"** link to jump to that PDF's review page with the question pre-focused (absent for LLM-generated/scio.ly-imported questions, which have no source PDF).

4. **Review** (`review_app.py`) — a browser UI shows each PDF page next to its extracted questions. You can:
   - **Pull figures from a supplementary document** — some scioly.org submissions ship diagrams in a separate file alongside the test (e.g. `_sheet.pdf`, `_notes.pdf`) rather than embedded in the test PDF itself; the extraction pipeline never looks at these automatically (left as-is, by design — auto-guessing which figure belongs to which question across two documents isn't reliable). Instead, a toggle button appears next to **Test PDF** / **Key PDF** for each such file found alongside the current test, so you can flip over to it and use **📌 Pick image** (or any other capture tool) against it exactly as you would the test PDF.
   - **Drag a rectangle on the PDF** to capture stem, choices, or answer text directly from the source
   - **+ Add question from region** — drag once; the next available Q# is auto-assigned, MC options are auto-split into the choices list when present
   - **+ Add matching question** — drag the left column, then the right column (auto-advances, no second click needed); creates a two-column matching question (e.g. "match each term to its definition") with an editable pairs key. The automatic extraction pipeline also detects and structures matching tables on its own when processing a PDF — this button is for fixing a mis-detected one or building one from scratch.
   - **Capture math** — drag around an equation; Haiku converts it to LaTeX (`$...$`) and inserts at your cursor. KaTeX live-preview shows the rendered formula as you type.
   - **Reassign images** by click-then-click (click image → click target question card)
   - **Edit anything** inline — number, text, topic (with create-new), choices, answer
   - **Validate answers** with Haiku per-question or per-page; verdict + rationale + primary source is stored next to each answer
   - **Reprocess** a PDF to apply pipeline improvements; your annotations are replayed on top (hold Shift to wipe instead)
   - **Attach images** to a question — 📁 Upload a file (PNG/JPG/SVG/WebP) from disk, OR 🤖 Generate diagram via a small Claude Sonnet chat. The chat is seeded with the question stem + topic + any author-provided diagram description (third-party LLM imports can carry an `image_description` field that lights up as a "Diagram recommended:" note until you fulfil it). Each assistant turn renders the SVG live; one click saves it into the event's `images/` dir and attaches it to the question with an optional textual description. The description doubles as alt-text and as the seed for the *next* diagram-chat session if you re-open it.

5. **Generate** (per-event `/event/<slug>/sources` page) — pull in third-party questions and feed the LLM new material so it can write fresh questions:

   ### Sources & LLM generation
   - **Scrape Sci-Oly wiki** — one button. Uses the saved scioly.org cookies, converts the wiki HTML into clean markdown at `<event>/texts/wiki.md`
   - **Upload PDFs** — drop your own textbook chapters, study guides, or notes into `<event>/texts/` (via the UI or by hand). A *Process → MD* button converts each PDF to markdown.
   - **Generate** — pick a source, pick count + types (multiple choice / short answer / numerical), click Generate. Haiku drafts candidates with rationale and a quoted source snippet. The button is disabled and a progress panel with an elapsed-time counter is shown while the LLM is working; a **Cancel** button aborts in-flight. Output token budget scales with `n` (~550 tokens per question) so larger requests don't get silently truncated.
   - **Dedup** — every candidate is compared (Jaccard on word-3-grams, threshold 0.4) against every question already in the bank; near-duplicates are auto-rejected and listed separately.
   - **Keep / Drop** — review each candidate, accept individually or *Accept all kept*. Accepted questions go into a synthetic PDF bucket `_generated_<event>.pdf` so they're easy to identify, and they carry the LLM's rationale + source snippet in their `validation` field.
   - **Failure surfacing** — if Haiku returns malformed JSON or hits `stop_reason: max_tokens`, the UI shows the raw response inline instead of failing silently. Network/HTTP errors and parse errors get a status-bar message; warnings (e.g. truncation) are rendered as a banner above the candidates.

   ### Pull from scio.ly/practice
   - One-click scrape of public practice questions from [scio.ly/practice](https://scio.ly/practice)'s public JSON API. No auth required.
   - Form fields: scio.ly event name (defaults to the current event's display name, editable for events scio.ly names differently like "Anatomy - Endocrine"), count, division, MCQ/FRQ toggles.
   - "Validate with Haiku" toggle: each scraped question is run through the same answer validator the rest of the bank uses. scio.ly's questions are notoriously partial/context-stripped — the validator flags incomplete or unanswerable ones as `uncertain`/`incorrect` so you can drop them in one click.
   - Result view: every candidate shows its validation badge (✓ correct / ⚠ likely wrong / ? incomplete) with the rationale inline. Quick-filter buttons: **Keep only ✓ Correct**, **Drop ⚠/?**, **Keep all**, **Drop all**.
   - **Two-layer dedup**:
     1. *Exact* — scio.ly's question UUIDs are tracked so re-scraping the same event never re-imports a question already in the scio.ly bucket.
     2. *Fuzzy* — every candidate's stem is compared (Jaccard on word-3-grams, threshold 0.4) against every question already in the bank, regardless of which bucket they came from (PDF-extracted, LLM-generated, prior scio.ly scrapes). Matches are auto-rejected and shown in a collapsible "rejected as duplicates" section with the matched bank question's text and source for visual verification.
   - Accepted questions land in a synthetic PDF bucket `_scioly_<event>.pdf` with `source: "scio.ly · <tournament name>"` and their full validation verdict.
   - Polite rate-limit: ~2 req/s, and you can only scrape one event at a time per session.

   ### Shared textbooks (generate from one chapter, reusable across every event)
   - A textbook is too big and too generic to dump wholesale into one event's source list — upload it once at the top-level `textbooks/` directory (via the **Shared textbooks** panel on any event's Sources page) and it's available to *every* event's Generate dropdown, split by chapter.
   - **Chapter detection**, tried in order: (1) the PDF's own embedded outline/bookmarks (`fitz`'s `get_toc()`), used as-is when present; (2) a regex scan of the page-tagged markdown dump for `Chapter N` / `Unit N` / `Section N` style heading lines. If neither finds anything, the textbook is flagged `needs_manual_chapters` and a **Set chapters manually** panel lets you type `Title, start page` one per line — end pages cascade from the next chapter's start automatically.
   - Re-run detection any time via **Detect chapters** (e.g. after re-uploading a cleaner copy of the same book).
   - In the Generate source dropdown, textbooks appear under a **Shared textbooks** optgroup; picking one reveals a **Chapter** dropdown, and Generate is disabled until a chapter is chosen. The selected chapter's page range is extracted and sent to Haiku exactly like any other source — same dedup, same Keep/Drop flow.

   ### Import questions (from another LLM or hand-written JSON)
   - Below the Generate panel: paste JSON into a textarea, or upload a `.json` file from disk.
   - Accepts the same candidate shape `qgen.py` produces — either the full `{"candidates": [...]}` envelope or a bare array. Click the schema link in the panel for the exact field list.
   - **Auto-repairs broken JSON server-side**, covering the two most common real-world breakages in hand-typed or LLM-exported question banks: unescaped LaTeX (`\theta`, `\frac{Q}{T}`, `\Delta`, …  — valid LaTeX, invalid bare JSON escapes) and unescaped literal quotes inside string values (e.g. units like `5"`). The browser no longer hard-rejects unparsable input — it sends the raw text to the server, which runs it through `build_question_bank.repair_json_text()` / `_parse_json()`'s repair ladder (markdown fences, smart quotes, trailing commas, single-quoted strings, backslash/quote/control-character repair) before parsing. The response's `repaired` flag tells you whether a fix was needed.
   - Runs through the identical normalisation + dedup pass as Generate (strip point-markers, re-letter choices A/B/C/…, Jaccard dedup against the whole bank) before landing in `_generated_<event>.pdf` alongside Haiku-generated questions.
   - **Mark all as validated** checkbox (default off) — when checked, every imported question gets `validation.status = "correct"` immediately, skipping the usual Haiku validation step. Use this when you trust the source (e.g. you wrote the questions yourself, or already validated them elsewhere); leave it off to validate later via the normal per-question/per-page Validate buttons.
   - Response reports `added` / `rejected_duplicates` / `rejected_invalid` so you know exactly what happened to a large batch.

   ### Drafting questions in another LLM (ChatGPT, Gemini, Claude.ai, …) before importing
   - If you'd rather draft in a chat UI than use this app's own Generate panel, paste the system prompt below as your *first* message, then follow up with your source material and how many questions you want. The model's reply is valid input for the Import panel above as-is.
   - It compresses the same field list shown in the Import panel's "qgen output format" link (a candidate's `type`, `topic`, `text`, `choices`, `answer`, `rationale`, `source_snippet`, `image_description`) into one paste-able block, so the model produces correctly-shaped JSON on the first try instead of you reformatting its output by hand.
   - Any field outside this list (e.g. a difficulty rating) is silently ignored on import, not stored — if you want difficulty tracked, fold it into `rationale` or `source_snippet` as free text instead of inventing a new top-level key.

     ```
     You generate Science Olympiad practice questions as JSON only.

     FORMAT — for every question, provide:
     - topic: one of the event's topics (an unrecognized topic falls back to "Other / General")
     - type: "mc" for multiple choice (4+ choices labeled A, B, C, ... exactly one
       correct; the others are plausible distractors reflecting common student
       mistakes), "short" for a short-answer question needing a 1-2 sentence
       response (leave choices empty), or "numerical" for a numeric answer with
       units (include the equation and a brief solution outline in the
       rationale; leave choices empty)
     - text: the question stem. Use LaTeX for any equations/expressions, e.g.
       $V = IR$ or $P = \frac{V^2}{R}$
     - choices: for "mc" only — an array of {"letter": "A", "text": "..."}.
       Use LaTeX in choice text too if it needs an equation/expression
     - answer: the correct letter for "mc", or the full answer for
       "short"/"numerical"
     - rationale: a complete step-by-step solution showing the derivation,
       with LaTeX equations
     - source_snippet: a short quote from the source material that supports
       the question
     - image_description: optional — only if the question needs an
       accompanying diagram/figure. A fully self-contained description,
       detailed enough that it could be handed to another tool to draw a
       clean line diagram from. No image file is attached at this stage —
       it just seeds a later diagram-generation step.

     Reply with ONLY valid JSON — no markdown fences, no commentary before or
     after. Each question is one entry in a "candidates" array:

     {"candidates": [
       {
         "type": "mc" | "short" | "numerical",
         "topic": "<topic>",
         "text": "<question stem>",
         "choices": [{"letter": "A", "text": "..."}],
         "answer": "<letter for mc, or full answer for short/numerical>",
         "rationale": "<step-by-step solution, with LaTeX>",
         "source_snippet": "<short quote from source>",
         "image_description": "<diagram description, if needed>"
       }
     ]}

     Acknowledge that you understand these rules. Do not generate questions
     yet — wait for the next message with the source material and how many
     questions to generate.
     ```

6. **Markdown** — `build_question_bank.py` writes `question_bank.md` grouped by topic, with figures, choice lists, answers, and validation/derivation verdicts as blockquotes. (Generated only when you run the CLI; the web UI works directly from the JSON state and no longer exposes a regenerate-markdown button — JSON is canonical.)

## The cache & annotations

Everything writes back to a single file per event: `<event>/.qbank_state.json`. It contains:

- `_schema_version` — bump-on-breaking-change marker; older files are migrated forward on load
- `questions[<pdf>]` — the canonical extracted question list. Generated questions live under a synthetic key `_generated_<event>.pdf`; scio.ly-imported questions under `_scioly_<event>.pdf`. Pipeline reprocess skips any PDF whose name starts with `_`.
- `vision[<pdf>]` — cached Haiku outputs (so vision is paid for once)
- `annotations[<pdf>]` — your edits, organised by what you did:
  - `field_overrides` — text/topic/answer edits (incl. multi-page `extra_pages` and `context_id` link)
  - `added` / `deleted` — questions you created or removed
  - `image_overrides` — image reassignments (an assignment value may be a list to share one image across multiple questions)
  - `regions` — provenance of every drag-rectangle (for auditing)
  - `validations` — LLM verdicts: `{status, correct_answer, rationale, source, ...}`
  - `contexts` — shared context blocks (passages/intros/tables that several questions reference, e.g. a Disease Detectives case study): `[{id, title?, text, images:[], pages:[]}]`. A question opts in via `context_id`; the review UI then renders the context above the stem and edits to the shared text flow through to every linked question. Contexts can span pages just like questions. `title` is an optional human label (e.g. "Avian flu outbreak") shown wherever a context is picked from a list — falls back to the auto id (`ctx_1`, …) when blank.

    **Grouping several questions at once**: linking one question at a time via its Context dropdown doesn't scale to a 6-question case study. The review page's toolbar **"🔗 Group questions"** toggle enters a multi-select mode — click question cards (a checkbox appears) or their bounding boxes on the PDF pane to build up a selection across page turns (case studies often span pages), then **"Assign to context ▾"** in the sticky selection bar to bulk-link them to an existing context or to a freshly captured one ("+ New context from region" auto-links the capture to whatever's currently selected). Works either direction — capture the passage first then select its questions, or select the questions first then capture the passage. A collapsible **"📎 Case studies in this PDF"** panel above the page-specific cards lists every context regardless of which page you're on, with linked-question jump links and inline title editing — the per-page section only shows a context once you're on a page it touches, so this is the place to audit a multi-page group's full membership.

    **Where grouping shows up beyond the review page**: the quiz page's "Keep case studies together" checkbox (off by default, so today's behavior is unchanged unless you opt in) expands the selection to pull in every sibling of a picked case-study question and shows the shared passage above each one; `build_markdown()`/the PDF/CSV/JSON exports cluster a case study's questions together with the shared passage rendered once, instead of scattering them across the topic listing.

Writes are atomic (tempfile + `os.replace`) and serialised by a per-event lock, so concurrent saves never leave a half-written JSON on disk. The lock spans the whole load → modify → save cycle for every short request-handler save (annotation edits, Browse-page PATCHes, imports), not just the write itself — so two coaches editing the same event around the same moment can't silently lose one edit to the other (the same guarantee applies to `auth.py`'s users file, `seasons.py`'s seasons/rosters, and `testing.py`'s windows/tests/responses files). The one deliberate exception is a long-running OCR/extraction job's own checkpoint saves, which hold a single in-memory snapshot for the job's full duration rather than re-locking per checkpoint — concurrent edits to a *different* PDF in the same event during that job are unaffected, but an edit to the *same* PDF mid-job can still be overwritten by the job's next checkpoint.

When the pipeline runs again (CLI or "Reprocess" in the UI), it re-extracts fresh from the PDF then **replays your annotations on top**. So:

- Pipeline improvements take effect on questions you didn't touch
- Your manual corrections stick
- Validation verdicts stay attached (and auto-flag as **stale** if the question text or answer changes underneath them)

The review page's **Reprocess ▾** menu offers three modes:

| Mode | Effect |
|---|---|
| Re-extract · keep annotations | Default. Wipe extraction cache, replay your saved annotations on top. |
| Re-extract · wipe annotations | Start from scratch — discards every field edit / delete / image move for this PDF. |
| Manual · region-by-region | Wipes all questions; you then drag a region on each page to build new questions. Multi-page (Shift+drag) supported. A "Try Haiku" fallback appears after each capture so you can re-extract via vision when pure-Python struggles. |

(Manual mode itself is instant — it just empties the question list — but the other two re-run extraction, which is where the background-job system below kicks in.)

## Background jobs

The server this app runs on is intentionally underpowered, and several operations — PDF extraction with vision OCR, scio.ly scraping/downloading, LLM question generation, wiki scraping — can take anywhere from seconds to several minutes. Those run as **background jobs** (`jobs.py`) instead of blocking your browser:

- **Reprocess** (single PDF and bulk), **upload a test PDF**, **download PDFs from scioly.org**, **scrape scio.ly candidates**, **generate questions** (the full-document/textbook-chapter flow), and **scrape the wiki** all enqueue a job and immediately return — you get a job id, and a progress window opens showing live phase/progress-bar/console output (the same `print()` lines you'd see running the CLI directly) without you having to stay on that page.
- **Exactly one job runs at a time, globally**, across every event — this machine can't do two PDF extractions or vision-OCR loads concurrently. Anything else queues (FIFO) until the running job finishes; queueing more than ~8 jobs on one event gets refused (HTTP 429) rather than piling up unboundedly.
- **Cancellation** is cooperative (checked between PDF pages, between LLM chunks/candidates, etc. — not a hard kill) and available to the job's own starter or **any coach**, matching the same trust model as user management elsewhere in the app.
- **Jobs survive a server restart's bookkeeping, but not the work itself**: job status/progress/console output is written to disk (`<event>/.qbank_jobs.json` + `<event>/.qbank_jobs/<job_id>.log`), so a `systemctl restart qbank` (every code deploy does this) doesn't erase job history — but a job that was actually running when the process died comes back as **"interrupted"**, not silently resumed. Just re-trigger the same action; PDF vision-OCR results are checkpointed page-by-page as they're produced, so re-running after an interruption only re-does the page that was in flight, not the whole PDF.
- **Visibility**: anyone with access to an event sees that event's job history and full console output via its **Jobs** page (linked from the PDF list) — same access rule as everything else (`_select_event`), no extra restriction. Coaches additionally get a cross-event **All jobs** dashboard (`/admin/jobs`). A small badge in the header shows how many jobs are currently running/queued across whatever you can see, so a job that outlives you logging out and back in is easy to find again.
- Short, single-LLM-call actions (validate one question, capture one region, one diagram-chat message, OCR a single page) are deliberately **not** jobs — they finish within one request and just show a normal spinner.

## Cost notes

Vision calls go to `claude-haiku-4-5-20251001` (cheapest with vision). Rough per-PDF cost when vision is enabled:
- Image association: ~$0.01–0.05 (one Haiku call per page with embedded images)
- OCR a fully-image-based PDF: ~$0.10
- Answer validation: ~$0.003 per question
- Math capture: ~$0.005 per equation
- Question generation: ~$0.01 per Generate-button click (~5 candidates)

Set `--text-only` on the CLI to skip vision entirely. The review UI is the place to spend money selectively — vision, validation, math capture, and generation are explicit button-presses, never automatic.

## Configuration

Put your key in `.env` next to the scripts:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without a key, the pipeline still runs: it skips vision OCR (image-based PDFs yield no questions) and skips image-association refinement (falls back to y-coordinate heuristic). The review UI still works; vision-dependent buttons disable themselves.

### Separating app code from data (`DATA_ROOT`)

By default every event's data — PDFs, images, generated markdown, state/job files — plus `auth_users.json`, `events_custom.json`, and the shared `textbooks/` directory all live right next to the app's code, under the same directory `events.py` is in (`REPO_ROOT`). That's fine for local dev or a small instance, but on a real deployment the data grows continuously while the code doesn't — set `DATA_ROOT` in `.env` to put data on a separate (e.g. larger) disk instead:

```
DATA_ROOT=/mnt/qbank-data
```

Leave it unset and nothing changes — `DATA_ROOT` defaults to the app directory, so this is purely opt-in. Moving an *existing* instance's data to a new `DATA_ROOT` needs a one-time manual migration — see ["Maintaining the server"](#maintaining-the-server) below; don't just set the variable on a box that already has data in the old location, or the app will look for (and start creating) a second, empty copy at the new path instead of finding what's already there.

What stays with the code regardless of `DATA_ROOT` (none of it grows unboundedly or benefits from a bigger disk): `scioly_tests.json` (shipped, read-only), `.scioly_cookies.json` (small, refreshed every ~7 days), `.env`, `deploy/instances.conf`, `static/`.

## Authentication & roles

The app supports three roles, stored in `auth_users.json` (`auth.py` — same flat-JSON-file pattern as `events_custom.json`, gitignored, never committed):

- **Coach** — full admin. Every event, plus user management (Manage Users, inside **⚙ Settings**), shared-textbook uploads, Club Management, and the Tests dashboard.
- **Volunteer** — edit access only to the specific events a coach assigns them. Unassigned events are hidden from their landing page and 403 on direct URL. Can also be assigned to build/grade tests for the [season testing workflow](#season-long-testing-workflow) below, a separate grant unrelated to event access.
- **Student** — no question-bank access at all (not even read-only — see the [season testing workflow](#season-long-testing-workflow)). Scoped entirely to the tests they're rostered on for the current season: `/my-tests` (take a live test, view released results) and `/scores` (everyone, including students, sees every student's named score — response-level detail is restricted to coaches and whoever actually graded that test).

**First-time setup** — a fresh `auth_users.json` has no accounts, so there's no one who could use the in-app admin UI yet. Bootstrap the first coach from the CLI:

```
python auth.py --create-coach
```

After that, log in and use **⚙ Settings → Manage Users** (coach-only, linked from the header) to create volunteer accounts and check which events each one can access.

**⚙ Settings** (linked from the header, every logged-in user) is the one place for account self-service:
- **My Account** — change your display name (a friendlier label shown in the header instead of your username; purely cosmetic, doesn't change your login) and change your own password (requires re-entering your current password first).
- **LLM API Keys** — supply your own Anthropic/OpenAI/Gemini/DeepSeek/Mistral key(s) for this browser only (localStorage, never sent to the server except as a request header) — used to override the server's own key, with automatic fallback through the list if one is rate-limited or out of credits. This used to be a floating button on every page; it's now a plain section here instead.
- **Manage Users** (coach-only) — the same user CRUD that used to live at `/admin/users` (still works as a redirect here for old links/bookmarks).

Sessions are plain signed Flask cookies — set `FLASK_SECRET_KEY` (a random 32+ byte hex string) in `.env` for production so logins survive a restart; without it the app generates a throwaway key per process start and everyone gets logged out. Once served over HTTPS, also set `SESSION_COOKIE_SECURE=true` so the session cookie requires TLS.

## Deploying (shared, multi-user instance)

For a small team sharing one workspace, bare metal + a reverse proxy is enough — no Docker needed:

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# (requirements-dev.txt's playwright is only needed if you'll run
#  download_event.py --reauth from this machine — see "running on a
#  headless server" above for why production servers typically skip it)
sudo dnf install libreoffice-headless   # only needed for .docx/.doc test/key ingestion
venv/bin/python auth.py --create-coach
```

Then install [`deploy/qbank.service`](deploy/qbank.service) as a systemd unit (`gunicorn`, bound to `127.0.0.1:5000`) and [`deploy/Caddyfile`](deploy/Caddyfile) as a reverse proxy in front of it — both files have install steps in their header comments.

**`--workers 1` is load-bearing** — `build_question_bank.py`'s per-event state lock is an in-process `threading.RLock`, which only serialises writes correctly within a single worker process. `--threads 8` gives real request concurrency for the I/O-bound LLM calls without that risk. Don't raise `--workers` above 1 without first replacing that lock with a file-based one (each worker process gets its own copy of an in-process lock, so it would silently stop protecting anything) **and** redesigning `jobs.py`'s background-job queue, which is explicitly single-process by design — see that module's docstring. **`--threads` is the one safe to tune** if you move to a more powerful box, and is now adjustable per instance from the admin app's "Threads" control (see below) instead of hand-editing the unit file — it writes a systemd drop-in, never `instances.conf` (which gets overwritten from the git-tracked source on every Update).

Redeploy flow: `git pull && venv/bin/pip install -r requirements.txt && sudo systemctl restart qbank`.

### Subpath mounting (e.g. `/testbank/ncms` instead of the domain root)

If the reverse proxy doesn't own the whole domain, set `APPLICATION_ROOT=/testbank/ncms` (or whatever prefix) in `.env`. `review_app.py`'s `_PrefixMiddleware` strips that prefix into `SCRIPT_NAME` so `url_for()`/`request.script_root` produce correctly-prefixed URLs, and every template's JS already reads the `APP_ROOT` global (injected by `_user_badge.html`) to prefix its `fetch()` calls — no further app changes needed. The Caddyfile's `handle /testbank/ncms* { reverse_proxy ... }` block must forward the path **unstripped** (`handle`, not `handle_path`) to match what the middleware expects.

See ["Production deployment (current state)"](#production-deployment-current-state) below for the actual, concrete setup these patterns describe.

### Running multiple independent instances on one server (e.g. two schools)

`auth.py`/`events.py`/`archive.py` all locate their data files relative to `Path(__file__).parent` — there's no `DATA_ROOT`-style override — so two instances can't share one code directory and stay isolated. Each school needs its own full code+data directory tree, but the venv can be shared since it's just an interpreter path referenced by each systemd unit, independent of `REPO_ROOT`. The pattern (mirrors `/opt/qbank` for NCMS and `/opt/qbank-chs` for CHS):

- A dedicated, non-root system user per instance (`qbank`, `qbank-chs`, ...) — keeps a bug in one school's instance from touching another's files.
- One `.env` per instance with its own `FLASK_SECRET_KEY` and `APPLICATION_ROOT` (and, if usage tracking per school matters, its own `ANTHROPIC_API_KEY`).
- One systemd unit per instance (copy `deploy/qbank.service`, change `WorkingDirectory`, `EnvironmentFile`, `User`/`Group`, and the gunicorn `--bind` port — `127.0.0.1:5000`, `5001`, ... one per instance), all pointing at the same shared venv path if you're sharing one.
- One more `handle /testbank/<school>* { reverse_proxy 127.0.0.1:<port> }` block per instance in the same Caddyfile, same domain, same certificate — no new DNS/port-forward/TLS work per additional school.
- **Cookie isolation**: `review_app.py` sets `app.config["APPLICATION_ROOT"]`/`SESSION_COOKIE_PATH` (and passes `path=` on the CSRF cookie) from the `APPLICATION_ROOT` env var, so each instance's session/CSRF cookies are scoped to its own path — without this, two instances sharing a domain would each receive the other's cookies (harmless here since each instance also has its own `FLASK_SECRET_KEY`, so a foreign cookie just fails signature validation, but wasteful and worth keeping closed).

### Updating code across multiple instances from GitHub

Once there's more than one instance, `git pull && restart` in one directory doesn't touch the others. [`deploy/update-from-github.sh`](deploy/update-from-github.sh) fetches the public repo into a canonical clone and redeploys the code-only allow-list (`*.py`, `templates/`, `static/`, `deploy/`, `requirements*.txt` — never `.env`, `auth_users.json`, event directories, or anything else data-shaped) to every instance, restarting each independently. It's split into two scripts so the routine "pull updates" action never needs broad sudo:

- **`update-from-github.sh`** runs as a dedicated low-privilege account (e.g. `qbank-deploy`) that owns its own clone outright — no elevated access needed for the git operations themselves.
- **[`deploy/_apply-update.sh`](deploy/_apply-update.sh)** is the only part that needs root (installing into the shared venv, `chown`ing into each instance's user, restarting systemd units) — it takes no arguments and reads no untrusted input, so a `NOPASSWD` sudoers rule scoped to that *exact* script path (see the header comments in both scripts for the one-time setup commands) can't be used for anything beyond what's written in the script.
- Validates with `py_compile` + `pytest tests/ -q` against the freshly fetched code *before* touching any live instance; a failed validation deploys nothing.
- Run by hand (`sudo -u qbank-deploy bash update-from-github.sh [ref]`) when you're ready — deliberately not on a cron/timer, since auto-deploying unreviewed pushes adds blast-radius without saving meaningful effort at this scale.

### Home-network deployment (dynamic IP, no static cloud IP)

Running behind a residential router instead of a cloud VM needs a few extra pieces beyond `deploy/Caddyfile`/`deploy/qbank.service`:

- DHCP-reserve the box's LAN IP on the router, then forward WAN ports 80 and 443 to it. Caddy's automatic Let's Encrypt HTTP-01 challenge needs port 80 reachable from the internet, not just 443.
- If the public IP isn't static, run `ddclient` (or an equivalent script) on the box to keep the domain's DNS A record pointed at the current IP — the exact config depends on whoever hosts the domain's DNS.
- Don't forward SSH to the WAN — administer the box over the LAN (or a VPN back into the LAN) instead.

### Backups

See ["Production deployment (current state)"](#production-deployment-current-state) for the actual repo/bucket names currently in use. Per instance, two complementary mechanisms, neither touching `.env`/`auth_users.json` (handle those separately — see below):

- **[`deploy/backup-extracted-data.sh`](deploy/backup-extracted-data.sh)** copies each event's `.qbank_state.json`/`question_bank.md` + `events_custom.json` + job history (`.qbank_jobs.json`/`.qbank_jobs/`, see "Background jobs" above) into a private GitHub repo clone and commits — git gives line-level diffs and full history for free, which matters for text/JSON the way it doesn't for binaries. Commit messages are generated by [`deploy/qbank-commit-message.py`](deploy/qbank-commit-message.py), which diffs each question's `lastEditedBy`/`lastEditedDateTime` between the previous commit and the fresh copy, so `git log --oneline` reads like a changelog (`circuit_lab: Q5 edited by srikanth`) without ever opening a diff. Safe to run often (cron every 1-2 hours) — backing up more frequently is strictly safer.
- **[`deploy/backup-bulk-data.sh`](deploy/backup-bulk-data.sh)** backs up everything else (PDFs, `images/`, `texts/`, `textbooks/`) to S3 via [`restic`](https://restic.net/) — client-side encrypted (S3 itself never sees plaintext), deduplicated, with `--keep-daily/weekly/monthly` retention pruning. Discovers event directories dynamically (anything with a `.qbank_state.json`, for the git script; anything that isn't a known code directory, for this one) rather than a hardcoded list, so new events need no script changes.
- Both scripts read their destination credentials (GitHub PAT, AWS keys, restic password) from `/opt/qbank/backup/.env` — a separate file from the app's own `.env`, never read by `review_app.py`/gunicorn, so a future app-level bug can't also leak backup-destination access.
- Both scripts also take an optional trailing argument — the instance's own `.env` path (e.g. `/opt/qbank/.env`) — read only to pick up [`DATA_ROOT`](#separating-app-code-from-data-data_root) if that instance has migrated its data off the app directory. Omit it and both scripts back up `<instance-app-dir>` exactly as before; this is how an instance that hasn't migrated yet keeps working with zero changes.
- **The restic repository password is the single most critical secret in this design** — losing it makes the entire S3 backup permanently undecryptable even though the encrypted data is still sitting there. Keep it in a password manager, never only on the server.
- **How big does the S3 bucket actually get?** Not `(live data size) × (number of retained snapshots)` — restic dedupes at the content-chunk level, so a snapshot is just pointers into a shared pool of unique chunks; unchanged files cost nothing to "back up again." Bucket size ≈ the total amount of *unique* data referenced by any currently-retained snapshot, not a full copy per snapshot. For this app's actual usage pattern (PDFs uploaded/downloaded once, accumulating over time, occasionally deleted, rarely edited) that means a library that starts at, say, 5GB and mostly just grows will keep the bucket close to that live size plus modest overhead — not 5GB times the ~34 snapshot slots the `--keep-daily/weekly/monthly` policy can hold over a year. A deleted PDF isn't reclaimed immediately either: it keeps costing storage until every snapshot referencing it ages out via `restic forget --prune`, which under the current policy can take up to ~12 months (the last monthly snapshot that still has it). Heavy upload/delete churn pushes the bound up toward the total unique content seen across the trailing year, but never multiplies by snapshot count the way a naive "full backup per run" tool would.
- `auth_users.json` and each instance's `.env` are deliberately excluded from both of the above — back those up separately (e.g. an `age`-encrypted snapshot to a key only you hold), and only by hand, since they change rarely and are too sensitive for an unattended pipeline.

## Production deployment (current state)

Everything above this point describes the *general* deployment patterns. This section is the concrete answer to "what is actually running, where" — useful when picking this project back up after time away, or onboarding someone else to administer it.

**Host**: RHEL 10.2, hostname `testbank`, LAN IP `192.168.1.201` (DHCP-reserved on the router so it can never drift), sitting behind a Ubiquiti Cloud Gateway. Public domain `scioly-02864.com`, DNS hosted on Namecheap; the UCG's built-in Dynamic DNS client keeps the A record pointed at the current public IP (the ISP connection doesn't have a static IP).

**Reverse proxy**: Caddy, installed as a static binary at `/usr/local/bin/caddy` with the `cap_net_bind_service` capability set (so it can bind 80/443 without running as root). One Caddyfile (`/etc/caddy/Caddyfile`) serves both schools under a single Let's Encrypt certificate for the bare domain — see ["Subpath mounting"](#subpath-mounting-eg-testbankncms-instead-of-the-domain-root) above for why one cert covers multiple `/testbank/<school>` paths.

**Current instances**:

| School | URL | Directory | System user | Port | systemd unit |
|---|---|---|---|---|---|
| NCMS | `https://scioly-02864.com/testbank/ncms/` | `/opt/qbank/app` | `qbank` | `127.0.0.1:5000` | `qbank.service` |
| CHS | `https://scioly-02864.com/testbank/chs/` | `/opt/qbank-chs/app` | `qbank-chs` | `127.0.0.1:5001` | `qbank-chs.service` |
| Admin app | `https://scioly-02864.com/testbank/admin/` | `/opt/qbank-admin/app` | `qbank-admin` | `127.0.0.1:5002` | `admin-app.service` |

Both qbank/qbank-chs units' `ExecStart` point at the same shared venv, `/opt/qbank/venv` — see ["Running multiple independent instances"](#running-multiple-independent-instances-on-one-server-eg-two-schools) above for why the venv can be shared even though the code+data trees can't. The admin app reuses the same venv too (Flask/Werkzeug/gunicorn are already there — no extra dependencies).

**Data location**: both schools' event data has been migrated off the 70GB root disk onto the 932GB `/data` mount via [`DATA_ROOT`](#separating-app-code-from-data-data_root) — NCMS at `/data/qbank/ncms` (`DATA_ROOT` set in `/opt/qbank/.env`), CHS at `/data/qbank/chs` (`/opt/qbank-chs/.env`). Each instance's app directory now holds only code.

**Code-update account**: `qbank-deploy` owns `/opt/qbank-src` (the canonical `git clone` of the public repo that [`deploy/update-from-github.sh`](deploy/update-from-github.sh) fetches into). The one step that needs root — [`deploy/_apply-update.sh`](deploy/_apply-update.sh), installed on the server as `/usr/local/sbin/qbank-apply-update.sh` — is reachable via a `NOPASSWD` sudoers rule scoped to that *exact* path (`/etc/sudoers.d/qbank-deploy`), nothing broader. Both `qbank.service`/`qbank-chs.service` and `instances.conf` (the registry both the apply script and the admin app read) live alongside this in [`deploy/`](deploy/).

### Admin app

[`admin_app.py`](admin_app.py) is a small, separate Flask app (its own process, system user, and sudoers grants — a deliberately different privilege boundary from the review apps it manages) at `/testbank/admin` for doing routine server operations from a browser instead of SSH:

- **Stop / start / restart** any instance listed in [`deploy/instances.conf`](deploy/instances.conf) — shells out to `sudo /usr/local/sbin/qbank-service-ctl.sh <verb> <instance>`.
- **Update from GitHub** — runs `update-from-github.sh` as `qbank-deploy` (same low-privilege fetch step as the manual command below), streaming its output live. `_apply-update.sh` now backs up each instance's current code to `/opt/qbank-backups/<instance>/<timestamp>/` *before* overwriting it — a fast, local, code-only safety net, distinct in purpose from the offsite S3/databank data backups described below.
- **Roll back** any instance to one of its last 10 local code backups — `sudo /usr/local/sbin/qbank-rollback.sh <instance> <timestamp>`.
- **Set threads** — change an instance's gunicorn `--threads` count (e.g. after moving to a more powerful box) without SSH: `sudo /usr/local/sbin/qbank-set-threads.sh <instance> <count>` writes a systemd drop-in and restarts. `--workers` is shown read-only (always 1, with an explanation) — see `spec.md` §16 for why raising it needs a lock redesign first, and why the configured thread count is deliberately never stored in `instances.conf` (it would be silently overwritten on the next Update).
- **View console** — the last 200 `journalctl` lines for an instance's systemd unit, refreshable on demand. No sudo needed for this: the `qbank-admin` system user is a member of the `systemd-journal` group, which grants read-only journal access by default.

Single hardcoded username (`admin`); the password's hash (never the plaintext) lives in `/opt/qbank-admin/.env` as `ADMIN_PASSWORD_HASH`, generated with:
```
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
```
**Update / standalone Restart / Rollback / Set threads all re-prompt for this password** immediately before running, even though the operator is already logged in — a stolen session cookie alone isn't enough to trigger any of the four. This re-checks the same hash `/login` already does (rate-limited separately from the login form), not a literal Unix `sudo` password — see `spec.md` §16 for why. Stop/Start stay unprompted.

Every action is appended to `/var/log/qbank-admin-actions.log` (who/when/what/outcome, including password-rejected attempts) — see `spec.md` for why the rollback/service-ctl scripts validate their own arguments against `instances.conf` rather than following `_apply-update.sh`'s fixed-path-no-arguments rule.

**Backup destinations** (NCMS only as of this writing — CHS doesn't have its own yet):
- Credentials for both backup scripts live in `/opt/qbank/backup/.env` (mode 600, `qbank`-owned, never read by the app itself).
- Extracted data (JSON/markdown/images) → private GitHub repo [`scioly-ncms-test-bank-data`](https://github.com/musdevanathan31-star/scioly-ncms-test-bank-data).
- Bulk data (PDFs/texts/textbooks, and — by choice — images too) → S3 bucket `ncms-files-texts-and-practice-tests` (region `us-east-2`), via `restic`.
- Cron, running as `qbank`: extracted-data backup every 2 hours, bulk backup nightly at 02:00, both logging to `/var/log/qbank-backup.log` — both lines now pass `/opt/qbank/.env` as the trailing argument so they resolve `DATA_ROOT` (see above) instead of looking in the now-empty app directory.

**Known loose ends, kept visible on purpose so they don't get lost**:
- `/etc/sudoers.d/ncms-deploy` (`ncms ALL=(ALL) NOPASSWD: ALL`) — set up for initial interactive bring-up, broader than anything routine needs now that `qbank-deploy` exists. Candidate for removal or narrowing.
- CHS has no backup destination yet (no GitHub repo or S3 bucket created for it), and is still running with a placeholder `ANTHROPIC_API_KEY` in its `.env`.
- The GitHub PAT and AWS access key used to set up NCMS's backups, and the RHEL admin password used during initial bring-up, were all typed in plaintext during setup conversations — worth rotating once things are confirmed stable. The admin app's password is the *same* RHEL admin password, by deliberate choice when it was set up — rotating one should mean rotating the other.
- No IP allowlist in front of `/testbank/admin` — the login is the only gate, also a deliberate choice (so it stays reachable while traveling), revisit if that tradeoff stops making sense.

## Maintaining the server

A periodic checklist, organized by how often each item actually needs doing:

**Every time you push a code change**:
```
git push
sudo -u qbank-deploy bash /opt/qbank-deploy/update-from-github.sh
```
This is the *only* routine action that needs `qbank-deploy`'s narrow sudo grant — if you find yourself reaching for broader sudo access for a routine task, that's a sign something should be added to the update script instead. Equivalently, click **Update from GitHub** in the [admin app](#admin-app) — same script, same backup-before-apply behavior, no SSH needed. The admin app's Stop/Start/Restart buttons and Console view cover most other routine SSH trips too.

**Weekly-ish**:
- Skim `/var/log/qbank-backup.log` for failures.
- `restic snapshots` (after sourcing `/opt/qbank/backup/.env`) to confirm the nightly bulk backup is actually landing, not silently failing.
- Check the databank repo's commit history on GitHub for recent activity matching actual usage.
- Refresh `.scioly_cookies.json` if the header's cookie-expiry badge is showing (cookies last ~7 days) — see ["running on a headless server"](#running-on-a-headless-server-no-x11-no-playwright). Same trip works for dropping in any new question-bank files via `scp`, picked up by each event's [Scan files](#workflow) page.

**Monthly-ish**:
- `sudo dnf update -y` on the box for security patches.
- `df -h` for disk headroom — event data only grows.
- Sanity-check the Let's Encrypt cert hasn't drifted (Caddy renews automatically; this just confirms it's working): `openssl s_client -connect scioly-02864.com:443 -servername scioly-02864.com 2>/dev/null | openssl x509 -noout -dates`.
- Confirm the UCG's Dynamic DNS is still pointing the domain at the actual current public IP — this *did* silently drift once during initial setup (see "Home-network deployment" above); worth not assuming it stays correct forever.

**Quarterly-ish**:
- Do a real `restic restore` of a snapshot to a scratch directory and verify a file — a backup that's never been restored isn't a verified backup.
- Review `auth_users.json` per instance for stale/disabled accounts worth actually removing (`auth.delete_user`, operator CLI only — see [Security hardening](#security-hardening)).
- Review `/etc/sudoers.d/*` for grants broader than currently needed (the `ncms-deploy` item above is the known one).

**As-needed, not scheduled**: rotating the GitHub PAT / AWS keys / restic password is *not* a calendar habit — it's a deliberate one-off. The restic password especially must never be rotated casually: rotating it means re-encrypting or losing access to every existing snapshot, so confirm you don't need anything from the old encryption before touching it.

**When onboarding a new school**: repeat the full pattern in ["Running multiple independent instances"](#running-multiple-independent-instances-on-one-server-eg-two-schools) for the app itself, then create that school's own private GitHub databank repo and S3 bucket and repeat ["Backups"](#backups) — nothing in the backup mechanism is shared between schools by design. Add a line for it to [`deploy/instances.conf`](deploy/instances.conf) so the admin app and `_apply-update.sh` both pick it up immediately — that's the only file to touch for the admin app to start managing it.

### Migrating an instance's data to a new `DATA_ROOT` (one-time, as-needed)

Do this when "`df -h` for disk headroom" above stops being reassuring — moving an instance's data to a bigger/separate disk via [`DATA_ROOT`](#separating-app-code-from-data-data_root). [`deploy/migrate-data-root.sh`](deploy/migrate-data-root.sh) handles the copy/cleanup mechanics; the stop/restart/verify steps around it stay manual, operator-run actions on purpose (consistent with this project's existing manual-over-automatic stance — see the GitHub-update mechanism's own rationale):

1. **Stop the instance**: `sudo systemctl stop qbank` (or whichever unit, per [`deploy/instances.conf`](deploy/instances.conf)) — or use the admin app's Stop button.
2. **Copy, don't move yet**: `sudo deploy/migrate-data-root.sh /opt/qbank/app /mnt/qbank-data qbank:qbank` — discovers every event directory plus `auth_users.json`/`events_custom.json`/`textbooks/` the same way `backup-bulk-data.sh` does, `rsync -a`s each into the new location, and `chown`s it to match. Never touches or deletes the source; safe to re-run if interrupted partway.
3. **Point the instance at it**: add `DATA_ROOT=/mnt/qbank-data` to that instance's `.env`.
4. **Restart and verify**: `sudo systemctl start qbank`, then load the landing page and confirm every event still lists the same question counts as before — this is the real "did it work" check, not just "did the service start."
5. **Only after verifying**: `sudo deploy/migrate-data-root.sh /opt/qbank/app /mnt/qbank-data --cleanup` — re-checks each item's content against the new location (refusing to touch anything that doesn't match byte-for-byte) before removing it from the app directory, to actually reclaim the space. The app itself never does this for you, matching its no-permanent-deletion stance everywhere else; this script is the one deliberate exception, and only once you've told it to.
6. **If you've set up backups for this instance**: add the instance's `.env` as a trailing argument to its two cron lines (see ["Backups"](#backups)) so the backup scripts start resolving `DATA_ROOT` instead of looking in the now-empty app directory.

Repeat per-instance — `DATA_ROOT` is set in each instance's own `.env`, so e.g. NCMS and CHS can have entirely independent data locations (or share one, if you point both at the same mount and their slugs don't collide).

## Security hardening

### Nothing is ever permanently deleted through the app

Every "delete" action in the UI is reversible — including for coaches. The only way to actually free disk space is an operator running a script directly on the server, never through the web app:

- **Reprocess → "wipe annotations" / "manual mode"** snapshots the PDF's annotations, manual-edit tracking, and question list to `<event>/.archive/<pdfname>/<timestamp>.json` (`archive.py`) *before* wiping anything. Restore any snapshot from the Reprocess dropdown's "Snapshot history" entry on the review page.
- **"Remove" an event** (`events.py`'s `archive_custom_event`) sets an `archived` flag — it disappears from the landing page but its directory, PDFs, and state file are never touched. Unarchive it from the landing page's "Show archived events" section.
- **"Remove" a user** (`auth.py`'s `disable_user`) sets a `disabled` flag — blocks login and kicks any active session immediately, but the account and all their work stay intact. Re-enable from Manage Users.
- Single/bulk question delete were already soft (recorded in `annotations[...].deleted`, survive reprocess) — unchanged.
- There's still no route that deletes an uploaded file (test/key/source/textbook PDF) at all — by design.
- Real cleanup, when an operator actually wants it: `python archive.py --purge-snapshots-older-than-days N` removes old snapshot files from disk. Nothing equivalent exists for archived events or disabled users — handle those by hand if truly necessary.

### Upload & request hardening

- Every uploaded PDF is checked for a `%PDF-` magic header before PyMuPDF ever touches it, capped at 2000 pages, and parsed with a 30s timeout (`pdf_safety.py`) — closes off a renamed-non-PDF or a hostile/malformed PDF hanging or OOMing the process. Uploaded `.docx`/`.doc` get the equivalent magic-byte check (`doc_convert.py`) before being handed to LibreOffice for conversion, which itself runs with a 60s timeout under a scratch `$HOME` (so a hung/malformed document can't wedge the conversion or leak across requests via a shared profile dir).
- Uploaded SVG question images have `<script>` tags and `on*=` event-handler attributes stripped before being saved (`_sanitize_svg` in `review_app.py`).
- File-serving routes that build a path from a user-supplied filename (`serve_image`, source `process`/`raw`) go through a containment check (`_safe_join`) so a crafted filename can't resolve outside its intended directory.
- All state-changing requests (`POST`/`PATCH`/`DELETE`) require a matching CSRF double-submit-cookie token, issued on login and auto-attached by the frontend's existing fetch-patch pattern — no per-call-site changes needed.
- `/login` locks out an IP for 15 minutes after 5 failed attempts (in-memory, resets on restart — a real improvement over nothing, not a claim of perfect brute-force resistance).
- The app refuses to start if it looks like a real deployment (`SESSION_COOKIE_SECURE=true`, or `--host` bound to something other than loopback) without `FLASK_SECRET_KEY` set, instead of silently running with a key that gets regenerated — and every session invalidated — on every restart.

## CLI reference

```
python build_question_bank.py --event <slug> [--limit N] [--rebuild] [--text-only]
```

- `--event <slug>` — required. See [Configured events](#configured-events) for valid values.
- `--limit N` — only process the first N test PDFs
- `--rebuild` — ignore caches, clear extracted images, restart from scratch
- `--text-only` — never call the Anthropic API

```
python download_event.py --event <slug> [--no-skip-existing] [--no-bypass-bot] [--reauth]
```

- `--event <slug>` — required. See [Configured events](#configured-events). Accepts case- and underscore-insensitive matches: `--event diseasedetectives` resolves to `disease_detectives` and writes into `disease_detectives/`.
- `--no-skip-existing` — re-download files that already exist
- `--no-bypass-bot` — don't launch Playwright on a bot-challenge response (just fail)
- `--reauth` — discard the saved cookie file and re-run the browser bypass

The review page's empty-PDFs state lets you trigger `download_event.py` from the browser with a live progress bar — no terminal required.

```
python review_app.py [--host 127.0.0.1] [--port 5000]
```

```
python backfill_last_edited.py
```

One-time, idempotent migration that sets `lastEditedBy`/`lastEditedDateTime` on every pre-existing question missing them (added once those fields existed in the schema — see Browse page docs above). Safe to re-run; only touches questions that don't have the field yet.

```
python -m pytest tests/ -q
```

Runs the regression suite covering the extraction/dedup heuristics (`split_choices`, `_strip_points`, `classify_topic`, `_section_suffix`, `qgen.is_duplicate`). Add a case any time you tune one of those functions.

## Adding a new event

You have two routes — pick based on whether you need topic-keyword classification working from day one.

### Route A (recommended for most users): register from the web UI

1. Open the landing page at `http://127.0.0.1:5000/`
2. Expand **+ Register a new event**, fill in slug, display name, **scioly.org event name** (lowercase, with spaces — this also drives the PDF filename prefix by default), optional wiki page, optional initial topics, and **optional rotating foci** (see [Rotating foci](#rotating-foci) below — leave blank for events that don't rotate, like Circuit Lab). The **Filename prefix** field is optional and only needed when scioly.org's PDFs are named differently than the event itself (e.g. Anatomy &amp; Physiology PDFs are `anatomy_*.pdf`, not `anatomyphysiology_*.pdf`).
3. Submit. The event is added to the registry and persisted to `events_custom.json` so it survives restarts. The UI redirects you straight into the event.
4. From there: scrape the wiki, upload PDFs, generate questions, or `python download_event.py --event <slug>` to pull scioly.org tests.

UI-registered events have `topic_keywords = {}` — the keyword scorer will not classify their questions, so every pipeline-extracted question lands in `Other / General` until you re-topic it manually (via the topic dropdown on each question card). Generated questions from `qgen.py` get their topic directly from Haiku, so generation still works fine.

### Route B (recommended when you want auto-classification): edit `events.py`

1. Open `events.py`. Add one `Event(...)` entry to `EVENTS` with:
   - `slug` — directory/url slug (e.g. `"wright_stuff"`)
   - `name` — human display name
   - `event_match` — lowercase substrings used to filter `scioly_tests.json` event names. The first one also drives the PDF filename prefix by default (`"disease detectives"` → `diseasedetectives_*.pdf`).
   - `filename_prefix` — *optional*. Override only when the PDFs on scioly.org are named differently than the event (e.g. Anatomy &amp; Physiology PDFs are `anatomy_*.pdf`).
   - `topics` — your taxonomy tuple
   - `topic_keywords` — `{topic: [(phrase, weight), ...]}` for classification
2. `python download_event.py --event <slug>`
3. `python build_question_bank.py --event <slug>`
4. The review UI auto-discovers it on next page load.

No other code changes needed: the pipeline, the review UI, the annotation/validation system are all event-agnostic.

`spec.md` documents the data shapes, pipeline stages, and the custom-event JSON schema in detail.
