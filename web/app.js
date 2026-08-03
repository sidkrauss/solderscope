/* solderscope UI: live view, capture, recording, annotation. */

const $ = (id) => document.getElementById(id);
const PI_HOST = location.hostname;

// MediaMTX serves WebRTC on 8889; this UI is served by the capture service.
// controls=false hides the browser's video bar: a scrubber and running timer
// are meaningless on a live stream. Fullscreen is offered by our own button.
const STREAM_URL = `http://${PI_HOST}:8889/cam/?controls=false&muted=true&autoplay=true&playsinline=true`;
$("player").src = STREAM_URL;

let recording = false;
let busy = false;

function hint(msg, kind = "") {
  const h = $("hint");
  h.textContent = msg;
  h.className = "hint " + kind;
}

function setBusy(on, text) {
  busy = on;
  $("busy").classList.toggle("hidden", !on);
  if (text) $("busyText").textContent = text;
  // A session holds the sensor for its whole run, so neither button may come
  // back just because this capture finished.
  $("photo").disabled = on || sessionRunning;
  $("record").disabled = on || sessionRunning;
}

function reloadPlayer() {
  // Force the WebRTC session to re-establish after MediaMTX restarted.
  $("player").src = `${STREAM_URL}&t=${Date.now()}`;
}

/* After a capture MediaMTX needs ~10s until the stream reliably serves again,
   and it briefly answers before the camera path is up. Reloading during that
   window leaves the iframe stuck on the browser's error page, which only a
   manual refresh clears. So keep the player blank until the server confirms
   the playlist twice in a row, then load it once. */
let recovering = false;

async function recoverStream() {
  if (recovering) return;        // a second capture must not blank the player again
  recovering = true;
  $("player").src = "about:blank";
  let seen = 0;
  for (let i = 0; i < 25; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    let ready = false;
    try { ready = (await api("/api/stream-ready")).ready; } catch { /* retry */ }
    seen = ready ? seen + 1 : 0;
    if (seen >= 2) { reloadPlayer(); recovering = false; return; }
  }
  reloadPlayer();   // give up waiting and try anyway
  recovering = false;
}

// Fullscreen: iOS Safari implements the Fullscreen API only on <video>
// elements (webkitEnterFullscreen), never on a div or iframe. So we drive a
// CSS class instead -- works identically everywhere, including iPhone -- and
// use the native API on top where it exists.
$("fullscreen").onclick = () => {
  const el = document.querySelector(".frame");
  const on = el.classList.toggle("expanded");
  document.body.classList.toggle("noscroll", on);
  $("fullscreen").textContent = on ? "⤡" : "⛶";

  if (on) {
    if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  } else if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
};

// Leaving native fullscreen (Esc, system gesture) must also drop the CSS class.
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement) {
    document.querySelector(".frame").classList.remove("expanded");
    document.body.classList.remove("noscroll");
    $("fullscreen").textContent = "⛶";
  }
});

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

/* ---------- status ---------- */

let sessionRunning = false;

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    recording = s.recording;
    applySession(s.session);
    $("dot").className = "dot " + (sessionRunning ? "session"
      : s.recording ? "rec" : s.stream_active ? "live" : "off");
    $("statusText").textContent = sessionRunning ? "Session running"
      : s.recording ? "Recording"
      : s.stream_active ? "Live" : "Stream down";
    $("disk").textContent = s.disk ? `· ${s.disk.free_gb} GB free` : "";
    $("record").textContent = recording ? "⏹ Stop recording" : "⏺ Start recording";
    $("record").classList.toggle("recording", recording);
  } catch {
    $("dot").className = "dot off";
    $("statusText").textContent = "no connection";
  }
}

