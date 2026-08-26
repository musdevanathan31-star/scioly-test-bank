"""
The image bay must list images added from the review page, not just ones
the extraction pipeline produced.

"Images extracted from this PDF" is how a reviewer re-uses one figure across
several questions — click a thumbnail, then click a question card (shift to
share it with more than one). An image that never appears there cannot be
re-used at all.

The bay selects files by checking whether the source PDF's stem occurs in
the filename, with hyphens folded to underscores. The pipeline's own images
go through `bqb._slug`, so they are already fully underscored and matched.
Images added from the review page do not: `_slug_image_name` embeds the
bucket (the PDF filename) verbatim, so a pick off
`circuitlab_2020_bc_ssss-avdestroyer_test.pdf` lands on disk with the hyphen
intact. Folding only the needle and not the filename silently excluded every
manually picked, uploaded and generated image.

Run with: `python -m pytest tests/test_image_bay_listing.py -q`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_app as ra  # noqa: E402


def _bay_matches(pdfname: str, filename: str) -> bool:
    """The bay's selection rule, mirrored from api_images."""
    src_prefix = pdfname.replace("_test.pdf", "").replace(".pdf", "")
    needle = src_prefix.lower().replace("-", "_")
    return needle in filename.lower().replace("-", "_")


HYPHENATED = "circuitlab_2020_bc_ssss-avdestroyer_test.pdf"
PLAIN = "circuitlab_2019_b_uflorida_test.pdf"


@pytest.mark.parametrize("kind", ["pick", "up", "gen", "ctx", "img"])
def test_review_page_images_appear_in_the_bay(kind):
    """Every kind _slug_image_name produces must be listed — pick-image,
    upload, generated diagram and context figure all share that naming."""
    fname = ra._slug_image_name(HYPHENATED, "5", "png", kind)
    assert _bay_matches(HYPHENATED, fname), f"{kind!r} image {fname!r} missing from the bay"


def test_pipeline_extracted_images_still_appear():
    """The already-underscored names must not regress."""
    fname = "basic_electrical_concepts_circuitlab_2020_bc_ssss_avdestroyer_p10_i2_c5f1.png"
    assert _bay_matches(HYPHENATED, fname)


def test_unhyphenated_source_still_works():
    fname = ra._slug_image_name(PLAIN, "3", "png", "pick")
    assert _bay_matches(PLAIN, fname)


def test_a_different_pdfs_images_are_not_listed():
    """The bay is per-PDF; widening the match must not pull in neighbours."""
    other = ra._slug_image_name(PLAIN, "1", "png", "pick")
    assert not _bay_matches(HYPHENATED, other)
    mine = ra._slug_image_name(HYPHENATED, "1", "png", "pick")
    assert not _bay_matches(PLAIN, mine)


def test_question_number_with_suffix_is_listed():
    """Split question groups produce 21b/21c-style numbers."""
    fname = ra._slug_image_name(HYPHENATED, "21b", "png", "pick")
    assert _bay_matches(HYPHENATED, fname)
