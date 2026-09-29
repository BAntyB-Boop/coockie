/* Cookie Run Bot dashboard — vanilla JS. Never starts the bot automatically. */
(() => {
  "use strict";
  const PROTOCOL = 1;
  const MAX_LOGS = 300;
  const MIN_NORM = 0.02;
  const $ = (id) => document.getElementById(id);

  const S = {
    token: sessionStorage.getItem("crb_token") || null,
    config: null, status: null, telemetry: null,
    online: false, eventsOk: false, previewOk: false,
    view: "annotated", edit: "none", roiTab: "jump",
    metas: new Map(), meta: null, bitmap: null,
    drag: null, devices: null, previewTimes: [],
    teach: null, pick: null, screens: [], scores: {}, // autoplay teach picks (frame px; never saved to config)
  };

  // ------------------------------------------------------------------ utils
  const get = (o, path) => path.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
  const set = (o, path, v) => { const ks = path.split("."); const last = ks.pop(); let t = o; for (const k of ks) t = t[k]; t[last] = v; };
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const r4 = (v) => Math.round(v * 10000) / 10000;
  const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    if (S.token) headers.Authorization = "Bearer " + S.token;
    let res;
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    } catch (e) {
      setOnline(false);
      throw new Error("backend unreachable");
    }
    setOnline(true);
    if (res.status === 401) { askToken(); throw new Error("authentication required"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      const msg = Array.isArray(d) ? d.map((e) => (e.loc ? e.loc.slice(1).join(".") + ": " : "") + e.msg).join("; ") : d || res.statusText;
      throw new Error(msg);
    }
    return data;
  }

  function setOnline(v) {
    S.online = v;
    $("offline-banner").hidden = v && S.eventsOk;
  }

  function askToken() {
    const dlg = $("auth-dialog");
    if (!dlg.open) dlg.showModal();
  }
  $("auth-form").addEventListener("submit", () => {
    S.token = $("auth-token").value.trim();
    sessionStorage.setItem("crb_token", S.token);
    $("auth-token").value = "";
    reconnectAll();
    loadConfig();
  });

  function localLog(level, msg) { appendLogs([{ t: Date.now() / 1000, level, msg: "[ui] " + msg }]); }

  // ------------------------------------------------------------- websockets
  function wsUrl(path) { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path; }

  function openSocket(path, handlers) {
    let ws = null, backoff = 500, closedByUs = false, timer = null;
    const connect = () => {
      ws = new WebSocket(wsUrl(path));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        backoff = 500;
        if (S.token) ws.send(JSON.stringify({ type: "auth", token: S.token }));
        handlers.open && handlers.open(ws);
      };
      ws.onmessage = (ev) => handlers.message(ev, ws);
      ws.onclose = () => {
        handlers.close && handlers.close();
        if (!closedByUs) { timer = setTimeout(connect, backoff); backoff = Math.min(8000, backoff * 2); }
      };
      ws.onerror = () => { try { ws.close(); } catch (_) { /* ignore */ } };
    };
    connect();
    return {
      send: (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); },
      restart: () => { closedByUs = true; clearTimeout(timer); try { ws.close(); } catch (_) {} closedByUs = false; backoff = 500; setTimeout(connect, 50); },
    };
  }

  let eventsSock, previewSock;
  function startSockets() {
    eventsSock = openSocket("/ws/events", {
      open: () => {},
      message: (ev) => {
        const m = JSON.parse(ev.data);
        if (m.v !== PROTOCOL && m.type !== "auth_failed") return;
        if (m.type === "auth_failed") { askToken(); return; }
        if (m.type === "hello" || m.type === "auth_ok") { S.eventsOk = true; renderConn(); if (m.type === "hello") loadConfig(); }
        else if (m.type === "status") { S.status = m; renderStatus(); }
        else if (m.type === "telemetry") { S.telemetry = m; renderTelemetry(); }
        else if (m.type === "log") appendLogs(m.entries);
      },
      close: () => { S.eventsOk = false; renderConn(); },
    });
    previewSock = openSocket("/ws/preview", {
      open: (ws) => { S.previewOk = true; ws.send(JSON.stringify({ type: "view", view: S.view })); renderConn(); },
      message: onPreviewMessage,
      close: () => { S.previewOk = false; renderConn(); },
    });
  }
  function reconnectAll() { eventsSock && eventsSock.restart(); previewSock && previewSock.restart(); }

  function renderConn() {
    const p = $("conn-pill");
    const ok = S.eventsOk;
    p.textContent = ok ? (S.previewOk ? "● Connected" : "● Connected (no preview)") : "● Offline";
    p.className = "pill " + (ok ? "pill-on" : "pill-off");
    $("offline-banner").hidden = ok;
    if (!ok) { $("state-pill").textContent = "UNKNOWN"; $("state-pill").dataset.state = ""; updateButtons(); }
  }

  // ---------------------------------------------------------------- preview
  async function onPreviewMessage(ev) {
    if (typeof ev.data === "string") {
      const m = JSON.parse(ev.data);
      if (m.type === "auth_failed") { askToken(); return; }
      if (m.type !== "preview_meta" || m.v !== PROTOCOL) return;
      S.metas.set(m.frame_id, m);
      if (S.metas.size > 4) S.metas.delete(S.metas.keys().next().value);
      return;
    }
    const buf = ev.data;
    if (buf.byteLength < 16) return;
    const dv = new DataView(buf);
    const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
    if (magic !== "CRPV" || dv.getUint8(4) !== PROTOCOL) return;
    const fid = Number(dv.getBigUint64(8));
    const meta = S.metas.get(fid);
    if (!meta) return; // unmatched image: drop
    S.metas.delete(fid);
    try {
      const bmp = await createImageBitmap(new Blob([buf.slice(16)], { type: "image/jpeg" }));
      if (S.bitmap && S.bitmap.close) S.bitmap.close();
      S.bitmap = bmp;
      S.meta = meta;
      const now = performance.now();
      S.previewTimes.push(now);
      while (S.previewTimes.length && now - S.previewTimes[0] > 2000) S.previewTimes.shift();
      $("preview-empty").hidden = true;
      $("ind-jump").classList.toggle("on", !!meta.jump.confirmed);
      $("ind-slide").classList.toggle("on", !!meta.slide.confirmed);
      const pf = S.previewTimes.length > 1 ? (S.previewTimes.length - 1) / ((now - S.previewTimes[0]) / 1000) : 0;
      $("preview-meta").textContent =
        `frame #${meta.frame_id} · source ${meta.src_width}×${meta.src_height} · preview ${meta.width}×${meta.height} · ${meta.view} · ` +
        `received ${pf.toFixed(1)} fps · jump density ${(meta.jump.density * 100).toFixed(1)}% score ${meta.jump.score} · ` +
        `slide density ${(meta.slide.density * 100).toFixed(1)}% score ${meta.slide.score} (heuristic)`;
      draw();
    } catch (_) { /* corrupt image: ignore */ }
  }

  // ------------------------------------------------------------ canvas math
  const canvas = $("preview");
  const ctx = canvas.getContext("2d");

  function resizeCanvas() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    draw();
  }
  new ResizeObserver(resizeCanvas).observe($("canvas-wrap"));
  window.matchMedia && window.addEventListener("resize", resizeCanvas);

  /** Letterbox layout in CSS pixels. k converts preview px -> source frame px. */
  function layout() {
    if (!S.meta) return null;
    const r = canvas.getBoundingClientRect();
    const iw = S.meta.width, ih = S.meta.height;
    const scale = Math.min(r.width / iw, r.height / ih);
    return { scale, ox: (r.width - iw * scale) / 2, oy: (r.height - ih * scale) / 2, k: S.meta.src_width / iw,
      W: S.meta.src_width, H: S.meta.src_height, cw: r.width, ch: r.height };
  }
  const frameToCanvas = (L, fx, fy) => [L.ox + (fx / L.k) * L.scale, L.oy + (fy / L.k) * L.scale];
  const canvasToFrame = (L, cx, cy) => [((cx - L.ox) / L.scale) * L.k, ((cy - L.oy) / L.scale) * L.k];
  function pointerToCanvas(e) { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

  function currentGeom() {
    const cfg = S.drag ? S.drag.draft : S.config;
    return cfg ? { viewport: cfg.viewport, jump_roi: cfg.rois.jump_roi, slide_roi: cfg.rois.slide_roi,
      jump_point: cfg.input.jump_point, slide_point: cfg.input.slide_point } : null;
  }
  function vpPx(L, vp) { return { x: vp.x * L.W, y: vp.y * L.H, w: vp.w * L.W, h: vp.h * L.H }; }
  function rectPx(L, g, target) {
    const vp = vpPx(L, g.viewport);
    if (target === "viewport") return vp;
    const r = g[target];
    return { x: vp.x + r.x * vp.w, y: vp.y + r.y * vp.h, w: r.w * vp.w, h: r.h * vp.h };
  }
  function pointPx(L, g, target) { const vp = vpPx(L, g.viewport); const p = g[target]; return { x: vp.x + p.x * vp.w, y: vp.y + p.y * vp.h }; }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const L = layout();
    if (!L || !S.bitmap) return;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(S.bitmap, L.ox, L.oy, S.meta.width * L.scale, S.meta.height * L.scale);
    drawTeach(L);
    const g = currentGeom();
    if (!g || S.edit === "none") return;
    const items = [["viewport", "#38bdf8"], ["jump_roi", "#22c55e"], ["slide_roi", "#ef4444"]];
    for (const [t, color] of items) {
      const r = rectPx(L, g, t);
      const [x1, y1] = frameToCanvas(L, r.x, r.y);
      const [x2, y2] = frameToCanvas(L, r.x + r.w, r.y + r.h);
      ctx.strokeStyle = color; ctx.lineWidth = S.edit === t ? 2.5 : 1;
      ctx.setLineDash(t === "viewport" ? [6, 4] : []);
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      ctx.setLineDash([]);
      if (S.edit === t) {
        ctx.fillStyle = color + "22"; ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
        ctx.fillStyle = color;
        for (const [hx, hy] of [[x1, y1], [x2, y1], [x1, y2], [x2, y2]]) ctx.fillRect(hx - 5, hy - 5, 10, 10);
      }
    }
    for (const [t, color] of [["jump_point", "#22c55e"], ["slide_point", "#ef4444"]]) {
      const p = pointPx(L, g, t);
      const [cx, cy] = frameToCanvas(L, p.x, p.y);
      ctx.strokeStyle = color; ctx.lineWidth = S.edit === t ? 3 : 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, S.edit === t ? 11 : 7, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx - 14, cy); ctx.lineTo(cx + 14, cy); ctx.moveTo(cx, cy - 14); ctx.lineTo(cx, cy + 14); ctx.stroke();
    }
  }

  // autoplay teach overlay: box/tap from the form inputs (NaN when empty -> skipped)
  function drawTeach(L) {
    const [x, y, w, h, tx, ty] = ["ap-bx", "ap-by", "ap-bw", "ap-bh", "ap-tx", "ap-ty"].map((id) => $(id).valueAsNumber);
    ctx.strokeStyle = "#f59e0b"; ctx.lineWidth = 2;
    if (w > 0 && h > 0) {
      const [x1, y1] = frameToCanvas(L, x, y), [x2, y2] = frameToCanvas(L, x + w, y + h);
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
    if (tx >= 0 && ty >= 0) {
      const [cx, cy] = frameToCanvas(L, tx, ty);
      ctx.beginPath(); ctx.arc(cx, cy, 8, 0, Math.PI * 2); ctx.moveTo(cx - 12, cy); ctx.lineTo(cx + 12, cy); ctx.moveTo(cx, cy - 12); ctx.lineTo(cx, cy + 12); ctx.stroke();
    }
  }

  // --------------------------------------------------------- pointer editing
  const EDIT_HINTS = {
    none: "", viewport: "Drag to move, corners to resize, or drag outside to draw the game viewport.",
    jump_roi: "Jump trigger zone (low obstacles). Released changes are sent to the backend.",
    slide_roi: "Slide trigger zone (high obstacles).", jump_point: "Click/drag where a jump tap lands.",
    slide_point: "Click/drag where a slide tap lands.",
  };

  canvas.addEventListener("pointerdown", (e) => {
    if (S.edit === "none" || !S.config || !S.meta || e.button > 0) return;
    const L = layout(); const [cx, cy] = pointerToCanvas(e);
    const draft = clone(S.config);
    const g = { viewport: draft.viewport, jump_roi: draft.rois.jump_roi, slide_roi: draft.rois.slide_roi,
      jump_point: draft.input.jump_point, slide_point: draft.input.slide_point };
    const [fx, fy] = canvasToFrame(L, cx, cy);
    let mode = null, corner = null;
    if (S.edit.endsWith("_point")) mode = "point";
    else {
      const r = rectPx(L, g, S.edit);
      const [x1, y1] = frameToCanvas(L, r.x, r.y), [x2, y2] = frameToCanvas(L, r.x + r.w, r.y + r.h);
      const near = (ax, ay) => Math.abs(cx - ax) <= 10 && Math.abs(cy - ay) <= 10;
      if (near(x1, y1)) corner = "tl"; else if (near(x2, y1)) corner = "tr"; else if (near(x1, y2)) corner = "bl"; else if (near(x2, y2)) corner = "br";
      if (corner) mode = "resize";
      else if (cx >= x1 && cx <= x2 && cy >= y1 && cy <= y2) mode = "move";
      else { mode = "draw"; }
    }
    S.drag = { id: e.pointerId, mode, corner, target: S.edit, draft, start: [fx, fy], startRect: S.edit.endsWith("_point") ? null : rectPx(L, g, S.edit) };
    canvas.setPointerCapture(e.pointerId);
    applyDrag(e);
    e.preventDefault();
  });

  function applyDrag(e) {
    const d = S.drag; const L = layout();
    if (!d || !L) return;
    const [cx, cy] = pointerToCanvas(e);
    let [fx, fy] = canvasToFrame(L, cx, cy);
    const g = { viewport: d.draft.viewport };
    const container = d.target === "viewport" ? { x: 0, y: 0, w: L.W, h: L.H } : vpPx(L, d.draft.viewport);
    fx = clamp(fx, container.x, container.x + container.w); fy = clamp(fy, container.y, container.y + container.h);
    if (d.mode === "point") {
      const nx = r4(clamp((fx - container.x) / container.w, 0, 1)), ny = r4(clamp((fy - container.y) / container.h, 0, 1));
      d.draft.input[d.target] = { x: nx, y: ny };
      draw(); return;
    }
    const minW = MIN_NORM * container.w, minH = MIN_NORM * container.h;
    let { x, y, w, h } = d.startRect;
    const dx = fx - d.start[0], dy = fy - d.start[1];
    if (d.mode === "move") {
      x = clamp(x + dx, container.x, container.x + container.w - w); y = clamp(y + dy, container.y, container.y + container.h - h);
    } else if (d.mode === "draw") {
      x = Math.min(d.start[0], fx); y = Math.min(d.start[1], fy); w = Math.max(minW, Math.abs(fx - d.start[0])); h = Math.max(minH, Math.abs(fy - d.start[1]));
    } else {
      let x1 = x, y1 = y, x2 = x + w, y2 = y + h;
      if (d.corner.includes("l")) x1 = Math.min(fx, x2 - minW); else x2 = Math.max(fx, x1 + minW);
      if (d.corner.includes("t")) y1 = Math.min(fy, y2 - minH); else y2 = Math.max(fy, y1 + minH);
      x = x1; y = y1; w = x2 - x1; h = y2 - y1;
    }
    let nx = clamp((x - container.x) / container.w, 0, 1 - MIN_NORM), ny = clamp((y - container.y) / container.h, 0, 1 - MIN_NORM);
    let nw = clamp(w / container.w, MIN_NORM, 1), nh = clamp(h / container.h, MIN_NORM, 1);
    nx = r4(nx); ny = r4(ny); nw = r4(Math.min(nw, 1 - nx)); nh = r4(Math.min(nh, 1 - ny));
    const rect = { x: nx, y: ny, w: nw, h: nh };
    if (d.target === "viewport") d.draft.viewport = rect; else d.draft.rois[d.target] = rect;
    void g;
    draw();
  }

  canvas.addEventListener("pointermove", (e) => { if (S.drag && e.pointerId === S.drag.id) applyDrag(e); });
  canvas.addEventListener("pointerup", async (e) => {
    if (!S.drag || e.pointerId !== S.drag.id) return;
    applyDrag(e);
    const d = S.drag;
    d.committing = true; // keep showing the draft until the backend confirms
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
    await saveConfig(d.draft, `${d.target} updated`);
    if (S.drag === d) S.drag = null;
    draw();
  });
  const cancelDrag = (e) => {
    if (S.drag && !S.drag.committing && (!e || e.pointerId === S.drag.id)) { S.drag = null; draw(); localLog("info", "edit cancelled"); }
  };
  canvas.addEventListener("pointercancel", cancelDrag);
  canvas.addEventListener("lostpointercapture", (e) => { if (S.drag && e.pointerId === S.drag.id) cancelDrag(e); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape") cancelDrag(); });

  // ------------------------------------------------------------ config I/O
  async function loadConfig() {
    try { S.config = await api("GET", "/api/config"); renderConfig(); } catch (e) { /* offline */ }
    loadProfiles(); loadDevices(); loadScreens();
  }
  async function saveConfig(cfg, label) {
    try {
      S.config = await api("PUT", "/api/config", cfg); // display backend-confirmed config
      renderConfig();
      localLog("info", label || "configuration saved");
      return true;
    } catch (e) {
      localLog("error", "config rejected: " + e.message);
      renderConfig();
      return false;
    }
  }

  // --------------------------------------------------------------- sliders
  const P = (f) => "processing." + f, D = (f) => "detection." + f;
  const DETECT = [
    { k: P("method"), label: "Mask method", type: "select", opts: ["canny", "adaptive"], help: "Canny edges or adaptive threshold" },
    { k: P("blur_ksize"), label: "Blur kernel", min: 1, max: 31, step: 2, unit: "px", help: "1 = no blur" },
    { k: P("canny_low"), label: "Canny low threshold", min: 0, max: 254, step: 1, unit: "", show: (r) => r.processing.method === "canny" },
    { k: P("canny_high"), label: "Canny high threshold", min: 1, max: 255, step: 1, unit: "", show: (r) => r.processing.method === "canny" },
    { k: P("adaptive_block"), label: "Adaptive block size", min: 3, max: 99, step: 2, unit: "px", show: (r) => r.processing.method === "adaptive" },
    { k: P("adaptive_c"), label: "Adaptive offset C", min: -50, max: 50, step: 1, unit: "", show: (r) => r.processing.method === "adaptive" },
    { k: P("morph"), label: "Morphology", type: "select", opts: ["none", "open", "close", "dilate", "erode"] },
    { k: P("morph_ksize"), label: "Morphology kernel", min: 1, max: 25, step: 1, unit: "px" },
    { k: P("morph_iterations"), label: "Morphology iterations", min: 1, max: 5, step: 1, unit: "×" },
    { k: D("require"), label: "Detection rule", type: "select", opts: ["both", "either", "density", "contour"], help: "Combine density and contour tests" },
    { k: D("min_density"), label: "Min mask density", min: 0, max: 0.5, step: 0.005, unit: "%", pct: true },
    { k: D("min_area_frac"), label: "Min contour box area", min: 0, max: 1, step: 0.005, unit: "% of ROI", pct: true },
    { k: D("max_area_frac"), label: "Max contour box area", min: 0, max: 1, step: 0.005, unit: "% of ROI", pct: true },
    { k: D("min_width_frac"), label: "Min box width", min: 0, max: 1, step: 0.01, unit: "% of ROI", pct: true },
    { k: D("min_height_frac"), label: "Min box height", min: 0, max: 1, step: 0.01, unit: "% of ROI", pct: true },
    { k: D("min_aspect"), label: "Min aspect (w/h)", min: 0, max: 10, step: 0.05, unit: "" },
    { k: D("max_aspect"), label: "Max aspect (w/h)", min: 0.1, max: 50, step: 0.1, unit: "" },
    { k: D("center_y_min"), label: "Box center Y min", min: 0, max: 1, step: 0.01, unit: "% of ROI", pct: true },
    { k: D("center_y_max"), label: "Box center Y max", min: 0, max: 1, step: 0.01, unit: "% of ROI", pct: true },
    { k: D("confirm_frames"), label: "Confirm after", min: 1, max: 30, step: 1, unit: "frames" },
    { k: D("release_frames"), label: "Release after", min: 1, max: 30, step: 1, unit: "frames" },
  ];
  const TIMING = [
    { k: "decision.jump_cooldown_ms", label: "Jump cooldown", min: 30, max: 2000, step: 10, unit: "ms" },
    { k: "decision.slide_cooldown_ms", label: "Slide cooldown", min: 30, max: 2000, step: 10, unit: "ms" },
    { k: "decision.detection_ttl_ms", label: "Detection expiry", min: 10, max: 1000, step: 10, unit: "ms" },
    { k: "decision.max_frame_age_ms", label: "Max frame age at decision", min: 10, max: 1000, step: 10, unit: "ms" },
    { k: "decision.max_command_age_ms", label: "Max command age", min: 10, max: 1000, step: 10, unit: "ms" },
    { k: "decision.tap_ms", label: "Key tap duration", min: 10, max: 500, step: 5, unit: "ms" },
    { k: "decision.arbitration", label: "Jump + slide together", type: "select", opts: ["prefer_jump", "prefer_slide", "suppress_both"] },
    { k: "decision.double_jump", label: "Double jump", type: "bool", cap: "double_jump" },
    { k: "decision.double_jump_delay_ms", label: "Double jump delay", min: 30, max: 1000, step: 10, unit: "ms" },
    { k: "decision.slide_hold", label: "Slide hold", type: "bool", cap: "hold" },
    { k: "decision.slide_hold_ms", label: "Slide hold duration", min: 50, max: 3000, step: 10, unit: "ms" },
  ];
  const PREVIEW = [
    { k: "preview.fps", label: "Preview rate", min: 1, max: 30, step: 1, unit: "fps" },
    { k: "preview.max_width", label: "Preview width", min: 160, max: 1920, step: 32, unit: "px" },
    { k: "preview.jpeg_quality", label: "JPEG quality", min: 30, max: 95, step: 1, unit: "" },
  ];

  function activeCaps() {
    if (!S.status) return null;
    return S.status.dry_run ? S.status.input.dryrun_capabilities : S.status.input.capabilities;
  }

  function buildFields(container, fields, base, prefix) {
    container.innerHTML = "";
    const caps = S.status ? S.status.input.capabilities : null;
    for (const f of fields) {
      if (f.show && !f.show(base)) continue;
      const path = (prefix ? prefix + "." : "") + f.k;
      const val = get(S.config, path);
      const row = document.createElement("div");
      row.className = "slider";
      const fmtVal = (v) => (f.pct ? (v * 100).toFixed(1) + " " + f.unit : v + (f.unit ? " " + f.unit : ""));
      if (f.type === "select") {
        row.innerHTML = `<span class="name">${esc(f.label)}</span><span></span><select aria-label="${esc(f.label)}">${f.opts.map((o) => `<option ${o === val ? "selected" : ""}>${o}</option>`).join("")}</select>`;
        row.querySelector("select").addEventListener("change", (e) => commitField(path, e.target.value));
      } else if (f.type === "bool") {
        let note = "";
        if (caps) {
          if (f.cap === "double_jump" && !caps.double_jump) note = `Unsupported by ${caps.name} input`;
          if (f.cap === "hold") note = caps.independent_hold_release ? "Key down / timed key up" : caps.timed_hold ? `${caps.name}: fixed-duration swipe only (no independent hold/release)` : `Unsupported by ${caps.name} input`;
        }
        const unsupported = note.startsWith("Unsupported");
        row.innerHTML = `<label class="check name"><input type="checkbox" ${val ? "checked" : ""} ${unsupported ? "disabled" : ""}> ${esc(f.label)}</label><span></span><span class="help">${esc(note)}</span>`;
        if (unsupported) row.classList.add("disabled");
        row.querySelector("input").addEventListener("change", (e) => commitField(path, e.target.checked));
      } else {
        row.innerHTML = `<span class="name">${esc(f.label)}</span><span class="val">${esc(fmtVal(val))}</span>` +
          `<input type="range" min="${f.min}" max="${f.max}" step="${f.step}" value="${val}" aria-label="${esc(f.label)}">` + (f.help ? `<span class="help">${esc(f.help)}</span>` : "");
        const input = row.querySelector("input"), out = row.querySelector(".val");
        input.addEventListener("input", () => { out.textContent = fmtVal(Number(input.value)); });
        input.addEventListener("change", () => commitField(path, Number(input.value)));
      }
      container.appendChild(row);
    }
  }

  function commitField(path, value) {
    const draft = clone(S.config);
    set(draft, path, value);
    saveConfig(draft, `${path} = ${value}`);
  }

  // ----------------------------------------------------------- render: cfg
  function renderConfig() {
    if (!S.config) return;
    const rv = S.config.vision[S.roiTab];
    buildFields($("detect-fields"), DETECT, rv, "vision." + S.roiTab);
    buildFields($("timing-fields"), TIMING, S.config, "");
    buildFields($("preview-fields"), PREVIEW, S.config, "");
    const form = $("backend-form");
    if (!form.contains(document.activeElement)) {
      for (const el of form.elements) {
        if (!el.name) continue;
        const v = get(S.config, el.name);
        if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
      }
    }
    updateFormVisibility();
    draw();
  }

  function updateFormVisibility() {
    const form = $("backend-form");
    const cap = form.elements["capture.backend"].value, inp = form.elements["input.backend"].value;
    form.querySelectorAll("[data-show]").forEach((el) => { el.hidden = el.dataset.show !== cap; });
    form.querySelectorAll("[data-show-input]").forEach((el) => { el.hidden = el.dataset.showInput !== inp; });
  }
  $("backend-form").addEventListener("change", updateFormVisibility);

  $("backend-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const draft = clone(S.config);
    for (const el of e.target.elements) {
      if (!el.name) continue;
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") v = Number(v);
      set(draft, el.name, v);
    }
    const ok = await saveConfig(draft, "backend settings applied");
    $("backend-result").textContent = ok ? "Saved. Calibration is cleared if coordinates/backends changed." : "Rejected — see logs.";
    $("backend-result").className = "small " + (ok ? "ok" : "bad");
    loadDevices();
  });

  // -------------------------------------------------------- render: status
  function renderStatus() {
    const st = S.status;
    const pill = $("state-pill");
    pill.textContent = st.state; pill.dataset.state = st.state;
    $("state-reason").textContent = st.reason + (st.last_error && st.state === "ERROR" ? "" : "");
    const dry = $("dry-run");
    dry.checked = st.dry_run;
    dry.closest(".dryrun-toggle").classList.toggle("real", !st.dry_run);
    $("dry-run-label").textContent = st.dry_run ? "DRY-RUN ON" : "REAL INPUT";
    if (st.estop_file) $("estop-path").textContent = st.estop_file;
    const cal = $("cal-status");
    if (st.calibrated) {
      const c = st.calibration;
      const probs = st.calibration_problems || [];
      cal.innerHTML = probs.length ? `<span class="bad">⚠ ${esc(probs.join("; "))}</span>`
        : `<span class="ok">✓ Calibrated</span> for ${c.frame_w}×${c.frame_h}, ${esc(c.capture_backend)} → ${esc(c.input_backend)}` + (c.display_w ? `, display ${c.display_w}×${c.display_h}` : "") + ` <span class="muted">(${esc(c.confirmed_at)})</span>`;
    } else cal.innerHTML = `<span class="bad">Not calibrated</span> — required before real input.`;
    const caps = st.input.capabilities;
    const capNames = [["tap", "tap"], ["double_jump", "double jump"], ["timed_hold", "timed hold"], ["independent_hold_release", "hold/release"], ["uses_coordinates", "coordinates"]];
    $("caps").innerHTML = `<span class="cap ${caps.real ? "no" : "yes"}" style="text-decoration:none">${esc(caps.name)}${caps.real ? " (real)" : ""}</span>` +
      capNames.map(([k, n]) => `<span class="cap ${caps[k] ? "yes" : "no"}">${n}</span>`).join("") +
      (caps.notes || []).map((n) => `<span class="muted">· ${esc(n)}</span>`).join(" ");
    if (st.macro) renderMacro(st.macro);
    if (st.autoplay) renderAutoplay(st.autoplay);
    if (st.game_state) renderGameState(st.game_state);
    updateButtons();
  }

  function renderGameState(g) {
    const pill = $("game-state-pill"), detail = $("game-state-detail");
    if (!pill || !detail) return;
    pill.textContent = g.enabled ? g.state : "OFF";
    pill.dataset.state = g.state === "RUNNING" ? "RUNNING" : (g.state === "DANGER" ? "ERROR" : "PAUSED");
    const match = g.screen ? `${g.screen} (${Number(g.score || 0).toFixed(2)})` : "no template match";
    const mode = g.guard_actions ? (g.actions_allowed ? "guard allowing actions" : "guard blocking actions") : "observe only";
    detail.textContent = !g.enabled ? "Disabled in backend settings." :
      `${match} · ${mode}` + (g.configured ? "" : " · teach a running screen to enable Guard");
    detail.className = "small " + (g.state === "DANGER" || (g.guard_actions && !g.actions_allowed) ? "bad" : "muted");
  }

  function updateButtons() {
    const st = S.eventsOk ? S.status : null;
    const running = st && (st.state === "RUNNING" || st.state === "READY");
    const bStart = $("btn-start"), bPause = $("btn-pause"), bStop = $("btn-stop");
    bStart.disabled = !st || !st.can_start;
    bPause.disabled = !running;
    bStop.disabled = !st;
    bStart.title = !st ? "Backend disconnected" : running ? "Already running" : (st.start_blockers || []).join("\n");
    bPause.title = running ? "" : "Bot is not running";
    const blockers = !st ? ["Backend disconnected"] : running ? [] : st.start_blockers || [];
    $("blockers").innerHTML = blockers.map((b) => `<li>${esc(b)}</li>`).join("");
    const testBlock = !st ? "Backend disconnected" : running ? "Pause or stop the bot before testing input" : "";
    $("btn-test-jump").disabled = $("btn-test-slide").disabled = !!testBlock;
    $("btn-calibrate").disabled = !!testBlock;
    $("test-reason").textContent = testBlock || (st && st.dry_run ? "Dry-run: tests are logged, nothing is sent." : "Tests send REAL input to the selected backend.");
    $("dry-run").disabled = !st;
    const sel = $("backend-form").elements["input.backend"];
    if (S.devices) {
      for (const o of sel.options) {
        const b = S.devices.input_backends.find((x) => x.name === o.value);
        o.disabled = b ? !b.available : false;
        o.title = b && !b.available ? (b.reason || "unavailable") : "";
      }
    }
    const caps = activeCaps();
    $("feature-support").textContent = caps ? `Active input path: ${caps.name}. ` + (caps.notes || []).join(" ") : "";
  }

  // ------------------------------------------------------ render: telemetry
  function renderTelemetry() {
    const t = S.telemetry;
    const ms = (s) => (s && s.n ? `${fmt(s.p50, 1)} / ${fmt(s.p95, 1)}` : "—");
    const last = t.last_executed && t.last_executed.action ? `${t.last_executed.action}${t.last_executed.double ? "×2" : ""} (${t.last_executed.backend}${t.last_executed.real ? ", real" : ""})` : "—";
    const det = t.last_detected && t.last_detected.actions ? t.last_detected.actions.join(" + ") + ` @#${t.last_detected.frame_id}` : "—";
    const sup = t.last_suppressed ? Object.entries(t.last_suppressed).filter(([k]) => k !== "frame_id").map(([k, v]) => `${k}: ${v}`).join(", ") : "";
    const cells = [
      ["Capture FPS", fmt(t.capture_fps), ""], ["Processing FPS", fmt(t.processing_fps), ""], ["Preview encode FPS", fmt(t.preview_fps), ""],
      ["Capture ms", ms(t.capture_ms), "p50 / p95"], ["Vision ms", ms(t.vision_ms), "p50 / p95"], ["Input dispatch ms", ms(t.dispatch_ms), "local completion p50 / p95"],
      ["Frame age @ decision", ms(t.frame_age_ms), "ms p50 / p95"], ["Input queue", t.input_queue, "pending"], ["Dropped frames", t.dropped_frames, "stale, overwritten"],
      ["Commands", `${t.executed_commands} ok`, `${t.expired_commands} expired · ${t.dropped_commands} dropped · ${t.rejected_commands} rejected`],
      ["Detected", det, sup ? "suppressed " + sup : ""], ["Executed", last, t.last_executed && t.last_executed.dispatch_ms != null ? `${t.last_executed.dispatch_ms} ms dispatch` : ""],
      ["Capture errors", t.capture_errors, ""], ["Latest error", t.last_error || "none", (t.notes || []).join("; ")],
      ["Game state", t.game_state ? t.game_state.state : "—", t.game_state && t.game_state.screen ? `${t.game_state.screen} · score ${Number(t.game_state.score || 0).toFixed(2)}` : ""],
    ];
    $("telemetry").innerHTML = cells.map(([k, v, s]) => `<div class="tele"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
  }

  // ------------------------------------------------------------------ logs
  function appendLogs(entries) {
    const box = $("logs");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    for (const e of entries) {
      const div = document.createElement("div");
      div.className = "log-" + (e.level || "info");
      div.textContent = `${new Date(e.t * 1000).toLocaleTimeString()} ${String(e.level).toUpperCase().padEnd(7)} ${e.msg}`;
      box.appendChild(div);
    }
    while (box.childElementCount > MAX_LOGS) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  $("btn-clear-logs").addEventListener("click", () => { $("logs").innerHTML = ""; });

  // -------------------------------------------------------------- controls
  async function control(path, body) {
    try {
      const r = await api("POST", path, body);
      if (!r.ok && r.reasons && r.reasons.length) localLog("warning", r.reasons.join("; "));
      return r;
    } catch (e) { localLog("error", e.message); return null; }
  }
  $("btn-start").addEventListener("click", () => control("/api/control/start"));
  $("btn-pause").addEventListener("click", () => control("/api/control/pause"));
  $("btn-stop").addEventListener("click", () => control("/api/control/stop"));
  $("dry-run").addEventListener("change", async (e) => {
    const enabled = e.target.checked;
    if (!enabled && !confirm("Disable dry-run? The bot will send REAL input to the game while RUNNING.")) { e.target.checked = true; return; }
    const r = await control("/api/control/dry-run", { enabled });
    if (r) e.target.checked = r.dry_run;
  });
  $("btn-test-jump").addEventListener("click", () => control("/api/control/test-input", { action: "jump" }));
  $("btn-test-slide").addEventListener("click", () => control("/api/control/test-input", { action: "slide" }));
  $("btn-calibrate").addEventListener("click", async () => { await control("/api/calibration/confirm"); loadConfig(); });
  $("btn-clear-cal").addEventListener("click", async () => {
    try { await api("DELETE", "/api/calibration"); loadConfig(); } catch (e) { localLog("error", e.message); }
  });

  $("view-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.view = b.dataset.view;
    $("view-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    previewSock && previewSock.send({ type: "view", view: S.view });
  });
  $("edit-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.edit = b.dataset.edit;
    $("edit-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    S.teach = null; teachUI();
    $("edit-hint").textContent = EDIT_HINTS[S.edit];
    draw();
  });
  $("roi-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.roiTab = b.dataset.roi;
    $("roi-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    renderConfig();
  });

  // ------------------------------------------------------ devices/profiles
  async function loadDevices() {
    try {
      const d = await api("GET", "/api/devices");
      S.devices = d;
      $("serial-list").innerHTML = d.adb.devices.map((x) => `<option value="${esc(x.serial)}">${esc(x.state)} ${esc(x.desc)}</option>`).join("");
      $("replay-list").innerHTML = d.replays.map((p) => `<option value="${esc(p)}">`).join("");
      $("adb-devices").textContent = d.adb.available
        ? (d.adb.devices.length ? "Devices: " + d.adb.devices.map((x) => `${x.serial} (${x.state})`).join(", ") : "ADB available, no devices attached.")
        : "ADB unavailable: " + (d.adb.error || "not found");
      updateButtons();
    } catch (_) { /* offline */ }
  }
  $("btn-refresh-dev").addEventListener("click", loadDevices);
  async function adbAction(kind) {
    const body = { endpoint: $("adb-endpoint").value.trim() };
    if (kind === "pair") body.code = $("adb-code").value.trim();
    try {
      const r = await api("POST", "/api/devices/adb/" + kind, body);
      $("adb-result").textContent = (r.ok ? "✓ " : "✗ ") + r.output;
      loadDevices();
    } catch (e) { $("adb-result").textContent = "✗ " + e.message; }
  }
  $("btn-adb-pair").addEventListener("click", () => adbAction("pair"));
  $("btn-adb-connect").addEventListener("click", () => adbAction("connect"));

  async function loadProfiles() {
    try {
      const r = await api("GET", "/api/profiles");
      $("profile-select").innerHTML = r.profiles.map((p) => `<option>${esc(p)}</option>`).join("");
      const keep = $("ap-profile").value;
      $("ap-profile").innerHTML = `<option value="">(none)</option>` + r.profiles.map((p) => `<option>${esc(p)}</option>`).join("");
      $("ap-profile").value = keep;
    } catch (_) {}
  }
  $("btn-load-profile").addEventListener("click", async () => {
    const name = $("profile-select").value; if (!name) return;
    try { S.config = await api("POST", `/api/profiles/${encodeURIComponent(name)}/load`); renderConfig(); $("profile-result").textContent = `Loaded "${name}" (dry-run stays on).`; }
    catch (e) { $("profile-result").textContent = e.message; }
  });
  $("btn-save-profile").addEventListener("click", async () => {
    const name = $("profile-name").value.trim();
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) { $("profile-result").textContent = "Use letters, digits, _ or - (max 40)."; return; }
    try { await api("POST", `/api/profiles/${encodeURIComponent(name)}/save`); $("profile-result").textContent = `Saved "${name}".`; loadProfiles(); }
    catch (e) { $("profile-result").textContent = e.message; }
  });

  // ---------------------------------------------------------------- macro
  function formatTime(s) {
    if (s == null || isNaN(s)) return "00:00.0";
    const mins = Math.floor(s / 60);
    const secs = (s % 60).toFixed(1);
    return `${String(mins).padStart(2, "0")}:${secs < 10 ? "0" : ""}${secs}`;
  }

  function renderMacro(m) {
    if (!m) return;
    const pill = $("macro-pill");
    if (pill) {
      pill.textContent = `● ${m.state}`;
      pill.className = "pill " + (m.state === "RECORDING" ? "pill-off" : (m.state === "PLAYING" ? "pill-on" : (m.state === "PAUSED" ? "pill-state" : "pill-off")));
      if (m.state === "PAUSED") pill.dataset.state = "PAUSED";
    }

    const bRec = $("btn-macro-record");
    const bStopRec = $("btn-macro-stop-rec");
    const bPlay = $("btn-macro-play");
    const bPause = $("btn-macro-pause");
    const bStopPlay = $("btn-macro-stop-play");

    const isRec = m.state === "RECORDING";
    const isPlay = m.state === "PLAYING";
    const isPaused = m.state === "PAUSED";

    if (bRec) {
      bRec.disabled = isRec || isPlay || isPaused;
      bRec.classList.toggle("recording", isRec);
    }
    if (bStopRec) bStopRec.disabled = !isRec;

    if (bPlay) {
      bPlay.disabled = isRec || isPlay || isPaused || (m.event_count === 0 && !m.name);
      bPlay.classList.toggle("playing", isPlay);
    }

    if (bPause) {
      bPause.disabled = !isPlay && !isPaused;
      bPause.textContent = isPaused ? "▶ Resume" : "❚❚ Pause";
    }
    if (bStopPlay) bStopPlay.disabled = !isPlay && !isPaused;

    // Loop info
    const loopInfo = $("macro-loop-info");
    if (loopInfo) {
      if (isPlay || isPaused) {
        loopInfo.textContent = `Playing Loop: ${m.current_loop} / ${m.target_loops || "∞"}`;
      } else if (isRec) {
        loopInfo.textContent = `Recording Round 1 (Loop 1)`;
      } else {
        loopInfo.textContent = m.name ? `Preset: ${m.name} (${m.event_count} actions, ${m.duration_s}s)` : "Loop: — (Ready to record)";
      }
    }

    // Timer & Progress
    const timerInfo = $("macro-timer-info");
    if (timerInfo) {
      timerInfo.textContent = `${formatTime(m.elapsed_s)} / ${formatTime(m.duration_s)}`;
    }
    const progBar = $("macro-progress-bar");
    if (progBar) {
      const pct = m.duration_s > 0 ? Math.min(100, (m.elapsed_s / m.duration_s) * 100) : 0;
      progBar.style.width = (isRec ? 100 : pct) + "%";
    }

    const evtCount = $("macro-event-count");
    if (evtCount) evtCount.textContent = `${m.event_count} actions recorded`;

    const lastAction = $("macro-last-action");
    if (lastAction) {
      if (m.last_error) {
        lastAction.textContent = `⚠ ${m.last_error}`;
        lastAction.className = "small bad";
      } else {
        lastAction.className = "small muted";
        lastAction.textContent = isRec ? "Listening for Jump/Slide & Restart click..." : (isPlay ? "Replaying inputs..." : "");
      }
    }

    // Event chips preview
    const previewBox = $("macro-events-preview");
    if (previewBox) {
      if (m.events_sample && m.events_sample.length) {
        previewBox.innerHTML = m.events_sample.slice(-25).map((e) => {
          let cls = e.action === "jump" ? "jump" : (e.action === "slide" ? "slide" : (e.action === "restart" ? "restart" : ""));
          let desc = `${e.t}s: ${e.action} ${e.kind}`;
          if (e.x != null && e.y != null) desc += ` (${e.x},${e.y})`;
          return `<span class="macro-chip ${cls}">${esc(desc)}</span>`;
        }).join("");
      } else if (!isRec) {
        previewBox.innerHTML = `<span class="small muted">No actions yet. Click Record to play round 1.</span>`;
      }
    }
  }

  async function loadMacroPresets() {
    try {
      const r = await api("GET", "/api/macro/list");
      const sel = $("macro-preset-select");
      if (sel) {
        sel.innerHTML = `<option value="">Load saved preset…</option>` + (r.macros || []).map((m) =>
          `<option value="${esc(m.name)}">${esc(m.name)} (${m.event_count} actions, ${m.duration_s}s)</option>`
        ).join("");
      }
    } catch (_) {}
  }

  $("btn-macro-record").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/macro/record/start");
      renderMacro(r.status);
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-stop-rec").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/macro/record/stop");
      renderMacro(r.status);
      loadMacroPresets();
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-play").addEventListener("click", async () => {
    const loops = Number($("macro-target-loops").value) || 1;
    const speed = Number($("macro-speed").value) || 1.0;
    const auto_restart = $("macro-auto-restart").checked;
    try {
      const r = await api("POST", "/api/macro/play/start", { target_loops: loops, speed, auto_restart });
      renderMacro(r.status);
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-pause").addEventListener("click", async () => {
    const isPaused = S.status && S.status.macro && S.status.macro.state === "PAUSED";
    try {
      const r = await api("POST", isPaused ? "/api/macro/play/resume" : "/api/macro/play/pause");
      renderMacro(r.status);
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-stop-play").addEventListener("click", async () => {
    try {
      const r = await api("POST", "/api/macro/play/stop");
      renderMacro(r.status);
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-save").addEventListener("click", async () => {
    const name = $("macro-save-name").value.trim();
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) {
      alert("Please use alphanumeric characters, dashes, or underscores for preset name (max 40 chars).");
      return;
    }
    try {
      await api("POST", `/api/macro/save/${encodeURIComponent(name)}`);
      localLog("info", `Saved macro preset "${name}".`);
      loadMacroPresets();
    } catch (e) { localLog("error", e.message); }
  });

  $("btn-macro-load").addEventListener("click", async () => {
    const name = $("macro-preset-select").value;
    if (!name) return;
    try {
      const r = await api("POST", `/api/macro/load/${encodeURIComponent(name)}`);
      renderMacro(r.status);
      localLog("info", `Loaded macro preset "${name}".`);
    } catch (e) { localLog("error", e.message); }
  });

  // ------------------------------------------------------------- autoplay
  const AP_NUM = ["ap-bx", "ap-by", "ap-bw", "ap-bh", "ap-tx", "ap-ty"];

  function teachUI() {
    $("btn-ap-pickbox").classList.toggle("btn-primary", S.teach === "box");
    $("btn-ap-picktap").classList.toggle("btn-primary", S.teach === "tap");
    canvas.classList.toggle("editing", S.edit !== "none" || !!S.teach);
    draw();
  }
  function setTeach(mode) {
    const next = S.teach === mode ? null : mode;
    $("edit-seg").querySelector('[data-edit="none"]').click(); // leave config edit targets (also clears S.teach)
    S.teach = next; teachUI();
    $("edit-hint").textContent = next === "box" ? "Drag a box around the screen marker (frame px, not saved to config)." : next === "tap" ? "Click where to tap (frame px, not saved to config)." : "";
  }
  $("btn-ap-pickbox").addEventListener("click", () => setTeach("box"));
  $("btn-ap-picktap").addEventListener("click", () => setTeach("tap"));
  AP_NUM.forEach((id) => $(id).addEventListener("input", draw));
  $("ap-role").addEventListener("change", () => { $("ap-profile-wrap").hidden = $("ap-role").value !== "map"; });

  function pickFrame(e) {
    const L = layout(); if (!L) return null;
    const [fx, fy] = canvasToFrame(L, ...pointerToCanvas(e));
    return [Math.round(clamp(fx, 0, L.W - 1)), Math.round(clamp(fy, 0, L.H - 1))];
  }
  function applyPick(p) {
    const q = S.pick.p0;
    if (S.teach === "tap") { $("ap-tx").value = p[0]; $("ap-ty").value = p[1]; }
    else { $("ap-bx").value = Math.min(q[0], p[0]); $("ap-by").value = Math.min(q[1], p[1]); $("ap-bw").value = Math.abs(p[0] - q[0]); $("ap-bh").value = Math.abs(p[1] - q[1]); }
    draw();
  }
  canvas.addEventListener("pointerdown", (e) => {
    if (!S.teach || e.button > 0) return;
    const p = pickFrame(e); if (!p) return;
    S.pick = { id: e.pointerId, p0: p };
    canvas.setPointerCapture(e.pointerId);
    applyPick(p); e.preventDefault();
  });
  canvas.addEventListener("pointermove", (e) => { if (S.pick && e.pointerId === S.pick.id) { const p = pickFrame(e); if (p) applyPick(p); } });
  const endPick = (e) => { if (S.pick && e.pointerId === S.pick.id) S.pick = null; };
  canvas.addEventListener("pointerup", endPick);
  canvas.addEventListener("pointercancel", endPick);

  $("btn-ap-save").addEventListener("click", async () => {
    const [x, y, w, h, tx, ty] = AP_NUM.map((id) => $(id).valueAsNumber);
    const role = $("ap-role").value, out = $("ap-teach-result");
    const fail = (m) => { out.textContent = m; out.className = "small bad"; };
    if ([x, y, w, h].some(Number.isNaN)) return fail("Pick or enter the box first.");
    if (Number.isNaN(tx) !== Number.isNaN(ty)) return fail("Tap needs both x and y.");
    try {
      await api("POST", "/api/autoplay/screens", {
        name: $("ap-name").value.trim(), role, box: [x, y, w, h], tap: Number.isNaN(tx) ? null : [tx, ty],
        threshold: $("ap-thr").valueAsNumber || 0.8, profile: role === "map" && $("ap-profile").value ? $("ap-profile").value : null,
      });
      out.textContent = "Saved."; out.className = "small ok";
      loadScreens();
    } catch (e) { fail(e.message); }
  });

  async function loadScreens() {
    try { S.screens = (await api("GET", "/api/autoplay/screens")).screens; } catch (_) { return; }
    const map = $("ap-map"), keepMap = map.value;
    const keepItems = new Set([...$("ap-items").querySelectorAll("input:checked")].map((i) => i.value));
    const of = (role) => S.screens.filter((s) => s.role === role);
    map.innerHTML = `<option value="">(none)</option>` + of("map").map((s) => `<option>${esc(s.name)}</option>`).join("");
    map.value = keepMap;
    $("ap-items").innerHTML = of("item").map((s) => `<label class="check"><input type="checkbox" value="${esc(s.name)}" ${keepItems.has(s.name) ? "checked" : ""}> ${esc(s.name)}</label>`).join("") || `<span class="muted">No item screens taught.</span>`;
    renderScreens();
  }
  function renderScreens() {
    $("ap-screens").innerHTML = S.screens.map((s) => {
      const sc = S.scores[s.name];
      const score = sc == null ? "" : `<span class="${sc >= s.threshold ? "ok" : "muted"}">${sc.toFixed(2)} / ${s.threshold}</span>`;
      return `<div class="row gap" style="justify-content: space-between; padding: 3px 0;"><span class="${s.role === "danger" ? "bad" : ""}"><b>${esc(s.name)}</b> <span class="muted">${esc(s.role)}</span></span>` +
        `<span class="row gap">${score}<button class="btn btn-sm btn-ghost" data-del="${esc(s.name)}">Delete</button></span></div>`;
    }).join("") || `<span class="muted">No screens taught yet.</span>`;
  }
  $("ap-screens").addEventListener("click", async (e) => {
    const name = e.target.dataset.del; if (!name) return;
    try { await api("DELETE", "/api/autoplay/screens/" + encodeURIComponent(name)); delete S.scores[name]; loadScreens(); } catch (err) { localLog("error", err.message); }
  });
  $("btn-ap-check").addEventListener("click", async () => {
    try { S.scores = (await api("GET", "/api/autoplay/match")).scores; renderScreens(); } catch (e) { localLog("error", e.message); }
  });

  $("ap-loops").addEventListener("change", () => { $("ap-loops-wrap").hidden = $("ap-loops").value !== "custom"; });
  $("btn-ap-start").addEventListener("click", async () => {
    const custom = $("ap-loops").value === "custom";
    const r = await control("/api/autoplay/start", {
      map: $("ap-map").value || null,
      items: [...$("ap-items").querySelectorAll("input:checked")].map((i) => i.value),
      loops: custom ? $("ap-loops-custom").valueAsNumber : Number($("ap-loops").value),
      roll_cap: $("ap-roll-cap").valueAsNumber, heart_wait_min: $("ap-heart").valueAsNumber, unknown_back_s: $("ap-back").valueAsNumber,
    });
    if (r && r.status) renderAutoplay(r.status);
  });
  $("btn-ap-stop").addEventListener("click", async () => { const r = await control("/api/autoplay/stop"); if (r && r.status) renderAutoplay(r.status); });

  function renderAutoplay(a) {
    $("btn-ap-start").disabled = a.active;
    $("btn-ap-stop").disabled = !a.active;
    const el = $("ap-status");
    el.textContent = (a.active ? "ACTIVE" : "Idle") + ` · loops ${a.loops_done ?? 0}/${a.loops ?? "—"} · rolls ${a.rolls ?? 0} · screen ${a.screen || "—"}` +
      (a.wait_s > 0 ? ` · waiting ${Math.round(a.wait_s)}s` : "") + (a.message ? ` · ${a.message}` : "");
    el.className = "small " + (a.alert ? "bad" : "muted");
  }

  // ---------------------------------------------------------------- start
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  setInterval(() => { if (!S.eventsOk) api("GET", "/api/health").catch(() => {}); }, 5000);
  renderConn();
  updateButtons();
  startSockets();
  loadConfig();
  loadMacroPresets();
})();
