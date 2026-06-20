"""
Download all PDFs for a Sci-Oly event from scioly.org into <event>/

Usage:
  python download_event.py --event circuit_lab
  python download_event.py --event thermodynamics
  python download_event.py --event circuit_lab --no-skip-existing
  python download_event.py --event thermodynamics --no-bypass-bot   # skip Playwright

scioly.org uses Anubis (JavaScript proof-of-work) bot protection. The first
time the script hits a challenge page, it launches a real Chromium via
Playwright, waits ~5-30s for the PoW to complete, then captures the cookies
to `.scioly_cookies.json` for reuse on subsequent runs.

Install Playwright once:
  pip install playwright
  playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

from events import get_event, EVENTS

TESTS_JSON   = Path(__file__).parent / "scioly_tests.json"
COOKIE_FILE  = Path(__file__).parent / ".scioly_cookies.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Referer": "https://scioly.org/tests/",
}


# ---------------------------------------------------------------------------
# Anubis bot-challenge bypass via Playwright
# ---------------------------------------------------------------------------

def _looks_like_challenge(resp: requests.Response) -> bool:
    """True if the response is the Anubis bot-check HTML, not a real PDF."""
    ct = resp.headers.get("content-type", "")
    if not ct.startswith("text/html"):
        return False
    head = resp.text[:600].lower()
    return ("not a bot" in head) or ("anubis" in head)


def _load_cookies() -> list[dict] | None:
    if not COOKIE_FILE.exists():
        return None
    try:
        cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        fresh = [c for c in cookies
                 if c.get("expires", -1) == -1 or c["expires"] > now]
        return fresh or None
    except Exception:
        return None


def _save_cookies(cookies: list[dict]) -> None:
    COOKIE_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def _apply_cookies(session: requests.Session, cookies: list[dict]) -> None:
    for c in cookies:
        session.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain") or "scioly.org",
            path=c.get("path", "/"),
        )


def acquire_anubis_cookies(timeout_s: int = 60, headless: bool = False) -> list[dict] | None:
    """
    Launch Chromium via Playwright, visit scioly.org so Anubis runs its
    JavaScript proof-of-work, then return the resulting cookies. Returns
    None if Playwright isn't installed or the challenge times out.

    `headless` runs Chromium with no visible window — useful for unattended
    CI runs. Anubis's proof-of-work is a pure-JS computation so headless
    works for it, but a visible window is the safer default if you might
    need to solve a fallback CAPTCHA manually.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n[!] Playwright not installed. To auto-solve the bot challenge:")
        print("      pip install playwright")
        print("      playwright install chromium")
        print("    Then re-run this command.")
        return None

    print(f"\n[bot-bypass] Launching Chromium ({'headless' if headless else 'windowed'}) "
          f"to solve Anubis (timeout {timeout_s}s)...")
    if not headless:
        print("            A window will open; leave it alone — it closes itself.")
    cookies_out: list[dict] | None = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = context.new_page()
                page.goto("https://scioly.org/tests/",
                          wait_until="domcontentloaded",
                          timeout=timeout_s * 1000)
                # Anubis injects a PoW script; the title changes once cleared.
                try:
                    page.wait_for_function(
                        "!document.title.toLowerCase().includes('not a bot')",
                        timeout=timeout_s * 1000,
                    )
                except Exception:
                    print("[bot-bypass] Title check timed out — capturing cookies anyway.")
                page.wait_for_timeout(2000)   # let any final cookies settle
                cookies_out = context.cookies()
            finally:
                browser.close()
    except Exception as e:
        print(f"[bot-bypass] Failed: {e}")
        return None

    print(f"[bot-bypass] Captured {len(cookies_out or [])} cookie(s).")
    return cookies_out


def get_targets(event_slug: str) -> list[dict]:
    event = get_event(event_slug)
    with open(TESTS_JSON) as f:
        data = json.load(f)

    targets: list[dict] = []
    for item in data:
        ev = item.get("event", "").lower()
        if not any(m in ev for m in event.event_match):
            continue
        for key in ("test_link", "key_link", "notes_link"):
            if key in item:
                url = item[key]
                targets.append({
                    "url": url,
                    "filename": url.split("/")[-1],
                    "kind": key.replace("_link", ""),
                })
        for other in item.get("other_links", []):
            if other["href"].endswith(".pdf"):
                targets.append({
                    "url": other["href"],
                    "filename": other["href"].split("/")[-1],
                    "kind": other.get("text", "other"),
                })
    return targets


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    cookies = _load_cookies()
    if cookies:
        _apply_cookies(s, cookies)
        print(f"[cookies] reusing {len(cookies)} saved cookie(s) from {COOKIE_FILE.name}")
    return s


