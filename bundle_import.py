"""
Import a curated question *bundle* (manifest.json + images/) into an event's
question bank.

The bundle format is the one QUESTION_EXPORT_PROMPT.md asks other Claude
conversations to produce: a zip holding `manifest.json` (a wrapper with
`event`, `season`, `contexts[]`, `questions[]`) plus an `images/` folder. A
bare `manifest.json` (no zip, so no images) is accepted too.

Split in two so the slow part can run as a background job:

  1. `open_bundle()` + `preview()` — run in the upload request. Read and
     check the file, then report what an import WOULD do (counts, remapped
     topics, missing images, duplicates) without writing anything.
  2. `run_import()` — the jobs.py target. Copies images into the event's
     image dir, then adds every question and shared context in ONE
     `_state_transaction()`. A cancel or failure part-way leaves the bank
     untouched (the transaction only saves when its body doesn't raise) and
     deletes any image file this run newly created.

Nothing in here ever extracts a zip member by its own name: images are read
into memory, sniffed, and written under a server-chosen filename, so a
hostile member path ("../../x") has nowhere to go.

Mapping notes (see spec.md "Question bundle import"):
  - `numerical` is not a stored qtype yet; it lands as `frq`, with
    `import_meta.qtype = "numerical"` and a best-effort parsed
    `import_meta.numeric = {value, unit}` so a later numerical-question
    feature can promote these without re-importing.
  - Topics not in the event's taxonomy are remapped (case-insensitive match,
    else `classify_topic()`); the bundle's own name is kept in
    `import_meta.topic`.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

import build_question_bank as bqb
import qgen
from text_utils import parse_answer_letters

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_QUESTIONS = 5000
RESULT_QUESTION_CAP = 200

# Allowed image extensions → the canonical format their bytes must sniff as.
# SVG is deliberately absent: it's served same-origin by serve_image() and can
# carry script.
_IMAGE_EXTS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
               ".gif": "gif", ".webp": "webp"}
_BUNDLE_QTYPES = {"mcq", "tf", "numerical", "frq"}


class BundleError(ValueError):
    """The uploaded file isn't a usable bundle. Message is user-facing."""


