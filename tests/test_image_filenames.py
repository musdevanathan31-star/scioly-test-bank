"""
Generated image filenames must survive the serving round-trip unchanged.

Images are written to disk under a name built by `_slug_image_name()`, and
served back through `serve_image()` -> `_safe_join()`, which runs the name
through `secure_filename()`. If the two sides disagree by even one
character the file exists under one name while the app looks for another,
`p.exists()` is False, and the `<img>` 404s into a permanently broken
thumbnail with no error anywhere the user can see.

This bit in practice: the PDF's own filename is embedded in the image name,
and a real scraped test in this repo is called
`circuitlab_2019_c_ssss-utf-8u+6211u+662f_test.pdf`. `secure_filename()`
strips the `+`, so every picked/generated image on that PDF 404'd. Only
upload-image happened to wrap the call; pick-image, save-svg and
context-image did not.

Run with: `python -m pytest tests/test_image_filenames.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from werkzeug.utils import secure_filename

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_app as ra  # noqa: E402


# Buckets that actually occur, plus the hostile-character cases that broke.
BUCKETS = [
    "circuitlab_2019_b_uflorida_test.pdf",
    "circuitlab_2019_c_ssss-utf-8u+6211u+662f_test.pdf",   # the real-world failure
    "some test with spaces_test.pdf",
    "_leading_underscore_test.pdf",
    "naïve_accents_test.pdf",
]

KINDS = ["img", "up", "gen", "pick", "ctx"]


@pytest.mark.parametrize("bucket", BUCKETS)
@pytest.mark.parametrize("kind", KINDS)
def test_generated_name_is_stable_under_secure_filename(bucket, kind):
    """The name written to disk must equal the name the serving side derives
    from it — i.e. it must already be a secure_filename fixed point."""
    name = ra._slug_image_name(bucket, "5", "png", kind)
    assert name == secure_filename(name), (
        f"{name!r} is rewritten to {secure_filename(name)!r} when served, "
        "so the file would 404"
    )


@pytest.mark.parametrize("bucket", BUCKETS)
def test_generated_name_is_nonempty_and_keeps_extension(bucket):
    name = ra._slug_image_name(bucket, "5", "png", "pick")
    assert name
    assert name.endswith(".png")


def test_question_number_with_dup_suffix_survives():
    """extract_questions() emits dup-numbered questions as 5b/5c/5d."""
    name = ra._slug_image_name("circuitlab_2019_b_uflorida_test.pdf", "5b", "png", "pick")
    assert name == secure_filename(name)
    assert "_q5b_" in name


def test_names_are_unique_per_call():
    """The random suffix must keep repeat captures on one question distinct,
    or a second capture would overwrite the first on disk."""
    names = {ra._slug_image_name("circuitlab_2019_b_uflorida_test.pdf", "5", "png", "pick")
             for _ in range(50)}
    assert len(names) == 50
