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


def test_new_state_starts_running_with_no_photos():
    st = session.State(name="Rework", interval=10, started=1000)
    assert st.running is True
    assert st.photos == 0
    assert st.stop_reason == "running"
    assert st.folder == session.folder_name(1000, "Rework")


def test_recording_a_frame_updates_the_counters():
    st = session.State(name="", interval=10, started=1000)
    st.record_frame("2026-08-03_14-31-05.jpg")
    assert st.photos == 1
    assert st.last_frame == "2026-08-03_14-31-05.jpg"
    assert st.last_error is None
    assert st.consecutive_failures == 0


def test_a_failure_is_remembered_and_counted():
    st = session.State(name="", interval=10, started=1000)
    st.record_failure("Capture failed")
    assert st.photos == 0
    assert st.last_error == "Capture failed"
    assert st.consecutive_failures == 1


def test_a_good_frame_clears_the_failure_streak():
    st = session.State(name="", interval=10, started=1000)
    st.record_failure("Capture failed")
    st.record_frame("a.jpg")
    assert st.consecutive_failures == 0
    assert st.last_error is None


def test_finishing_records_the_reason_and_end_time():
    st = session.State(name="", interval=10, started=1000)
    st.finish("manual", now=1600)
    assert st.running is False
    assert st.stop_reason == "manual"
    assert st.ended == 1600


def test_as_json_carries_what_the_session_file_needs():
    st = session.State(name="Rework", interval=10, started=1000)
    st.record_frame("a.jpg")
    doc = st.as_json()
    assert doc["name"] == "Rework"
    assert doc["interval"] == 10
    assert doc["started"] == 1000
    assert doc["photos"] == 1
    assert doc["stop_reason"] == "running"
    assert doc["folder"] == st.folder


def test_plenty_of_disk_and_no_failures_keeps_running():
    st = session.State(name="", interval=10, started=1000)
    assert session.stop_reason_for(st, free_bytes=20e9) is None


def test_low_disk_stops_the_session():
    st = session.State(name="", interval=10, started=1000)
    assert session.stop_reason_for(st, free_bytes=1e9) == "disk"


def test_three_consecutive_failures_stop_the_session():
    st = session.State(name="", interval=10, started=1000)
    for _ in range(2):
        st.record_failure("boom")
    assert session.stop_reason_for(st, free_bytes=20e9) is None
    st.record_failure("boom")
    assert session.stop_reason_for(st, free_bytes=20e9) == "error"


def test_scattered_failures_do_not_stop_the_session():
    # One bad frame must not end a forty-minute session.
    st = session.State(name="", interval=10, started=1000)
    for _ in range(5):
        st.record_failure("boom")
        st.record_frame("ok.jpg")
    assert session.stop_reason_for(st, free_bytes=20e9) is None


def test_resolving_a_session_folder_returns_the_path(tmp_path):
    root = tmp_path / "sessions"
    (root / "2026-08-03_14-31-05_job").mkdir(parents=True)
    got = session.resolve_folder(root, "2026-08-03_14-31-05_job")
    assert got == root / "2026-08-03_14-31-05_job"


def test_traversal_out_of_the_sessions_root_is_refused(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (tmp_path / "secret").mkdir()
    assert session.resolve_folder(root, "../secret") is None
    assert session.resolve_folder(root, "/etc") is None


def test_a_missing_or_empty_folder_name_is_refused(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    assert session.resolve_folder(root, "") is None
    assert session.resolve_folder(root, "does-not-exist") is None


def test_a_file_masquerading_as_a_session_is_refused(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "notafolder").write_text("x")
    assert session.resolve_folder(root, "notafolder") is None
