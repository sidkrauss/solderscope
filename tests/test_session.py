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


def test_a_null_byte_in_the_name_is_refused(tmp_path):
    # unquote() turns %00 in a URL into a real null byte, and pathlib raises
    # ValueError on one. This function promises None rather than an exception,
    # so the route above it answers 404 instead of a 500.
    root = tmp_path / "sessions"
    root.mkdir()
    assert session.resolve_folder(root, "foo\x00bar") is None


def test_a_file_masquerading_as_a_session_is_refused(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "notafolder").write_text("x")
    assert session.resolve_folder(root, "notafolder") is None


import json
import threading


class FakeClock:
    """A clock the test advances by hand.

    wait() returns True when the stop event was set (mirroring
    threading.Event.wait), and otherwise jumps straight to the deadline so a
    session covering an hour runs in microseconds. Every delay is recorded so a
    test can assert the schedule, not just that some waiting happened.
    """

    def __init__(self, now=1000.0):
        self.now = now
        self.waits = []

    def time(self):
        return self.now

    def wait(self, event, timeout):
        self.waits.append(timeout)
        self.now += max(0.0, timeout)
        return event.is_set()


def run_session(tmp_path, capture, free_bytes=lambda: 20e9, name="", interval=10,
                stop_after=3, clock=None):
    """Drive a session to completion with a capture stub.

    The stub is wrapped so the stop event fires after `stop_after` captures,
    which is how the test ends a loop that would otherwise run forever. The
    event is set during the final capture, so the loop breaks after writing
    that frame and the wait after it is skipped.
    """
    clock = clock or FakeClock()
    stop = threading.Event()
    calls = {"n": 0}

    def wrapped(target_dir, stamp):
        calls["n"] += 1
        if calls["n"] >= stop_after:
            stop.set()
        return capture(target_dir, stamp)

    st = session.State(name=name, interval=interval, started=int(clock.time()))
    session.run_loop(
        state=st,
        sessions_root=tmp_path,
        capture=wrapped,
        free_bytes=free_bytes,
        clock=clock,
        stop_event=stop,
    )
    return st


def read_session_file(tmp_path, state):
    return json.loads((tmp_path / state.folder / "session.json").read_text())


def test_a_session_writes_frames_and_a_session_file(tmp_path):
    def capture(target_dir, stamp):
        f = target_dir / f"{stamp}.jpg"
        f.write_bytes(b"jpeg")
        return f.name

    st = run_session(tmp_path, capture, stop_after=3)

    folder = tmp_path / st.folder
    assert st.photos == 3
    assert len(list(folder.glob("*.jpg"))) == 3
    doc = json.loads((folder / "session.json").read_text())
    assert doc["photos"] == 3
    assert doc["stop_reason"] == "manual"
    assert doc["ended"] is not None


def test_frames_are_spaced_by_the_interval(tmp_path):
    stamps = []
    clock = FakeClock()

    def capture(target_dir, stamp):
        stamps.append(clock.time())
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    run_session(tmp_path, capture, interval=10, stop_after=3, clock=clock)
    # Exact tick times, not merely distinct ones: a scheduler that slept a full
    # interval after each capture would also produce three distinct stamps.
    assert stamps == [1000.0, 1010.0, 1020.0]
    # No wait after the final frame -- the stop event is set during it.
    assert clock.waits == [10.0, 10.0]


def test_the_first_frame_is_taken_at_once(tmp_path):
    # Second 0 is the first photo. Waiting out an interval before the first
    # frame would lose the "before" shot, which is the one worth having: the
    # operator presses start and expects the board as it is right now.
    stamps = []
    clock = FakeClock()

    def capture(target_dir, stamp):
        stamps.append(clock.time())
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    run_session(tmp_path, capture, interval=3600, stop_after=1, clock=clock)
    # An hour-long interval: if the first capture waited for a tick, this test
    # would record 4600.0 rather than the start time.
    assert stamps == [1000.0]


def test_a_slow_capture_does_not_push_the_schedule_out(tmp_path):
    # The regression test for drift. Each capture costs 3s of wall time; because
    # next_tick() is anchored to state.started, the loop must wait only the
    # remaining 7s and still fire on the 10s grid. A naive sleep(interval) after
    # each frame would land on 1000/1013/1026 and wait 10s every time.
    clock = FakeClock()
    stamps = []

    def capture(target_dir, stamp):
        stamps.append(clock.time())
        clock.now += 3.0
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    run_session(tmp_path, capture, interval=10, stop_after=3, clock=clock)
    assert stamps == [1000.0, 1010.0, 1020.0]
    assert clock.waits == [7.0, 7.0]


