// TableSense setup page: draw table zones on a camera snapshot and pick the staff uniform colour.
// Zones are saved normalised (0..1) so they survive camera resolution changes.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const img = $("snap"), svg = $("svg"), maskCv = $("mask"), stage = $("stage");
  const pix = document.createElement("canvas");   // offscreen copy for colour sampling
  const pctx = pix.getContext("2d", { willReadFrequently: true });
  let W = 0, H = 0, pixels = null;

  let zones = [];          // {id, name, seats, pts: [[x,y] px]}
  let pendingNorm = null;  // zones from server waiting for the image size
  let sel = -1;            // selected zone index
  let draft = null;        // {pts: [...], cursor: [x,y]} while drawing
  let drag = null;         // {kind: "vertex"|"move", zi, vi, last: [x,y]}
  let tab = "tables";
  let dirty = false;
  let mode = "simulation";

  let samples = [];        // per-click {rgb: [r,g,b], hsv: [[h,s,v]...]}
  let savedUniform = null;

  // ------------------------------------------------------------ utilities
  function toast(text, ok = true) {
    const el = document.createElement("div");
    el.className = `toast ${ok ? "border-emerald-500/40" : "border-red-500/50"}`;
    el.innerHTML = `<span>${ok ? "✅" : "⚠️"}</span><div class="flex-1">${esc(text)}</div>`;
    $("toasts").prepend(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 4000);
  }
  function setDirty(v) { dirty = v; $("dirty").classList.toggle("hidden", !v); }
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  function toImg(ev) {
    const r = svg.getBoundingClientRect();
    return [((ev.clientX - r.left) / r.width) * W, ((ev.clientY - r.top) / r.height) * H];
  }
  const screenScale = () => W / svg.getBoundingClientRect().width; // image px per screen px
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const centroid = (pts) => [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];

  function nextId() {
    let n = 1;
    const ids = new Set(zones.map((z) => z.id));
    while (ids.has(`T${n}`)) n++;
    return `T${n}`;
  }

  // -------------------------------------------------------------- loading
  async function loadImage(url, fallbackMsg) {
    try {
      const res = await fetch(`${url}${url.includes("?") ? "&" : "?"}t=${Date.now()}`);
      if (!res.ok) {
        const msg = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
        throw new Error(msg);
      }
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      await new Promise((ok, fail) => { img.onload = ok; img.onerror = fail; img.src = objUrl; });
      W = img.naturalWidth; H = img.naturalHeight;
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      pix.width = maskCv.width = W; pix.height = maskCv.height = H;
      pctx.drawImage(img, 0, 0);
      pixels = pctx.getImageData(0, 0, W, H).data;
      if (pendingNorm) {
        zones = pendingNorm.map((z) => ({ ...z, pts: z.polygon.map(([x, y]) => [x * W, y * H]) }));
        pendingNorm = null;
      }
      $("src-note").classList.add("hidden");
      render();
      renderMask();
      return true;
    } catch (err) {
      $("src-note").textContent = `${fallbackMsg || "Could not load image"}: ${err.message}`;
      $("src-note").classList.remove("hidden");
      return false;
    }
  }

  async function init() {
    const z = await (await fetch("/api/zones")).json();
    mode = z.mode;
    const badge = $("mode-badge");
    badge.textContent = mode === "live" ? "● LIVE" : "SIMULATION";
    badge.className = `badge ${mode === "live" ? "bg-red-500/15 text-red-300 border border-red-500/30" : "bg-amber-400/15 text-amber-300 border border-amber-400/30"}`;
    pendingNorm = z.tables;
    savedUniform = z.uniform_hsv;
    $("method").value = z.staff_method || "combined";
    $("ratio").value = Math.round((z.uniform_min_ratio || 0.3) * 100);
    $("ratio-val").textContent = `${$("ratio").value}%`;
    showSavedUniform();

    // Prefer the live frame; otherwise grab directly from the configured video source.
    const ok = mode === "live"
      ? await loadImage("/api/snapshot", "Live frame not available yet")
      : await loadImage("/api/source_frame", "No camera/video source");
    if (!ok) {
      await loadImage("/api/snapshot", "Fallback failed");
      $("src-note").textContent += " Showing the simulated floor plan instead. Zones you draw here only make sense on a real camera image, so put a video in videos/ or connect your camera first.";
      $("src-note").classList.remove("hidden");
    }
    $("stage-title").textContent = mode === "live" ? "Live camera snapshot" : "Video source snapshot";
    setDirty(false);
    renderList();
    updateHint();
  }

  // ------------------------------------------------------------ rendering
  function el(name, attrs) {
    const e = document.createElementNS(SVGNS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  function render() {
    svg.replaceChildren();
    const s = screenScale();
    zones.forEach((z, i) => {
      const poly = el("polygon", { points: z.pts.map((p) => p.join(",")).join(" "), class: `zone ${i === sel ? "sel" : ""}`, "data-z": i });
      svg.appendChild(poly);
      const [cx, cy] = centroid(z.pts);
      const label = el("text", { x: cx, y: cy, "text-anchor": "middle", "dominant-baseline": "middle", class: "zone-label", style: `font-size:${15 * s}px` });
      label.textContent = `${z.name} · ${z.seats}`;
      svg.appendChild(label);
    });
    if (sel >= 0 && zones[sel] && tab === "tables") {
      zones[sel].pts.forEach((p, vi) => svg.appendChild(el("circle", { cx: p[0], cy: p[1], r: 6 * s, class: "handle", "data-v": vi })));
    }
    if (draft) {
      const pts = draft.cursor ? [...draft.pts, draft.cursor] : draft.pts;
      if (pts.length > 1) svg.appendChild(el("polygon", { points: pts.map((p) => p.join(",")).join(" "), class: "draft" }));
      draft.pts.forEach((p, i) => svg.appendChild(el("circle", { cx: p[0], cy: p[1], r: (i === 0 ? 7 : 4) * s, class: i === 0 ? "draft-first" : "draft-pt" })));
    }
    stage.classList.toggle("drawing", !!draft);
    stage.classList.toggle("picking", tab === "uniform");
  }

  function renderList() {
    const list = $("zlist");
    if (!zones.length) {
      list.innerHTML = `<li class="text-sm text-slate-500 py-2">No zones yet. Draw one per table.</li>`;
      return;
    }
    list.innerHTML = zones.map((z, i) => `
      <li class="zrow ${i === sel ? "sel" : ""}" data-row="${i}">
        <span class="w-2.5 h-2.5 rounded-full ${i === sel ? "bg-emerald-400" : "bg-blue-400"}"></span>
        <input class="input !py-1 !px-2 min-w-0" data-name="${i}" value="${esc(z.name)}" maxlength="30" aria-label="Table name" />
        <label class="flex items-center gap-1 text-[11px] text-slate-400"><input class="input !py-1 !px-1.5 w-11 text-center" type="number" min="1" max="30" data-seats="${i}" value="${z.seats}" aria-label="Seats" />seats</label>
        <button class="btn-xs !px-2" data-del="${i}" title="Delete zone">✕</button>
      </li>`).join("");
  }

  function updateHint() {
    let t = "";
    if (tab === "uniform") t = "Click on a waiter's shirt to sample the uniform colour.";
    else if (draft) t = draft.pts.length < 3 ? `Click the corners of the seating area (${draft.pts.length} points so far).` : "Click the first point (yellow) or press Enter to finish this zone.";
    else if (sel >= 0) t = `Editing ${zones[sel].name}: drag the corners or the whole zone. Press Del to delete it.`;
    else t = zones.length ? `${zones.length} zone${zones.length > 1 ? "s" : ""}. Select one to edit it, or draw another.` : "Start with “Draw a table zone”.";
    $("hint").textContent = t;
  }

  // -------------------------------------------------------------- drawing
  function startDraft() {
    if (tab !== "tables") switchTab("tables");
    draft = { pts: [], cursor: null };
    sel = -1;
    render(); renderList(); updateHint();
  }
  function finishDraft() {
    if (!draft || draft.pts.length < 3) return;
    const id = nextId();
    zones.push({ id, name: id, seats: 4, pts: draft.pts });
    sel = zones.length - 1;
    draft = null;
    setDirty(true);
    render(); renderList(); updateHint();
    const inp = document.querySelector(`[data-name="${sel}"]`);
    if (inp) { inp.focus(); inp.select(); }
  }

  svg.addEventListener("pointerdown", (ev) => {
    if (!W) return;
    const p = toImg(ev);
    if (tab === "uniform") { addSample(p); return; }
    const t = ev.target;
    if (draft) {
      if (draft.pts.length >= 3 && dist(p, draft.pts[0]) < 12 * screenScale()) finishDraft();
      else { draft.pts.push(p); render(); updateHint(); }
      return;
    }
    if (t.dataset.v !== undefined && sel >= 0) {
      drag = { kind: "vertex", zi: sel, vi: Number(t.dataset.v) };
    } else if (t.dataset.z !== undefined) {
      sel = Number(t.dataset.z);
      drag = { kind: "move", zi: sel, last: p };
      renderList();
    } else {
      sel = -1;
      renderList();
    }
    svg.setPointerCapture(ev.pointerId);
    render(); updateHint();
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!W) return;
    const p = toImg(ev);
    if (draft) { draft.cursor = p; render(); return; }
    if (!drag) return;
    const z = zones[drag.zi];
    const clamp = (q) => [Math.min(W, Math.max(0, q[0])), Math.min(H, Math.max(0, q[1]))];
    if (drag.kind === "vertex") z.pts[drag.vi] = clamp(p);
    else {
      const dx = p[0] - drag.last[0], dy = p[1] - drag.last[1];
      z.pts = z.pts.map((q) => clamp([q[0] + dx, q[1] + dy]));
      drag.last = p;
    }
    setDirty(true);
    render();
  });
  svg.addEventListener("pointerup", () => { drag = null; });
  svg.addEventListener("dblclick", () => finishDraft());

  document.addEventListener("keydown", (ev) => {
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (ev.key === "Escape") { draft = null; render(); updateHint(); return; }
    if (typing) return;
    if (ev.key === "Enter") finishDraft();
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z" && draft) { draft.pts.pop(); render(); updateHint(); }
    if ((ev.key === "Delete" || ev.key === "Backspace") && sel >= 0 && !draft) removeZone(sel);
  });

  function removeZone(i) {
    zones.splice(i, 1);
    sel = -1;
    setDirty(true);
    render(); renderList(); updateHint();
  }

  $("zlist").addEventListener("input", (ev) => {
    const t = ev.target;
    if (t.dataset.name !== undefined) zones[Number(t.dataset.name)].name = t.value.trim() || zones[Number(t.dataset.name)].id;
    if (t.dataset.seats !== undefined) zones[Number(t.dataset.seats)].seats = Math.max(1, Math.min(30, Number(t.value) || 1));
    setDirty(true);
    render();
  });
  $("zlist").addEventListener("click", (ev) => {
    const del = ev.target.closest("[data-del]");
    if (del) { removeZone(Number(del.dataset.del)); return; }
    const row = ev.target.closest("[data-row]");
    if (row && !draft) { sel = Number(row.dataset.row); render(); updateHint(); document.querySelectorAll(".zrow").forEach((r, i) => r.classList.toggle("sel", i === sel)); }
  });
  $("add").onclick = startDraft;

  $("save").onclick = async () => {
    if (draft) { toast("Finish or cancel the zone you're drawing first", false); return; }
    const names = zones.map((z) => z.name.toLowerCase());
    if (new Set(names).size !== names.length) { toast("Two tables have the same name", false); return; }
    const body = {
      frame_size: [W, H],
      tables: zones.map((z) => ({
        id: z.id, name: z.name, seats: z.seats,
        polygon: z.pts.map(([x, y]) => [+(x / W).toFixed(4), +(y / H).toFixed(4)]),
      })),
    };
    const res = await fetch("/api/zones", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) { toast(`Save failed: ${(await res.json().catch(() => ({}))).detail || res.status}`, false); return; }
    const r = await res.json();
    setDirty(false);
    toast(r.applied_live ? `Saved ${r.tables} zones. Live tracking updated.` : `Saved ${r.tables} zones. They'll be used when mode: live.`);
  };

  $("load-live").onclick = () => loadImage("/api/snapshot", "Live frame not available");
  $("load-source").onclick = () => loadImage("/api/source_frame", "Could not read the video source");

  // ------------------------------------------------------------ uniform
  function rgb2hsv(r, g, b) { // OpenCV scale: H 0-179, S/V 0-255
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) {
      if (mx === r) h = ((g - b) / d) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
      if (h < 0) h += 360;
    }
    return [Math.round(h / 2) % 180, mx ? Math.round((d / mx) * 255) : 0, mx];
  }
  function addSample([x, y]) {
    if (!pixels) return;
    const r = 4, hsv = [];
    let sr = 0, sg = 0, sb = 0, n = 0;
    for (let yy = Math.max(0, Math.round(y) - r); yy <= Math.min(H - 1, Math.round(y) + r); yy++) {
      for (let xx = Math.max(0, Math.round(x) - r); xx <= Math.min(W - 1, Math.round(x) + r); xx++) {
        const i = (yy * W + xx) * 4;
        const R = pixels[i], G = pixels[i + 1], B = pixels[i + 2];
        sr += R; sg += G; sb += B; n++;
        hsv.push(rgb2hsv(R, G, B));
      }
    }
    samples.push({ rgb: [sr / n, sg / n, sb / n].map(Math.round), hsv });
    renderSamples();
  }
  function pct(arr, q) { const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(q * (s.length - 1)))]; }
  let neutral = false;
  function computeRange() {
    neutral = false;
    if (!samples.length) return null;
    const all = samples.flatMap((s) => s.hsv);
    // Black / white / grey uniforms have no meaningful hue: match on saturation + brightness.
    if (pct(all.map((v) => v[1]), 0.5) < 45) {
      neutral = true;
      const sHi = Math.min(255, pct(all.map((v) => v[1]), 0.9) + 35);
      const vLo = Math.max(0, pct(all.map((v) => v[2]), 0.1) - 40);
      const vHi = Math.min(255, pct(all.map((v) => v[2]), 0.9) + 40);
      return { lower: [0, 0, vLo], upper: [179, sHi, vHi] };
    }
    // circular mean of hue (OpenCV hue is degrees/2)
    let sx = 0, sy = 0;
    all.forEach(([h]) => { const a = (h * 2 * Math.PI) / 180; sx += Math.cos(a); sy += Math.sin(a); });
    const mean = Math.round((((Math.atan2(sy, sx) * 180) / Math.PI + 360) % 360) / 2) % 180;
    const tol = Number($("tol").value);
    const sMin = Math.max(30, pct(all.map((v) => v[1]), 0.1) - 40);
    const vMin = Math.max(25, pct(all.map((v) => v[2]), 0.1) - 50);
    return { lower: [(mean - tol + 180) % 180, sMin, vMin], upper: [(mean + tol) % 180, 255, 255] };
  }
  function inRange([h, s, v], rg) {
    const [l, u] = [rg.lower, rg.upper];
    const hueOk = l[0] <= u[0] ? h >= l[0] && h <= u[0] : h >= l[0] || h <= u[0];
    return hueOk && s >= l[1] && s <= u[1] && v >= l[2] && v <= u[2];
  }
  function currentRange() { return computeRange() || savedUniform; }
  function renderSamples() {
    $("samples").innerHTML = samples.length
      ? samples.map((s, i) => `<button class="swatch" style="background:rgb(${s.rgb.join(",")})" data-sample="${i}" title="Remove sample"></button>`).join("") +
        `<button class="btn-xs" id="clear-samples">Clear samples</button>`
      : `<span class="text-xs text-slate-500">No samples yet.</span>`;
    const rg = currentRange();
    $("range-text").textContent = rg ? `[${rg.lower.join(",")}] → [${rg.upper.join(",")}]` : "—";
    $("tol").disabled = neutral;
    $("tol-val").textContent = neutral ? "n/a (neutral colour)" : `±${$("tol").value}`;
    renderMask();
  }
  function renderMask() {
    const ctx = maskCv.getContext("2d");
    ctx.clearRect(0, 0, maskCv.width, maskCv.height);
    const rg = currentRange();
    if (!pixels || !rg || !$("show-mask").checked || tab !== "uniform") { $("match-pct").textContent = ""; return; }
    const out = ctx.createImageData(W, H);
    let hits = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (inRange(rgb2hsv(pixels[i], pixels[i + 1], pixels[i + 2]), rg)) {
        out.data[i] = 168; out.data[i + 1] = 85; out.data[i + 2] = 247; out.data[i + 3] = 170;
        hits++;
      }
    }
    ctx.putImageData(out, 0, 0);
    const p = (100 * hits) / (W * H);
    let msg = `${p.toFixed(1)}% of the image matches.`;
    if (neutral) msg += " Neutral colour (black, white or grey) detected, so matching is by brightness. Shadows and guests' dark clothes can match too; keep the method on Combined.";
    if (p > 25) msg += " That's a lot: sample again or lower the tolerance, otherwise guests in similar colours may be marked as staff.";
    $("match-pct").textContent = msg;
  }
  function showSavedUniform() {
    $("saved-uniform").textContent = savedUniform
      ? `Saved: [${savedUniform.lower.join(",")}] → [${savedUniform.upper.join(",")}]`
      : "No uniform colour saved yet, so staff detection uses posture and motion.";
  }
  $("samples").addEventListener("click", (ev) => {
    if (ev.target.id === "clear-samples") samples = [];
    else if (ev.target.dataset.sample !== undefined) samples.splice(Number(ev.target.dataset.sample), 1);
    else return;
    renderSamples();
  });
  $("tol").oninput = () => { $("tol-val").textContent = `±${$("tol").value}`; renderSamples(); };
  $("show-mask").onchange = renderMask;
  $("ratio").oninput = () => { $("ratio-val").textContent = `${$("ratio").value}%`; };

  async function postUniform(body, msg) {
    const res = await fetch("/api/uniform", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) { toast(`Save failed (${res.status})`, false); return null; }
    toast(msg);
    return res.json();
  }
  $("save-uniform").onclick = async () => {
    const rg = computeRange();
    const body = { method: $("method").value, min_ratio: Number($("ratio").value) / 100 };
    if (rg) Object.assign(body, rg);
    const r = await postUniform(body, rg ? "Uniform colour saved" : "Staff settings saved");
    if (r && r.uniform_hsv !== undefined) { savedUniform = r.uniform_hsv; samples = []; renderSamples(); }
    showSavedUniform();
  };
  $("clear-uniform").onclick = async () => {
    if (await postUniform({}, "Uniform colour cleared")) { savedUniform = null; samples = []; renderSamples(); showSavedUniform(); }
  };

  // ---------------------------------------------------------------- tabs
  function switchTab(t) {
    tab = t;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    $("tab-tables").classList.toggle("hidden", t !== "tables");
    $("tab-uniform").classList.toggle("hidden", t !== "uniform");
    if (t === "uniform") draft = null;
    render(); renderMask(); updateHint();
  }
  document.querySelectorAll(".tab-btn").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
  window.addEventListener("resize", render);

  renderSamples();
  init();
})();
