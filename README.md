# NCMS Sci-Oly Question Banks

A Science Olympiad test-prep tool. It downloads past test PDFs from scioly.org, extracts questions topic-by-topic, attaches diagrams to the right question, validates answers with an LLM, and produces a browsable markdown question bank per event. A Flask review UI lets you correct anything the pipeline got wrong; corrections persist through re-runs.

Adding another event is a single entry in `events.py` (see [Adding a new event](#adding-a-new-event)).

> **Desktop browsers only.** The web UI is built for desktop-sized viewports (≥ 1200 px). Mobile and tablet layouts are explicitly out of scope; the review page assumes the PDF-pane / question-pane split, drag-to-capture uses a mouse, and the browse page sidebar is always visible. Use Chrome / Edge / Firefox / Safari on a real computer.

## Configured events

| `--event <slug>` | Display name | Source-filename prefix | Topic taxonomy |
|---|---|---|---|
| `circuit_lab` | Circuit Lab | `circuitlab_*.pdf` | 11 topics (Ohm's Law, Series & Parallel, Kirchhoff, RLC, semiconductors, …) |
| `thermodynamics` | Thermodynamics | `thermodynamics_*.pdf` | 12 topics (Heat Transfer, Gas Laws, Carnot Engine, Entropy, …) |

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

## Files

| File | Purpose |
|---|---|
| `events.py` | Event registry: per-event topics, keywords, paths, filename prefix, wiki page |
| `download_event.py` | Generic PDF downloader; takes `--event <slug>` (required). Auto-bypasses scioly.org's Anubis bot challenge via Playwright. |
| `build_question_bank.py` | The extraction pipeline (CLI). Run after downloading. `--event <slug>` (required). |
| `texts.py` | Scrapes the scioly.org wiki for an event into markdown; converts user-supplied source PDFs to markdown |
| `qgen.py` | LLM (Haiku) question generation from source texts; Jaccard-based dedup against the existing bank |
| `scrape_scioly.py` | Pulls public questions from scio.ly/practice's JSON API; normalizes them into the canonical Question shape |
| `review_app.py` | Flask review UI — single server, multi-event, with a Generate page (sources + LLM generation) per event |
| `templates/*.html` | Jinja2 page templates for the review UI (`events`, `event_index`, `browse`, `review`, `sources`, `quiz`) |
| `text_utils.py` | Shared text-normalization helpers (`strip_points`) used by the pipeline, scraper, and generator without an import cycle |
| `scioly_tests.json` | Pre-scraped metadata for **all** Science Olympiad tests, 887 entries |
| `.env.example` | Template for the Anthropic API key |
| `.scioly_cookies.json` | Cached Anubis cookies (auto-managed; delete to force a fresh bot-bypass) |
| `events_custom.json` | Auto-generated registry of events created via the web UI (built-ins stay in `events.py`) |
| `TODO_brittleness.md` | Self-audit of fragility / brittleness in the codebase with a suggested refactor ordering |
| `TODO_ux.md` | Self-audit of UX rough edges with suggestions grouped by effort (⚡/🛠/🏗) |
| `spec.md` | Technical spec — data shapes, pipeline stages, annotation/validation/generation schemas |

## Workflow

1. **Download** — `download_event.py --event <slug>` pulls every test and key PDF for the chosen event from scioly.org. It uses a Chrome User-Agent and auto-bypasses Anubis bot protection via Playwright on the first challenge (saving cookies to `.scioly_cookies.json` for subsequent runs). Already have a test on disk? The event index page (`/event/<slug>/`) has an **upload test PDF + key** form — drop in a test (and optionally its key), and it's saved with the correct `_test.pdf`/`_key.pdf` naming and run through the extraction pipeline immediately, no separate Reprocess step needed.

2. **Extract** — `build_question_bank.py` runs three stages per PDF:
   - PyMuPDF text extraction → topic-classified questions, choices, answers
   - Spatial image association → each circuit diagram is attached to the question it sits closest to on the page
   - (Optional) Haiku vision pass to OCR image-only PDFs and to refine image-to-question assignments when the heuristic is ambiguous

3. **Browse** (`/event/<slug>/browse`) — an event-wide question explorer that aggregates every question across every bucket (PDF-extracted, LLM-generated, scio.ly-scraped) into one searchable, filterable view. Filter by topic, focus, source, **bucket**, validation status, MCQ-vs-FRQ, has-image; sort by Q#, topic, source, length, or validation. Filter state is persisted in the URL, so reload and back-button work and links are shareable. Search box is hotkeyed to `/`; image lightbox on click; sidebar shows live counts of topics/sources/validation.

   **Every card is directly editable, no Edit click required** — Topic, Focus, Stem, Choices, and Answer are live fields right on the card (topic select keeps the deterministic colour-hash background so it's still recognisable at a glance). Changes autosave ~600ms after you stop typing (no Save button) and a small status line shows "Saving…" → "Saved HH:MM:SS". An **"↺ Undo"** button appears after each autosave and reverts that question's fields back to what they were just before your most recent batch of edits (single-level — a *new* edit replaces the undo point). Every card also shows **"Last edited by `<user>` · `<timestamp>`"**, stamped server-side from the logged-in session on every save (never client-supplied).

   **Validation, both AI and manual** — an **"🤖 AI Validate"** button calls the same Haiku check used elsewhere and persists the verdict immediately (Browse previously couldn't persist this at all). A **Validation** dropdown next to it (Correct/Incorrect/Uncertain/unset) lets a human set or override the status directly — whichever happens most recently simply wins, so a human can always correct a stale or wrong AI verdict, and re-running AI Validate can likewise override a human's. The badge shows who set it (`(ai)`/`(human)`).

   PDF-sourced questions keep a persistent **"Open source ↗"** link to jump to that PDF's review page with the question pre-focused (absent for LLM-generated/scio.ly-imported questions, which have no source PDF).

4. **Review** (`review_app.py`) — a browser UI shows each PDF page next to its extracted questions. You can:
   - **Drag a rectangle on the PDF** to capture stem, choices, or answer text directly from the source
   - **+ Add question from region** — drag once; the next available Q# is auto-assigned, MC options are auto-split into the choices list when present
   - **Capture math** — drag around an equation; Haiku converts it to LaTeX (`$...$`) and inserts at your cursor. KaTeX live-preview shows the rendered formula as you type.
   - **Reassign images** by click-then-click (click image → click target question card)
   - **Edit anything** inline — number, text, topic (with create-new), choices, answer
   - **Validate answers** with Haiku per-question or per-page; verdict + rationale + primary source is stored next to each answer
   - **Reprocess** a PDF to apply pipeline improvements; your annotations are replayed on top (hold Shift to wipe instead)
   - **Attach images** to a question — 📁 Upload a file (PNG/JPG/SVG/WebP) from disk, OR 🤖 Generate diagram via a small Claude Sonnet chat. The chat is seeded with the question stem + topic + any author-provided diagram description (third-party LLM imports can carry an `image_description` field that lights up as a "Diagram recommended:" note until you fulfil it). Each assistant turn renders the SVG live; one click saves it into the event's `images/` dir and attaches it to the question with an optional textual description. The description doubles as alt-text and as the seed for the *next* diagram-chat session if you re-open it.

5. **Generate** (per-event `/event/<slug>/sources` page) — pull in third-party questions and feed the LLM new material so it can write fresh questions:

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

   ### Sources & LLM generation
   - **Scrape Sci-Oly wiki** — one button. Uses the saved scioly.org cookies, converts the wiki HTML into clean markdown at `<event>/texts/wiki.md`
   - **Upload PDFs** — drop your own textbook chapters, study guides, or notes into `<event>/texts/` (via the UI or by hand). A *Process → MD* button converts each PDF to markdown.
   - **Generate** — pick a source, pick count + types (multiple choice / short answer / numerical), click Generate. Haiku drafts candidates with rationale and a quoted source snippet. The button is disabled and a progress panel with an elapsed-time counter is shown while the LLM is working; a **Cancel** button aborts in-flight. Output token budget scales with `n` (~550 tokens per question) so larger requests don't get silently truncated.
   - **Dedup** — every candidate is compared (Jaccard on word-3-grams, threshold 0.4) against every question already in the bank; near-duplicates are auto-rejected and listed separately.
   - **Keep / Drop** — review each candidate, accept individually or *Accept all kept*. Accepted questions go into a synthetic PDF bucket `_generated_<event>.pdf` so they're easy to identify, and they carry the LLM's rationale + source snippet in their `validation` field.
   - **Failure surfacing** — if Haiku returns malformed JSON or hits `stop_reason: max_tokens`, the UI shows the raw response inline instead of failing silently. Network/HTTP errors and parse errors get a status-bar message; warnings (e.g. truncation) are rendered as a banner above the candidates.

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
  - `contexts` — shared context blocks (passages/intros/tables that several questions reference): `[{id, text, images:[], pages:[]}]`. A question opts in via `context_id`; the review UI then renders the context above the stem and edits to the shared text flow through to every linked question. Contexts can span pages just like questions.

Writes are atomic (tempfile + `os.replace`) and serialised by a per-event lock, so concurrent saves never leave a half-written JSON on disk.

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

## Authentication & roles

The app supports two roles, stored in `auth_users.json` (`auth.py` — same flat-JSON-file pattern as `events_custom.json`, gitignored, never committed):

- **Coach** — full admin. Every event, plus user management (`/admin/users`) and shared-textbook uploads.
- **Volunteer** — edit access only to the specific events a coach assigns them. Unassigned events are hidden from their landing page and 403 on direct URL.

A **student** role (read-only) is intentionally not built yet — the data model (a `role` string + a per-user `events` list) is generic enough to add one later without rework.

**First-time setup** — a fresh `auth_users.json` has no accounts, so there's no one who could use the in-app admin UI yet. Bootstrap the first coach from the CLI:

```
python auth.py --create-coach
```

After that, log in and use **Manage users** (coach-only, linked from the header) to create volunteer accounts and check which events each one can access.

Sessions are plain signed Flask cookies — set `FLASK_SECRET_KEY` (a random 32+ byte hex string) in `.env` for production so logins survive a restart; without it the app generates a throwaway key per process start and everyone gets logged out. Once served over HTTPS, also set `SESSION_COOKIE_SECURE=true` so the session cookie requires TLS.

## Deploying (shared, multi-user instance)

For a small team sharing one workspace, bare metal + a reverse proxy is enough — no Docker needed:

```
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# (requirements-dev.txt's playwright is only needed if you'll run
#  download_event.py --reauth from this machine)
venv/bin/python auth.py --create-coach
```

Then install [`deploy/qbank.service`](deploy/qbank.service) as a systemd unit (`gunicorn`, bound to `127.0.0.1:5000`) and [`deploy/Caddyfile`](deploy/Caddyfile) as a reverse proxy in front of it — both files have install steps in their header comments.

**`--workers 1` is load-bearing** — `build_question_bank.py`'s per-event state lock is an in-process `threading.RLock`, which only serialises writes correctly within a single worker process. `--threads 8` gives real request concurrency for the I/O-bound LLM calls without that risk. Don't raise `--workers` above 1 without first replacing that lock with a file-based one (each worker process gets its own copy of an in-process lock, so it would silently stop protecting anything).

Redeploy flow: `git pull && venv/bin/pip install -r requirements.txt && sudo systemctl restart qbank`.

### Subpath mounting (e.g. `/testbank/ncms` instead of the domain root)

If the reverse proxy doesn't own the whole domain, set `APPLICATION_ROOT=/testbank/ncms` (or whatever prefix) in `.env`. `review_app.py`'s `_PrefixMiddleware` strips that prefix into `SCRIPT_NAME` so `url_for()`/`request.script_root` produce correctly-prefixed URLs, and every template's JS already reads the `APP_ROOT` global (injected by `_user_badge.html`) to prefix its `fetch()` calls — no further app changes needed. The Caddyfile's `handle /testbank/ncms* { reverse_proxy ... }` block must forward the path **unstripped** (`handle`, not `handle_path`) to match what the middleware expects.

### Home-network deployment (dynamic IP, no static cloud IP)

Running behind a residential router instead of a cloud VM needs a few extra pieces beyond `deploy/Caddyfile`/`deploy/qbank.service`:

- DHCP-reserve the box's LAN IP on the router, then forward WAN ports 80 and 443 to it. Caddy's automatic Let's Encrypt HTTP-01 challenge needs port 80 reachable from the internet, not just 443.
- If the public IP isn't static, run `ddclient` (or an equivalent script) on the box to keep the domain's DNS A record pointed at the current IP — the exact config depends on whoever hosts the domain's DNS.
- Don't forward SSH to the WAN — administer the box over the LAN (or a VPN back into the LAN) instead.

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

- Every uploaded PDF is checked for a `%PDF-` magic header before PyMuPDF ever touches it, capped at 2000 pages, and parsed with a 30s timeout (`pdf_safety.py`) — closes off a renamed-non-PDF or a hostile/malformed PDF hanging or OOMing the process.
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
