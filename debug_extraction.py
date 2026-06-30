"""
LOCAL DEBUG ONLY — never deploy this, never add it to a .service file.

A standalone Flask app (separate process, separate port, zero auth/session/
CSRF/jobs machinery) for debugging the manual question-capture extraction
flows: drag-a-region text/MCQ capture, drag-a-column matching capture, and
their Haiku-vision fallbacks. It imports build_question_bank.py's actual
extraction functions directly, so results here are guaranteed to match what
review_app.py's production routes would do — but every intermediate step is
exposed side by side instead of just the one result production returns.

Run: python debug_extraction.py
Browse: http://localhost:5099/
"""
from __future__ import annotations

import base64

import fitz
from flask import Flask, abort, jsonify, render_template, request, Response

import build_question_bank as bqb
import events as events_mod

app = Flask(__name__, template_folder="templates_debug")


def _list_test_pdfs(ev) -> list[str]:
    return sorted(p.name for p in ev.base_dir.glob(f"{ev.filename_prefix}_*_test.pdf"))


def _open_pdf(ev, pdfname: str) -> fitz.Document:
    path = ev.base_dir / pdfname
    if not path.exists():
        abort(404, f"PDF not found: {path}")
    return fitz.open(str(path))


def _strip_bytes(obj):
    """Recursively replace any bytes value with a short placeholder so
    page.get_text("dict")'s embedded image/mask blobs don't blow up
    jsonify() — we only care about the text/layout structure here."""
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes, omitted>"
    if isinstance(obj, dict):
        return {k: _strip_bytes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_bytes(v) for v in obj]
    return obj


def _region_rect(page: fitz.Page, data: dict) -> fitz.Rect:
    try:
        x = float(data["x"]); y = float(data["y"])
        w = float(data["w"]); h = float(data["h"])
    except (KeyError, ValueError, TypeError):
        abort(400, "bad region: need numeric x/y/w/h")
    if w < 4 or h < 4:
        abort(400, "region too small")
    dpi = float(data.get("dpi", 120))
    f = 72.0 / dpi
    return fitz.Rect(x * f, y * f, (x + w) * f, (y + h) * f)


@app.route("/")
def index():
    evs = sorted(events_mod.EVENTS.values(), key=lambda e: e.slug)
    return render_template("debug_extraction.html", mode="events", events=evs)


@app.route("/event/<slug>")
def event_page(slug):
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    pdfs = _list_test_pdfs(ev)
    return render_template("debug_extraction.html", mode="pdfs", event=ev, pdfs=pdfs)


@app.route("/event/<slug>/pdf/<pdfname>")
def pdf_page(slug, pdfname):
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    page_count = doc.page_count
    pno = int(request.args.get("page", "1"))
    pno = max(1, min(pno, page_count))
    return render_template(
        "debug_extraction.html", mode="debug",
        event=ev, pdfname=pdfname, page_count=page_count, pno=pno,
        vision_available=bqb._vision_available(),
    )


@app.route("/event/<slug>/pdf/<pdfname>/page/<int:pno>.png")
def render_page(slug, pdfname, pno):
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    mat = fitz.Matrix(120 / 72, 120 / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return Response(pix.tobytes("png"), mimetype="image/png")


@app.route("/event/<slug>/pdf/<pdfname>/page/<int:pno>/debug/region", methods=["POST"])
def debug_region(slug, pdfname, pno):
    """Runs BOTH split_choices() and split_choices_by_lines() on the dragged
    region, regardless of which one would actually win in production —
    api_extract_region() only falls back to the line-splitter when the
    primary parser finds zero choices, so seeing both always is the point."""
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    data = request.get_json() or {}
    rect = _region_rect(page, data)
    raw = page.get_text("text", clip=rect) or ""
    text = " ".join(raw.split())
    text = bqb._strip_points(text)

    stem1, choices1 = bqb.split_choices(text)
    stem2, choices2 = bqb.split_choices_by_lines(raw)
    production_would_use = "split_choices_by_lines" if (not choices1 and choices2) else "split_choices"

    return jsonify({
        "raw_text_repr": repr(raw),
        "joined_text": text,
        "split_choices": {"stem": bqb._strip_points(stem1),
                           "choices": [{"letter": c["letter"], "text": bqb._strip_points(c["text"])}
                                       for c in choices1]},
        "split_choices_by_lines": {"stem": bqb._strip_points(stem2 or ""),
                                    "choices": [{"letter": c["letter"], "text": bqb._strip_points(c["text"])}
                                                for c in choices2]},
        "production_would_use": production_would_use,
    })


@app.route("/event/<slug>/pdf/<pdfname>/page/<int:pno>/debug/column", methods=["POST"])
def debug_column(slug, pdfname, pno):
    """Runs split_column_items() for BOTH label charsets side by side — a
    mismatched charset (e.g. numeric labels parsed as alpha) becomes visible
    immediately instead of having to guess and re-drag."""
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    data = request.get_json() or {}
    rect = _region_rect(page, data)
    raw = page.get_text("text", clip=rect) or ""

    return jsonify({
        "raw_text_repr": repr(raw),
        "numeric": bqb.split_column_items(raw, "numeric"),
        "alpha": bqb.split_column_items(raw, "alpha"),
    })


@app.route("/event/<slug>/pdf/<pdfname>/page/<int:pno>/debug/vision", methods=["POST"])
def debug_vision(slug, pdfname, pno):
    """Haiku-vision fallback for either mode — returns the raw LLM
    response/error verbatim, no shape-mapping for production consumption."""
    if not bqb._vision_available():
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 400
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    data = request.get_json() or {}
    rect = _region_rect(page, data)
    b64 = bqb.region_image_b64(page, rect, dpi=200)
    column_mode = data.get("column_mode")
    if column_mode in ("numeric", "alpha"):
        result = bqb.vision_extract_column(b64, column_mode)
    else:
        result = bqb.vision_extract_region(b64)
    return jsonify({"result": result, "b64_preview_len": len(b64)})


@app.route("/event/<slug>/pdf/<pdfname>/page/<int:pno>/debug/dump")
def debug_dump(slug, pdfname, pno):
    """No region needed — full-page text and block/line/span structure, for
    "what does PyMuPDF even see on this page" questions independent of any
    specific drag."""
    ev = events_mod.EVENTS.get(slug)
    if ev is None:
        abort(404, f"unknown event: {slug}")
    bqb.set_event(slug)
    doc = _open_pdf(ev, pdfname)
    if pno < 1 or pno > doc.page_count:
        abort(404)
    page = doc[pno - 1]
    return jsonify({
        "text": page.get_text("text"),
        "dict": _strip_bytes(page.get_text("dict")),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5099)
