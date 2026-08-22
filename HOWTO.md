# How To — Using the Question Bank by Role

A task-oriented guide: "I want to do X" rather than "how does X work." For *why* things work the way they do, see `spec.md`; for setup/deployment, see `README.md`.

## Roles at a glance

- **Coach** — full admin. Sees every event, can manage users and shared textbooks, runs Club Management and the Assessments dashboard, and can do everything a volunteer can do on every event (not just assigned ones).
- **Volunteer** — sees and can edit only the specific events a coach assigned them. Everything else is hidden from their landing page and returns a 403 on a direct URL. May also be assigned to prepare/grade a season assessment for an event — a separate grant, unrelated to event access (see "For Volunteers" below).
- **Student** — no question-bank access at all, not even read-only. Scoped to `/my-assessments` (take a live assessment, see released results for past ones) and `/scores` (see everyone's named scores). Logging in takes you straight to My Assessments, since there's no bank to land on. See "For Students" below.

Log in at `/login` with the username/password a coach gave you. Where you land depends on your role: coaches and volunteers arrive on the **Assessments** dashboard — during a season that's the recurring job, while curating the question bank is off-season work — and students arrive on **My Assessments**. The event list is still one click away under **☰ → Events**, and following a link straight to a page takes you there instead.

## Getting around (any role)

Click **☰** at the far right of the header on any page (it's pinned there on every page you can reach as a logged-in user, except the test-taking page itself; it replaces the per-page back arrow that used to sit on the left) to open the navigation menu — it's the one place to reach every major section, scoped to what your role can actually access: **Events** jumps to the landing page (hidden for students — they have no bank to manage); **Test bank** / **Question bank** / **Primary sources** expand to a list of your events (click one to jump straight in — empty/absent for students, who have none); **Jobs**, **Club**, and **Assessments** are coach/volunteer destinations; **Scores** is open to everyone, including students; **Notifications** shows recent toast messages; and your identity line, **Settings**, and **Logout** live at the bottom of the same menu. A student's menu is correspondingly short: My Assessments, Scores, Notifications, Settings, Logout.

## Account settings (any role)

Open the **☰** navigation menu and click **Settings** — this is the same page for everyone, it just shows more sections if you're a coach.

- **My Account** — change your **display name** (a friendlier label shown in the navigation menu instead of your username — purely cosmetic, your username for logging in never changes) and **change your password** (you'll need to re-enter your current password first; a wrong one is rejected with no change made).
- **LLM API Keys** — optionally supply your own Anthropic/OpenAI/Gemini/DeepSeek/Mistral API key(s) for *this browser only*. Stored in localStorage, never sent to the server except as a request header on this app's own calls — useful if you'd rather use your own billing than the server's shared key, or if the server's key runs out of credits (the app automatically falls back through whichever keys you've set, in that order). Coaches and volunteers with a key set here see a running cost badge in the navigation menu; everyone else doesn't, since they can't have caused any personal-key spend.

**Manage Users** is coach-only and lives on the **Club Management** page now, not Settings — see below.

## For Coaches

### First login / bootstrapping a brand-new instance

A fresh instance has no accounts at all, so the very first one has to be created from the command line, not the UI:
```
python auth.py --create-coach
```
This prompts for a username and password and creates the first coach account directly. After that, log in normally and use **Club Management → Manage Users** for everyone else.

### Managing users

Open **Club** from the navigation menu, then scroll to **Manage Users**. The section lists every account with its role and assigned events.
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

### Previewing a PDF without opening the review page

On the event's PDF list, click **👁 Preview** on any row to slide in a panel on the right showing that PDF — its own Test/Key/sheet toggle, page nav, and zoom, all without leaving the list. Handy for a quick glance (e.g. confirming which file is actually the test before deciding whether you need ⇄ Swap) when you don't need the full review page.

### Onboarding files copied directly onto the server

If you (or a script) `scp` files straight into an event's directory instead of using the upload form or the scioly.org download — e.g. while assembling a question bank from elsewhere — they won't show up anywhere until they're named like everything else. The event's **Scan files** page finds them: a **Ready to process** bucket for already-correctly-named files that were never extracted (one-click **Process all**), a **Needs conversion** bucket for `.docx`/`.doc` files still waiting on PDF conversion, and an **Unrecognized** bucket for anything else, with a small form to onboard each one by role:
- **Test** / **Key** — needs a best-effort year/division guess (always editable) plus a submitter label; renamed in place to match the naming convention.
- **Supplementary** — figures/images for *one specific* test; pick which test it belongs to and a short label (e.g. "sheet"). Becomes browsable on that test's review page via the target toggle.
- **Notes** — reading material for *generating new questions*, the same kind of thing as anything already uploaded on the Sources page. No extra fields — it's moved straight into the event's source list (`.pdf`/`.docx`/`.doc`/`.md`/`.txt` all accepted; Word docs convert to PDF automatically). **Supplementary and Notes are easy to confuse** but serve different purposes: supplementary is *for a test*, notes is *for the LLM*.

This is a manual "Refresh" page, not a background watcher — revisit it after dropping in new files. The landing page also shows a small "N unrecognized" badge next to any event that has files waiting here.

### Reviewing a PDF page-by-page

Click a PDF's name from the event page (or **Review by PDF**) to open the review page — the PDF on one side, extracted question cards on the other. From here you can:
- **Tournament / Year** — two small editable fields in the header, next to the PDF name. They start out pre-filled with a guess from the filename; correct them and the fix applies immediately to every question already extracted from this PDF (and survives a future Reprocess) — useful when the filename's source slug isn't a real tournament name, or the year was wrong.
- **⇄ Swap test/key** — if the test and key files got named backwards (a common upload mistake), this trades their names so extraction reads the right one. Any already-extracted questions for this PDF are snapshotted then cleared (you'll need to click Reprocess afterward) — also available from the event page's PDF list next to a row with a key file.
- **Pull figures from a supplementary document** — if a sheet/notes/figures file was uploaded alongside this test (or discovered already sitting next to it), a toggle button for it appears next to **Test PDF** / **Key PDF**. Switch to it and use **📌 Pick image** (or any other capture tool) against it exactly like the test PDF — useful when a test's diagrams live in a separate file the extraction pipeline doesn't automatically associate with questions.
- **Drag a rectangle** on the PDF and use the field buttons (**Stem**, **Choices**, **Math → Stem**, **Math → Answer**) to capture text or convert an equation to LaTeX directly into a field.
- **+ Add question from region** — drag once over an unextracted question; it gets the next free number automatically, with multiple-choice options auto-split into the choices list if present.
- **+ Add matching question** — for a "match each term to its definition" table the automatic extraction missed or mis-split: drag the left column, then the right column (it auto-advances, no second click needed). You get an editable two-column card — fix up any row, attach an image to a cell the same way you'd reassign any other figure, and set the correct A→B pairs in the dropdown list at the bottom. The pipeline also detects these tables on its own when processing a PDF now (previously the whole table landed as one unstructured question); this button is for fixing one up or building one from scratch. Wrapped multi-line entries, either-charset labels (numbers or letters, on either column), and leading answer-blank placeholders ("____") are all handled automatically during capture. If a table continues onto another page, each column header on the card has a 📋 **Capture more from PDF** button — navigate to that page and drag the continuation; it appends to the existing column instead of starting a new question.
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
- Filter by topic, focus, source, bucket, validation status, question type (MCQ / FRQ / Matching), has-image; the search box is hotkeyed to `/`.
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

**Refreshing scioly.org cookies without scp** (coach-only) — next to the **Scrape Sci-Oly wiki** button, a badge shows the bot-bypass cookie's freshness. If it's expired or about to be, you don't need to scp a Playwright-exported file from another machine: visit scioly.org in your own browser, solve the challenge there, open devtools and run `document.cookie`, then paste the result into the textbox and click **Save cookie**.

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

Click **Quiz** from an event's page, set your filters (topic/count/type/etc. — "Matching only" is one of the type options), and **▶ Start quiz**. **Skip**/**Submit**/**Next →** move through it; **↺ Another quiz** repeats with the same settings.

A matching question shows a dropdown next to each left-column item listing every right-column label, with the right column displayed alongside so you can see every option before picking. It's graded with **partial credit** — getting 3 of 5 pairs right adds 0.6 to your running score, not all-or-nothing — and the feedback/mistake-review screens show exactly which pairs you got right or wrong.

### Set up a new season

A season groups events, students, and tests under one label (e.g. "2027"). Open **☰ → Club**.
1. Expand **+ New season** — pick a `season_id` (e.g. `2027`), an optional label, and check off which events run this season (its "lineup"). Click **Create**. If this is the very first season this instance has ever had, it's automatically marked current — no extra step needed. A second or later season is **not** auto-switched, so you can stage next year's season ahead of time without disrupting the live one.
2. If it isn't already current, click **Mark as current** on it — exactly one season is ever current, and that's what "My Assessments" defaults to for students. If you skip this, a yellow banner appears on this page and on the Assessments dashboard ("⚠ No season is marked current…" or "⚠ You're viewing X, but Y is the current season…") — students won't see any tests until you fix it.
3. Add students: either one-by-one via **Manage Users** on this same Club Management page (role = Student), or in bulk — expand **+ Bulk-add students from CSV**, download the template, fill in `display_name` (required), and optionally `username`/`password`/`events` per row. Leave `username` blank to auto-generate one from the name; leave `password` blank to auto-generate `{school}{season}{username}` (the student changes it after first login via Settings); `events` is a `;`-separated list of event slugs to roster them onto immediately. Upload — the results table shows every generated username/password once, plus any row that failed and why.
4. On the roster grid below, check students into the season's events (or fix up anything the CSV didn't cover). This roster is what scopes "My Assessments" and the Scores page for each student — it has no effect on who can edit that event's question bank.
5. Running a new season off an old one's roster? Pick the prior season from **Copy roster from…** and click Copy — only events present in both seasons' lineups copy over, and any since-disabled student is silently skipped.

Note: a season's event lineup only scopes the roster grid and which events a assessment window can target. It never restricts question-bank access — any volunteer/coach with `User.events` access (or coach status) can still browse/edit any event's bank regardless of the current season's lineup.

### Prepare and publish a test

The Assessments dashboard's **⬇ Test** and **⬇ Key** buttons download the paper version. You get a PDF with any question figures embedded; if the server doesn't have `reportlab` installed you get markdown instead, which is the same content without the images. Add `.md` to the URL if you specifically want the text to edit.

On the **Tests** dashboard, pick the season, then:
1. Expand **+ New assessment window** — give it a label, opens/closes datetime (pre-filled to next Wednesday 1:30–2:30 PM as a convenience default; stretch `closes_at` onto a later day for a multi-day window), and check off which of the season's events are tested in this window. Create.
2. For each event row, click the **Assign…** button to open a picker — it lists every coach plus every volunteer who has bank-edit access to that specific event, check off who should prepare it, and **Save**. Only people with bank access to that event (or any coach) are offered here.
3. That coach or volunteer clicks **Prepare** on the row — opens the test builder: filter/search the validated question pool exactly like Browse, check questions to add them to the **Kept** list (persists across re-filtering), or click **🎲 Select N at random** to pull random *validated-correct* questions, repeatable to top up the kept set. Set a max-points value on any FRQ row (MCQ/matching default to 1 pt). Autosaves as you go.
4. When the kept set looks right, click **Publish** — this freezes (snapshots) the exact question content into the test, so later bank edits/deletions never change a test that's already been prepared.
5. Back on the Assessments dashboard, the row now shows "published." Click **Go live** to make it visible to rostered students as upcoming/current (they still can't see questions until the window opens). Need to fix something after going live? **Un-publish** reverts it to "preparing" — only works before the window opens and before any student has saved an answer.

### Run a assessment window and grade results

The **Grade** page shows each question's **expected answer** in a boxed panel above the student responses, so you can mark without opening the bank in another tab. If no answer was ever recorded for that question it says so explicitly rather than showing a blank. Each free-response answer has a **✓ Full** button next to its score box — one click awards the maximum points for that question and saves immediately, instead of typing the number in. It greys out once that answer is already at full marks, so you can see at a glance which ones you've already given full credit. Type in the box instead for partial credit.

Once a test is live and its window opens, rostered students see it as "Current" on **My Assessments** and can take it (one question at a time, no correctness feedback, countdown to close). After the window closes (or sooner):
1. Click **Grade** on the test's row (Assessments dashboard) — lists every free-response answer needing a score, with the snapshotted reference answer alongside each student's submission. Enter points (capped at that question's max) per answer; autosaves on blur.
2. The row's "N/M FRQs graded" badge tracks progress; **Release grades** stays disabled until every FRQ for every submitted response is graded.
3. Click **Release grades** — this is the one truly coach-only step. It flips every student's response to released in one batch; only after this do students see their results on My Assessments, and the test's column on **Scores** shows real numbers instead of "pending release."

### Grant a student a makeup window

On the **Assessments** dashboard, a live assessment has **+ Makeup window**. Type any part of a name to filter the list — it searches both the display name and the username the CSV import generated, which are often different — then tick **as many students as you need** and set one window for all of them. Only students rostered onto that event for this season are offered, since a makeup window means nothing for anyone else.

Every granted window is listed under its assessment on the same page, with the times, the reason, who granted it, and a **Revoke** button. A personal window is an independent clock, not an extension of the class window, which is why the actual times are shown rather than just a count.

If a student missed the class-wide window (absence, tech issue, etc.), you can give them an independent open/close window instead of touching the test for everyone else:
1. On the Assessments dashboard, find the live assessment's row and click **+ Makeup window**.
2. Enter the student's username, an opens/closes datetime, and a short reason. Click **Grant**.
3. That student can now access the test during their personal window, completely independent of whether the class-wide window is open, closed, or hasn't started yet — it doesn't extend the class window, it's a separate clock that wins outright for that one student. Use the same modal with an earlier/blank window to revoke it later if needed.

### Permanently deleting things (only if enabled)

Normally nothing in this app is really deleted — "Remove", "Archive" and "Disable" all set a reversible flag. On an instance where the operator has set `ALLOW_HARD_DELETE`, extra red **🗑 Delete** buttons appear for coaches: on users and seasons (Club Management), events (landing page), assessment windows and individual tests (Assessments dashboard), and one student's response (Grade page).

These really delete. Before anything happens you get a dialog stating exactly what goes with it — *"This removes: 1 assessment window, 1 test, 41 student responses."* Read that line; deleting one season can take a whole term of student work with it.

Two specifics worth knowing:

- **Deleting one student's response** on the Grade page removes only that response, so a student who submitted by accident can retake the test. Everyone else's answers, and the test itself, are untouched.
- **Deleting an event** moves its folder of PDFs and images into a `.deleted` directory on the server rather than erasing it, so an accident is recoverable — but only by whoever administers the box, not from this app.
- **Deleting a test PDF** also removes the questions extracted from it — the bank keys questions by the PDF they came from, so leaving them would strand questions whose source can no longer be opened. The dialog tells you how many.
- **Source files and shared textbooks** can be deleted the same way, from the Primary sources page. Deleting a source leaves the markdown already generated from it in place.

If you don't see these buttons, the flag isn't set on your instance, which is the intended setting once you're running real tests.

### Browsing the tournament archive

**☰ → Tournament archive** (coach only) shows the uploaded collection of past
tournament tests, laid out as `<Division>/<Event>/<Year>/<Tournament>`. Click
through one level at a time; each folder shows how many files and how much
data sit beneath it.

Right now this is **read-only** — you can look, but nothing here moves,
renames or deletes yet. That is deliberate: browsing has to prove itself
against the full collection before anything is built that can change it.

Two things to know:

- **Rebuild index** is what makes the file counts appear, and it needs
  running once after you upload. Browsing works without it; you just see
  folders marked *not indexed* instead of totals.
- Folders you add after the last rebuild still show up immediately — only
  their totals wait for the next one.
- **Duplicates** are found during the same rebuild, by content rather than
  by name — the same test saved as `test.pdf` in one folder and
  `CircuitLab2019.pdf` in another is still recognised as one file. A banner
  shows how many sets exist and how much space keeping one copy each would
  reclaim; individual files carry a **copy** badge in the listing. Nothing
  is deleted for you.

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

### Preparing or grading a season assessment you've been assigned to

A coach can assign you to prepare or grade a test for an event — the assignment picker only offers you for an event if you already have bank-edit access to it (or you're a coach), so this normally lines up with your assigned-events list above. Click **Tests** in the header to see what you've been assigned, then **Prepare** (pick/randomly-suggest/publish questions) or **Grade** (score free-response answers) on that row — see the coach's "Prepare and publish a test" / "Run a assessment window and grade results" walkthroughs above; the steps are identical for a volunteer, just scoped to your specific assignment.

### Checking whether the server is busy before you start something big

The box runs **one worker process per school** and **one background job at a time across every event**. A reprocess, a scio.ly scrape or an LLM generation run started while someone else's is already going doesn't run alongside it — it queues behind it, and you both wait.

Two things in the UI tell you what's happening without asking anyone:

- **`👥 3 active`** in the page header — people who did something on this server in the last 5 minutes, across all schools' events, not just yours. During a assessment window it also breaks out how many are students taking a test, which is the heaviest thing this server ever does. No badge means it's just you.
- **`👥 2 others here`** next to an event on the landing page, and on the event's own page — other people are working in that event right now. You're never counted in this one, so any number you see is someone else. Worth a message before you reprocess the PDF they may be mid-review on.

Alongside them, **`⏳ 1 job running`** tells you the queue is already occupied. Seeing that plus a couple of active people is the moment to hold off on a bulk reprocess for a few minutes — nothing will break if you don't, it'll just all be slower for everyone, including you.

Neither number updates instantly; both refresh about every 20 seconds.

### What you can't do, and why

| Action | Why not |
|---|---|
| See or open an unassigned event | `_select_event` (the access chokepoint every `/event/<slug>/...` route calls first) returns 403 for volunteers outside their assigned list — see `spec.md` §9 |
| Manage users (Club Management → Manage Users) | Gated by `@coach_required` |
| Create/edit/archive events | Gated by `@coach_required` |
| Upload a new shared textbook | Write routes gated by `@coach_required`; reading/using existing ones is open to everyone |

## For Students

### Logging in for the first time

A coach creates your account (one-by-one, or in bulk via a CSV upload) and gives you a username/password. Log in at `/login` — you land directly on **My Assessments**, your home page (there's no question bank to manage, so the navigation menu only shows what you can actually use: My Assessments, Scores, Notifications, Settings, Logout). Go to **☰ → Settings → My Account** to change your password whenever you like.

### Taking a test

Tests are bucketed **Upcoming** (rostered, but the window hasn't opened — no questions visible yet, not even via a direct API call), **Current** (window open — click **Take test**), and **Past** (already submitted, or window closed). While taking a test you see one question at a time with Prev/Next, a countdown to when the window closes, and **no indication of whether your answer is right** — that only shows up after grading. Your answers autosave as you go, so reloading mid-test never loses progress, and your question order stays the same across reloads even though it's shuffled differently from other students. Click **Submit test** when done, or it auto-submits whatever you've saved if the window closes while you're still working.

If you missed the window, ask a coach for a personal makeup window — once granted, the test becomes accessible to you on your own separate schedule, regardless of whether the class window is open.

### Viewing results and scores

Once a coach releases grades for a test, **My Assessments** shows it under Past with your full results — your answers, the correct answers, and your score per question (including partial credit on matching questions). Until release, it just shows as submitted/pending, even if a volunteer has already graded the free-response parts behind the scenes.

**Scores** (☰ menu, visible to every role) shows every rostered student's named score on every graded test for the season — not just your own. You can only drill into the question-by-question detail of your *own* responses; other students' rows show the score only.

## For the server operator — moving to a new machine

Not a role in the app; this is the person with root on the box. `README.md` has the reasoning and the per-script detail, `spec.md` §18 has the design rationale — this is the checklist to work through in order, top to bottom.

**Before you start**, on the old host:

```
sudo deploy/migrate-secrets.sh --check
```

It lists every secret file this host has (presence, owner, mode — never contents) and, below that, the secrets that aren't files at all. Read that second list properly. If the restic repository password exists only in `/opt/qbank/backup/.env` on a box you're about to decommission, every S3 snapshot goes permanently unreadable with it.

**1. Provision the new box.** Get the repo onto it, then:

```
sudo deploy/provision-host.sh --dry-run
```

Read the plan, then run it without `--dry-run`. It's idempotent — re-run it as often as you like. It creates accounts, directories, venvs, systemd units, the `/usr/local/sbin` action scripts and the sudoers grants. It does not write secrets, does not install Caddy, and does not start anything.

**2. Move the secrets.** On the old host:

```
sudo deploy/migrate-secrets.sh --export /root/qbank-secrets.age
```

You'll be prompted for a passphrase (`age`, or `gpg` if `age` isn't installed). `scp` the file to the new host, then there:

```
sudo deploy/migrate-secrets.sh --import /root/qbank-secrets.age
```

This must come *after* provisioning — it applies ownership by account name, and the accounts have to exist.

**3. Move the data.** `restic restore` for the bulk data, `git clone` of the databank repo for the extracted JSON/markdown. Then open each instance's `.env` and **fix `DATA_ROOT`** — it came from the old host and still points at the old host's path. This is the single most common way a migration ends with an app that starts cleanly and shows zero questions.

**4. Verify, before cutting over.**

```
sudo deploy/migrate-secrets.sh --check
sudo systemctl start qbank.service qbank-chs.service admin-app.service caddy
```

Load each school's landing page and compare the question counts against the old box. **Nothing automates this comparison** — check unit states, listening ports, event counts and per-event question counts by hand. "The service started" is not the same as "it works."

**5. Cut over.** Only once the new box genuinely serves: move the router's WAN 80/443 port-forward to the new LAN IP, and update the gateway's Dynamic DNS target if it's pinned to a particular host. Leave the old box installed but stopped — that's your rollback, and it costs nothing to keep for a week.

**Note on the cutover window**: anything a coach saves on the old box after your final data sync is lost when you flip. Either stop the old instances before that last sync, or plan to re-sync and accept a short outage.

**6. Clean up.** Delete the secrets bundle from both hosts (`shred -u`). Rotate anything that was exposed during the move.

### Measuring server capacity (load testing)

Not a role in the app; this is for whoever needs to know "how many students can log in and take an assessment at once" before a real tournament. `spec.md`'s `--workers`/lock discussion has the reasoning: `gunicorn --workers` is hard-locked to 1, and `--threads` is the only adjustable knob, so the ceiling depends on how much load one process's thread pool can actually absorb — not computable from CPU/RAM specs. (Response storage itself used to be a second, worse bottleneck — one global file/lock shared by every student on every test — but that's fixed as of the per-`(test_id, username)`-file redesign; see README.md's "Measuring server capacity" for both halves of the story.) The only reliable way to know the remaining ceiling is to measure it.

**1. Create a throwaway test, by hand, via the normal coach UI:**
- Club Management → New season (note its `season_id`).
- Tests → New window, for any one real event with a handful of MCQ/matching questions kept (those autosave on every click with no debounce — the realistic worst case).
- Publish the test, then go live. Note its `test_id` (visible in the Assessments dashboard / its URL).

**2. Run the script** from a machine that can reach the server:

```
python loadtest_students.py --url https://your-server --test-id <id> --season-id <id>
```

You'll be prompted for a coach username/password (or set `QBANK_LOADTEST_COACH_USER`/`QBANK_LOADTEST_COACH_PASS` first — never pass them as a plain `--flag`, that lands in shell history). It prints the target and ramp plan and asks for typed confirmation before sending any load — **run this off-hours**, since it generates real concurrent traffic against a shared server.

It ramps through increasing numbers of concurrent synthetic students (`--steps`, default `5,10,20,40,80,160`), each one logging in, loading the test, and answer-saving every question — and prints one row per step: save count, error count, and p50/p95/max latency. It stops automatically at the first step whose error rate or p95 latency crosses a threshold (`--max-error-rate`, `--max-p95-seconds`) — that step is your practical ceiling. It only ever creates/uses synthetic `loadtest_*` accounts and cleans them up (disables them) automatically when it exits, even on Ctrl-C.

**3. Clean up afterward.** The script never touches the throwaway season/window/test itself (no delete route exists for those) — remove it by hand via the coach UI once you're done experimenting with step sizes.

## Quick task index

| Task | Who | Where |
|---|---|---|
| Bootstrap the very first account | operator (CLI) | `python auth.py --create-coach` |
| Clear out leftover load-test accounts | operator (root) | `python deletion.py --purge-prefix loadtest_` (dry run), then `--yes` |
| Provision a brand-new server box | operator (root) | `sudo deploy/provision-host.sh --dry-run`, then for real |
| Inventory this host's secrets | operator (root) | `sudo deploy/migrate-secrets.sh --check` |
| Move the server to a new machine | operator (root) | "For the server operator" above |
| Measure how many students the server can handle at once | operator | `python loadtest_students.py --url ... --test-id ... --season-id ...` — see "Measuring server capacity" above |
| Change your password or display name | Coach, Volunteer | ☰ → Settings → My Account |
| Set your own LLM API key | Coach, Volunteer | ☰ → Settings → LLM API Keys |
| Create/disable a user | Coach | ☰ → Club → Manage Users |
| Permanently delete a user / season / event | Coach (needs `ALLOW_HARD_DELETE`) | 🗑 Delete on the relevant page |
| Permanently delete a assessment window or test | Coach (needs `ALLOW_HARD_DELETE`) | Assessments dashboard → 🗑 |
| Let a student retake an assessment they submitted | Coach (needs `ALLOW_HARD_DELETE`) | Grade page → 🗑 next to their name |
| Register a new event | Coach | Landing page → + Register a new event |
| Download scioly.org PDFs | Coach, Volunteer (assigned events) | Event page → ⬇ Download PDFs |
| Upload a test PDF (+ key, + figures) | Coach, Volunteer (assigned events) | Event page → + Upload test |

While a long action runs (upload/extract, reprocess, a scrape, LLM generation) a progress window shows the server's own console output live. It closes itself a moment after the job **succeeds**; a job that fails or is cancelled leaves it open, because that console is the only place the reason is written. A striped moving bar means "still working" — most PDFs never report a page count to measure against, so there is nothing to show a percentage of.

| Onboard files dropped in via scp (test/key/supplementary/notes) | Coach, Volunteer (assigned events) | Event page → Scan files (next to + Upload test) |
| Preview a PDF without opening the review page | Coach, Volunteer (assigned events) | Event page → 👁 Preview |
| Fix a backwards test/key upload | Coach, Volunteer (assigned events) | Event page → ⇄ Swap, or Review page toolbar → ⇄ Swap test/key |
| Correct a PDF's Tournament name / Year | Coach, Volunteer (assigned events) | Review page → header fields next to the PDF name |
| Review/edit one PDF's questions | Coach, Volunteer (assigned events) | Event page → click a PDF / Review by PDF |
| Pull figures from a supplementary doc | Coach, Volunteer (assigned events) | Review page → target toggle next to Test PDF/Key PDF |
| Browse/search/bulk-edit the whole bank | Coach, Volunteer (assigned events) | Browse questions |
| Validate an answer with AI | Coach, Volunteer (assigned events) | Review or Browse page → 🤖 AI Validate / ✓ Validate |
| Mark a verdict yourself, no LLM call | Coach, Volunteer (assigned events) | Review or Browse page → validation dropdown |
| Scrape scio.ly practice questions | Coach, Volunteer (assigned events) | Generate page → scio.ly panel |
| Generate questions from a source | Coach, Volunteer (assigned events) | Generate page |
| Upload a shared textbook | Coach only | Generate page → Shared textbooks |
| Import questions from JSON | Coach, Volunteer (assigned events) | Generate page → Import panel |
| Export the bank, a filtered set, or just what you ticked | Coach, Volunteer (assigned events) | Browse page → Export ▾ (choose scope, then format) |
| Take a quiz | Coach, Volunteer (assigned events) | Quiz |
| See whether the server is busy before a big job | Coach, Volunteer | `👥 N active` / `⏳ N jobs` badges in the header |
| See who else is working in an event | Coach, Volunteer (assigned events) | Landing page → `👥 N here now` on the event row |
| Archive/unarchive an event | Coach only | Landing page |
| Restore a wiped reprocess | Coach, Volunteer (assigned events) | Review page → 🕘 Snapshot history |
| Create/mark current a season | Coach | Club Management |
| Bulk-create students + roster via CSV | Coach | Club Management → + Bulk-add students from CSV |
| Roster a student onto an event | Coach | Club Management → roster grid |
| Create a assessment window, assign coaches/volunteers | Coach | Assessments dashboard → Assign… |
| Prepare a test (pick questions, publish) | Coach, assigned Volunteer | Assessments dashboard → Prepare |
| Go live / un-publish a test | Coach | Assessments dashboard |
| Grant a student a personal makeup window | Coach | Assessments dashboard → + Makeup window |
| Grade free-response answers | Coach, assigned Volunteer | Assessments dashboard → Grade |
| Award full marks on one answer in one click | Coach, assigned Volunteer | Grade page → ✓ Full next to the score box |
| Release grades | Coach only | Assessments dashboard → Release grades |
| Take a live assessment | Student | My Assessments |
| View your own released results | Student | My Assessments |
| View season-wide named scores | Coach, Volunteer, Student | Scores |
| View another student's response detail | Coach; Volunteer who graded it (or assigned, all-MCQ test) | Scores → click a score |