def test_a_failing_capture_does_not_end_the_session(tmp_path):
    calls = {"n": 0}

    def capture(target_dir, stamp):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Capture failed")
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    st = run_session(tmp_path, capture, stop_after=3)
    assert st.photos == 2
    assert st.stop_reason == "manual"


def test_three_failures_in_a_row_end_the_session(tmp_path):
    def capture(target_dir, stamp):
        raise RuntimeError("Capture failed")

    st = run_session(tmp_path, capture, stop_after=99)
    assert st.photos == 0
    assert st.stop_reason == "error"
    assert st.last_error == "Capture failed"


def test_a_full_disk_ends_the_session_before_writing(tmp_path):
    def capture(target_dir, stamp):
        raise AssertionError("must not capture with the disk this full")

    st = run_session(tmp_path, capture, free_bytes=lambda: 1e9, stop_after=99)
    assert st.photos == 0
    assert st.stop_reason == "disk"


def test_a_recovered_failure_still_shows_in_the_record(tmp_path):
    # consecutive_failures resets on a good frame, so without a cumulative
    # count a session that stumbled and recovered would read as though it
    # never had trouble.
    calls = {"n": 0}

    def capture(target_dir, stamp):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Capture failed")
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    st = run_session(tmp_path, capture, stop_after=3)
    assert st.consecutive_failures == 0
    assert st.total_failures == 1
    doc = json.loads((tmp_path / st.folder / "session.json").read_text())
    assert doc["total_failures"] == 1


def test_finishing_twice_keeps_the_first_reason(tmp_path):
    st = session.State(name="", interval=10, started=1000)
    st.finish("manual", now=1600)
    st.finish("disk", now=1700)
    assert st.stop_reason == "manual"
    assert st.ended == 1600


def test_the_session_file_survives_an_unexpected_crash(tmp_path):
    def capture(target_dir, stamp):
        raise KeyboardInterrupt("simulated crash")

    clock = FakeClock()
    st = session.State(name="", interval=10, started=int(clock.time()))
    try:
        session.run_loop(
            state=st, sessions_root=tmp_path, capture=capture,
            free_bytes=lambda: 20e9, clock=clock, stop_event=threading.Event(),
        )
    except KeyboardInterrupt:
        pass
    doc = json.loads((tmp_path / st.folder / "session.json").read_text())
    assert doc["stop_reason"] == "error"


def test_no_exit_path_leaves_the_file_claiming_the_session_runs(tmp_path):
    # The HTTP handler trusts session.json. Whatever ends a session -- a manual
    # stop, a failure streak, the disk floor, or a crash -- the file on disk
    # must never still say "running" with no end time.
    def good(target_dir, stamp):
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        return f"{stamp}.jpg"

    def bad(target_dir, stamp):
        raise RuntimeError("Capture failed")

    def crash(target_dir, stamp):
        raise KeyboardInterrupt("simulated crash")

    finished = [
        run_session(tmp_path / "manual", good, stop_after=3),
        run_session(tmp_path / "error", bad, stop_after=99),
        run_session(tmp_path / "disk", good, free_bytes=lambda: 1e9, stop_after=99),
    ]
    roots = [tmp_path / "manual", tmp_path / "error", tmp_path / "disk"]

    clock = FakeClock()
    crashed = session.State(name="", interval=10, started=int(clock.time()))
    try:
        session.run_loop(
            state=crashed, sessions_root=tmp_path / "crash", capture=crash,
            free_bytes=lambda: 20e9, clock=clock, stop_event=threading.Event(),
        )
    except KeyboardInterrupt:
        pass
    finished.append(crashed)
    roots.append(tmp_path / "crash")

    assert [st.stop_reason for st in finished] == ["manual", "error", "disk", "error"]
    for root, st in zip(roots, finished):
        doc = read_session_file(root, st)
        assert doc["stop_reason"] != "running"
        assert doc["ended"] is not None