const fmtDuration = (s) =>
  `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/* Swap the live view for the session panel. The stream is stopped while a
   session runs, so the last captured frame is all there is to show -- and it is
   what tells the operator the board is still in frame. */
function applySession(st) {
  const running = !!(st && st.running);
  const ended = sessionRunning && !running;
  sessionRunning = running;

  $("sessionPanel").classList.toggle("hidden", !running);
  $("sessionIdle").classList.toggle("hidden", running);
  document.querySelector(".frame").classList.toggle("hidden", running);
  // A manual photo or a recording during a session could only fail on the
  // capture lock, so do not offer them.
  $("photo").disabled = running || busy;
  $("record").disabled = running || busy;

  if (running) {
    $("sessionTitle").textContent = st.name || "Session";   // textContent: no escaping needed
    $("sessionStats").textContent =
      `${st.photos} photo${st.photos === 1 ? "" : "s"} · ${fmtDuration(st.elapsed)} · every ${st.interval}s · ${st.free_gb} GB free`;
    $("sessionError").textContent = st.last_error ? `last error: ${st.last_error}` : "";
    if (st.last_url && $("sessionShot").dataset.url !== st.last_url) {
      $("sessionShot").dataset.url = st.last_url;
      $("sessionShot").src = st.last_url;
    }
  }

  if (ended) {
    // The service clears its session state in the same breath as it stamps the
    // stop reason, so this payload is a bare {running:false}. The reason still
    // reaches the operator: it is written to session.json and shown on the card
    // that loadSessions() pulls in right below.
    hint("Session stopped", "ok");
    $("sessionShot").removeAttribute("src");
    delete $("sessionShot").dataset.url;
    $("sessionError").textContent = "";
    loadSessions();
    recoverStream();          // same blank-until-ready path as after a photo
  }
}

$("sessionStart").onclick = async () => {
  $("sessionStart").disabled = true;
  hint("");
  try {
    const st = await api("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("sessionName").value.trim(),
        interval: +$("sessionInterval").value,
      }),
    });
    // The stream is going down; do not leave the player showing a dead frame.
    $("player").src = "about:blank";
    applySession(st);
    hint(`Session started, one photo every ${st.interval}s`, "ok");
  } catch (e) {
    hint(`Error: ${e.message}`, "err");
  } finally {
    $("sessionStart").disabled = false;
  }
};

$("sessionStop").onclick = async () => {
  $("sessionStop").disabled = true;
  try {
    await api("/api/session/stop", { method: "POST" });
    hint("Stopping session…", "ok");
  } catch (e) {
    hint(`Error: ${e.message}`, "err");
  } finally {
    $("sessionStop").disabled = false;
    refreshStatus();
  }
};

/* ---------- capture ---------- */

$("photo").onclick = async () => {
  if (busy) return;
  setBusy(true, "Taking photo…");
  hint("");
  try {
    const job = $("job").value.trim();
    const res = await api("/api/photo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job }),
    });
    hint(`Saved: ${res.file}`, "ok");
    await loadMedia();
  } catch (e) {
    hint(`Error: ${e.message}`, "err");
  } finally {
    setBusy(false);
    refreshStatus();
    recoverStream();               // reconnects on its own, no manual refresh
  }
};

$("record").onclick = async () => {
  if (busy) return;
  $("record").disabled = true;
  try {
    await api("/api/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !recording }),
    });
    hint(recording ? "Recording stopped" : "Recording started", "ok");
    setTimeout(loadMedia, 1500);
  } catch (e) {
    hint(`Error: ${e.message}`, "err");
  } finally {
    $("record").disabled = false;
    refreshStatus();
  }
};

/* ---------- gallery ---------- */

const fmtSize = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " kB";
const fmtTime = (t) => new Date(t * 1000).toLocaleString(undefined,
  { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

/* The session name is free text and the cards are built with innerHTML.
   Escaping here keeps a name like "Q&A <board>" rendering as typed. */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function loadMedia() {
  try {
    const m = await api("/api/media");
    render($("photos"), m.photos, false);
    render($("videos"), m.videos, true);
  } catch { /* keep last view on transient errors */ }
}

/* ---------- sessions gallery ---------- */

let openSession = null;      // folder name while a session's frames are shown

async function loadSessions() {
  try {
    const { sessions } = await api("/api/sessions");
    renderSessions(sessions);
  } catch { /* keep the last view on transient errors */ }
}

function renderSessions(items) {
  const host = $("sessions");
  if (!items.length) {
    host.innerHTML = `<p class="empty">No sessions recorded yet.</p>`;
    return;
  }
  const why = { disk: "stopped: disk almost full", error: "stopped: captures failed" };
  // photos/interval come straight out of session.json, which the service does
  // not type-check. Coerce rather than escape: they are only ever numbers, and
  // a hand-edited file must not put markup on the card.
  const num = (v) => Number.isFinite(+v) ? +v : 0;
  host.innerHTML = items.map((s) => `
    <div class="sessionCard">
      <div class="sessionMeta">
        <b>${esc(s.name) || "Session"}</b>
        <span>${fmtTime(s.started)} · ${num(s.photos)} photo${num(s.photos) === 1 ? "" : "s"} · ${fmtSize(s.size)} · every ${num(s.interval)}s</span>
        ${why[s.stop_reason] ? `<span class="err">${why[s.stop_reason]}</span>` : ""}
      </div>
      <span class="acts">
        <button class="openSession" data-id="${esc(s.folder)}">Open</button>
        <button class="del" data-path="session:${esc(s.folder)}" data-name="${esc(s.name || s.folder)}" title="Delete session">🗑</button>
      </span>
    </div>`).join("");

  host.querySelectorAll(".openSession").forEach((b) => {
    b.onclick = () => showSessionFrames(b.dataset.id);
  });
  host.querySelectorAll(".del").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); askDelete(b.dataset.path, b.dataset.name); };
  });
}

async function showSessionFrames(id) {
  try {
    const { frames } = await api(`/api/session-frames?id=${encodeURIComponent(id)}`);
    openSession = id;
    const host = $("sessions");
    host.innerHTML = `<button id="backToSessions">← All sessions</button>
                      <div class="grid" id="sessionGrid"></div>`;
    $("backToSessions").onclick = () => { openSession = null; loadSessions(); };
    render($("sessionGrid"), frames, false);   // reuses the photo grid + editor
  } catch (e) {
    hint(`Error: ${e.message}`, "err");
  }
}

function render(host, items, isVideo) {
  if (!items.length) {
    host.innerHTML = `<p class="empty">Nothing captured yet.</p>`;
    return;
  }
  host.innerHTML = items.map((it) => `
    <div class="card">
      ${isVideo
        ? `<video src="${esc(it.url)}" controls preload="metadata"></video>`
        : `<img src="${esc(it.thumb || it.url)}" loading="lazy" decoding="async"
               data-full="${esc(it.url)}" data-name="${esc(it.name)}"
               data-fallback="${esc(it.url)}">`}
      <div class="meta"><b title="${esc(it.name)}">${esc(it.name)}</b><span>${fmtSize(it.size)}</span></div>
      <div class="meta">
        <span>${fmtTime(it.mtime)}</span>
        <span class="acts">
          <a href="${esc(it.url)}" download title="Download">⤓</a>
          <button class="del" data-path="${esc(it.url)}" data-name="${esc(it.name)}" title="Delete">🗑</button>
        </span>
      </div>
    </div>`).join("");

  if (!isVideo) {
    host.querySelectorAll("img").forEach((img) => {
      img.onclick = () => openEditor(img.dataset.full, img.dataset.name);
      // Thumbnails are generated in the background, so one may 404 on the first
      // listing after a capture. Fall back to the full frame, once.
      img.onerror = () => { img.onerror = null; img.src = img.dataset.fallback; };
    });
  }
  host.querySelectorAll(".del").forEach((b) => {
    b.onclick = (e) => { e.stopPropagation(); askDelete(b.dataset.path, b.dataset.name); };
  });
}

/* Two-step delete without a modal: the button turns into a confirmation and
   reverts by itself. confirm() is unreliable on iOS and blocks the page. */
function askDelete(path, name) {
  const btn = document.querySelector(`.del[data-path="${CSS.escape(path)}"]`);
  if (!btn) return;
  if (btn.dataset.armed === "1") { doDelete(path, name); return; }
  btn.dataset.armed = "1";
  btn.textContent = "sure?";
  btn.classList.add("armed");
  setTimeout(() => {
    if (btn.dataset.armed === "1") {
      delete btn.dataset.armed;
      btn.textContent = "🗑";
      btn.classList.remove("armed");
    }
  }, 4000);
}

async function doDelete(path, name) {
  try {
    await api("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    hint(`Deleted: ${name}`, "ok");
    if (openSession) { await showSessionFrames(openSession); }
    else { await loadMedia(); await loadSessions(); }
    refreshStatus();
  } catch (e) {
    hint(`Delete failed: ${e.message}`, "err");
  }
}

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("photos").classList.toggle("hidden", t.dataset.tab !== "photos");
    $("videos").classList.toggle("hidden", t.dataset.tab !== "videos");
    $("sessions").classList.toggle("hidden", t.dataset.tab !== "sessions");
    if (t.dataset.tab === "sessions" && !openSession) loadSessions();
  };
});

/* ---------- annotation editor ----------
   Modelled on Flameshot. Shapes are stored as objects in image coordinates and
   the whole stack is redrawn on every change -- that keeps undo/redo trivial
   and keeps annotations correct at any on-screen scale. */

const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const textInput = $("textInput");

let baseImg = null;
let shapes = [];
let redoStack = [];
let tool = "arrow";
let color = "#ff2d2d";
let widthStep = 3;
let filled = false;
let drawing = null;
let sourceName = "image";
let counter = 1;
let textDraft = null;   // {x, y} while typing
let cropRect = null;   // {x1,y1,x2,y2} while the crop selection is being drawn

function openEditor(url, name) {
  sourceName = name || "image";
  shapes = [];
  redoStack = [];
  counter = 1;
  cropRect = null;
  $("cropActions").classList.add("hidden");
  baseImg = new Image();
  baseImg.crossOrigin = "anonymous";
  baseImg.onload = () => {
    canvas.width = baseImg.naturalWidth;
    canvas.height = baseImg.naturalHeight;
    redraw();
    $("editor").classList.remove("hidden");
    selectTool(tool);        // syncs the shade button to the active tool
    editorHint("");
  };
  baseImg.onerror = () => alert("Could not load the image.");
  baseImg.src = url;
}

function closeEditor() {
  cancelText();
  // Drop an in-flight drag: Esc mid-stroke would otherwise leave it dangling
  // and paint it onto the next image opened.
  drawing = null;
  cropRect = null;
  $("cropActions").classList.add("hidden");
  $("editor").classList.add("hidden");
  // Release the backing bitmap: a 12 MP canvas holds ~49 MB, which matters
  // on a tablet.
  canvas.width = canvas.height = 0;
  baseImg = null;
  shapes = [];
  redoStack = [];
}

function editorHint(msg) {
  $("editorHint").textContent = msg;
}

$("close").onclick = closeEditor;

/* ---------- tool & style selection ---------- */

function selectTool(name) {
  commitText();                       // finish a pending text before switching
  tool = name;
  document.querySelectorAll(".tool").forEach((b) =>
    b.classList.toggle("active", b.dataset.tool === name));
  canvas.style.cursor = name === "text" ? "text" : "crosshair";
  // Shading only means anything for closed shapes, so only offer it there.
  $("fill").classList.toggle("hidden", name !== "rect" && name !== "ellipse");
  if (name !== "crop") { cropRect = null; $("cropActions").classList.add("hidden"); redraw(); }
  editorHint(name === "text" ? "Click on the image and type. Enter to confirm, Esc to cancel."
    : name === "counter" ? "Each click drops the next number in sequence."
    : name === "pixelate" ? "Drag over an area to obscure it."
    : name === "crop" ? "Drag the area to keep, then press Crop."
    : "");
}

document.querySelectorAll(".tool").forEach((b) => {
  b.onclick = () => selectTool(b.dataset.tool);
});
document.querySelectorAll("input[name=col]").forEach((r) => {
  r.onchange = () => { color = r.value; if (textDraft) styleTextInput(); };
});
$("width").oninput = (e) => {
  widthStep = +e.target.value;
  if (textDraft) styleTextInput();
};
$("fill").onclick = () => {
  filled = !filled;
  $("fill").classList.toggle("active", filled);
};

/* Stroke width scales with image size: a 12 MP frame is usually viewed scaled
   down in a report, where thin strokes disappear. */
const strokeW = () => Math.max(2, Math.round(canvas.width / 900 * widthStep));
const fontSize = () => Math.max(16, Math.round(canvas.width / 640 * widthStep * 6));

/* ---------- drawing ---------- */

function redraw() {
  ctx.drawImage(baseImg, 0, 0);
  for (const s of shapes) drawShape(s);
  if (drawing) drawShape(drawing);
  if (cropRect) drawCropOverlay();
}

/* Darken everything outside the selection so the kept area is obvious. */
function drawCropOverlay() {
  const x = Math.min(cropRect.x1, cropRect.x2), y = Math.min(cropRect.y1, cropRect.y2);
  const w = Math.abs(cropRect.x2 - cropRect.x1), h = Math.abs(cropRect.y2 - cropRect.y1);
  ctx.save();
  ctx.fillStyle = "rgba(0,0,0,.55)";
  ctx.beginPath();
  ctx.rect(0, 0, canvas.width, canvas.height);
  ctx.rect(x, y, w, h);
  ctx.fill("evenodd");
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = Math.max(2, canvas.width / 700);
  ctx.setLineDash([ctx.lineWidth * 4, ctx.lineWidth * 3]);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
}

function drawShape(s) {
  ctx.save();
  ctx.strokeStyle = s.color;
  ctx.fillStyle = s.color;
  ctx.lineWidth = s.w;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  const x = Math.min(s.x1, s.x2), y = Math.min(s.y1, s.y2);
  const w = Math.abs(s.x2 - s.x1), h = Math.abs(s.y2 - s.y1);

  switch (s.type) {
    // Filled shapes are translucent and keep their outline: the point is to
    // highlight an area, not to paint over the evidence underneath.
    case "rect":
      if (s.filled) {
        ctx.globalAlpha = 0.3;
        ctx.fillRect(x, y, w, h);
        ctx.globalAlpha = 1;
      }
      ctx.strokeRect(x, y, w, h);
      break;

    case "ellipse":
      ctx.beginPath();
      ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2);
      if (s.filled) {
        ctx.globalAlpha = 0.3;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.stroke();
      break;

    case "line":
      ctx.beginPath();
      ctx.moveTo(s.x1, s.y1); ctx.lineTo(s.x2, s.y2);
      ctx.stroke();
      break;

    case "arrow": {
      const len = Math.hypot(s.x2 - s.x1, s.y2 - s.y1);
      // Clamp the head so a short arrow cannot grow a tail pointing backwards.
      const head = Math.min(s.w * 4, len * 0.9) || s.w * 4;
      const a = Math.atan2(s.y2 - s.y1, s.x2 - s.x1);
      // Stop the shaft where the head begins. Drawing it all the way to the
      // tip leaves the round line cap poking out past the arrowhead, which is
      // what makes the point look blunt and offset.
      const backX = s.x2 - head * 0.85 * Math.cos(a);
      const backY = s.y2 - head * 0.85 * Math.sin(a);
      if (len > head * 0.85) {          // no shaft worth drawing on a stub
        ctx.beginPath();
        ctx.moveTo(s.x1, s.y1);
        ctx.lineTo(backX, backY);
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.moveTo(s.x2, s.y2);                       // exactly on the endpoint
      ctx.lineTo(s.x2 - head * Math.cos(a - Math.PI / 7),
                 s.y2 - head * Math.sin(a - Math.PI / 7));
      ctx.lineTo(backX, backY);                     // notch, so head meets shaft
      ctx.lineTo(s.x2 - head * Math.cos(a + Math.PI / 7),
                 s.y2 - head * Math.sin(a + Math.PI / 7));
      ctx.closePath();
      ctx.fill();
      break;
    }

    case "free":
    case "marker":
      if (s.type === "marker") {
        ctx.globalAlpha = 0.35;
        ctx.lineWidth = s.w * 5;
        ctx.lineCap = "butt";
      }
      ctx.beginPath();
      s.pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
      ctx.stroke();
      break;

    case "pixelate":
      drawPixelated(x, y, w, h);
      break;

    case "counter": {
      const r = s.size;
      ctx.beginPath();
      ctx.arc(s.x1, s.y1, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = pickContrast(s.color);
      ctx.font = `700 ${Math.round(r * 1.25)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(s.n), s.x1, s.y1 + r * 0.05);
      break;
    }

    case "text": {
      ctx.font = `600 ${s.size}px system-ui, sans-serif`;
      ctx.textBaseline = "top";
      const lines = s.text.split("\n");
      const lh = s.size * 1.25;
      const pad = s.size * 0.28;
      const wMax = Math.max(...lines.map((l) => ctx.measureText(l).width));
      ctx.fillStyle = "rgba(0,0,0,.55)";   // keep text readable on any board
      ctx.fillRect(s.x1 - pad, s.y1 - pad, wMax + pad * 2, lh * lines.length + pad * 2 - (lh - s.size));
      ctx.fillStyle = s.color;
      lines.forEach((l, i) => ctx.fillText(l, s.x1, s.y1 + i * lh));
      break;
    }
  }
  ctx.restore();
}

