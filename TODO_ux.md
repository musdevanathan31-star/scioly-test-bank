# UX Enhancements & Fixes — TODO

**Scope:** desktop browsers only (≥ 1200 px). Mobile/tablet layouts are explicitly out of scope; the review page assumes a desktop split and drag-with-mouse interactions.

After two polish sprints almost all of the originally-tracked items have shipped. What's left is a short list of large items that need their own session.

Legend: ✅ shipped · 🛠 still open (mid) · 🏗 still open (large)

---

## ✅ Shipped (both polish sprints)

### Cross-cutting
- ✅ Toast log with persistent message history (Messages popup, last 30, timestamped)
- ✅ Keyboard shortcuts framework (`hotkey(combo, handler, opts)`)
- ✅ Wired hotkeys: `Ctrl/Cmd+S` save, `/` focus search, `Esc` cancel/close, `Ctrl+Z` undo
- ✅ Unsaved-changes count badge
- ✅ Tooltips on every action button and badge
- ✅ Page title reflects state (`● pN · <pdf>`)
- ✅ Drag-and-drop PDF/MD/TXT anywhere on the Sources page
- ✅ Loading skeletons across pages
- ✅ Better empty-state messaging with CTAs

### Events landing
- ✅ Sortable event table
- ✅ Event-health annotation (`12/22 processed`)
- ✅ Per-event Remove button (built-ins protected)
- ✅ Per-event Edit button — change name / topics / foci / wiki page in place
- ✅ Form validation: Submit disabled until required fields are filled

### Event index
- ✅ Better column titles + explanatory tooltips
- ✅ Row highlighting (pink for 0 questions, amber for <30% answer coverage)
- ✅ Bulk select + bulk reprocess with confirm
- ✅ Per-row "View questions" link to filtered browse
- ✅ Group PDFs by year or division (collapsible group headers)
- ✅ Skeleton loader

### Review
- ✅ Stronger focused-card outline (3-px accent + ● dot)
- ✅ Page progress display (`5 / 12 (42%)`)
- ✅ "Go to Q#" input
- ✅ Page-thumbnail strip at the top of the PDF pane (clickable, low-DPI render)
- ✅ PDF zoom controls (− / 100% / + / reset; persisted to localStorage)
- ✅ PDF outline / TOC drawer (☰ button; lazy-loaded via PyMuPDF `doc.get_toc()`)
- ✅ Manual collapse / expand on question cards — per-card chevron + toolbar "▼ Collapse all" / "▲ Expand all"
- ✅ Right-click context menu on PDF when a question is focused (Capture as stem / choices / answer / math / +new)
- ✅ Persisted drag-rectangle overlays — every capture region is re-rendered as a soft-blue box on the page it was captured on (visual undo / audit trail)
- ✅ Floating action bar (Save) at bottom-right, mirrors header
- ✅ Hotkeys: Ctrl+S, Esc, `/`, `Ctrl+Z` (undo)
- ✅ Tooltips on Save / Reprocess / Capture
- ✅ **Bounding-box visualization** — every extracted question is outlined on the page render in soft green. Focused question turns orange; questions the algorithm found but the parser didn't extract turn dashed red (diagnostic). Click any box to focus the matching card. Toggle via the ▢ zoom-control button; preference persists in `localStorage`. Coords derived on-demand from PyMuPDF text blocks (see spec §9 "Question bounding boxes").
- ✅ **Undo** with `Ctrl+Z` and a ↶ Undo button in the floating action bar. Session-local 10-step snapshot stack; captures before delete / OCR-replace / image-assign / add-question. Restores STATE.questions + ANN + focused question + current page. Text edits use the textarea's own browser-provided undo so we don't double-up.
- ✅ **Side-by-side two-question diff** on the review page — ⇄ Compare button on the focused card's capture bar opens a picker, then a modal showing the two questions with shared words highlighted green.

### Sources
- ✅ Sortable sources table (file / kind / size / markdown size)
- ✅ Word count of processed markdown sources
- ✅ "Open in new tab" `↗` link per file
- ✅ Disable Generate until a source is picked
- ✅ Live cost estimates next to Generate and Scrape
- ✅ Persisted form values per-event (localStorage)
- ✅ Drag-drop file upload (multi-file)
- ✅ Chunked generation across 1–5 source chunks (per-chunk question count auto-divided)
- ✅ Source preview panel — see the first 4 KB of the markdown before sending to Haiku
- ✅ Word-level diff highlighting on dedup-rejection display
- ✅ ⚡ **Validate &amp; auto-accept correct** button — validate every kept candidate with Haiku, auto-save only the ✓ ones

### Browse
- ✅ URL-persisted filter state
- ✅ Bucket filter
- ✅ Topic colour coding (deterministic hash → stable hue per topic)
- ✅ Question-type icons (🔘 MCQ / 📝 FRQ)
- ✅ Click-anywhere-on-card to open the source
- ✅ Thumbnail of the first attached image in card head
- ✅ "Recently edited (≤ 7d)" filter
- ✅ "Recently edited first" sort option
- ✅ Histogram / distribution chart in sidebar (per-topic bar lengths, hue-coded)
- ✅ Inline edit on browse-page cards (✎ Edit button — full editor in place, saves via PATCH endpoint, also deletes via DELETE endpoint)
- ✅ Bulk select + Compare modal + Bulk delete
- ✅ Export menu — CSV (all), JSON (all), CSV (filtered set), JSON (filtered set), Markdown (filtered set)
- ✅ Backend export endpoints (`/api/export.csv`, `/api/export.json`)
- ✅ **Generate similar from a browse card** — ✨ Generate similar button on every card calls `/api/generate-similar` with the card's stem + topic as seed. Modal shows kept candidates with their rationale; one click saves them into the generated bucket.

---

## ✅ Recently shipped (this sprint)

- ✅ **Anki deck export (`.apkg`)** — `/api/export.apkg` builds per-topic sub-decks via `genanki` (`pip install genanki` to enable). MCQ + FRQ models with stable IDs so re-exports merge cleanly in Anki.
- ✅ **Printable PDF export** — `/api/export.pdf` via `reportlab` (`pip install reportlab` to enable). Questions grouped by topic; answer key on the final page.
- ✅ **Quiz mode** — `/event/<slug>/quiz`: filter by topic / bucket / type, per-question timer (none / 30s / 60s / 90s / 2m), shuffle/in-order, immediate feedback after each answer, score + review-mistakes summary at end. Linked from the event-index header.
- ✅ **Spend badge** — every header shows running Anthropic API cost (auto-refreshes every 30s via `/api/usage`).

---

## 🛠 / 🏗 Open items

Nothing in the original tracked set remains. Open the door for follow-ups (e.g. spaced-repetition scheduler, side-by-side bucket compare, server-side PDF preview) when the user has a specific need.

---

## Explicitly out of scope (per user)

- ❌ **Mobile / narrow-window layout** — desktop only; declared in README.
- ❌ **Save-on-blur** — explicit save model is preferred.
- ❌ **Per-event sparkline** — visual noise without clear value.
