"""
Playwright smoke test for the review web app.

Spins up review_app.py on port 5070, walks through every major page,
captures console errors / page errors / interaction failures, and reports
findings grouped by severity. Headless by default; pass --headed to watch.

Usage:
    python test_webapp.py [--headed]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

BASE = "http://127.0.0.1:5070"
REPO = Path(__file__).parent

EV_SLUG = "circuit_lab"   # known to have a populated state file
PDF_NAME = "circuitlab_2019_b_uflorida_test.pdf"


class TestRun:
    def __init__(self):
        self.findings: list[tuple[str, str, str]] = []
        self.page_errors: list[str] = []
        self.console_msgs: list[tuple[str, str]] = []

    def add(self, severity: str, page: str, msg: str):
        self.findings.append((severity, page, msg))

    def watch(self, page: Page):
        page.on("pageerror", lambda err: self.page_errors.append(str(err)))
        page.on("console", lambda m: self.console_msgs.append((m.type, m.text)))

    def reset_capture(self):
        self.page_errors.clear()
        self.console_msgs.clear()

    def harvest(self, label: str):
        for err in self.page_errors:
            self.add("err", label, f"pageerror: {err[:200]}")
        for ctype, text in self.console_msgs:
            if ctype == "error":
                self.add("err", label, f"console.error: {text[:200]}")
            elif ctype == "warning":
                self.add("warn", label, f"console.warning: {text[:200]}")


def _start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "review_app.py", "--port", "5070"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(REPO),
    )
    # Give Flask a moment to bind
    for _ in range(20):
        time.sleep(0.5)
        try:
            import urllib.request
            urllib.request.urlopen(BASE + "/", timeout=1).read()
            return proc
        except Exception:
            continue
    proc.terminate()
    raise RuntimeError("Server did not start in 10 seconds")


def _safe_goto(page: Page, url: str, run: TestRun, label: str) -> bool:
    run.reset_capture()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        run.add("err", label, f"navigation failed: {e}")
        return False
    page.wait_for_timeout(1200)   # let deferred scripts and KaTeX settle
    run.harvest(label)
    return True


def test_events_landing(page: Page, run: TestRun):
    label = "events landing"
    if not _safe_goto(page, BASE + "/", run, label):
        return
    rows = page.query_selector_all("#ev_tbl tbody tr")
    if not rows:
        run.add("err", label, "event table has zero rows")
    else:
        run.add("info", label, f"event table populated: {len(rows)} row(s)")
    # (Dark-mode toggle was removed; nothing to test.)
    # Sort
    try:
        page.click("#ev_tbl th[data-sort=name]")
        page.wait_for_timeout(150)
    except Exception as e:
        run.add("warn", label, f"sort click raised: {e}")
    # New-event form is initially disabled
    try:
        page.click("text=+ Register a new event")
        page.wait_for_timeout(200)
        disabled = page.evaluate("document.getElementById('ne_submit').disabled")
        if not disabled:
            run.add("err", label, "new-event Submit should start disabled")
    except Exception:
        pass


def test_event_index(page: Page, run: TestRun):
    label = "event index"
    if not _safe_goto(page, f"{BASE}/event/{EV_SLUG}/", run, label):
        return
    try:
        page.wait_for_selector("#rows tr a", timeout=8000)
    except Exception:
        run.add("err", label, "PDF rows never appeared")
        return
    n = len(page.query_selector_all("#rows tr a"))
    run.add("info", label, f"{n} PDF rows rendered")
    # Bulk reprocess starts disabled
    if not page.evaluate("document.getElementById('bulk_reprocess').disabled"):
        run.add("err", label, "bulk_reprocess should be disabled with no selection")
    # Check a row, verify enable
    cb = page.query_selector(".row-check")
    if cb:
        cb.check()
        page.wait_for_timeout(150)
        if page.evaluate("document.getElementById('bulk_reprocess').disabled"):
            run.add("err", label, "bulk_reprocess did not enable after row selection")
        cb.uncheck()
    # Group by year
    try:
        page.select_option("#group_by", "year")
        page.wait_for_timeout(600)
        if not page.query_selector_all(".group-row"):
            run.add("warn", label, "group rows did not appear when grouping by year")
        else:
            run.add("info", label, "year grouping working")
        page.select_option("#group_by", "")
    except Exception as e:
        run.add("warn", label, f"group-by raised: {e}")


def test_browse(page: Page, run: TestRun):
    label = "browse"
    if not _safe_goto(page, f"{BASE}/event/{EV_SLUG}/browse", run, label):
        return
    try:
        page.wait_for_selector(".qcard", timeout=12000)
    except Exception:
        run.add("err", label, "no question cards appeared (stuck loading?)")
        return
    n_cards = len(page.query_selector_all(".qcard"))
    run.add("info", label, f"{n_cards} question cards rendered initially")
    # New feature markers on first card
    first = page.query_selector(".qcard")
    for sel, desc in [(".qcard-check", "checkbox"),
                       (".badge.topic", "topic badge"),
                       (".edit-btn", "edit button"),
                       (".gensim-btn", "generate-similar button")]:
        if not first.query_selector(sel):
            run.add("err", label, f"first card missing: {desc}")
    # Filter — search
    page.fill("#f_search", "circuit")
    page.wait_for_timeout(400)
    if "search=circuit" not in page.url:
        run.add("warn", label, "search filter not persisted in URL")
    # Sort change
    page.select_option("#f_sort", "topic")
    page.wait_for_timeout(300)
    # Histogram
    if not page.query_selector("#topic_chart .histo-row"):
        run.add("warn", label, "histogram chart did not render any bars")
    else:
        run.add("info", label, "histogram chart rendered")
    # Bucket filter (one-row option)
    bopts = page.evaluate("[...document.getElementById('f_bucket').options].length")
    if bopts < 2:
        run.add("warn", label, f"bucket filter has only {bopts} options")
    # (Dark-mode toggle was removed; nothing to test.)
    # Clear all
    page.fill("#f_search", "")
    page.click("#clear_all")
    page.wait_for_timeout(200)
    # Re-harvest after interactions
    run.harvest(label)


def test_sources(page: Page, run: TestRun):
    label = "sources"
    if not _safe_goto(page, f"{BASE}/event/{EV_SLUG}/sources", run, label):
        return
    # Confirm form elements exist
    for sel, desc in [("#gen_n", "generate-N input"),
                       ("#gen_chunks", "chunk count input"),
                       ("#gen_preview_btn", "preview button"),
                       ("#validate_then_accept", "validate-and-accept button"),
                       ("#btn_scrape", "scio.ly scrape button"),
                       ("#drop_overlay", "drag-drop overlay"),
                       ("#sc_cost", "scrape cost label"),
                       ("#gen_cost", "generate cost label")]:
        if not page.query_selector(sel):
            run.add("err", label, f"missing: {desc} ({sel})")
    # Sources table — wait briefly
    page.wait_for_timeout(800)
    rows = page.query_selector_all("#src_rows tr")
    if not rows:
        run.add("warn", label, "sources table rendered no rows")
    else:
        run.add("info", label, f"sources table: {len(rows)} row(s)")
    run.harvest(label)


def test_review(page: Page, run: TestRun):
    label = "review"
    url = f"{BASE}/event/{EV_SLUG}/review/{PDF_NAME}"
    if not _safe_goto(page, url, run, label):
        return
    # Wait for STATE to populate. JS declares STATE = {questions:[],
    # page_count: 1} up-front, so we can't watch those. The `name` field
    # is only set by the /api/pdf response, so wait for that.
    try:
        page.wait_for_function("STATE && typeof STATE.name === 'string' && STATE.name.length > 0",
                               timeout=15000)
    except Exception:
        run.add("err", label, "STATE.name never set (load() failed)")
        return
    total = page.evaluate("STATE.questions.length")
    if total == 0:
        run.add("warn", label, "STATE.questions is empty for this PDF")
        return
    run.add("info", label, f"STATE.questions has {total} question(s) across the PDF")
    # Jump to the page of the first extracted question
    first_q_page = page.evaluate("STATE.questions[0].page || 1")
    page.fill("#pageInput", str(first_q_page))
    page.evaluate("document.getElementById('pageInput').dispatchEvent(new Event('change'))")
    page.wait_for_timeout(700)
    n_cards = len(page.query_selector_all(".q-card"))
    if n_cards == 0:
        run.add("warn", label, f"page {first_q_page} rendered zero cards even though Q1 claims that page")
    else:
        run.add("info", label, f"page {first_q_page} rendered {n_cards} card(s)")
    # Wait for PDF image to load
    try:
        page.wait_for_function("document.getElementById('pageImg').naturalWidth > 0", timeout=10000)
    except Exception:
        run.add("err", label, "PDF page image never loaded")
        return
    # Thumbnail strip
    n_thumbs = len(page.query_selector_all("#thumbStrip .thumb"))
    if n_thumbs == 0:
        run.add("err", label, "thumbnail strip empty")
    else:
        run.add("info", label, f"thumbnail strip: {n_thumbs} pages")
    # Bounding boxes (async-fetched after image load)
    page.wait_for_timeout(2000)
    n_boxes = len(page.query_selector_all(".q-bbox"))
    if n_boxes == 0:
        run.add("warn", label, "no question bounding boxes drawn on page 1")
    else:
        run.add("info", label, f"{n_boxes} bounding box(es) drawn on page 1")
    # Zoom
    page.evaluate("window.adjustZoom(1)")
    page.wait_for_timeout(200)
    zoom_lbl = page.evaluate("document.getElementById('zoomLabel').textContent")
    if not zoom_lbl or not zoom_lbl.startswith("125"):
        run.add("warn", label, f"zoom didn't update label: {zoom_lbl}")
    page.evaluate("window.adjustZoom(0)")   # reset
    # Outline drawer
    page.evaluate("window.toggleOutline()")
    page.wait_for_timeout(1500)
    if not page.query_selector(".outline-drawer.on"):
        run.add("warn", label, "outline drawer didn't open")
    page.evaluate("window.toggleOutline()")
    # Verify focus path works — programmatically set focusedId to the first
    # question on this page, re-render, and check the css class lands.
    if n_cards:
        page.evaluate("""() => {
          const firstQ = STATE.questions.find(q => (q.page||1) === curPage);
          if(firstQ){ focusedId = firstQ._id; renderCards(); }
        }""")
        page.wait_for_timeout(200)
        if not page.query_selector(".q-card.focused"):
            run.add("err", label, "focusedId assignment did not produce .focused css class")
        else:
            run.add("info", label, "card focus rendering works")
    # Floating actions: Undo button present
    fa_text = page.evaluate(
        "[...document.querySelectorAll('.floating-actions button')].map(b => b.textContent).join('|')"
    )
    if "Undo" not in fa_text:
        run.add("warn", label, "Undo button not in floating actions")
    # Page-progress label
    if not page.evaluate("document.getElementById('pageProgress').textContent"):
        run.add("warn", label, "page progress label empty")
    run.harvest(label)


def test_browse_to_review_link(page: Page, run: TestRun):
    label = "browse → review deep link"
    # Pick a known question number from the browse page
    if not _safe_goto(page, f"{BASE}/event/{EV_SLUG}/browse", run, label):
        return
    try:
        page.wait_for_selector(".qcard", timeout=10000)
    except Exception:
        return
    # Pull href from the first "Open source ↗" link
    href = page.evaluate(
        "document.querySelector('.qcard a[href*=\"/review/\"]')?.href || ''"
    )
    if not href:
        run.add("warn", label, "no Open-source link on first card")
        return
    if not _safe_goto(page, href, run, label):
        return
    try:
        page.wait_for_selector(".q-card.focused", timeout=8000)
        run.add("info", label, "review page focused the linked question correctly")
    except Exception:
        run.add("err", label, "?q=<num> did not focus the target question")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window while testing")
    args = ap.parse_args()

    proc = _start_server()
    print(f"Server up on {BASE}")
    run = TestRun()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            run.watch(page)

            tests = [
                ("Events landing",         test_events_landing),
                ("Event index",            test_event_index),
                ("Browse",                 test_browse),
                ("Sources",                test_sources),
                ("Review (PDF + Qs)",      test_review),
                ("Browse→Review deep link", test_browse_to_review_link),
            ]
            for name, fn in tests:
                print(f"  · {name}…")
                try:
                    fn(page, run)
                except Exception as e:
                    run.add("err", name, f"test raised: {e}")
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # Report
    by_kind: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for s, p, m in run.findings:
        by_kind[s].append((p, m))

    print("\n" + "=" * 60)
    print(f"  errors:   {len(by_kind['err'])}")
    print(f"  warnings: {len(by_kind['warn'])}")
    print(f"  info:     {len(by_kind['info'])}")
    print("=" * 60)

    if by_kind["err"]:
        print("\nERRORS:")
        for p_, m in by_kind["err"]:
            print(f"  [{p_:<30}] {m}")
    if by_kind["warn"]:
        print("\nWARNINGS:")
        for p_, m in by_kind["warn"]:
            print(f"  [{p_:<30}] {m}")
    if by_kind["info"]:
        print("\nINFO:")
        for p_, m in by_kind["info"]:
            print(f"  [{p_:<30}] {m}")

    return 1 if by_kind["err"] else 0


if __name__ == "__main__":
    sys.exit(main())