/* Mosaic effect: sample the untouched source image so repeated redraws stay
   stable instead of progressively smearing. */
function drawPixelated(x, y, w, h) {
  if (w < 2 || h < 2) return;
  const block = Math.max(6, Math.round(canvas.width / 90));
  const cols = Math.max(1, Math.round(w / block));
  const rows = Math.max(1, Math.round(h / block));
  const tmp = document.createElement("canvas");
  tmp.width = cols; tmp.height = rows;
  const tctx = tmp.getContext("2d");
  tctx.imageSmoothingEnabled = true;
  tctx.drawImage(baseImg, x, y, w, h, 0, 0, cols, rows);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(tmp, 0, 0, cols, rows, x, y, w, h);
  ctx.imageSmoothingEnabled = true;
}

function pickContrast(hex) {
  const c = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16));
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#000" : "#fff";
}

/* ---------- pointer handling ---------- */

function pos(ev) {
  const r = canvas.getBoundingClientRect();
  const p = ev.touches ? ev.touches[0] : ev;
  return {
    x: (p.clientX - r.left) * (canvas.width / r.width),
    y: (p.clientY - r.top) * (canvas.height / r.height),
  };
}

function pushShape(s) {
  shapes.push(s);
  redoStack = [];          // a new edit invalidates the redo branch
  redraw();
}

