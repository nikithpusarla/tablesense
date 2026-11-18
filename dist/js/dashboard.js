// TableSense dashboard: receives full state over WebSocket every second and renders it.
// Timers are interpolated locally between pushes (taking the sim speed into account)
// so they tick smoothly.
(() => {
  "use strict";

  const LABEL = { FREE: "Free", SEATED_WAITING: "Waiting", SERVED: "Served", NEEDS_CLEANING: "Cleaning" };
  const SPEEDS = [1, 5, 20];
  const $ = (id) => document.getElementById(id);

  let S = null;            // latest state from server
  let recvAt = 0;          // performance.now() when S arrived
  let rate = 1;            // engine-seconds per real second (sim speed)
  let selected = null;     // table id shown in the modal
  let filter = "all";
  const seenHistory = new Set();
  const activity = [];

  // ------------------------------------------------------------- helpers
  const pad = (n) => String(n).padStart(2, "0");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const drift = () => ((performance.now() - recvAt) / 1000) * rate;
  const mmss = (sec) => {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h}:${pad(m)}:${pad(sec % 60)}` : `${pad(m)}:${pad(sec % 60)}`;
  };
  const hhmm = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const mins = (sec) => {
    if (sec == null) return "—";
    if (sec <= 0) return "Now";
    if (sec < 60) return "<1 min";
    const m = Math.round(sec / 60);
    return m >= 60 ? `${Math.floor(m / 60)}h ${pad(m % 60)}m` : `${m} min`;
  };
  const ago = (sec) => (sec < 60 ? `${Math.floor(sec)}s` : `${Math.floor(sec / 60)}m`);

  async function api(method, url, body) {
    try {
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) toast("⚠️", `Request failed (${res.status})`, "");
      return res.ok;
    } catch (e) {
      toast("⚠️", "Server unreachable", "");
      return false;
    }
  }

  // --------------------------------------------------------------- header
  function renderHeader(s) {
    $("restaurant").textContent = s.restaurant;
    document.title = `${s.alerts.length ? `(${s.alerts.length}) ` : ""}${s.restaurant} — TableSense`;
    const badge = $("mode-badge");
    if (s.mode === "simulation") {
      badge.textContent = "SIMULATION";
      badge.className = "badge bg-amber-400/15 text-amber-300 border border-amber-400/30";
      $("feed-title").textContent = "Simulated floor plan";
    } else {
      badge.innerHTML = '<span class="inline-block w-1.5 h-1.5 rounded-full bg-red-500 mr-1 align-middle animate-pulse"></span>LIVE';
      badge.className = "badge bg-red-500/15 text-red-300 border border-red-500/30";
      $("feed-title").textContent = "Live camera";
    }
    const lv = s.live;
    if (lv) $("feed").classList.remove("aspect-[1120/680]"); // camera frames have their own shape
    $("feed-status").textContent = lv
      ? `${lv.source} · ${lv.status === "streaming" ? `AI ${lv.ai_fps} fps · ${lv.people} people (${lv.staff} staff)` : lv.status}`
      : "";
    const notice = s.notice || (lv && lv.error) || "";
    $("notice").textContent = notice;
    $("notice").classList.toggle("hidden", !notice);
    const c = s.counts;
    const chips = [
      ["Free", c.FREE, "FREE"],
      ["Occupied", c.SEATED_WAITING + c.SERVED, "SERVED"],
      ["Waiting", c.SEATED_WAITING, "SEATED_WAITING"],
      ["Cleaning", c.NEEDS_CLEANING, "NEEDS_CLEANING"],
    ];
    $("counts").innerHTML = chips.map(([l, n, st]) =>
      `<span class="state-${st} chip !text-[12px] !px-2 !py-1"><b class="text-sm tabular-nums">${n}</b><span class="text-slate-300 font-medium">${l}</span></span>`).join("");

    const ctl = $("speed-ctl");
    if (s.sim) {
      ctl.classList.remove("hidden");
      if (!ctl.children.length) {
        SPEEDS.forEach((sp) => {
          const b = document.createElement("button");
          b.dataset.speed = sp;
          b.textContent = `${sp}x`;
          b.onclick = () => api("POST", "/api/sim/speed", { speed: sp });
          ctl.appendChild(b);
        });
      }
      ctl.querySelectorAll("button").forEach((b) => b.classList.toggle("on", Number(b.dataset.speed) === s.sim.speed));
    } else {
      ctl.classList.add("hidden");
    }
  }

  function tickClock() {
    if (!S) return;
    const d = new Date((S.now + drift()) * 1000);
    $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    $("clock-sub").textContent = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) +
      (S.sim ? ` · sim ${S.sim.speed}x` : "");
  }

  // --------------------------------------------------------------- tables
  function progress(t) {
    const th = S.thresholds;
    if (t.state === "SEATED_WAITING") return Math.min(1, t.elapsed_sec / th.waiter_alert_sec);
    if (t.state === "NEEDS_CLEANING") return Math.min(1, t.elapsed_sec / th.clean_alert_sec);
    return 0;
  }

  function subline(t) {
    switch (t.state) {
      case "FREE": return "ready";
      case "SEATED_WAITING": return "no waiter yet";
      case "SERVED": return `session ${mins(t.session_elapsed_sec)}`;
      case "NEEDS_CLEANING": return "needs cleaning";
      default: return "";
    }
  }

  function renderTables(s) {
    if (!s.tables.length) {
      $("tables").innerHTML = `<p class="col-span-full text-sm text-slate-400 py-3">No tables yet. Open <a class="text-emerald-400 underline" href="/setup">Setup</a> and draw a zone around each table on the camera image.</p>`;
      return;
    }
    const alertBy = new Map(s.alerts.map((a) => [a.table_id, a]));
    $("tables").innerHTML = s.tables.map((t) => {
      const a = alertBy.get(t.id);
      const pulse = a ? (a.severity === "critical" ? "pulse-strong" : (a.type === "waiter" && !a.acknowledged_at ? "pulse" : "")) : "";
      const dim = filter !== "all" && t.state !== filter ? "dim" : "";
      const flag = a ? (a.type === "waiter" ? "🔔" : "🧹") : t.staff_present ? "🧑‍🍳" : "";
      return `<button data-table="${esc(t.id)}" class="tcard state-${t.state} ${pulse} ${dim}">
        <span class="flag">${flag}</span>
        <div class="flex items-baseline gap-1.5"><span class="name">${esc(t.name)}</span>
          <span class="meta">${t.guest_count ? `${t.guest_count}/` : ""}${t.seats} seats</span></div>
        <div class="timer" data-t0="${t.elapsed_sec}">${mmss(t.elapsed_sec)}</div>
        <div class="label">${LABEL[t.state]}</div>
        <div class="meta truncate">${subline(t)}</div>
        <span class="bar" style="width:${(progress(t) * 100).toFixed(1)}%"></span>
      </button>`;
    }).join("");
  }

  // --------------------------------------------------------------- alerts
  function renderAlerts(s) {
    $("alert-count").textContent = s.alerts.length;
    $("alert-count").className = `pill ml-1 ${s.alerts.length ? "!bg-red-500/20 !text-red-300" : ""}`;
    $("ack-all").classList.toggle("hidden", !s.alerts.some((a) => !a.acknowledged_at));
    if (!s.alerts.length) {
      $("alerts").innerHTML = `<li class="flex items-center gap-2 text-sm text-slate-400 py-1"><span class="text-emerald-400">✓</span>All tables on track</li>`;
      return;
    }
    const tables = new Map(s.tables.map((t) => [t.id, t]));
    $("alerts").innerHTML = s.alerts.map((a) => {
      const t = tables.get(a.table_id);
      const crit = a.severity === "critical";
      const cls = a.type === "cleaning" ? "alert-cleaning" : crit ? "alert-critical pulse-strong" : `alert-waiter ${a.acknowledged_at ? "" : "pulse"}`;
      const icon = a.type === "cleaning" ? "🧹" : crit ? "🚨" : "🔔";
      const title = a.type === "cleaning"
        ? `${esc(t?.name || a.table_id)} needs cleaning`
        : `${esc(t?.name || a.table_id)} waiting for a waiter`;
      return `<li class="alert ${cls} ${a.acknowledged_at ? "acked" : ""}">
        <div class="icon">${icon}</div>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-semibold truncate">${title}</div>
          <div class="text-[11px] text-slate-400">
            <span class="font-mono" data-t0="${t ? t.elapsed_sec : 0}">${mmss(t ? t.elapsed_sec : 0)}</span>
            ${crit ? ' · <b class="text-red-300">ESCALATED</b>' : ""}${a.repeat ? ` · reminder ×${a.repeat}` : ""}${a.acknowledged_at ? " · acknowledged" : ""}
          </div>
        </div>
        ${a.acknowledged_at ? "" : `<button data-ack="${a.id}" class="btn-xs shrink-0">Acknowledge</button>`}
      </li>`;
    }).join("");
  }

  // ---------------------------------------------------------------- queue
  function renderQueue(s) {
    $("queue-count").textContent = `${s.queue.length} waiting`;
    if (!s.queue.length) {
      $("queue").innerHTML = `<li class="text-sm text-slate-500 py-2">No one waiting. Add a walk-in above.</li>`;
      return;
    }
    const states = new Map(s.tables.map((t) => [t.id, t.state]));
    $("queue").innerHTML = s.queue.map((q, i) => {
      const st = states.get(q.suggested_table) || "FREE";
      const eta = q.est_wait_sec;
      const etaCls = eta === 0 ? "text-emerald-400" : eta != null && eta > 1200 ? "text-red-300" : "text-slate-100";
      return `<li class="row">
        <span class="text-[11px] text-slate-500 w-4 text-right">${i + 1}</span>
        <span class="size-badge" title="Party size">${q.size}</span>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium truncate">${esc(q.name)}${q.source === "sim" ? ' <span class="text-[10px] text-slate-500 font-normal">walk-in</span>' : ""}</div>
          <div class="text-[11px] text-slate-500">waiting ${ago(q.waited_sec)}${q.note ? ` · ${esc(q.note)}` : ""}</div>
        </div>
        <div class="text-right">
          ${q.suggested_table ? `<span class="state-${st} chip chip-dot">${esc(q.suggested_name)}</span>` : `<span class="text-[10px] text-amber-300">combine tables</span>`}
          <div class="eta ${etaCls}">${eta == null ? "—" : eta === 0 ? "Now" : "~" + mins(eta)}</div>
        </div>
        <div class="flex flex-col gap-1">
          <button class="btn-xs !px-2" title="Mark seated" data-seat="${q.id}" data-table-id="${esc(q.suggested_table || "")}">✓</button>
          <button class="btn-xs !px-2" title="Remove" data-remove="${q.id}">✕</button>
        </div>
      </li>`;
    }).join("");
  }

  function renderNext(s) {
    const list = s.predictions.slice(0, 8);
    if (!list.length) {
      $("next").innerHTML = `<li class="text-sm text-slate-500 py-2">Every table is free.</li>`;
      return;
    }
    const max = Math.max(...list.map((p) => p.eta_sec), 1);
    $("next").innerHTML = list.map((p) => `
      <li class="row state-${p.state}" data-table="${esc(p.table_id)}" role="button">
        <span class="chip chip-dot w-12 justify-center">${esc(p.name)}</span>
        <div class="flex-1 min-w-0">
          <div class="flex justify-between text-[11px] text-slate-400"><span>${p.seats} seats · ${LABEL[p.state]}</span><span>~${hhmm(p.eta_at)}</span></div>
          <div class="mini-bar mt-1"><i style="width:${(100 - (p.eta_sec / max) * 100).toFixed(0)}%"></i></div>
        </div>
        <span class="eta w-16 text-right">${mins(p.eta_sec)}</span>
      </li>`).join("");
  }

  // -------------------------------------------------------------- activity
  function pushActivity(at, icon, text) {
    activity.unshift({ at, icon, text });
    activity.splice(60);
  }

  function handleNotifications(s) {
    const tables = new Map(s.tables.map((t) => [t.id, t]));
    for (const n of s.notifications || []) {
      const a = n.alert;
      const name = tables.get(a.table_id)?.name || a.table_id;
      if (n.kind === "ack") continue;
      if (n.kind === "resolve") {
        pushActivity(s.now, "✅", a.type === "waiter" ? `${name}: waiter arrived` : `${name}: cleaned`);
        continue;
      }
      const sound = n.kind === "escalate" ? "critical" : a.type;
      const icon = a.type === "cleaning" ? "🧹" : n.kind === "escalate" ? "🚨" : "🔔";
      const prefix = n.kind === "renotify" ? "Reminder: " : n.kind === "escalate" ? "Escalated: " : "";
      window.TSAlerts.play(sound);
      window.TSAlerts.notify(`${prefix}${a.message}`, s.restaurant, `ts-${a.id}`);
      toast(icon, `${prefix}${a.message}`, a.type === "cleaning" ? "border-yellow-500/40" : "border-red-500/50");
      pushActivity(s.now, icon, `${prefix}${a.message}`);
    }
    for (const h of s.queue_history || []) {
      const key = `${h.id}-${h.reason}`;
      if (seenHistory.has(key)) continue;
      seenHistory.add(key);
      const tname = tables.get(h.table_id)?.name || h.table_id;
      const waited = h.waited_sec >= 60 ? ` after ${mins(h.waited_sec)} wait` : "";
      if (h.reason === "seated") pushActivity(h.at, "🪑", `${h.name} (${h.size}) seated${tname ? ` at ${tname}` : ""}${waited}`);
      else if (h.reason === "walked_away") pushActivity(h.at, "🚶", `${h.name} (${h.size}) left the queue after ${mins(h.waited_sec)}`);
    }
  }

  function renderActivity() {
    $("activity").innerHTML = activity.length
      ? activity.map((e) => `<li class="flex gap-2"><span class="font-mono text-slate-500">${hhmm(e.at)}</span><span>${e.icon}</span><span class="text-slate-300">${esc(e.text)}</span></li>`).join("")
      : `<li class="text-slate-500">Activity will appear here.</li>`;
  }

  // ----------------------------------------------------------------- modal
  function renderModal() {
    if (!selected || !S) return;
    const t = S.tables.find((x) => x.id === selected);
    if (!t) return;
    const alert = S.alerts.find((a) => a.table_id === t.id);
    const pred = S.predictions.find((p) => p.table_id === t.id);
    $("m-title").innerHTML = `${esc(t.name)} <span class="text-sm font-normal text-slate-400">· ${t.seats} seats</span>`;
    $("m-state").innerHTML = `<span class="state-${t.state} chip chip-dot">${LABEL[t.state]}</span>`;
    $("m-timer").className = `state-${t.state} font-mono text-4xl font-bold mt-3 tabular-nums`;
    $("m-timer").style.color = "rgb(var(--c))";
    $("m-timer").dataset.t0 = t.elapsed_sec;
    $("m-timer").textContent = mmss(t.elapsed_sec);
    const rows = [
      ["Guests", t.guest_count ? `${t.guest_count}` : "—"],
      ["Session", t.session_elapsed_sec != null ? mmss(t.session_elapsed_sec) : "—"],
      ["Seated at", t.session_start ? hhmm(t.session_start) : "—"],
      ["Waiter arrived", t.served_at ? hhmm(t.served_at) : "—"],
      ["Last staff visit", t.last_staff_visit ? hhmm(t.last_staff_visit) : "—"],
      ["Predicted free", pred ? `${mins(pred.eta_sec)} (~${hhmm(pred.eta_at)})` : "Now"],
    ];
    $("m-body").innerHTML = rows.map(([k, v]) => `<dt class="text-slate-400">${k}</dt><dd class="text-right font-medium">${esc(v)}</dd>`).join("");
    const ack = $("m-ack");
    ack.classList.toggle("hidden", !alert || !!alert.acknowledged_at);
    ack.dataset.ack = alert ? alert.id : "";
    $("m-clean").disabled = t.state === "FREE";
    $("m-clean").classList.toggle("opacity-40", t.state === "FREE");
    $("m-clean").textContent = t.state === "NEEDS_CLEANING" ? "Mark clean" : "Reset to free";
  }

  // ---------------------------------------------------------------- toasts
  function toast(icon, text, border) {
    const el = document.createElement("div");
    el.className = `toast ${border}`;
    el.innerHTML = `<span class="text-lg leading-none">${icon}</span><div class="flex-1">${esc(text)}</div>`;
    $("toasts").prepend(el);
    while ($("toasts").children.length > 4) $("toasts").lastChild.remove();
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 6000);
  }

  // ------------------------------------------------------------ render all
  function render(s) {
    S = s;
    recvAt = performance.now();
    rate = s.sim ? s.sim.speed : 1;
    renderHeader(s);
    handleNotifications(s);
    renderTables(s);
    renderAlerts(s);
    renderQueue(s);
    renderNext(s);
    renderActivity();
    renderModal();
    tickClock();
  }

  // Smoothly advance every visible timer between server pushes.
  setInterval(() => {
    if (!S) return;
    const d = drift();
    document.querySelectorAll("[data-t0]").forEach((el) => { el.textContent = mmss(Number(el.dataset.t0) + d); });
    tickClock();
  }, 250);

  // ---------------------------------------------------------------- events
  document.addEventListener("click", async (e) => {
    window.TSAlerts.unlock();
    const ackBtn = e.target.closest("[data-ack]");
    if (ackBtn && ackBtn.dataset.ack) {
      ackBtn.disabled = true;
      await api("POST", `/api/alerts/${ackBtn.dataset.ack}/ack`);
      return;
    }
    const seat = e.target.closest("[data-seat]");
    if (seat) {
      const tid = seat.dataset.tableId ? `&table_id=${encodeURIComponent(seat.dataset.tableId)}` : "";
      await api("DELETE", `/api/queue/${seat.dataset.seat}?reason=seated${tid}`);
      return;
    }
    const rm = e.target.closest("[data-remove]");
    if (rm) {
      await api("DELETE", `/api/queue/${rm.dataset.remove}?reason=removed`);
      return;
    }
    const card = e.target.closest("[data-table]");
    if (card) {
      selected = card.dataset.table;
      $("modal").classList.replace("hidden", "flex");
      renderModal();
    }
  });

  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    filter = b.dataset.filter;
    $("filters").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    if (S) renderTables(S);
  });

  $("ack-all").onclick = async () => {
    if (!S) return;
    await Promise.all(S.alerts.filter((a) => !a.acknowledged_at).map((a) => api("POST", `/api/alerts/${a.id}/ack`)));
  };

  const closeModal = () => { selected = null; $("modal").classList.replace("flex", "hidden"); };
  $("m-close").onclick = closeModal;
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  $("m-clean").onclick = async () => { if (selected) await api("POST", `/api/tables/${encodeURIComponent(selected)}/mark_clean`); };

  // Host queue form
  const sizeInput = $("q-size");
  document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
    sizeInput.value = Math.min(20, Math.max(1, (Number(sizeInput.value) || 1) + Number(b.dataset.step)));
  }));
  $("queue-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("q-name").value.trim() || "Guest";
    const size = Math.min(20, Math.max(1, Number(sizeInput.value) || 1));
    if (await api("POST", "/api/queue", { name, size })) {
      $("q-name").value = "";
      sizeInput.value = 2;
      toast("➕", `${name} (${size}) added to queue`, "border-emerald-500/40");
    }
  });

  // Sound / notification toggle
  function syncSoundBtn() {
    const on = window.TSAlerts.enabled;
    $("sound-icon").textContent = on ? "🔔" : "🔕";
    $("sound-label").textContent = on ? "Alerts on" : "Enable alerts";
    $("sound-btn").classList.toggle("armed", on);
  }
  $("sound-btn").onclick = async () => {
    if (window.TSAlerts.enabled) window.TSAlerts.disable(); else await window.TSAlerts.enable();
    syncSoundBtn();
  };
  syncSoundBtn();

  // ------------------------------------------------------------- websocket
  let retry = 0;
  function setConn(ok, text) {
    $("conn").innerHTML = `<i class="w-1.5 h-1.5 rounded-full ${ok ? "bg-emerald-400" : "bg-amber-400 animate-pulse"}"></i>${text}`;
    $("conn").className = `flex items-center gap-1.5 text-xs ${ok ? "text-emerald-400" : "text-amber-300"}`;
  }
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { retry = 0; setConn(true, "connected"); };
    ws.onmessage = (ev) => {
      try { render(JSON.parse(ev.data)); } catch (err) { console.error("render failed", err); }
    };
    ws.onclose = () => {
      setConn(false, "reconnecting");
      setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
    };
  }
  connect();

  // Video feed: auto-recover if the MJPEG stream drops.
  const feed = $("feed");
  feed.onerror = () => {
    $("feed-offline").classList.remove("hidden");
    setTimeout(() => (feed.src = `/video_feed?t=${Date.now()}`), 2000);
  };
  feed.onload = () => $("feed-offline").classList.add("hidden");
})();
