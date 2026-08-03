#!/usr/bin/env python3
"""Interval session bookkeeping for solderscope.

A session documents a soldering job as timed stills while MediaMTX is stopped,
so the sensor changes hands twice per session rather than twice per photo.

The camera call and the clock are injected by the caller. That keeps every
decision in here -- when the next frame is due, whether the disk still has room,
what the folder is called -- testable without a camera attached.
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

# Below ~5s a Zero 2 W cannot finish a 12 MP capture plus its thumbnail before
# the next tick is due, so the schedule would fall permanently behind.
MIN_INTERVAL = 5
MAX_INTERVAL = 3600
DEFAULT_INTERVAL = 30

# A 12 MP JPEG is ~4 MB, so a 10s interval writes ~1.4 GB/h. Without a floor a
# session left running overnight fills the card and takes the whole box down.
DISK_FLOOR_BYTES = 2_000_000_000
MAX_CONSECUTIVE_FAILURES = 3


def _slug(text):
    """Reduce free text to something safe for a folder name.

    Mirrors the slug rules in solderscope.py: keep alphanumerics, dash and
    underscore, turn everything else into a dash.
    """
    if not isinstance(text, str):
        text = ""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in text.strip()]
    return "".join(keep)[:40].strip("-")


def folder_name(started, name):
    """Folder for one session: timestamp first so folders sort chronologically."""
    stamp = datetime.fromtimestamp(started).strftime("%Y-%m-%d_%H-%M-%S")
    slug = _slug(name)
    return f"{stamp}_{slug}" if slug else stamp


def clamp_interval(value):
    """Coerce a client-supplied interval into the supported range."""
    if isinstance(value, bool):
        # bool passes as an int, so True would clamp to MIN_INTERVAL and run
        # the session at 5s. Treat it as the client bug it is.
        return DEFAULT_INTERVAL
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not hypothetical: the value arrives as parsed JSON,
        # and int(float("inf")) raises it rather than ValueError.
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, n))


def next_tick(started, interval, now):
    """Time of the next scheduled capture.

    Anchored to `started` rather than to the end of the last capture: sleeping a
    fixed interval after each frame would add the capture duration to every gap,
    so a nominal 10s interval would drift out to 13s or worse. If a capture
    overran one or more slots, this returns the next slot strictly in the
    future instead of firing the missed ones back to back.

    `interval` must be positive -- pass it through clamp_interval() first. A
    zero interval divides by zero and a negative one walks the schedule
    backwards, so the only caller (run_loop) reads it from a clamped State.
    """
    elapsed = now - started
    return started + interval * (math.floor(elapsed / interval) + 1)


class State:
    """Everything known about one session.

    Mutated by the capture loop, read by the HTTP handler. The caller holds a
    lock around both; this class does no locking of its own.
    """

    def __init__(self, name, interval, started):
        self.name = name or ""
        self.interval = interval
        self.started = started
        self.ended = None
        self.folder = folder_name(started, name)
        self.photos = 0
        self.last_frame = None
        self.last_error = None
        self.consecutive_failures = 0
        self.total_failures = 0        # survives a good frame, unlike the above
        self.running = True
        self.stop_reason = "running"

    def record_frame(self, filename):
        self.photos += 1
        self.last_frame = filename
        self.last_error = None
        self.consecutive_failures = 0

    def record_failure(self, message):
        self.last_error = message
        self.consecutive_failures += 1
        self.total_failures += 1

    def finish(self, reason, now):
        # First reason wins: run_loop calls this once from its finally block,
        # and a later caller must not relabel a manual stop as something else.
        if not self.running:
            return
        self.running = False
        self.stop_reason = reason
        self.ended = now

    def as_json(self):
        """The session.json document. Rewritten after every frame, so a session
        cut short by a power failure still leaves a readable record."""
        return {
            "name": self.name,
            "folder": self.folder,
            "interval": self.interval,
            "started": self.started,
            "ended": self.ended,
            "photos": self.photos,
            "total_failures": self.total_failures,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
        }


def stop_reason_for(state, free_bytes):
    """Why the session should stop now, or None to carry on.

    Checked before each capture rather than after, so the frame that would
    cross the disk floor is never written.
    """
    if free_bytes < DISK_FLOOR_BYTES:
        return "disk"
    if state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        return "error"
    return None


def resolve_folder(sessions_root, name):
    """Turn a client-supplied session id into a path, or None if it is not one.

    Same guard as delete_media() in solderscope.py: resolve first, then confirm
    containment. A name is only ever used to build a path through here.
    """
    if not name or not isinstance(name, str):
        return None
    try:
        root = Path(sessions_root).resolve()
        target = (root / name).resolve()
    except ValueError:
        # unquote() turns %00 in a URL into a real null byte, and pathlib
        # raises on one. Answering None keeps this a 404 rather than a 500.
        return None
    if not str(target).startswith(str(root) + os.sep):
        return None
    if not target.is_dir():
        return None
    return target


def write_session_file(folder, state):
    """Persist session.json atomically.

    Written after every frame, so a session cut short by a power cut still
    describes what it captured. Atomic because the Sessions tab may read it at
    the same moment.
    """
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "session.json.part"
    try:
        tmp.write_text(json.dumps(state.as_json(), indent=2))
        tmp.replace(folder / "session.json")
    finally:
        tmp.unlink(missing_ok=True)


def run_loop(state, sessions_root, capture, free_bytes, clock, stop_event):
    """Capture frames until stopped, the disk fills, or captures keep failing.

    `capture(folder, stamp)` writes one frame and returns its filename, or
    raises. `free_bytes()` reports free space. `clock` supplies time() and
    wait(event, timeout). Everything that touches hardware arrives through
    those, which is what makes this testable without a camera.

    The caller owns the sensor before calling this and restores the stream
    afterwards; this function never touches MediaMTX.
    """
    folder = Path(sessions_root) / state.folder
    folder.mkdir(parents=True, exist_ok=True)
    write_session_file(folder, state)

    reason = "manual"
    try:
        while not stop_event.is_set():
            blocked = stop_reason_for(state, free_bytes())
            if blocked:
                reason = blocked
                break

            stamp = datetime.fromtimestamp(clock.time()).strftime("%Y-%m-%d_%H-%M-%S")
            try:
                state.record_frame(capture(folder, stamp))
            except Exception as e:
                # One bad frame is not fatal; stop_reason_for() ends the session
                # once they come in a streak.
                state.record_failure(str(e) or e.__class__.__name__)

            write_session_file(folder, state)

            if stop_event.is_set():
                break
            delay = next_tick(state.started, state.interval, clock.time()) - clock.time()
            if clock.wait(stop_event, max(0.0, delay)):
                break
    except BaseException:
        # Never leave session.json claiming the session is still running.
        reason = "error"
        raise
    finally:
        state.finish(reason, int(clock.time()))
        write_session_file(folder, state)
