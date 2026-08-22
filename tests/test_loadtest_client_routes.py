"""
Every endpoint loadtest_students.py calls must still exist in the app.

That script is an HTTP client living outside the request path, so nothing
else here exercises it: the app's own tests pass, the templates render, and
the drift only surfaces as a 404 when someone actually runs a capacity
test. The Test -> Assessment rename did exactly that — it updated
review_app, the templates, the tests and the docs, and left this client
calling /api/tests/<id>.

Run with: `python -m pytest tests/test_loadtest_client_routes.py -q`
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# f-string URLs of the form f"{base_url}/some/path/{placeholder}"
_URL = re.compile(r"\{base_url\}(/[^\"']*)")


def _client_paths() -> list[str]:
    source = (REPO / "loadtest_students.py").read_text(encoding="utf-8")
    paths = set()
    for raw in _URL.findall(source):
        raw = raw.split("?")[0].rstrip("/")
        if not raw:
            continue
        # Substitute a plausible value for each {placeholder}; the URL map
        # only needs something that matches the converter.
        paths.add(re.sub(r"\{[^}]+\}", "x", raw))
    return sorted(paths)


@pytest.fixture(scope="module")
def url_adapter():
    import importlib
    os.environ.setdefault("DATA_ROOT", tempfile.mkdtemp(prefix="ltroutes-"))
    os.environ.setdefault("FLASK_SECRET_KEY", "test")
    import review_app
    importlib.reload(review_app)
    # Bound without the APPLICATION_ROOT prefix: _PrefixMiddleware strips it
    # before routing, so the map holds unprefixed rules.
    return review_app.app.url_map.bind("localhost")


def test_some_paths_were_actually_found():
    # If the extraction regex stops matching, every assertion below would
    # pass vacuously.
    paths = _client_paths()
    assert len(paths) >= 5, paths


def test_every_endpoint_the_loadtest_client_calls_exists(url_adapter):
    from werkzeug.exceptions import MethodNotAllowed, NotFound

    missing = []
    for path in _client_paths():
        # The question is whether the route exists at all, not which verbs
        # it accepts — so MethodNotAllowed counts as found. (An earlier
        # version used adapter.test(), which defaults to GET and therefore
        # reported every POST/DELETE-only endpoint here as missing.)
        try:
            url_adapter.match(path)
        except MethodNotAllowed:
            continue
        except NotFound:
            missing.append(path)
    assert not missing, (
        "loadtest_students.py calls endpoints this app no longer serves: "
        f"{missing}. Renaming a route means updating that client too — it "
        "lives outside the request path, so nothing else catches it."
    )
