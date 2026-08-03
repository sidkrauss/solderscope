#!/usr/bin/env python3
"""solderscope capture service.

Sits next to MediaMTX and owns the two things MediaMTX cannot do itself:
full-resolution stills and record start/stop.

The camera is exclusive: while MediaMTX holds the sensor, rpicam-still fails
with "Pipeline handler in use by another process". So a still capture stops
MediaMTX, grabs the frame, and starts it again. The stream drops for ~8s.
That is acceptable here because the operator works through the binocular --
the stream is documentation, not a live working view.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

MEDIA_ROOT = Path(os.environ.get("SOLDERSCOPE_MEDIA", "/home/master/solderscope-media"))
PHOTO_DIR = MEDIA_ROOT / "photos"
VIDEO_DIR = MEDIA_ROOT / "recordings"
THUMB_DIR = MEDIA_ROOT / "thumbs"
THUMB_MAX = 400          # px on the long edge
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

MTX_SERVICE = "mediamtx"
MTX_API = "http://127.0.0.1:9997"
STREAM_PATH = "cam"
LISTEN_PORT = int(os.environ.get("SOLDERSCOPE_PORT", "8080"))

# Full sensor resolution of the IMX477.
STILL_WIDTH, STILL_HEIGHT = 4056, 3040

# Serialises sensor handover. Without it two concurrent captures would fight
# over MediaMTX and could leave the stream stopped.
_capture_lock = threading.Lock()


def _run(cmd, timeout=90):
    """Run a command. Raises TimeoutExpired -- callers that must not fail
    (status polling, service control) use _run_safe instead."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class _Failed:
    """Stand-in result so callers can treat a timeout like a failed command."""
    returncode, stdout, stderr = 1, "", "timeout"


def _run_safe(cmd, timeout=30):
    try:
        return _run(cmd, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return _Failed()


def _systemctl(action):
    """Start or stop MediaMTX to hand the sensor over.

    No sudo: a polkit rule (config/50-solderscope.rules) grants this user
    manage-units on mediamtx.service and nothing else, so systemctl talks to
    systemd over D-Bus with exactly the privilege it needs.
    """
    return _run_safe(["systemctl", action, MTX_SERVICE], timeout=30)


def _mtx_active():
    return _run_safe(["systemctl", "is-active", MTX_SERVICE], timeout=10).stdout.strip() == "active"


def _stream_ready():
    """True once MediaMTX actually serves the HLS playlist.

    "Service is running" is not enough: MediaMTX accepts connections seconds
    before the camera path is up. Follow redirects (-L) -- since v1.19 the
    playlist answers 302 to set a cookie, and treating that as "not ready"
    would leave the player blank forever.
    """
    r = _run_safe(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                   "--max-time", "4", f"http://127.0.0.1:8888/{STREAM_PATH}/index.m3u8"],
                  timeout=8)
    return r.stdout.strip() == "200"


