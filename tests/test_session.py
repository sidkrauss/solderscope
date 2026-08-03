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


def test_interval_rejects_a_boolean():
    # bool is a subtype of int, so True would otherwise clamp to MIN_INTERVAL
    # and run a session at 5s. A JSON body carrying true for this field is a
    # client bug; the default is a safer reading of it than the fastest rate.
    assert session.clamp_interval(True) == session.DEFAULT_INTERVAL
    assert session.clamp_interval(False) == session.DEFAULT_INTERVAL


def test_interval_survives_infinity():
    # The value arrives as parsed JSON, so a client can hand us "inf" or a bare
    # Infinity literal. int(float("inf")) raises OverflowError, not ValueError,
    # so an uncaught one would surface as a 500 instead of a clamped interval.
    assert session.clamp_interval(float("inf")) == session.DEFAULT_INTERVAL
    assert session.clamp_interval(float("-inf")) == session.DEFAULT_INTERVAL
    assert session.clamp_interval("inf") == session.DEFAULT_INTERVAL


def test_next_tick_follows_the_fixed_schedule():
    # Ticks are anchored to the start time, not to when the last capture ended,
    # so a slow capture does not push every later frame further out.
    assert session.next_tick(started=1000, interval=10, now=1000) == 1010
    assert session.next_tick(started=1000, interval=10, now=1003) == 1010


def test_next_tick_skips_slots_missed_by_a_slow_capture():
    # A capture that overran two whole slots must not trigger a catch-up burst;
    # the loop jumps to the next slot in the future.
    assert session.next_tick(started=1000, interval=10, now=1025) == 1030


def test_next_tick_lands_exactly_on_a_slot_boundary():
    assert session.next_tick(started=1000, interval=10, now=1020) == 1030
