"""
Unit tests for text_utils.difficulty_band() / difficulty_value_for_band() --
the pure helper mapping a scio.ly-scale difficulty float (0.0-1.0) onto a
named band ("Easy"/"Medium"/"Hard"/"Very Hard") and back.

Thresholds (see text_utils.py's DIFFICULTY_BANDS docstring for why they're
skewed rather than even quartiles):
    Easy       value <= 0.3        represented as 0.3
    Medium     0.3  < value <= 0.5 represented as 0.5
    Hard       0.5  < value <= 0.7 represented as 0.7
    Very Hard  value >  0.7        represented as 0.9

None/missing is a distinct "unrated" state -- never "Easy" -- and is not
handled by these two functions alone; callers treat a None/absent band as
unrated.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import text_utils


@pytest.mark.parametrize("value,expected", [
    (0.0,  "Easy"),
    (0.3,  "Easy"),
    (0.31, "Medium"),
    (0.4,  "Medium"),
    (0.5,  "Medium"),
    (0.55, "Hard"),
    (0.6,  "Hard"),
    (0.7,  "Hard"),
    (0.75, "Very Hard"),
    (0.8,  "Very Hard"),
    (1.0,  "Very Hard"),
])
def test_band_boundaries(value, expected):
    assert text_utils.difficulty_band(value) == expected


@pytest.mark.parametrize("value", [None, "", "not-a-number"])
def test_missing_or_unparseable_is_unrated_not_easy(value):
    band = text_utils.difficulty_band(value)
    assert band is None
    assert band != "Easy"


@pytest.mark.parametrize("band,expected_value", [
    ("Easy", 0.3),
    ("Medium", 0.5),
    ("Hard", 0.7),
    ("Very Hard", 0.9),
])
def test_value_for_band(band, expected_value):
    assert text_utils.difficulty_value_for_band(band) == expected_value


def test_value_for_unknown_band_is_none():
    assert text_utils.difficulty_value_for_band(None) is None
    assert text_utils.difficulty_value_for_band("Impossible") is None


@pytest.mark.parametrize("band", ["Easy", "Medium", "Hard", "Very Hard"])
def test_round_trip_band_value_band_is_stable(band):
    value = text_utils.difficulty_value_for_band(band)
    assert text_utils.difficulty_band(value) == band