function start(ev) {
  if (textDraft) { commitText(); return; }   // click elsewhere finishes the text
  ev.preventDefault();
  const p = pos(ev);

  if (tool === "text") { beginText(p); return; }

  if (tool === "counter") {
    pushShape({ type: "counter", x1: p.x, y1: p.y, n: counter++,
                color, w: strokeW(), size: Math.max(14, strokeW() * 4) });
    return;
  }

  if (tool === "crop") {
    cropRect = { x1: p.x, y1: p.y, x2: p.x, y2: p.y };
    drawing = { type: "cropsel" };      // marker so move/end route here
    return;
  }

  drawing = (tool === "free" || tool === "marker")
    ? { type: tool, pts: [p], color, w: strokeW() }
    : { type: tool, x1: p.x, y1: p.y, x2: p.x, y2: p.y, color, w: strokeW(), filled };
}

function move(ev) {
  if (!drawing) return;
  ev.preventDefault();
  const p = pos(ev);
  if (drawing.type === "cropsel") { cropRect.x2 = p.x; cropRect.y2 = p.y; redraw(); return; }
  if (drawing.pts) drawing.pts.push(p);
  else { drawing.x2 = p.x; drawing.y2 = p.y; }
  redraw();
}

function end(ev) {
  if (!drawing) return;
  ev.preventDefault();

  if (drawing.type === "cropsel") {
    drawing = null;
    const w = Math.abs(cropRect.x2 - cropRect.x1), h = Math.abs(cropRect.y2 - cropRect.y1);
    if (w < 20 || h < 20) { cropRect = null; $("cropActions").classList.add("hidden"); }
    else {
      $("cropActions").classList.remove("hidden");
      editorHint(`Selection ${Math.round(w)} × ${Math.round(h)} px. Press Crop to apply.`);
    }
    redraw();
    return;
  }

  const s = drawing;
  drawing = null;
  // Discard accidental zero-size drags.
  const tiny = s.pts ? s.pts.length < 2
    : Math.abs(s.x2 - s.x1) < 3 && Math.abs(s.y2 - s.y1) < 3;
  if (tiny) { redraw(); return; }
  pushShape(s);
}

