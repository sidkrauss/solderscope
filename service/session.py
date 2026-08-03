#!/usr/bin/env python3
"""Interval session bookkeeping for solderscope.

A session documents a soldering job as timed stills while MediaMTX is stopped,
so the sensor changes hands twice per session rather than twice per photo.

The camera call and the clock are injected by the caller. That keeps every
decision in here -- when the next frame is due, whether the disk still has room,
what the folder is called -- testable without a camera attached.
"""

import math
from datetime import datetime

# Below ~5s a Zero 2 W cannot finish a 12 MP capture plus its thumbnail before
# the next tick is due, so the schedule would fall permanently behind.
MIN_INTERVAL = 5
MAX_INTERVAL = 3600
DEFAULT_INTERVAL = 30


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

    def finish(self, reason, now):
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
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
        }
