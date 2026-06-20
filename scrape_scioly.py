"""
Scrape public practice questions from scio.ly/practice.

scio.ly exposes a JSON endpoint that requires no authentication:

    GET /api/questions?event=<Display Name>&question_type=<mcq|frq>&limit=<N>

Response (shape gleaned from probing):

    {
      "success": true,
      "data": [
        {
          "id": "<uuid>",
          "question":   "<stem>",
          "options":    ["...", "..."],          // MCQ only
          "answers":    [<idx>],                  // indices into options for MCQ;
                                                  //   text for FRQ
          "subtopics":  ["..."],
          "tournament": "Holt Invitational 2020",
          "division":   "B" | "C",
          "difficulty": 0.6,
          "base52":     "tXnNS"
        }
      ]
    }

Limitations the caller should know about:
  - scio.ly questions never carry images (`images` will always be [])
  - many questions are partial / context-stripped — pair this with the
    Haiku answer-validator to flag the unsolvable ones for the user
  - this is a small student-run site; we rate-limit and never bulk-scrape
"""

from __future__ import annotations

import time
import requests

import build_question_bank as bqb
import qgen

API_URL = "https://www.scio.ly/api/questions"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept":  "application/json",
    "Referer": "https://www.scio.ly/practice",
}

REQUEST_DELAY = 0.5  # seconds between API calls — be a polite scraper


def split_event_and_focus(scioly_event_name: str) -> tuple[str, str]:
    """
    scio.ly splits rotating events into sub-categories with " - <focus>" syntax:
        "Anatomy - Endocrine"      -> ("Anatomy",            "Endocrine")
        "Water Quality - Freshwater" -> ("Water Quality",    "Freshwater")
        "Materials Science - Nanomaterials" -> ("Materials Science", "Nanomaterials")
        "Circuit Lab"              -> ("Circuit Lab",        "")
    """
    if " - " not in scioly_event_name:
        return scioly_event_name.strip(), ""
    parent, focus = scioly_event_name.split(" - ", 1)
    return parent.strip(), focus.strip()


def fetch_batch(event_name: str, question_type: str, limit: int = 50,
                division: str = "") -> list[dict]:
    """One API call. Returns the raw `data` list (possibly empty)."""
    params: dict = {
        "event":         event_name,
        "question_type": question_type,
        "limit":         max(1, min(int(limit), 50)),
    }
    if division:
        params["division"] = division
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"scio.ly returned HTTP {r.status_code}")
    j = r.json()
    if not j.get("success"):
        return []
    return j.get("data") or []


def _normalize(raw: dict, source_prefix: str = "scio.ly",
               focus: str = "") -> dict | None:
    """Convert a scio.ly raw record into a canonical Question dict."""
    text = (raw.get("question") or "").strip()
    if not text:
        return None
    text = bqb._strip_points(text)

    options = raw.get("options") or []
    answers = raw.get("answers") or []

    # Build choice list (MCQ).
    choices: list[dict] = []
    for i, opt in enumerate(options):
        if not opt:
            continue
        choices.append({
            "letter": chr(ord("A") + i),
            "text":   bqb._strip_points(str(opt)),
        })

    # Resolve answer.  For MCQ, `answers` is a list of indices into options.
    # For FRQ, scio.ly stores the canonical text in `answers[0]`.
    answer_str = ""
    if choices and answers:
        letters = []
        for a in answers:
            if isinstance(a, int) and 0 <= a < len(choices):
                letters.append(choices[a]["letter"])
        answer_str = ", ".join(letters)
    elif answers:
        first = answers[0]
        answer_str = first if isinstance(first, str) else str(first)
    answer_str = bqb._strip_points(answer_str)

    # Topic: prefer the subtopic if it matches our event taxonomy, else fall
    # back to the keyword classifier.
    topic = "Other / General"
    subtopics = raw.get("subtopics") or []
    for sub in subtopics:
        if isinstance(sub, str) and sub in bqb.EVENT.topics:
            topic = sub
            break
    if topic == "Other / General":
        guess = bqb.classify_topic(text)
        if guess and guess != "Other / General":
            topic = guess

    tournament = (raw.get("tournament") or "").strip()
    division   = (raw.get("division") or "").strip()
    source     = f"{source_prefix} · {tournament}" if tournament else source_prefix

    return {
        "number":      raw.get("base52") or (raw.get("id") or "")[:8],
        "topic":       topic,
        "focus":       focus,
        "text":        text,
        "choices":     choices,
        "answer":      answer_str,
        "images":      [],
        "source":      source,
        "year":        "",
        "division":    division,
        "page":        1,
        # provenance fields — used by the dedup check on re-scrape
        "_scioly_id":         raw.get("id"),
        "_scioly_difficulty": raw.get("difficulty"),
    }