canvas.addEventListener("mousedown", start);
canvas.addEventListener("mousemove", move);
canvas.addEventListener("mouseup", end);
canvas.addEventListener("mouseleave", end);
canvas.addEventListener("touchstart", start, { passive: false });
canvas.addEventListener("touchmove", move, { passive: false });
canvas.addEventListener("touchend", end, { passive: false });

/* ---------- in-place text entry ----------
   A textarea positioned over the canvas, styled to match what will be drawn.
   Avoids prompt(), which is blocked in some browsers and cannot be typed into
   on a tablet. */

function beginText(p) {
  textDraft = { x: p.x, y: p.y };
  textInput.value = "";
  textInput.classList.remove("hidden");
  styleTextInput();
  // Focus after the current mousedown/touchstart finishes -- focusing inline
  // would be undone by the browser's own focus handling for the same click,
  // which fires blur and closes the field again.
  setTimeout(() => textInput.focus({ preventScroll: true }), 0);
  editorHint("Enter to confirm · Shift+Enter for a new line · Esc to cancel");
}

function styleTextInput() {
  if (!textDraft) return;
  const r = canvas.getBoundingClientRect();
  const stage = canvas.parentElement.getBoundingClientRect();
  const scale = r.width / canvas.width;      // image px -> screen px
  textInput.style.left = (r.left - stage.left + textDraft.x * scale) + "px";
  textInput.style.top = (r.top - stage.top + textDraft.y * scale) + "px";
  textInput.style.fontSize = Math.max(12, fontSize() * scale) + "px";
  textInput.style.color = color;
  autoGrow();
}