def _write_session(root, folder, **fields):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    doc = {"name": "", "folder": folder, "interval": 10, "started": 1000,
           "ended": 1100, "photos": 2, "stop_reason": "manual", "last_error": None}
    doc.update(fields)
    (d / "session.json").write_text(json.dumps(doc))
    for i in range(doc["photos"]):
        (d / f"frame{i}.jpg").write_bytes(b"jpeg")
    return d


def test_sessions_are_listed_newest_first(tmp_path):
    _write_session(tmp_path, "2026-08-01_10-00-00_old", started=1000)
    _write_session(tmp_path, "2026-08-03_10-00-00_new", started=3000)
    got = session.list_sessions(tmp_path)
    assert [s["folder"] for s in got] == [
        "2026-08-03_10-00-00_new", "2026-08-01_10-00-00_old"]


def test_a_listed_session_carries_its_size_and_count(tmp_path):
    _write_session(tmp_path, "2026-08-03_10-00-00_job", photos=3)
    got = session.list_sessions(tmp_path)[0]
    assert got["photos"] == 3
    assert got["size"] == 3 * len(b"jpeg")


def test_a_folder_without_a_session_file_is_skipped(tmp_path):
    (tmp_path / "junk").mkdir()
    _write_session(tmp_path, "2026-08-03_10-00-00_job")
    assert len(session.list_sessions(tmp_path)) == 1


def test_a_corrupt_session_file_is_skipped(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "session.json").write_text("{not json")
    _write_session(tmp_path, "2026-08-03_10-00-00_job")
    assert len(session.list_sessions(tmp_path)) == 1


def test_listing_a_missing_root_is_empty(tmp_path):
    assert session.list_sessions(tmp_path / "nope") == []


def test_one_odd_session_file_does_not_sink_the_whole_listing(tmp_path):
    # A hand-edited session.json that parses but is not an object used to raise
    # past the parse guard, so a single bad folder emptied the Sessions tab
    # instead of losing one row.
    (tmp_path / "a-list").mkdir()
    (tmp_path / "a-list" / "session.json").write_text("[1, 2, 3]")
    (tmp_path / "a-string").mkdir()
    (tmp_path / "a-string" / "session.json").write_text('"nonsense"')
    _write_session(tmp_path, "2026-08-03_10-00-00_job")
    got = session.list_sessions(tmp_path)
    assert [s["folder"] for s in got] == ["2026-08-03_10-00-00_job"]


def test_a_non_numeric_started_does_not_break_the_sort(tmp_path):
    _write_session(tmp_path, "2026-08-03_10-00-00_good", started=3000)
    _write_session(tmp_path, "2026-08-01_10-00-00_odd", started="not-a-number")
    got = session.list_sessions(tmp_path)
    assert len(got) == 2
    assert got[0]["folder"] == "2026-08-03_10-00-00_good"   # unsortable sinks


def test_the_folder_comes_from_the_directory_not_the_file(tmp_path):
    # A session.json carrying someone else's folder name must not redirect the
    # UI at the folder it names.
    d = _write_session(tmp_path, "2026-08-03_10-00-00_job")
    doc = json.loads((d / "session.json").read_text())
    doc["folder"] = "../elsewhere"
    (d / "session.json").write_text(json.dumps(doc))
    assert session.list_sessions(tmp_path)[0]["folder"] == "2026-08-03_10-00-00_job"


def test_an_unwritable_folder_does_not_mask_the_stop_reason(tmp_path):
    # The finally block writes through the same function that just failed. If
    # that raises, it would replace the exception the loop was already raising
    # and leave the caller with a PermissionError instead of the real cause.
    calls = {"n": 0}

    def capture(target_dir, stamp):
        calls["n"] += 1
        (target_dir / f"{stamp}.jpg").write_bytes(b"jpeg")
        if calls["n"] == 1:
            target_dir.chmod(0o500)          # now unwritable
        return f"{stamp}.jpg"

    clock = FakeClock()
    stop = threading.Event()
    st = session.State(name="", interval=10, started=int(clock.time()))
    folder = tmp_path / st.folder
    try:
        session.run_loop(state=st, sessions_root=tmp_path, capture=capture,
                         free_bytes=lambda: 20e9, clock=clock, stop_event=stop)
    except PermissionError:
        raise AssertionError("a failed session.json write must not escape")
    finally:
        folder.chmod(0o700)                  # so tmp_path cleanup works
    assert st.running is False
    assert st.stop_reason in ("manual", "error")
