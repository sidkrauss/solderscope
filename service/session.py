#!/usr/bin/env python3
"""Interval session bookkeeping for solderscope.

A session documents a soldering job as timed stills while MediaMTX is stopped,
so the sensor changes hands twice per session rather than twice per photo.

The camera call and the clock are injected by the caller. That keeps every
decision in here -- when the next frame is due, whether the disk still has room,
what the folder is called -- testable without a camera attached.
"""

from datetime import datetime


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