function autoGrow() {
  textInput.style.height = "auto";
  textInput.style.height = textInput.scrollHeight + "px";
  textInput.style.width =
    Math.max(60, textInput.value.split("\n")
      .reduce((m, l) => Math.max(m, l.length), 0) * parseFloat(textInput.style.fontSize) * 0.62 + 24) + "px";
}

function commitText() {
  if (!textDraft) return;
  const text = textInput.value.replace(/\s+$/, "");
  const at = textDraft;
  textDraft = null;
  textInput.classList.add("hidden");
  editorHint("");
  if (text) {
    pushShape({ type: "text", x1: at.x, y1: at.y, text, color, size: fontSize() });
  }
}

function cancelText() {
  if (!textDraft) return;
  textDraft = null;
  textInput.classList.add("hidden");
  editorHint("");
}

textInput.addEventListener("input", autoGrow);
textInput.addEventListener("keydown", (e) => {
  e.stopPropagation();                       // don't trigger editor shortcuts
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commitText(); }
  else if (e.key === "Escape") { e.preventDefault(); cancelText(); }
});
// Only commit on blur once the field actually held focus; otherwise the
// initial click that creates it would immediately close it again.
textInput.addEventListener("focus", () => { textInput.dataset.ready = "1"; });
textInput.addEventListener("blur", () => {
  if (textInput.dataset.ready === "1") { delete textInput.dataset.ready; commitText(); }
});

