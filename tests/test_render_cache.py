"""
The two caches in front of PDF page rendering.

Rendering costs real CPU on the single gunicorn worker students' answer
saves also share (26.5ms at 120dpi on this repo's own circuit_lab PDF), and
before this every page view paid it: the frontend appended a cache-busting
query param and the response carried no validators at all.

What matters: a returning client gets a 304 without rendering, a cold
client gets a file read rather than a rasterise, and neither can serve
pages from a PDF that has since been replaced.

Run with: `python -m pytest tests/test_render_cache.py -q`
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SAMPLE = REPO / "circuit_lab" / "circuitlab_2019_b_uflorida_test.pdf"
pytestmark = pytest.mark.skipif(
    not SAMPLE.is_file(),
    reason="needs a real multi-page PDF from the circuit_lab event dir",
)


@pytest.fixture()
def client(monkeypatch):
    import build_question_bank as bqb
    previous_event = bqb.current_event()

    root = tempfile.mkdtemp(prefix="rcache-")
    monkeypatch.setenv("DATA_ROOT", root)
    monkeypatch.setenv("FLASK_SECRET_KEY", "test")
    import auth, events
    for mod in (events, bqb, auth):
        importlib.reload(mod)
    import review_app
    importlib.reload(review_app)

    slug = sorted(review_app.EVENTS)[0]
    auth.create_user("coach1", "password123", "coach")
    bqb.set_event(slug)
    bqb.EVENT.base_dir.mkdir(parents=True, exist_ok=True)
    name = "circuitlab_2019_b_demo_test.pdf"
    shutil.copy(SAMPLE, bqb.EVENT.base_dir / name)

    review_app.app.config["SESSION_COOKIE_SECURE"] = False
    c = review_app.app.test_client()
    c.post("/login", data={"username": "coach1", "password": "password123"})
    url = f"/event/{slug}/api/pdf/{name}/page/1.png?dpi=120&target=test"
    yield c, url, review_app, bqb.EVENT.base_dir / name, Path(root)

    for mod in (events, bqb, auth):
        importlib.reload(mod)
    if previous_event is not None:
        bqb.set_event(previous_event.slug)


def test_a_render_is_served_with_an_etag_and_revalidation(client):
    c, url, _app, _pdf, _root = client
    r = c.get(url)
    assert r.status_code == 200
    assert r.headers["ETag"]
    # "no-cache" means revalidate before reuse, not "don't store" — the
    # browser keeps the bytes and asks with If-None-Match.
    assert r.headers["Cache-Control"] == "private, no-cache"


def test_a_matching_etag_returns_304_with_no_body(client):
    c, url, _app, _pdf, _root = client
    etag = c.get(url).headers["ETag"]
    r = c.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.get_data() == b""


def test_the_second_request_is_served_from_disk_byte_for_byte(client):
    c, url, review_app, _pdf, root = client
    first = c.get(url).get_data()
    assert list((root / ".render_cache").rglob("*.png")), "nothing was cached"
    assert c.get(url).get_data() == first


def test_replacing_the_pdf_changes_the_etag(client):
    # The key includes the source file's mtime and size, so a swapped or
    # reprocessed PDF can never be served from the previous document's
    # cached pages.
    c, url, _app, pdf, _root = client
    etag = c.get(url).headers["ETag"]
    time.sleep(0.01)
    os.utime(pdf, None)
    assert c.get(url).headers["ETag"] != etag


def test_a_stale_etag_revalidates_to_a_fresh_render(client):
    c, url, _app, pdf, _root = client
    stale = c.get(url).headers["ETag"]
    time.sleep(0.01)
    os.utime(pdf, None)
    r = c.get(url, headers={"If-None-Match": stale})
    assert r.status_code == 200
    assert len(r.get_data()) > 0


def test_different_dpi_and_target_do_not_collide(client):
    c, url, _app, _pdf, _root = client
    a = c.get(url).headers["ETag"]
    b = c.get(url.replace("dpi=120", "dpi=24")).headers["ETag"]
    assert a != b


def test_dpi_is_clamped_rather_than_trusted(client):
    # dpi drives an allocation and lands in a cache key, so an arbitrary
    # value is both a memory and a disk-filling risk.
    c, url, _app, _pdf, _root = client
    assert c.get(url.replace("dpi=120", "dpi=99999")).status_code == 200
    assert c.get(url.replace("dpi=120", "dpi=notanumber")).status_code == 200


def test_the_cache_lives_under_a_dot_directory(client):
    # backup-bulk-data.sh and migrate-data-root.sh both discover event
    # directories with a bare `*/` glob, which skips dotfiles — that is what
    # keeps derived data out of the nightly restic snapshot.
    c, url, review_app, _pdf, root = client
    c.get(url)
    assert review_app._RENDER_CACHE_DIR.name.startswith(".")
    assert review_app._RENDER_CACHE_DIR.parent == root


def test_disabling_the_disk_cache_still_serves_pages(client, monkeypatch):
    c, url, review_app, _pdf, root = client
    monkeypatch.setattr(review_app, "RENDER_CACHE_MAX_MB", 0)
    r = c.get(url)
    assert r.status_code == 200 and len(r.get_data()) > 0
    assert not list((root / ".render_cache").rglob("*.png"))