def download_all(event_slug: str, skip_existing: bool = True,
                 bypass_bot: bool = True,
                 bypass_headless: bool = False) -> bool:
    event = get_event(event_slug)
    out_dir = event.base_dir
    out_dir.mkdir(exist_ok=True)

    targets = get_targets(event_slug)
    print(f"Event: {event.name}  (slug={event.slug})")
    print(f"Output: {out_dir}")
    print(f"Total targets: {len(targets)}\n")

    session = _make_session()
    using_saved_cookies = COOKIE_FILE.exists()
    challenge_seen = False    # ensure we only spin up Playwright once

    ok, skipped, failed = 0, 0, []
    i = 0
    while i < len(targets):
        t = targets[i]
        dest = out_dir / t["filename"]
        if skip_existing and dest.exists() and dest.stat().st_size > 1000:
            skipped += 1
            print(f"  [{i+1:02d}/{len(targets)}] SKIP  {t['filename']}")
            i += 1
            continue
        try:
            r = session.get(t["url"], timeout=30)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and ctype.startswith("application/pdf"):
                dest.write_bytes(r.content)
                ok += 1
                print(f"  [{i+1:02d}/{len(targets)}] OK    {t['filename']}  "
                      f"({len(r.content) // 1024} KB)")
                i += 1
            elif _looks_like_challenge(r):
                if challenge_seen or not bypass_bot:
                    failed.append(t["filename"])
                    print(f"  [{i+1:02d}/{len(targets)}] BOT   {t['filename']}  "
                          f"(Anubis challenge — bypass {'failed' if challenge_seen else 'disabled'})")
                    i += 1
                else:
                    challenge_seen = True
                    # Distinguish "first-run challenge" from "saved cookies
                    # expired mid-batch" — the latter previously looked like
                    # the same generic Anubis line; users couldn't tell the
                    # session cookie they had been reusing had just rotted.
                    if using_saved_cookies:
                        print(f"  [{i+1:02d}/{len(targets)}] BOT   {t['filename']}  "
                              f"(saved cookies expired — re-running browser bypass)")
                        # Drop the stale cookie file so we don't reuse it on retry
                        try:
                            COOKIE_FILE.unlink()
                        except OSError:
                            pass
                        using_saved_cookies = False
                    else:
                        print(f"  [{i+1:02d}/{len(targets)}] BOT   {t['filename']}  "
                              "(Anubis challenge detected)")
                    cookies = acquire_anubis_cookies(headless=bypass_headless)
                    if not cookies:
                        print("  Aborting: could not bypass bot challenge.")
                        return False
                    _save_cookies(cookies)
                    session = _make_session()
                    # retry the same target (don't advance i)
                    continue
            else:
                failed.append(t["filename"])
                print(f"  [{i+1:02d}/{len(targets)}] FAIL  {t['filename']}  "
                      f"HTTP {r.status_code} ct={ctype!r}")
                i += 1
        except Exception as e:
            failed.append(t["filename"])
            print(f"  [{i+1:02d}/{len(targets)}] ERR   {t['filename']}  {e}")
            i += 1
        time.sleep(0.3)

    print(f"\nDone: {ok} downloaded, {skipped} skipped, {len(failed)} failed")
    if failed:
        print("Failed files:", failed)
    return len(failed) == 0


def resolve_event_slug(arg: str) -> str:
    """Map a user-supplied --event value to a registered slug.

    Accepts exact slug, case-insensitive match, and underscore-insensitive
    match so users can pass `diseasedetectives` and get `disease_detectives`.
    """
    if not arg:
        raise SystemExit("--event is required")
    if arg in EVENTS:
        return arg
    norm = arg.strip().lower()
    if norm in EVENTS:
        return norm
    flat = norm.replace("_", "").replace("-", "").replace(" ", "")
    for slug in EVENTS:
        if slug.replace("_", "") == flat:
            return slug
    # Also match against display name (e.g. "Disease Detectives" → disease_detectives)
    for slug, ev in EVENTS.items():
        if ev.name.lower().replace(" ", "") == flat:
            return slug
    raise SystemExit(
        f"Unknown event {arg!r}. Known events: {', '.join(sorted(EVENTS))}.\n"
        f"  - Custom events are loaded from events_custom.json; register one "
        f"via the web UI (events page → 'Register a new event')."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Download Sci-Oly event PDFs from scioly.org")
    parser.add_argument("--event", required=True,
                        help=("Event slug to download (required). "
                              "Accepts case- and underscore-insensitive matches: "
                              "`diseasedetectives` resolves to `disease_detectives`. "
                              f"Known: {', '.join(sorted(EVENTS))}"))
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="Re-download files that already exist")
    parser.add_argument("--no-bypass-bot", action="store_true",
                        help="Do NOT launch Playwright on bot challenge; just fail")
    parser.add_argument("--bypass-bot-headless", action="store_true",
                        help="Solve the bot challenge with a headless Chromium "
                             "(no visible window). For unattended CI runs.")
    parser.add_argument("--reauth", action="store_true",
                        help="Discard saved cookies and run the browser bypass again")
    args = parser.parse_args()

    if args.reauth and COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
        print(f"[cookies] removed {COOKIE_FILE.name}")

    event_slug = resolve_event_slug(args.event)
    success = download_all(
        event_slug,
        skip_existing=not args.no_skip_existing,
        bypass_bot=not args.no_bypass_bot,
        bypass_headless=args.bypass_bot_headless,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