/* ---------- crop ----------
   Applying a crop replaces the base image with the selected region. Existing
   annotations are shifted by the same offset so they stay on the features they
   point at; the pixelate tool also samples from the new base. */

$("cropApply").onclick = () => {
  if (!cropRect) return;
  const x = Math.round(Math.min(cropRect.x1, cropRect.x2));
  const y = Math.round(Math.min(cropRect.y1, cropRect.y2));
  const w = Math.round(Math.abs(cropRect.x2 - cropRect.x1));
  const h = Math.round(Math.abs(cropRect.y2 - cropRect.y1));
  if (w < 20 || h < 20) return;

  // Bake the current base image (without overlay) into the cropped region.
  const cut = document.createElement("canvas");
  cut.width = w; cut.height = h;
  cut.getContext("2d").drawImage(baseImg, x, y, w, h, 0, 0, w, h);

  const shifted = shapes.map((s) => {
    const c = { ...s };
    if (c.pts) c.pts = c.pts.map((p) => ({ x: p.x - x, y: p.y - y }));
    else {
      c.x1 -= x; c.y1 -= y;
      if (c.x2 !== undefined) { c.x2 -= x; c.y2 -= y; }
    }
    return c;
  });

  const img = new Image();
  img.onload = () => {
    baseImg = img;
    canvas.width = w;
    canvas.height = h;
    shapes = shifted;
    redoStack = [];                  // offsets differ before/after -- no redo
    cropRect = null;
    $("cropActions").classList.add("hidden");
    redraw();
    selectTool("arrow");                 // clears the hint, so set it after
    editorHint(`Cropped to ${w} × ${h} px.`);
  };
  img.src = cut.toDataURL("image/jpeg", 0.95);
};

