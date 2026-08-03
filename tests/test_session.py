"""Unit tests for the session module.

Everything here runs without a camera: session.py takes its capture function
and clock as parameters, so tests inject stubs.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))

import session

# The stamp is formatted in local time. Compute the expectation the same way
# rather than hardcoding a string, so the suite passes in any timezone.
STAMP = datetime.fromtimestamp(1754226665).strftime("%Y-%m-%d_%H-%M-%S")


def test_folder_name_combines_stamp_and_slug():
    assert session.folder_name(1754226665, "Board rework") == f"{STAMP}_Board-rework"


def test_folder_name_without_name_is_just_the_stamp():
    assert session.folder_name(1754226665, "") == STAMP


def test_folder_name_sanitises_separators():
    assert session.folder_name(1754226665, "cust/omer 12") == f"{STAMP}_cust-omer-12"


def test_folder_name_puts_the_stamp_first_so_folders_sort_by_time():
    earlier = session.folder_name(1754226665, "zzz")
    later = session.folder_name(1754226665 + 3600, "aaa")
    assert earlier < later


def test_interval_is_clamped_to_the_supported_range():
    assert session.clamp_interval(30) == 30
    assert session.clamp_interval(1) == session.MIN_INTERVAL
    assert session.clamp_interval(99999) == session.MAX_INTERVAL


def test_interval_falls_back_to_the_default_when_unusable():
    assert session.clamp_interval("abc") == session.DEFAULT_INTERVAL
    assert session.clamp_interval(None) == session.DEFAULT_INTERVAL


def test_interval_accepts_a_numeric_string():
    assert session.clamp_interval("45") == 45
