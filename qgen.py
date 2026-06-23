"""
LLM-driven question generation with simple duplicate detection.

`generate_questions(source_text, ...)` calls Haiku with a chunk of source
material and returns candidate questions in the canonical question shape used
by the rest of the pipeline (number/topic/text/choices/answer/...).

Dedup uses Jaccard similarity on word-3-grams of the question stem against the
existing bank — cheap and good enough to catch obvious repeats. Borderline
cases pass through; the user reviews accept/reject in the UI.
"""

from __future__ import annotations

import re
from datetime import datetime

from typing import Callable

import build_question_bank as bqb
import jobs
import llm_providers


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _ngrams(text: str, n: int = 3) -> set[str]:
    words = _WORD_RE.findall((text or "").lower())
    if len(words) < n:
        return set(words)
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard_similarity(a: str, b: str, n: int = 3) -> float:
    ga, gb = _ngrams(a, n), _ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def is_duplicate(candidate: dict, existing: list[dict],
                 threshold: float = 0.4) -> tuple[bool, str | None]:
    """
    True if candidate looks too similar to any existing question.
    Returns (is_dup, matched_number_or_None).
    """
    cand_text = candidate.get("text") or ""
    if not cand_text.strip():
        return False, None
    cand_ngrams = _ngrams(cand_text, 3)
    if not cand_ngrams:
        return False, None
    best = (0.0, None)
    for q in existing:
        e_text = q.get("text") or ""
        if not e_text:
            continue
        sim = jaccard_similarity(cand_text, e_text)
        if sim > best[0]:
            best = (sim, q.get("number"))
        if sim >= threshold:
            return True, q.get("number")
    return False, best[1]


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = 9000) -> list[str]:
    """Split text on paragraph boundaries into chunks <= max_chars."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        plen = len(p)
        if cur_len + plen > max_chars and cur:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [p], plen
        else:
            cur.append(p)
            cur_len += plen + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

_TYPE_HINTS = {
    "mc": ('multiple choice with 4 distinct choices A-D; one is correct, '
           'the others are plausible distractors'),
    "short": ('short-answer (1-2 sentence answer); leave "choices" empty'),
    "numerical": ('numerical answer with units; include the equation and '
                  'a brief solution outline in the rationale; '
                  'leave "choices" empty'),
}


def _build_prompt(chunk: str, n: int, types: list[str],
                  topics: list[str], avoid_samples: list[str]) -> str:
    types_clause = "\n".join(
        f'  - "{t}": {_TYPE_HINTS[t]}' for t in types if t in _TYPE_HINTS
    )
    topics_str = ", ".join(f'"{t}"' for t in topics if t != "Other / General")
    avoid_clause = ""
    if avoid_samples:
        avoid_clause = (
            "\n\nDo NOT recreate variants of these existing questions:\n"
            + "\n".join(f"  - {s[:160]}" for s in avoid_samples[:20])
        )
    return (
        f"You are writing new Science Olympiad questions for the "
        f"{bqb.EVENT.name} event (US middle/high school).\n\n"
        f"Source material:\n---\n{chunk}\n---\n\n"
        f"Generate exactly {n} novel question(s), distributed across these types:\n"
        f"{types_clause}\n\n"
        f"Classify each question into one of these topics: {topics_str}.\n"
        f"Pick \"Other / General\" only if none truly fit.{avoid_clause}\n\n"
        "Quality requirements:\n"
        " - Questions must be answerable from the source material above\n"
        " - For numerical: prefer answers with realistic round numbers\n"
        " - For multiple choice: the correct answer must be unambiguous and\n"
        "   distractors should reflect common mistakes students make\n"
        " - Use LaTeX (e.g. $V = IR$) for equations\n\n"
        'Reply with ONLY valid JSON, no markdown fences:\n'
        '{"questions": ['
        '{"type":"mc"|"short"|"numerical","topic":"<topic>",'
        '"text":"<stem>","choices":[{"letter":"A","text":"..."}],'
        '"answer":"<answer>","rationale":"<how to solve, with equations>",'
        '"source_snippet":"<short quote from source>"}'
        ']}'
    )


def generate_questions(
    source_text: str,
    n: int = 5,
    types: list[str] | None = None,
    existing_questions: list[dict] | None = None,
    max_chunks: int = 1,
    keys: dict | None = None,
    should_cancel: Callable[[], bool] = lambda: False,
    on_progress: Callable[..., None] = lambda **kw: None,
) -> dict:
    """
    Returns:
      {
        "candidates": [Question dicts ready to add to the bank],
        "rejected": [{"reason": "duplicate", "matched": "<qnum>", "text": "..."}],
        "model": "<model id>",
        "chunks_used": N,
        "raw_count": N,
      }

    `keys`: optional per-provider API keys (browser-supplied, see
    review_app._request_llm_keys); defaults to the server's own
    ANTHROPIC_API_KEY when omitted, so CLI/batch callers are unaffected.

    `should_cancel`/`on_progress`: optional job-queue hooks (see jobs.py),
    checked/called once per chunk — each chunk is one LLM call, the only
    unit of work here slow enough to need them. Both default to no-ops so
    direct/CLI callers are unaffected.
    """
    types = types or ["mc", "short", "numerical"]
    types = [t for t in types if t in _TYPE_HINTS]
    if not types:
        types = ["mc"]

    keys = keys or llm_providers.default_keys()
    if not llm_providers.available_providers(keys):
        return {"error": "No LLM API key configured. Add one in Settings.",
                "candidates": [], "rejected": []}

    avoid = [q.get("text", "") for q in (existing_questions or [])
             if q.get("text")]
    # Keep the 20 most-similar existing texts in the prompt;
    # picking by content overlap is overkill — first 20 is fine.
    avoid_samples = avoid[:30]

    chunks = chunk_text(source_text)[:max_chunks]
    raw_candidates: list[dict] = []
    warnings: list[str] = []
    used_model = bqb.VISION_MODEL  # overwritten with the actual provider/model used below

    # Budget tokens for the response: each question ~ 500 tokens with rationale
    # + source snippet + choices; add 600 overhead for envelope.
    out_max_tokens = min(16000, 600 + 550 * n * max(1, len(chunks)))

    for ci, chunk in enumerate(chunks):
        if should_cancel():
            raise jobs.JobCancelled()
        on_progress(phase=f"generating chunk {ci+1}/{len(chunks)}", done=ci, total=len(chunks))
        prompt = _build_prompt(chunk, n, types, list(bqb.EVENT.topics), avoid_samples)
        try:
            result = llm_providers.chat(
                keys=keys, system=None,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=out_max_tokens,
                model_overrides={"anthropic": bqb.VISION_MODEL},
            )
            if result["provider"] == "anthropic":
                bqb._track_usage_tokens(result["input_tokens"], result["output_tokens"])
            raw = result["text"]
            stop = result["stop_reason"]
            used_model = result["model"]
        except llm_providers.LLMError as e:
            return {"error": f"LLM call failed: {e}",
                    "candidates": [], "rejected": [], "warnings": warnings}

        if stop == "max_tokens":
            warnings.append(
                f"chunk {ci+1}: {used_model} hit the token limit "
                f"({out_max_tokens}) — JSON may be truncated. "
                f"Try a smaller `n` or split the source."
            )

        parsed = bqb._parse_json(raw)
        if not isinstance(parsed, dict):
            warnings.append(
                f"chunk {ci+1}: LLM did not return valid JSON "
                f"(stop_reason={stop!r}, response len={len(raw)} chars)."
            )
            # If this was the only chunk and we have no candidates yet, surface
            # the raw text so the UI can show what came back.
            if ci == 0 and not raw_candidates:
                return {
                    "error":         "LLM response was not valid JSON.",
                    "stop_reason":   stop,
                    "raw_response":  raw[:1500],
                    "out_max_tokens": out_max_tokens,
                    "candidates":    [],
                    "rejected":      [],
                    "warnings":      warnings,
                }
            continue
        qs = parsed.get("questions") or []
        if not isinstance(qs, list):
            warnings.append(f"chunk {ci+1}: 'questions' field was not a list.")
            continue
        for q in qs:
            if not isinstance(q, dict):
                continue
            raw_candidates.append(q)

    # Dedup
    candidates: list[dict] = []
    rejected: list[dict] = []
    existing = list(existing_questions or [])
    # Also dedup against questions generated earlier in this batch
    for cand in raw_candidates:
        text = (cand.get("text") or "").strip()
        if len(text) < 12:
            rejected.append({"reason": "too short", "text": text})
            continue
        # Strip point markers like "(2 points)" from generated text/answer
        text = bqb._strip_points(text)
        ans  = bqb._strip_points(cand.get("answer") or "")
        ch_in = cand.get("choices") or []
        choices: list[dict] = []
        for c in ch_in:
            if isinstance(c, dict):
                txt = bqb._strip_points(c.get("text") or "")
                if txt:
                    choices.append({
                        "letter": (c.get("letter") or "").upper()[:1],
                        "text": txt,
                    })
        # If LLM returned letters without proper sequence, re-letter
        for i, c in enumerate(choices):
            c["letter"] = chr(ord("A") + i)

        is_dup, matched = is_duplicate({"text": text}, existing + candidates)
        if is_dup:
            rejected.append({"reason": "duplicate", "matched": matched,
                             "text": text})
            continue

        topic = cand.get("topic") or "Other / General"
        if topic not in bqb.EVENT.topics:
            topic = bqb.classify_topic(text) or "Other / General"

        candidates.append({
            "type":           cand.get("type") or "short",
            "topic":          topic,
            "text":           text,
            "choices":        choices,
            "answer":         ans,
            "rationale":      (cand.get("rationale") or "").strip(),
            "source_snippet": (cand.get("source_snippet") or "").strip()[:240],
        })

    return {
        "candidates":     candidates,
        "rejected":       rejected,
        "warnings":       warnings,
        "model":          used_model,
        "chunks_used":    len(chunks),
        "raw_count":      len(raw_candidates),
        "out_max_tokens": out_max_tokens,
        "generated_at":   datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Persistence helper — convert a candidate into a canonical Question dict
# ---------------------------------------------------------------------------

def candidate_to_question(cand: dict, number: str, source_label: str) -> dict:
    """Convert a generated candidate into a Question record for the bank."""
    rationale = cand.get("rationale", "")
    snippet   = cand.get("source_snippet", "")
    q = {
        "number":   str(number),
        "topic":    cand.get("topic") or "Other / General",
        "text":     cand.get("text") or "",
        "choices":  list(cand.get("choices") or []),
        "answer":   cand.get("answer") or "",
        "images":   [],
        "source":   source_label,
        "year":     "",
        "division": "",
        "page":     1,
    }
    # External candidates (other LLMs, hand-written JSON) may carry a textual
    # description of a referenced diagram even before any image file exists.
    # Preserve it so the per-question "Generate diagram" chat is pre-seeded
    # with the author's intent.
    pending = (cand.get("image_description") or "").strip()
    if pending:
        # Store under a sentinel key — no actual image attached yet. The
        # review-page chat panel reads this when seeding its system prompt.
        q["image_descriptions"] = {"__pending__": pending}
    if rationale or snippet:
        # Reuse the validation slot — LLM-generated questions come with their
        # own derivation already; mark as such so the markdown renders it.
        q["validation"] = {
            "status":         "uncertain",
            "correct_answer": None,
            "rationale":      rationale,
            "source":         f"LLM-derived from: {snippet[:80]}" if snippet else "LLM-derived",
            "validated_at":   datetime.now().isoformat(timespec="seconds"),
            "model":          bqb.VISION_MODEL,
            "text_at_validation":   q["text"][:300],
            "answer_at_validation": q["answer"],
            "generated":      True,
        }
    return q
