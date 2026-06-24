# How To — Using the Question Bank by Role

A task-oriented guide: "I want to do X" rather than "how does X work." For *why* things work the way they do, see `spec.md`; for setup/deployment, see `README.md`.

## Roles at a glance

- **Coach** — full admin. Sees every event, can manage users and shared textbooks, and can do everything a volunteer can do on every event (not just assigned ones).
- **Volunteer** — sees and can edit only the specific events a coach assigned them. Everything else is hidden from their landing page and returns a 403 on a direct URL.

There's no read-only "student" role yet (see README's "Authentication & roles"). Log in at `/login` with the username/password a coach gave you.

## Account settings (any role)

Click **⚙ Settings** in the header from any page — this is the same page for everyone, it just shows more sections if you're a coach.

- **My Account** — change your **display name** (a friendlier label shown in the header instead of your username — purely cosmetic, your username for logging in never changes) and **change your password** (you'll need to re-enter your current password first; a wrong one is rejected with no change made).
- **LLM API Keys** — optionally supply your own Anthropic/OpenAI/Gemini/DeepSeek/Mistral API key(s) for *this browser only*. Stored in localStorage, never sent to the server except as a request header on this app's own calls — useful if you'd rather use your own billing than the server's shared key, or if the server's key runs out of credits (the app automatically falls back through whichever keys you've set, in that order).
- **Manage Users** — coach-only, see below.

## For Coaches

### First login / bootstrapping a brand-new instance

A fresh instance has no accounts at all, so the very first one has to be created from the command line, not the UI:
```
python auth.py --create-coach
```
This prompts for a username and password and creates the first coach account directly. After that, log in normally and use **⚙ Settings → Manage Users** for everyone else.

### Managing users

From the header, click **⚙ Settings**, then scroll to **Manage Users**. The section lists every account with its role and assigned events.
- **+ Add a user** — expand it, fill in username/password/role, and (for volunteers) which events they can access. Click **Create user**.
- **✎ Edit** on any row — change role or assigned events, then **Save**.
- **⛔ Disable** — blocks that person's login and kicks any session they currently have open, immediately. Nothing about their account or work is deleted — it's fully reversible.
- **↩ Enable** — undoes a disable.
- You can't disable your own account while logged in (the app refuses the request outright).

### Registering a new event

Two ways to add an event — see README's "Adding a new event" for the full tradeoff:
- **From the UI** (no code, works immediately): on the landing page, expand **+ Register a new event**, fill in slug/display name/scioly.org event name/optional wiki page/topics/rotating foci, and click **Create event**. Good for getting started fast; topic auto-classification won't work until you manually topic a few questions, since UI-registered events start with no keyword list.
- **By editing `events.py`** (a code change, needs a redeploy): worth it once you have a topic taxonomy worked out, since it gets keyword-based auto-classification from day one.

To temporarily hide an event without losing anything, use **🗄 Archive** next to it on the landing page — reversible via **Show archived events** → **↩ Unarchive**. Every event, including Circuit Lab and Thermodynamics, can be edited and archived the same way.

### Downloading test PDFs from scioly.org

On an event's main page, click **⬇ Download PDFs from scioly.org** — runs in the background with a live progress bar, no terminal needed. (Equivalent CLI: `python download_event.py --event <slug>`.)

### Uploading your own test PDF

Same event page has a **+ Upload test** button near the top that opens a small form with three slots: the test (required), its answer key (optional), and a figures/supplementary document (optional — for tests that ship their diagrams in a separate file, e.g. a `_sheet`/`_notes` PDF; see "pulling figures from a supplementary document" below). Each slot accepts a PDF, `.docx`, or `.doc` — Word documents are converted to PDF automatically (needs `soffice`/LibreOffice installed on the server; if it isn't, the upload fails with an install hint instead of hanging). The test and key are run through extraction immediately — you'll see questions on the very next page load, no separate step required. The figures file is never extracted; it's just stored for browsing on the review page.

### Onboarding files copied directly onto the server

If you (or a script) `scp` files straight into an event's directory instead of using the upload form or the scioly.org download — e.g. while assembling a question bank from elsewhere — they won't show up anywhere until they're named like everything else. The event's **Scan files** page finds them: a **Ready to process** bucket for already-correctly-named files that were never extracted (one-click **Process all**), a **Needs conversion** bucket for `.docx`/`.doc` files still waiting on PDF conversion, and an **Unrecognized** bucket for anything else, with a small form to onboard each one by role:
- **Test** / **Key** — needs a best-effort year/division guess (always editable) plus a submitter label; renamed in place to match the naming convention.
- **Supplementary** — figures/images for *one specific* test; pick which test it belongs to and a short label (e.g. "sheet"). Becomes browsable on that test's review page via the target toggle.
- **Notes** — reading material for *generating new questions*, the same kind of thing as anything already uploaded on the Sources page. No extra fields — it's moved straight into the event's source list (`.pdf`/`.docx`/`.doc`/`.md`/`.txt` all accepted; Word docs convert to PDF automatically). **Supplementary and Notes are easy to confuse** but serve different purposes: supplementary is *for a test*, notes is *for the LLM*.

This is a manual "Refresh" page, not a background watcher — revisit it after dropping in new files. The landing page also shows a small "N unrecognized" badge next to any event that has files waiting here.

### Reviewing a PDF page-by-page

Click a PDF's name from the event page (or **Review by PDF**) to open the review page — the PDF on one side, extracted question cards on the other. From here you can:
- **Pull figures from a supplementary document** — if a sheet/notes/figures file was uploaded alongside this test (or discovered already sitting next to it), a toggle button for it appears next to **Test PDF** / **Key PDF**. Switch to it and use **📌 Pick image** (or any other capture tool) against it exactly like the test PDF — useful when a test's diagrams live in a separate file the extraction pipeline doesn't automatically associate with questions.
- **Drag a rectangle** on the PDF and use the field buttons (**Stem**, **Choices**, **Math → Stem**, **Math → Answer**) to capture text or convert an equation to LaTeX directly into a field.
- **+ Add question from region** — drag once over an unextracted question; it gets the next free number automatically, with multiple-choice options auto-split into the choices list if present.
- **+ Add context from region** — for a shared passage/table/intro that several questions reference; the captured text becomes a context block other questions can link to.
- **+ Add blank** — an empty card to fill in by hand.
- Reassign a figure to a different question by clicking the image, then clicking the target card.
- **✓ Validate answer** (per question) or **✓ Validate page** (everything on the current page) — sends the question to Haiku and stores a verdict + rationale.
- **Mark a verdict yourself, no LLM call** — the small dropdown next to each question's validation status ((unset) / ✓ Correct / ⚠ Incorrect / ? Uncertain) lets you set or override it directly, instantly, free. Whichever happens most recently wins — re-running AI Validate can overwrite your manual verdict, and you can always override a stale or wrong AI one back. Same dropdown as the Browse page already has, just available here too now.
- **🤖 Generate diagram** — opens a small chat with Claude Sonnet seeded with the question's stem/topic; each reply renders an SVG you can save and attach with one click.
- **Reprocess ▾** — re-runs extraction. The default mode keeps your annotations; "wipe annotations" and "manual mode" discard them but snapshot first (see **🕘 Snapshot history** to restore any prior state — nothing here is ever truly lost).
- **💾 Save** (or Ctrl+S) persists everything to `.qbank_state.json`. **↶ Undo** (or Ctrl+Z) reverts the last destructive action.

### Browsing, searching, and bulk-editing the whole bank

**Browse questions** (from any event's page) is the event-wide view: every question, across every PDF/source, on one filterable/sortable page.
- Filter by topic, focus, source, bucket, validation status, MCQ vs. FRQ, has-image; the search box is hotkeyed to `/`.
- **Every card is directly editable** — topic, focus, stem, choices, and answer are live fields right on the card; edits autosave about 600ms after you stop typing, no Save button. **↺ Undo** reverts a card's last autosaved batch of edits.
- **🤖 AI Validate** persists a Haiku verdict immediately; the **Validation** dropdown next to it lets you set or override the status yourself — whichever happens most recently wins, so you can always correct a wrong AI verdict (or a stale human one).
- **✨ Generate similar** / **🤖 Generate diagram** are available per-card too, seeded from that specific question.
- Select questions with the checkboxes, then use the selection bar: **Compare** (side-by-side), or **Delete** (removes them from their buckets — reversible, recorded as an annotation, replays correctly on reprocess, exactly like every other delete in this app, regardless of what its tooltip currently says).
- **Export ▾** — CSV/JSON/Markdown/Anki deck/printable PDF, either the whole bank or just your current filtered set.

### Pulling practice questions from scio.ly

On an event's **Generate** page (linked from the event's main page), the **scio.ly/practice** panel lets you one-click-scrape public practice questions. Toggle **Validate with Haiku** to flag incomplete/unanswerable ones automatically, then use the quick-filter buttons (**Keep only ✓ Correct**, **Drop ⚠/?**, **Keep all**, **Drop all**) before **Accept kept & save**. Duplicates (exact UUID and fuzzy text match against your *entire* bank) are auto-rejected and shown separately for inspection.

### Generating new questions from a wiki page or uploaded source

Same **Generate** page:
1. **Scrape Sci-Oly wiki** pulls the event's scioly.org wiki page into clean markdown, or **Upload** your own PDF/text source into the event's source list (PDFs need a follow-up **Process** step to convert to markdown).
2. Pick the source, choose a count and question type(s), click **Generate**. Watch the progress panel; **Cancel** aborts an in-flight request.
3. Review each candidate — duplicates against your whole bank are auto-rejected and listed separately — then **Keep**/**Drop** individually or **Accept all kept**.

### Managing shared textbooks

The **Shared textbooks** panel (same Generate page, any event) is for material useful across *multiple* events. Upload once; it's available from every event's Generate dropdown, split by chapter. **Detect chapters** tries the PDF's own bookmarks first, then a heading-text scan; if neither finds anything, **Set chapters manually** lets you type `Title, start page` one per line. Re-run detection any time, e.g. after replacing the file with a cleaner scan.

### Importing questions from another LLM or a hand-written JSON file

Below the Generate panel: paste JSON or upload a `.json` file. Accepts the same shape `qgen.py` produces. Malformed JSON (common breakages like unescaped LaTeX or literal quotes in strings) is auto-repaired server-side where possible. Runs through the same dedup as Generate. The **Mark all as validated** checkbox skips the usual validation step — use it only when you already trust the source.

**Drafting in ChatGPT/Gemini/Claude.ai instead of this app's Generate panel?** Paste this as your first message (system prompt), then send your source material and how many questions you want — the reply pastes straight into the Import panel:

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

Fields outside this list (e.g. a difficulty rating) are silently dropped on import, not stored — see README's "Drafting questions in another LLM" for the full rationale. If you want difficulty tracked, fold it into `rationale` or `source_snippet` as free text instead.

### Taking or building a practice quiz

Click **Quiz** from an event's page, set your filters (topic/count/etc.), and **▶ Start quiz**. **Skip**/**Submit**/**Next →** move through it; **↺ Another quiz** repeats with the same settings.

### What only a coach can do, at a glance

| Action | Coach | Volunteer |
|---|---|---|
| View/edit an event they're assigned to | ✅ | ✅ |
| View/edit an event they're *not* assigned to | ✅ (all events, implicitly) | ❌ (hidden + 403) |
| Create/edit/archive events | ✅ | ❌ |
| Manage users | ✅ | ❌ |
| Upload/edit shared textbooks | ✅ | view/use only |
| Generate questions, scrape scio.ly, review PDFs, browse/export, quizzes | ✅ | ✅, for assigned events only |

## For Volunteers

### Logging in for the first time

A coach creates your account and tells you the username/password (and which events you can access) — there's no self-registration. Log in at `/login`.

### What you'll see

Only the events a coach assigned you. Everything else doesn't appear on your landing page, and typing its URL directly returns a 403, not an error page that reveals it exists.

### Reviewing and editing questions in your assigned events

Identical to the coach workflow above for **Review by PDF** — drag-capture, image reassignment, math capture, reprocess, snapshot/restore. The only difference is scope: you only see PDFs for events you've been assigned.

### Browsing, searching, and editing within your assigned events

Identical to the coach's **Browse questions** workflow — inline-editable cards, AI/manual validation, compare, export, bulk delete (reversible) — scoped to your assigned events.

### Generating or scraping new questions for your assigned events

Identical to the coach's Generate page: wiki scrape, source upload, LLM generation, scio.ly scraping, JSON import. Shared textbooks are visible and usable (any event can read them), but uploading a *new* shared textbook is coach-only.

### Taking a practice quiz

Same as the coach workflow — **Quiz** from any of your assigned events.

### What you can't do, and why

| Action | Why not |
|---|---|
| See or open an unassigned event | `_select_event` (the access chokepoint every `/event/<slug>/...` route calls first) returns 403 for volunteers outside their assigned list — see `spec.md` §9 |
| Manage users (Settings → Manage Users) | Gated by `@coach_required` |
| Create/edit/archive events | Gated by `@coach_required` |
| Upload a new shared textbook | Write routes gated by `@coach_required`; reading/using existing ones is open to everyone |

## Quick task index

| Task | Who | Where |
|---|---|---|
| Bootstrap the very first account | operator (CLI) | `python auth.py --create-coach` |
| Change your password or display name | Coach, Volunteer | ⚙ Settings → My Account |
| Set your own LLM API key | Coach, Volunteer | ⚙ Settings → LLM API Keys |
| Create/disable a user | Coach | ⚙ Settings → Manage Users |
| Register a new event | Coach | Landing page → + Register a new event |
| Download scioly.org PDFs | Coach, Volunteer (assigned events) | Event page → ⬇ Download PDFs |
| Upload a test PDF (+ key, + figures) | Coach, Volunteer (assigned events) | Event page → + Upload test |
| Onboard files dropped in via scp (test/key/supplementary/notes) | Coach, Volunteer (assigned events) | Event page → Scan files |
| Review/edit one PDF's questions | Coach, Volunteer (assigned events) | Event page → click a PDF / Review by PDF |
| Pull figures from a supplementary doc | Coach, Volunteer (assigned events) | Review page → target toggle next to Test PDF/Key PDF |
| Browse/search/bulk-edit the whole bank | Coach, Volunteer (assigned events) | Browse questions |
| Validate an answer with AI | Coach, Volunteer (assigned events) | Review or Browse page → 🤖 AI Validate / ✓ Validate |
| Mark a verdict yourself, no LLM call | Coach, Volunteer (assigned events) | Review or Browse page → validation dropdown |
| Scrape scio.ly practice questions | Coach, Volunteer (assigned events) | Generate page → scio.ly panel |
| Generate questions from a source | Coach, Volunteer (assigned events) | Generate page |
| Upload a shared textbook | Coach only | Generate page → Shared textbooks |
| Import questions from JSON | Coach, Volunteer (assigned events) | Generate page → Import panel |
| Export the bank | Coach, Volunteer (assigned events) | Browse page → Export ▾ |
| Take a quiz | Coach, Volunteer (assigned events) | Quiz |
| Archive/unarchive an event | Coach only | Landing page |
| Restore a wiped reprocess | Coach, Volunteer (assigned events) | Review page → 🕘 Snapshot history |