def sniff_image(data: bytes) -> str | None:
    """Format name from magic bytes, or None if it isn't an allowed image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# ---------------------------------------------------------------------------
# Reading the upload
# ---------------------------------------------------------------------------

@dataclass
class Bundle:
    path: Path
    manifest: dict
    digest: str                          # sha256 of the file, first 8 hex chars
    is_zip: bool
    image_members: dict[str, str] = field(default_factory=dict)  # basename -> zip member

    def read_image(self, name: str) -> tuple[bytes | None, str]:
        """(bytes, "") for a usable image, else (None, reason)."""
        base = PurePosixPath(str(name).replace("\\", "/")).name
        ext = PurePosixPath(base).suffix.lower()
        if ext == ".svg":
            return None, "SVG images aren't accepted"
        if ext not in _IMAGE_EXTS:
            return None, f"unsupported image type {ext or '(none)'}"
        member = self.image_members.get(base)
        if not self.is_zip or member is None:
            return None, "not found in the bundle"
        with zipfile.ZipFile(self.path) as zf:
            info = zf.getinfo(member)
            if info.file_size > MAX_IMAGE_BYTES:
                return None, f"larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
            try:
                with zf.open(info) as fh:
                    data = fh.read(MAX_IMAGE_BYTES + 1)
            except (RuntimeError, zipfile.BadZipFile, OSError) as e:
                return None, f"couldn't be read ({e})"
        if len(data) > MAX_IMAGE_BYTES:
            return None, f"larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        kind = sniff_image(data)
        if kind is None or kind != _IMAGE_EXTS[ext]:
            return None, f"contents aren't a {_IMAGE_EXTS[ext].upper()} image"
        return data, ""


def _parse_manifest_text(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BundleError("manifest.json isn't UTF-8 text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # Same LaTeX-backslash repair ladder the JSON importer uses.
        parsed = bqb._parse_json(text)
        if parsed is None:
            raise BundleError(f"manifest.json isn't valid JSON: {e}")
    if not isinstance(parsed, dict):
        raise BundleError("manifest.json must be a JSON object with a \"questions\" list")
    qs = parsed.get("questions")
    if not isinstance(qs, list) or not qs:
        raise BundleError("manifest.json has no \"questions\" list")
    if len(qs) > MAX_QUESTIONS:
        raise BundleError(f"too many questions ({len(qs)}; limit {MAX_QUESTIONS})")
    if not isinstance(parsed.get("contexts") or [], list):
        raise BundleError("\"contexts\" must be a list")
    return parsed


def open_bundle(path: Path) -> Bundle:
    """Read and sanity-check a staged upload (zip or bare manifest.json).
    Raises BundleError with a user-facing message."""
    path = Path(path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        head = fh.read(4)
        fh.seek(0)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()[:8]

    if head != b"PK\x03\x04":
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise BundleError("manifest.json is too large")
        return Bundle(path=path, manifest=_parse_manifest_text(path.read_bytes()),
                      digest=digest, is_zip=False)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise BundleError("the file isn't a readable zip")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES:
            raise BundleError(f"the zip has {len(infos)} files (limit {MAX_ZIP_ENTRIES})")
        # Skip macOS resource-fork noise that Finder's "Compress" adds.
        infos = [i for i in infos if not i.filename.startswith("__MACOSX/")
                 and not PurePosixPath(i.filename).name.startswith("._")]
        manifests = [i for i in infos if PurePosixPath(i.filename).name == MANIFEST_NAME]
        if not manifests:
            raise BundleError("the zip has no manifest.json")
        # A zipped folder puts everything one level down — take the shallowest.
        mi = min(manifests, key=lambda i: len(PurePosixPath(i.filename).parts))
        if mi.file_size > MAX_MANIFEST_BYTES:
            raise BundleError("manifest.json is too large")
        try:
            manifest = _parse_manifest_text(zf.read(mi))
        except (RuntimeError, zipfile.BadZipFile) as e:
            raise BundleError(f"manifest.json couldn't be read ({e})")

        total = 0
        members: dict[str, str] = {}
        for i in infos:
            name = PurePosixPath(i.filename).name
            if PurePosixPath(name).suffix.lower() not in (set(_IMAGE_EXTS) | {".svg"}):
                continue
            total += i.file_size
            # First one wins when two folders hold the same basename; prefer
            # the conventional images/ folder by visiting it first below.
            members.setdefault(name, i.filename)
        # Re-prefer members that sit directly in an images/ folder.
        for i in infos:
            p = PurePosixPath(i.filename)
            if p.parent.name == "images" and p.name in members:
                members[p.name] = i.filename
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise BundleError(
                f"images total {total // (1024 * 1024)} MB "
                f"(limit {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MB)")
    return Bundle(path=path, manifest=manifest, digest=digest, is_zip=True,
                  image_members=members)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------

_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_NUMERIC_RE = re.compile(
    r"""^\s*(?:≈|~|about\s+|approx(?:imately|\.)?\s+)?
        (?P<num>[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|[+\-−]?\.\d+)
        (?:\s*[eE](?P<e1>[+\-−]?\d+)
          |\s*(?:x|×|\*|·)\s*10\s*(?:\^|\*\*)?\s*\{?(?P<e2>[+\-−]?\d+)\}?)?
        \s*(?P<unit>.*?)\s*\.?\s*$""",
    re.X | re.I)


def parse_numeric_answer(answer: str) -> dict | None:
    """Best-effort "4.2 m/s" -> {"value": 4.2, "unit": "m/s"}. None when the
    answer doesn't start with a number or the tail is prose, not a unit.
    Stored only as import metadata — the answer text itself is never touched."""
    s = str(answer or "").strip()
    if not s:
        return None
    # Undo the common LaTeX wrappers: $...$, \times, \text{...}, \, and ^{...}.
    s = s.replace("$", "")
    s = re.sub(r"\\(?:text|mathrm|rm)\s*\{([^}]*)\}", r"\1", s)
    s = s.replace("\\times", "×").replace("\\cdot", "·")
    s = re.sub(r"\\[,;: ]", " ", s)
    s = s.translate(_SUPERSCRIPT)
    m = _NUMERIC_RE.match(s)
    if not m:
        return None
    unit = m.group("unit").strip()
    # A unit is short and has at most a couple of tokens ("m/s", "kg m/s^2");
    # anything longer is an explanation ("4 because the current doubles").
    if len(unit) > 24 or len(unit.split()) > 3:
        return None
    try:
        value = float(m.group("num").replace(",", "").replace("−", "-"))
        exp = m.group("e1") or m.group("e2")
        if exp:
            value *= 10 ** int(exp.replace("−", "-"))
    except (ValueError, OverflowError):
        return None
    return {"value": value, "unit": unit}


def stored_image_name(digest: str, name: str) -> str:
    """Server-chosen filename for a bundle image: `imp_<digest>_<stem><ext>`.
    Deterministic per bundle file, so re-importing the same bundle maps to
    the same files instead of piling up copies."""
    from werkzeug.utils import secure_filename
    p = PurePosixPath(str(name).replace("\\", "/"))
    ext = p.suffix.lower()
    stem = secure_filename(p.stem)[:60] or "image"
    return f"imp_{digest}_{stem}{ext}"


def context_prefix(digest: str) -> str:
    return f"imp{digest}_"


def _clean_difficulty(v) -> tuple[float | None, str]:
    if v is None or v == "":
        return None, ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, f"difficulty {v!r} isn't a number — left unrated"
    if f != f:  # NaN
        return None, "difficulty isn't a number — left unrated"
    if f < 0.0 or f > 1.0:
        return max(0.0, min(1.0, f)), f"difficulty {f} clamped to 0-1"
    return f, ""


def _resolve_topic(raw: str, text: str, topics: list[str],
                   classify: Callable[[str], str] | None = None) -> tuple[str, bool]:
    """(topic, remapped?). Exact → case-insensitive → classify_topic().

    The classifier sees the bundle's own topic name ahead of the stem: the
    name is the author's summary of what the question is about ("Ohm's Law"),
    and the stem alone can be dominated by incidental keywords ("voltage"
    pulled an Ohm's-law question into AC Circuits in a live test)."""
    raw = (raw or "").strip()
    if raw in topics:
        return raw, False
    for t in topics:
        if t.lower() == raw.lower():
            return t, False
    guess = (classify or bqb.classify_topic)(f"{raw} {text}".strip())
    return (guess or "Other / General"), True


def convert_question(bq: dict, *, digest: str, topics: list[str], season: str,
                     source_label: str, image_names: dict[str, str | None],
                     classify: Callable[[str], str] | None = None
                     ) -> tuple[dict | None, list[str]]:
    """Map one bundle question onto the bank's Question dict shape.

    `image_names` maps each bundle image reference -> stored filename, or
    None when that image is missing/refused (the reference is then dropped).
    Returns (question_without_number, issues); question is None when the
    entry can't be imported at all (issues then says why). The caller
    assigns `number`."""
    issues: list[str] = []
    if not isinstance(bq, dict):
        return None, ["not a JSON object"]
    text = bqb._strip_points(str(bq.get("text") or "")).strip()
    if not text:
        return None, ["no question text"]

    raw_qtype = str(bq.get("qtype") or "").strip().lower()
    raw_choices = [c for c in (bq.get("choices") or []) if isinstance(c, dict)]
    if raw_qtype not in _BUNDLE_QTYPES:
        inferred = "mcq" if raw_choices else "frq"
        if raw_qtype:
            issues.append(f"unknown qtype {raw_qtype!r}, treated as {inferred}")
        raw_qtype = inferred

    answer = bqb._strip_points(str(bq.get("answer") or "")).strip()
    choices: list[dict] = []
    stored_qtype = raw_qtype
    if raw_qtype == "mcq":
        old_choices = []
        for c in raw_choices:
            ctext = bqb._strip_points(str(c.get("text") or "")).strip()
            if ctext:
                old_choices.append({"letter": str(c.get("letter") or "").strip().upper()[:1],
                                    "text": ctext})
        if not old_choices:
            issues.append("mcq with no choices, imported as frq")
            stored_qtype = "frq"
        else:
            relabel = {}
            for i, c in enumerate(old_choices):
                new = chr(ord("A") + i)
                if c["letter"]:
                    relabel[c["letter"]] = new
                choices.append({"letter": new, "text": c["text"]})
            picked = parse_answer_letters(answer, old_choices)
            if picked:
                answer = ", ".join(sorted(relabel.get(p, p) for p in picked))
            elif answer:
                issues.append("mcq answer isn't one of its choice letters")
    elif raw_qtype == "tf":
        norm = bqb._normalize_tf_answer(answer)
        if norm is not None:
            answer = norm
        else:
            issues.append("tf answer isn't True/False")
    if stored_qtype == "numerical":
        stored_qtype = "frq"
    if not answer:
        issues.append("no answer")

    topic, remapped = _resolve_topic(str(bq.get("topic") or ""), text, topics, classify)
    year = season if re.fullmatch(r"\d{4}", season or "") else ""
    q: dict = {
        "topic":    topic,
        "text":     text,
        "choices":  choices,
        "answer":   answer,
        "images":   [],
        "source":   source_label,
        "year":     year,
        "division": "",
        "page":     1,
        "qtype":    stored_qtype,
    }

    for ref in (bq.get("images") or []):
        stored = image_names.get(str(ref))
        if stored:
            if stored not in q["images"]:
                q["images"].append(stored)
        else:
            issues.append(f"image {ref!r} missing or refused")

    difficulty, why = _clean_difficulty(bq.get("difficulty"))
    if why:
        issues.append(why)
    if difficulty is not None:
        q["difficulty"] = difficulty

    ctx = bq.get("context_id")
    if ctx:
        q["context_id"] = context_prefix(digest) + str(ctx)

    pending = str(bq.get("image_description") or "").strip()
    if pending:
        q["image_descriptions"] = {"__pending__": pending}

    justification = str(bq.get("justification") or "").strip()
    snippet = str(bq.get("source_snippet") or "").strip()[:240]
    if justification or snippet:
        q["validation"] = {
            "status":               "uncertain",
            "correct_answer":       None,
            "rationale":            justification,
            "source":               f"Imported bundle; from: {snippet[:80]}" if snippet else "Imported bundle",
            "validated_at":         datetime.now().isoformat(timespec="seconds"),
            "model":                "import",
            "text_at_validation":   text[:300],
            "answer_at_validation": answer,
            "generated":            True,
        }
    if not justification:
        issues.append("no justification")

    meta: dict = {"bundle": digest, "id": str(bq.get("id") or "")}
    if raw_qtype == "numerical":
        meta["qtype"] = "numerical"
        parsed = parse_numeric_answer(answer)
        if parsed:
            meta["numeric"] = parsed
    if remapped:
        meta["topic"] = str(bq.get("topic") or "")
    q["import_meta"] = meta
    return q, issues


def convert_context(bc: dict, *, digest: str, image_names: dict[str, str | None]
                    ) -> dict | None:
    if not isinstance(bc, dict) or not bc.get("id"):
        return None
    ctx = {
        "id":     context_prefix(digest) + str(bc["id"]),
        "text":   str(bc.get("text") or ""),
        "images": [image_names[str(r)] for r in (bc.get("images") or [])
                   if image_names.get(str(r))],
        "pages":  [],
    }
    if bc.get("title"):
        ctx["title"] = str(bc["title"])
    return ctx


def _all_image_refs(manifest: dict) -> list[str]:
    refs: list[str] = []
    for item in list(manifest.get("questions") or []) + list(manifest.get("contexts") or []):
        if isinstance(item, dict):
            for r in (item.get("images") or []):
                if str(r) not in refs:
                    refs.append(str(r))
    return refs


def _label(bundle: Bundle, filename: str) -> str:
    ev = str(bundle.manifest.get("event") or "").strip()
    return f"Imported · {ev or filename}"[:120]


def _warnings(bundle: Bundle) -> list[str]:
    out = []
    ev = str(bundle.manifest.get("event") or "").strip()
    cur = bqb.EVENT
    if ev and cur.name.lower() not in ev.lower() and ev.lower() not in cur.name.lower():
        out.append(f"bundle says event \"{ev}\" but you're importing into \"{cur.name}\"")
    if not bundle.is_zip and _all_image_refs(bundle.manifest):
        out.append("a bare manifest.json has no images/ folder — referenced images will be skipped")
    return out


def _dedup_pool(state: dict) -> list[dict]:
    pool: list[dict] = []
    for qs in (state.get("questions") or {}).values():
        pool.extend(qs or [])
    return pool


def _plan(bundle: Bundle, state: dict, filename: str, image_names: dict[str, str | None],
          should_cancel: Callable[[], bool] | None = None,
          on_step: Callable[[int, int], None] | None = None) -> dict:
    """Convert + dedup every question against `state`. Pure w.r.t. `state`."""
    topics = list(bqb.EVENT.topics)
    season = str(bundle.manifest.get("season") or "")
    label = _label(bundle, filename)
    existing = _dedup_pool(state)
    accepted: list[dict] = []
    duplicates, invalid, issues = [], [], []
    raw_qs = bundle.manifest.get("questions") or []
    for i, bq in enumerate(raw_qs):
        if should_cancel and should_cancel():
            from jobs import JobCancelled
            raise JobCancelled()
        bid = str(bq.get("id") or f"#{i + 1}") if isinstance(bq, dict) else f"#{i + 1}"
        q, q_issues = convert_question(bq, digest=bundle.digest, topics=topics, season=season,
                                       source_label=label, image_names=image_names)
        if q is None:
            invalid.append({"id": bid, "reason": "; ".join(q_issues)})
        else:
            # Parts of one shared-context group legitimately look alike
            # ("Refer to the table. What is..."), so don't dedup a question
            # against its own siblings from this bundle.
            siblings = {id(a) for a in accepted
                        if q.get("context_id") and a.get("context_id") == q.get("context_id")}
            pool = existing + [a for a in accepted if id(a) not in siblings]
            is_dup, matched = qgen.is_duplicate({"text": q["text"]}, pool)
            if is_dup:
                duplicates.append({"id": bid, "matched": matched, "text": q["text"][:120]})
            else:
                accepted.append(q)
                if q_issues:
                    issues.append({"id": bid, "issues": q_issues})
        if on_step:
            on_step(i + 1, len(raw_qs))
    return {"accepted": accepted, "duplicates": duplicates, "invalid": invalid,
            "issues": issues}


def _summary(bundle: Bundle, plan: dict, image_problems: list[dict]) -> dict:
    by_topic: dict[str, dict[str, int]] = {}
    remapped: dict[str, str] = {}
    for q in plan["accepted"]:
        kind = (q.get("import_meta") or {}).get("qtype") or q["qtype"]
        by_topic.setdefault(q["topic"], {}).setdefault(kind, 0)
        by_topic[q["topic"]][kind] += 1
        orig = (q.get("import_meta") or {}).get("topic")
        if orig is not None:
            remapped[orig or "(blank)"] = q["topic"]
    return {
        "event":         str(bundle.manifest.get("event") or ""),
        "season":        str(bundle.manifest.get("season") or ""),
        "total":         len(bundle.manifest.get("questions") or []),
        "importable":    len(plan["accepted"]),
        "by_topic":      by_topic,
        "remapped_topics": remapped,
        "duplicates":    plan["duplicates"],
        "invalid":       plan["invalid"],
        "issues":        plan["issues"],
        "image_problems": image_problems,
        "with_images":   sum(1 for q in plan["accepted"] if q["images"]),
        "with_difficulty": sum(1 for q in plan["accepted"] if "difficulty" in q),
        "contexts":      len(bundle.manifest.get("contexts") or []),
        "warnings":      _warnings(bundle),
    }


def _check_images(bundle: Bundle, on_step=None, should_cancel=None
                  ) -> tuple[dict[str, str | None], dict[str, bytes], list[dict]]:
    """Read + sniff every referenced image once. Returns
    (ref -> stored name or None, stored name -> bytes, problems)."""
    names: dict[str, str | None] = {}
    blobs: dict[str, bytes] = {}
    problems: list[dict] = []
    refs = _all_image_refs(bundle.manifest)
    for i, ref in enumerate(refs):
        if should_cancel and should_cancel():
            from jobs import JobCancelled
            raise JobCancelled()
        data, why = bundle.read_image(ref)
        if data is None:
            names[ref] = None
            problems.append({"image": ref, "reason": why})
        else:
            stored = stored_image_name(bundle.digest, ref)
            names[ref] = stored
            blobs[stored] = data
        if on_step:
            on_step(i + 1, len(refs))
    return names, blobs, problems


def preview(bundle: Bundle, filename: str) -> dict:
    """What an import would do, against the bank as it is right now.
    Writes nothing. Caller must have bound the event (bqb.set_event)."""
    names, _blobs, problems = _check_images(bundle)
    plan = _plan(bundle, bqb._load_state(), filename, names)
    return _summary(bundle, plan, problems)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def run_import(bundle: Bundle, *, filename: str, bucket: str, mark_validated: bool,
               next_number: Callable[[dict], int],
               should_cancel: Callable[[], bool],
               on_progress: Callable[..., None]) -> dict:
    """jobs.py target body. Caller has bound the event. Returns the summary
    dict stored as the job's `result`.

    Phase names stay fixed within a stage and the count goes in done/total:
    jobs.py rewrites the job index on every phase *change* but throttles
    count-only updates to ~1/s, so a per-item phase string would rewrite the
    index once per question."""
    on_progress(phase="checking images", done=0, total=0)
    print(f"Bundle {filename} ({bundle.digest}): "
          f"{len(bundle.manifest.get('questions') or [])} question(s)")
    names, blobs, problems = _check_images(
        bundle, should_cancel=should_cancel,
        on_step=lambda d, t: on_progress(phase="checking images", done=d, total=t))
    for p in problems:
        print(f"  image {p['image']}: {p['reason']}")

    image_dir: Path = bqb.EVENT.image_dir
    image_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for i, (stored, data) in enumerate(blobs.items()):
            if should_cancel():
                from jobs import JobCancelled
                raise JobCancelled()
            dest = image_dir / stored
            if not dest.exists():
                dest.write_bytes(data)
                created.append(dest)
            on_progress(phase="copying images", done=i + 1, total=len(blobs))

        added: list[dict] = []
        skipped_validation: list[dict] = []
        with bqb._state_transaction() as state:
            def step(d, t):
                on_progress(phase="importing questions", done=d, total=t)
            plan = _plan(bundle, state, filename, names,
                         should_cancel=should_cancel, on_step=step)

            num = next_number(state)
            for q in plan["accepted"]:
                q["number"] = str(num)
                num += 1
                if mark_validated:
                    ok, reason = bqb.question_gradeability(q)
                    if ok:
                        q["validation"] = {
                            "status":               "correct",
                            "correct_answer":       None,
                            "rationale":            ((q.get("validation") or {}).get("rationale")
                                                     or "Marked validated on import."),
                            "source":               "Imported bundle (manually marked validated)",
                            "validated_at":         datetime.now().isoformat(timespec="seconds"),
                            "model":                "import",
                            "text_at_validation":   q["text"][:300],
                            "answer_at_validation": q["answer"],
                        }
                    else:
                        skipped_validation.append({"number": q["number"], "reason": reason})
                added.append(q)
                print(f"  + Q{q['number']} [{q['topic']}] {q['text'][:70]}")
            for d in plan["duplicates"]:
                print(f"  = {d['id']} duplicate of Q{d['matched']}: {d['text'][:60]}")
            for bad in plan["invalid"]:
                print(f"  ! {bad['id']} skipped: {bad['reason']}")

            if should_cancel():
                from jobs import JobCancelled
                raise JobCancelled()

            if added:
                qs = state.setdefault("questions", {})
                qs[bucket] = list(qs.get(bucket, [])) + added
                state.setdefault("manual", {})[bucket] = {
                    "edited_at": datetime.now().isoformat(timespec="seconds"),
                }
                used_ctx = {q["context_id"] for q in added if q.get("context_id")}
                ann = state.setdefault("annotations", {}).setdefault(bucket, {})
                ctx_list = list(ann.get("contexts") or [])
                have = {c.get("id") for c in ctx_list}
                for bc in (bundle.manifest.get("contexts") or []):
                    c = convert_context(bc, digest=bundle.digest, image_names=names)
                    if c and c["id"] in used_ctx and c["id"] not in have:
                        ctx_list.append(c)
                        have.add(c["id"])
                ann["contexts"] = ctx_list
    except BaseException:
        for p in created:
            try:
                p.unlink()
            except OSError:
                pass
        raise

    # Images nothing ended up referencing (every question using them was a
    # duplicate) — don't leave them behind.
    referenced = {img for q in added for img in q["images"]}
    for c in (bundle.manifest.get("contexts") or []):
        cc = convert_context(c, digest=bundle.digest, image_names=names)
        if cc and any(q.get("context_id") == cc["id"] for q in added):
            referenced.update(cc["images"])
    for p in created:
        if p.name not in referenced:
            try:
                p.unlink()
            except OSError:
                pass

    summary = _summary(bundle, plan, problems)
    summary.update({
        "added": len(added),
        "bucket": bucket,
        "skipped_validation_ungradeable": skipped_validation,
        # Capped: the result is persisted in the event's job index, and the
        # page only needs enough to show what landed (Browse has the rest).
        "questions": [dict(q, _bucket=bucket) for q in added[:RESULT_QUESTION_CAP]],
    })
    print(f"Done: {len(added)} added, {len(plan['duplicates'])} duplicate(s), "
          f"{len(plan['invalid'])} invalid.")
    return summary