$("cropCancel").onclick = () => {
  cropRect = null;
  $("cropActions").classList.add("hidden");
  editorHint("");
  redraw();
};

/* ---------- history ---------- */

$("undo").onclick = () => {
  commitText();
  if (!shapes.length) return;
  const gone = shapes.pop();
  if (gone.type === "counter") counter = gone.n;   // reuse that number next time
  redoStack.push(gone);
  redraw();
};
$("redo").onclick = () => {
  if (!redoStack.length) return;
  const back = redoStack.pop();
  if (back.type === "counter") counter = back.n + 1;
  shapes.push(back);
  redraw();
};
$("clear").onclick = () => {
  cancelText();
  if (!shapes.length) return;
  // Append, do not prepend: the cleared shapes are older than anything already
  // on the redo stack, so redo must hand them back last and in order.
  redoStack = redoStack.concat(shapes.slice().reverse());
  shapes = [];
  counter = 1;
  redraw();
};

/* ---------- keyboard shortcuts (Flameshot-like) ---------- */

const KEYS = { a: "arrow", l: "line", r: "rect", e: "ellipse",
               f: "free", m: "marker", t: "text", c: "counter", p: "pixelate",
               k: "crop" };

document.addEventListener("keydown", (e) => {
  if ($("editor").classList.contains("hidden")) return;
  if (e.target === textInput) return;

  if (e.key === "Escape") {
    if (cropRect) { $("cropCancel").click(); return; }   // step out of crop first
    closeEditor(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault(); e.shiftKey ? $("redo").click() : $("undo").click(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
    e.preventDefault(); $("redo").click(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault(); $("save").click(); return;
  }
  const t = KEYS[e.key.toLowerCase()];
  if (t && !e.ctrlKey && !e.metaKey) { e.preventDefault(); selectTool(t); }
});

/* ---------- output ---------- */

function annotatedName() {
  const base = sourceName.replace(/\.jpe?g$/i, "");
  return `${base}_annot.jpg`;
}

$("download").onclick = () => {
  commitText();
  const a = document.createElement("a");
  a.download = annotatedName();
  a.href = canvas.toDataURL("image/jpeg", 0.92);
  a.click();
};

$("save").onclick = async () => {
  commitText();
  $("save").disabled = true;
  try {
    const dataUrl = canvas.toDataURL("image/jpeg", 0.92);
    const res = await api("/api/save-annotated", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataUrl, source: sourceName }),
    });
    closeEditor();
    hint(`Annotated image saved: ${res.file}`, "ok");
    loadMedia();
  } catch (e) {
    alert("Save failed: " + e.message);
  } finally {
    $("save").disabled = false;
  }
};

window.addEventListener("resize", () => { if (textDraft) styleTextInput(); });

/* Safety net: if the iframe ever ends up on the browser error page (server was
   briefly unreachable), a click on the frame reloads it without a page refresh. */
$("player").addEventListener("error", () => setTimeout(reloadPlayer, 2000));
document.querySelector(".frame").addEventListener("dblclick", (e) => {
  if (e.target.id !== "fullscreen") reloadPlayer();
});

/* ---------- boot ---------- */
refreshStatus();
loadMedia();
loadSessions();
setInterval(() => { if (!busy) refreshStatus(); }, 5000);