def scrape_questions(
    event_name: str,
    count: int = 20,
    types: list[str] | None = None,
    division: str = "",
    existing_scioly_ids: set | None = None,
    existing_questions: list[dict] | None = None,
    dedup_threshold: float = 0.4,
    focus: str = "",
) -> dict:
    """
    Scrape up to `count` questions for `event_name` from scio.ly/practice.

    Two layers of dedup:
      1. UUID dedup: skip records whose `id` matches anything in
         `existing_scioly_ids` (cheap, exact).
      2. Text dedup: skip records whose question text has Jaccard similarity
         >= `dedup_threshold` to any question in `existing_questions` OR to
         any other candidate already kept in this batch. Catches questions
         we've seen via other ingestion paths (PDF extraction, LLM
         generation, prior scio.ly scrapes that came in under a different
         UUID).

    Returns:
      {
        "questions":          [normalized question dicts],
        "raw_count":          total records returned by the API,
        "fetched_per_type":   {qt: n_returned},
        "skipped_id_dups":    n skipped via scio.ly UUID match,
        "rejected_text_dups": [{text, matched_q_number, matched_q_source}],
        "errors":             [str]
      }
    """
    types = [t.lower() for t in (types or ["mcq", "frq"])
             if t.lower() in ("mcq", "frq")]
    if not types:
        types = ["mcq"]
    existing_scioly_ids = existing_scioly_ids or set()
    existing_questions = existing_questions or []

    # If the caller didn't pass `focus`, but the scio.ly event name has the
    # rotating-event " - <focus>" suffix, infer it. This way scraping
    # "Anatomy - Endocrine" without thinking still tags every question with
    # focus="Endocrine".
    if not focus:
        _, derived = split_event_and_focus(event_name)
        focus = derived

    # Pre-index existing questions by number for source-lookup on rejection
    existing_by_num: dict[str, dict] = {q.get("number"): q
                                        for q in existing_questions
                                        if q.get("number")}

    # Round-robin across types so the user gets a mix
    per_type = max(1, -(-count // len(types)))   # ceil division
    questions: list[dict] = []
    fetched: dict[str, int] = {}
    errors: list[str] = []
    skipped_id_dups = 0
    rejected_text_dups: list[dict] = []

    for qt in types:
        # Fetch generously — we expect to filter out text dupes after the
        # UUID dedup, so under-fetching would leave us short of `count`.
        try:
            raw = fetch_batch(event_name, qt,
                              limit=min(per_type * 3 + 10, 50),
                              division=division)
            fetched[qt] = len(raw)
        except Exception as e:
            errors.append(f"{qt}: {e}")
            continue
        time.sleep(REQUEST_DELAY)
        for r in raw:
            qid = r.get("id")
            if qid and qid in existing_scioly_ids:
                skipped_id_dups += 1
                continue
            normalized = _normalize(r, focus=focus)
            if not normalized:
                continue
            # Layer 2: textual dedup against the whole bank and the batch
            is_dup, matched_num = qgen.is_duplicate(
                {"text": normalized["text"]},
                existing_questions + questions,
                threshold=dedup_threshold,
            )
            if is_dup:
                matched_q = existing_by_num.get(matched_num) or next(
                    (q for q in questions if q.get("number") == matched_num), {})
                rejected_text_dups.append({
                    "text":             normalized["text"][:200],
                    "matched_q_number": matched_num,
                    "matched_q_source": matched_q.get("source", ""),
                    "matched_q_text":   (matched_q.get("text") or "")[:200],
                    "_scioly_id":       qid,
                })
                continue
            questions.append(normalized)
            if len(questions) >= count:
                break
        if len(questions) >= count:
            break

    return {
        "questions":           questions[:count],
        "raw_count":           sum(fetched.values()),
        "fetched_per_type":    fetched,
        "skipped_id_dups":     skipped_id_dups,
        "rejected_text_dups":  rejected_text_dups,
        "errors":              errors,
    }