def _wait_for_stream(timeout=25):
    """Block until the stream serves again, or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _stream_ready():
            return True
        time.sleep(1)
    return False


def capture_still(job=None):
    """Stop MediaMTX, take a full-resolution frame, restart MediaMTX.

    Returns (ok, payload). The finally-block restarts the stream even if the
    capture itself fails -- never leave the operator without a live view.
    """
    if not _capture_lock.acquire(blocking=False):
        return False, {"error": "A capture is already running"}

    handed_off = False        # True once a background thread owns the lock
    try:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        prefix = f"{_slug(job)}_" if job else ""
        target = PHOTO_DIR / f"{prefix}{stamp}.jpg"

        was_active = _mtx_active()
        try:
            if was_active:
                _systemctl("stop")
                time.sleep(0.3)  # brief settle; measured as enough to free the sensor

            cmd = ["rpicam-still", "-o", str(target),
                   "--width", str(STILL_WIDTH), "--height", str(STILL_HEIGHT),
                   # 500ms warm-up: the microscope light is constant, so the
                   # exposure loop converges immediately (measured identical
                   # mean brightness at 300..1500ms).
                   "-q", "95", "-n", "-t", "500"]
            cmd += _orientation_flags()

            try:
                r = _run(cmd, timeout=90)
            except subprocess.TimeoutExpired:
                # Must not escape: the finally-block below still restores the
                # stream, but the caller needs a JSON error, not a traceback.
                return False, {"error": "Capture timed out"}

            if not target.exists():
                err = (r.stderr or r.stdout or "").strip().splitlines()
                return False, {"error": "Capture failed",
                               "detail": err[-3:] if err else []}

            make_thumb(target)      # ready before the gallery reloads
            return True, {"file": target.name,
                          "size": target.stat().st_size,
                          "url": f"/media/photos/{target.name}",
                          "thumb": f"/thumb/{target.name}"}
        finally:
            if was_active:
                _systemctl("start")
                # Do not block the response on the stream coming back: the photo
                # is already on disk, and waiting for HLS cost ~8s of the ~14s
                # round trip. The browser reconnects on its own; the lock is
                # held until the stream is up so a second capture cannot race
                # a half-started MediaMTX.
                # Set the flag only once the thread is actually running: if
                # start() raises, the outer finally must still free the lock,
                # or captures stay blocked until the service restarts.
                t = threading.Thread(target=_release_when_ready, daemon=True)
                t.start()
                handed_off = True
    finally:
        if not handed_off:
            _capture_lock.release()


def _release_when_ready():
    """Wait for MediaMTX to serve again, then free the capture lock."""
    try:
        _wait_for_stream()
    finally:
        _capture_lock.release()


def _thumb_path(photo: Path) -> Path:
    return THUMB_DIR / (photo.stem + ".jpg")


def make_thumb(photo: Path):
    """Create a gallery thumbnail, reusing an up-to-date one.

    Uses PIL's draft mode: the JPEG decoder scales while decoding, so a 12 MP
    frame becomes a thumbnail in ~2s instead of ~20s on a Zero 2 W. Without
    thumbnails the gallery pulls several 4 MB originals just to show previews.
    """
    thumb = _thumb_path(photo)
    try:
        if thumb.exists() and thumb.stat().st_mtime >= photo.stat().st_mtime:
            return thumb
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        with Image.open(photo) as im:
            im.draft("RGB", (THUMB_MAX, THUMB_MAX))
            im = im.convert("RGB")
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            # Unique temp name: the backfill thread and an inbound /thumb/
            # request can hit the same file at once, and a shared .part would
            # interleave their writes.
            tmp = thumb.with_suffix(f".{os.getpid()}.{threading.get_ident()}.part")
            im.save(tmp, "JPEG", quality=75)
            tmp.replace(thumb)          # atomic: never serve a half-written file
        return thumb
    except Exception:
        return None                     # gallery falls back to the original


def _thumbs_in_background(paths):
    def work():
        for p in paths:
            make_thumb(p)
    threading.Thread(target=work, daemon=True).start()


def _orientation_flags():
    """Mirror the stream's flip settings so stills match the live view.

    Read from mediamtx.yml rather than hard-coded: if the camera is ever
    remounted, changing the stream config alone keeps both in sync.
    """
    flags = []
    try:
        # Skip comments: the shipped config carries commented-out examples,
        # and matching those would silently flip stills the other way.
        active = [ln.split("#", 1)[0] for ln in
                  Path("/etc/mediamtx.yml").read_text().splitlines()]
        cfg = "\n".join(active)
        if "rpiCameraHFlip: true" in cfg:
            flags.append("--hflip")
        if "rpiCameraVFlip: true" in cfg:
            flags.append("--vflip")
    except OSError:
        flags = ["--hflip", "--vflip"]  # current physical mounting
    return flags


def _slug(text):
    if not isinstance(text, str):
        text = ""                    # JSON may hand us a number or a list
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in text.strip()]
    return "".join(keep)[:40].strip("-")


def _mtx_api(method, path, body=None):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", "8", "-X", method, f"{MTX_API}{path}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return _run_safe(cmd, timeout=12).stdout.strip()


def set_recording(enabled):
    """Toggle recording at runtime via the MediaMTX control API."""
    code = _mtx_api("PATCH", f"/v3/config/paths/patch/{STREAM_PATH}",
                    {"record": bool(enabled)})
    if code.startswith("2"):
        return True, {"recording": bool(enabled)}
    return False, {"error": f"MediaMTX API returned {code or 'nothing'}"}


def recording_state():
    r = _run_safe(["curl", "-s", "--max-time", "8",
                  f"{MTX_API}/v3/config/paths/get/{STREAM_PATH}"], timeout=12)
    try:
        return bool(json.loads(r.stdout).get("record", False))
    except Exception:
        return False


def _by_mtime(paths, limit=200):
    """Newest first, tolerating files that vanish mid-scan."""
    out = []
    for p in paths:
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            pass
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out[:limit]]


def list_media():
    photos, videos = [], []
    missing = []
    if PHOTO_DIR.is_dir():
        for p in _by_mtime(PHOTO_DIR.glob("*.jpg")):
            try:
                st = p.stat()
            except OSError:
                continue             # deleted while we were listing
            t = _thumb_path(p)
            if not (t.exists() and t.stat().st_mtime >= st.st_mtime):
                missing.append(p)
            photos.append({"name": p.name, "size": st.st_size,
                           "mtime": int(st.st_mtime),
                           "url": f"/media/photos/{p.name}",
                           "thumb": f"/thumb/{p.name}"})
    if missing:
        _thumbs_in_background(missing)   # backfill without blocking the list
    if VIDEO_DIR.is_dir():
        for p in _by_mtime(VIDEO_DIR.rglob("*.mp4")):
            try:
                st = p.stat()
            except OSError:
                continue
            videos.append({"name": p.name, "size": st.st_size,
                           "mtime": int(st.st_mtime),
                           "url": f"/media/recordings/{p.relative_to(VIDEO_DIR)}"})
    return {"photos": photos, "videos": videos}


def delete_media(rel):
    """Delete one photo or recording below MEDIA_ROOT."""
    if not rel:
        return False, {"error": "no file given"}
    target = (MEDIA_ROOT / rel).resolve()
    root = MEDIA_ROOT.resolve()
    # Path traversal guard: resolve() first, then confirm containment.
    if not str(target).startswith(str(root) + os.sep):
        return False, {"error": "invalid path"}
    if target.suffix.lower() not in (".jpg", ".jpeg", ".mp4"):
        return False, {"error": "file type not allowed"}
    if not target.is_file():
        return False, {"error": "file not found"}
    try:
        target.unlink()
        _thumb_path(target).unlink(missing_ok=True)
    except OSError as e:
        return False, {"error": f"delete failed: {e}"}
    return True, {"deleted": target.name}


def disk_info():
    total, used, free = shutil.disk_usage(MEDIA_ROOT if MEDIA_ROOT.exists() else "/")
    return {"free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # journald already records what matters

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype):
        """Serve a file in chunks, honouring a single Range request.

        Never read the whole thing: a 10-minute recording at 12 Mbit/s is
        several hundred MB, and this box has 416 MB of RAM. Range support also
        makes seeking in the browser's video player work.
        """
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200

        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
        if m and size:
            g1, g2 = m.group(1), m.group(2)
            if g1:
                start = int(g1)            # do not clamp: a start past the end
                                           # is an unsatisfiable range, not a
                                           # request for the last byte
                end = min(int(g2), size - 1) if g2 else size - 1
            elif g2:                       # suffix range: last N bytes
                start = max(0, size - int(g2))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                           # client navigated away mid-download

    def do_GET(self):
        u = urlparse(self.path)
        route = u.path

        if route in ("/", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/app.js":
            self._file(WEB_DIR / "app.js", "application/javascript")
        elif route == "/style.css":
            self._file(WEB_DIR / "style.css", "text/css")
        elif route == "/api/status":
            self._json({"stream_active": _mtx_active(),
                        "recording": recording_state(),
                        "disk": disk_info()})
        elif route == "/api/stream-ready":
            # "Service is running" is not the same as "stream is serving":
            # MediaMTX accepts connections seconds before the camera path is
            # up. Ask for the actual playlist so the browser only reloads the
            # player once a reconnect will succeed.
            self._json({"ready": _stream_ready()})
        elif route == "/api/media":
            self._json(list_media())
        elif route.startswith("/thumb/"):
            name = unquote(route[len("/thumb/"):])
            photo = (PHOTO_DIR / name).resolve()
            if not str(photo).startswith(str(PHOTO_DIR.resolve()) + os.sep):
                self._json({"error": "forbidden"}, 403)
                return
            t = make_thumb(photo)
            self._file(t or photo, "image/jpeg")   # fall back to the original
        elif route.startswith("/media/"):
            rel = unquote(route[len("/media/"):])
            target = (MEDIA_ROOT / rel).resolve()
            if not str(target).startswith(str(MEDIA_ROOT.resolve()) + os.sep):
                self._json({"error": "forbidden"}, 403)   # path traversal guard
                return
            ctype = ("image/jpeg" if target.suffix.lower() in (".jpg", ".jpeg")
                     else "video/mp4")
            self._file(target, ctype)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 40 * 1024 * 1024:      # an annotated 12 MP JPEG is ~5 MB
            self._json({"error": "request too large"}, 413)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}

        if u.path == "/api/photo":
            ok, payload = capture_still(body.get("job") or (qs.get("job") or [None])[0])
            self._json(payload, 200 if ok else 500)
        elif u.path == "/api/record":
            ok, payload = set_recording(bool(body.get("enabled")))
            self._json(payload, 200 if ok else 500)
        elif u.path == "/api/save-annotated":
            ok, payload = self._save_annotated(body)
            self._json(payload, 200 if ok else 500)
        elif u.path == "/api/delete":
            rel = body.get("path") or ""
            rel = rel[len("/media/"):] if rel.startswith("/media/") else rel
            ok, payload = delete_media(unquote(rel))
            self._json(payload, 200 if ok else 400)
        else:
            self._json({"error": "not found"}, 404)

    def _save_annotated(self, body):
        """Persist a browser-annotated image (data URL) next to the original."""
        import base64
        data = (body.get("dataUrl") or "").split(",", 1)
        if len(data) != 2:
            return False, {"error": "no image supplied"}
        source = _slug(Path(body.get("source") or "bild").stem)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        target = PHOTO_DIR / f"{source}_annot.jpg"
        n = 2
        while target.exists():
            target = PHOTO_DIR / f"{source}_annot{n}.jpg"
            n += 1
        try:
            target.write_bytes(base64.b64decode(data[1]))
        except Exception as e:
            return False, {"error": f"save failed: {e}"}
        return True, {"file": target.name, "url": f"/media/photos/{target.name}"}


def main():
    for d in (PHOTO_DIR, VIDEO_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"solderscope capture service on :{LISTEN_PORT}, media at {MEDIA_ROOT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
