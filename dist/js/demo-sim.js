// TableSense in-browser simulation: a JavaScript port of backend/table_engine.py,
// backend/simulator.py and backend/queue_predictor.py, used by the static demo build.
// Keep the logic in step with the Python originals (the Python versions are the reference).
window.TSDemo = (() => {
  "use strict";

  // ------------------------------------------------------------------ config
  const CFG = {
    restaurant: "Spice Garden",
    engine: { seat_confirm_sec: 8, staff_serve_sec: 3, leave_confirm_sec: 20, staff_clean_sec: 5,
              debounce_window_sec: 3, majority_ratio: 0.5, waiter_alert_sec: 180, realert_sec: 120,
              escalate_sec: 300, clean_alert_sec: 120 },
    sim: { speed: 5, start: "19:00", warmup_min: 60, miss_prob: 0.04, ghost_prob: 0.005,
           resp: [1, 3], resp_slow: [3.2, 6], slow_prob: 0.15, dining: [35, 75],
           clean: [1, 2], clean_slow: [2, 6], clean_slow_prob: 0.3, waiters: 5 },
  };
  const rand = Math.random;
  const uniform = (a, b) => a + rand() * (b - a);
  const choice = (arr) => arr[Math.floor(rand() * arr.length)];

  // ================================================================== engine
  class Engine {
    constructor(defs, s, start, onAlert) {
      this.s = s;
      this.onAlert = onAlert;
      this.alerts = new Map();
      this.alertSeq = 0;
      this.last = start;
      this.tables = new Map();
      defs.forEach((d) => this.tables.set(d.id, {
        id: d.id, name: d.name, seats: d.seats, polygon: d.polygon || [], state: "FREE", state_since: start,
        guest_count: 0, last_staff_visit: null, session_start: null, served_at: null, samples: [],
        gp: false, sp: false, gps: null, gas: null, sps: null, peak: 0,
      }));
    }

    update(now, obs) {
      this.last = now;
      for (const t of this.tables.values()) {
        const o = obs[t.id] || { guests: 0, staff: 0 };
        this.debounce(t, now, o);
        this.transition(t, now);
        this.checkAlerts(t, now);
      }
    }

    debounce(t, now, o) {
      const s = this.s;
      t.samples.push([now, Math.max(0, o.guests), Math.max(0, o.staff)]);
      const cutoff = now - s.debounce_window_sec;
      while (t.samples.length > 1 && t.samples[0][0] < cutoff) t.samples.shift();
      const n = t.samples.length;
      const gp = t.samples.filter((x) => x[1] > 0).length / n > s.majority_ratio;
      const sp = t.samples.filter((x) => x[2] > 0).length / n > s.majority_ratio;
      const first = (pred) => (t.samples.find(pred) || [now])[0];
      if (gp) {
        const counts = t.samples.filter((x) => x[1] > 0).map((x) => x[1]).sort((a, b) => a - b);
        const m = counts.length;
        t.guest_count = Math.round(m % 2 ? counts[(m - 1) / 2] : (counts[m / 2 - 1] + counts[m / 2]) / 2);
        t.peak = Math.max(t.peak, t.guest_count);
        t.gas = null;
        if (t.gps === null) t.gps = first((x) => x[1] > 0);
      } else {
        t.guest_count = 0;
        t.gps = null;
        if (t.gas === null) t.gas = first((x) => x[1] === 0);
      }
      if (sp) {
        if (t.sps === null) t.sps = first((x) => x[2] > 0);
        t.last_staff_visit = now;
      } else t.sps = null;
      t.gp = gp; t.sp = sp;
    }

    transition(t, now) {
      const s = this.s, st = t.state;
      const gFor = t.gps !== null ? now - t.gps : 0;
      const aFor = t.gas !== null ? now - t.gas : 0;
      const sFor = t.sps !== null ? now - t.sps : 0;
      if (st === "FREE" || st === "NEEDS_CLEANING") {
        if (t.gp && gFor >= s.seat_confirm_sec) {
          const arrived = t.gps ?? now;
          t.session_start = arrived; t.served_at = null; t.peak = t.guest_count;
          this.setState(t, "SEATED_WAITING", now, arrived);
          return;
        }
        if (st === "NEEDS_CLEANING" && t.sp && sFor >= s.staff_clean_sec) { this.setState(t, "FREE", now); return; }
      }
      if (st === "SEATED_WAITING" && t.sp && sFor >= s.staff_serve_sec) {
        t.served_at = t.sps ?? now;
        this.setState(t, "SERVED", now, t.served_at);
        return;
      }
      if ((st === "SEATED_WAITING" || st === "SERVED") && !t.gp && aFor >= s.leave_confirm_sec) {
        this.setState(t, "NEEDS_CLEANING", now, t.gas ?? now);
        t.session_start = null;
      }
    }

    setState(t, state, now, since) {
      const old = t.state;
      t.state = state;
      t.state_since = since ?? now;
      if (old === "SEATED_WAITING") this.resolve(t.id, "waiter", now);
      if (old === "NEEDS_CLEANING") this.resolve(t.id, "cleaning", now);
    }

    checkAlerts(t, now) {
      const s = this.s, el = now - t.state_since;
      if (t.state === "SEATED_WAITING" && el >= s.waiter_alert_sec) {
        let a = this.active(t.id, "waiter");
        const sev = el >= s.escalate_sec ? "critical" : "warning";
        const msg = `Table ${t.name} waiting ${Math.floor(el / 60)} min with no waiter`;
        if (!a) { a = this.create(t, "waiter", sev, msg, now); this.onAlert(a, "new"); }
        else if (sev === "critical" && a.severity !== "critical") {
          Object.assign(a, { severity: sev, message: msg, last_notified_at: now, acknowledged_at: null });
          this.onAlert(a, "escalate");
        } else if (now - a.last_notified_at >= s.realert_sec) {
          a.repeat++;
          Object.assign(a, { message: msg, last_notified_at: now, acknowledged_at: null });
          this.onAlert(a, "renotify");
        }
      } else if (t.state === "NEEDS_CLEANING" && el >= s.clean_alert_sec && !this.active(t.id, "cleaning")) {
        this.onAlert(this.create(t, "cleaning", "warning", `Table ${t.name} needs cleaning`, now), "new");
      }
    }

    active(tid, type) { for (const a of this.alerts.values()) if (a.table_id === tid && a.type === type) return a; return null; }
    create(t, type, severity, message, now) {
      const a = { id: ++this.alertSeq, table_id: t.id, type, severity, message, created_at: now,
                  last_notified_at: now, acknowledged_at: null, resolved_at: null, repeat: 0 };
      this.alerts.set(a.id, a);
      return a;
    }
    resolve(tid, type, now) {
      for (const a of [...this.alerts.values()]) {
        if (a.table_id === tid && a.type === type) { a.resolved_at = now; this.alerts.delete(a.id); this.onAlert(a, "resolve"); }
      }
    }
    ack(id, now) { const a = this.alerts.get(id); if (!a) return false; a.acknowledged_at = now; this.onAlert(a, "ack"); return true; }
    markClean(tid, now) {
      const t = this.tables.get(tid);
      if (!t) return false;
      if (t.state !== "FREE") {
        this.setState(t, "FREE", now);
        t.samples = []; t.gp = t.sp = false; t.gps = t.sps = null; t.gas = now;
      }
      return true;
    }
    snapshot(now) {
      const tables = [...this.tables.values()].map((t) => ({
        id: t.id, name: t.name, seats: t.seats, polygon: t.polygon, state: t.state, state_since: t.state_since,
        elapsed_sec: Math.max(0, now - t.state_since), guest_count: t.guest_count, staff_present: t.sp,
        last_staff_visit: t.last_staff_visit, session_start: t.session_start,
        session_elapsed_sec: t.session_start ? now - t.session_start : null, served_at: t.served_at,
      }));
      const counts = { FREE: 0, SEATED_WAITING: 0, SERVED: 0, NEEDS_CLEANING: 0 };
      tables.forEach((t) => counts[t.state]++);
      const alerts = [...this.alerts.values()].map((a) => ({ ...a })).sort((a, b) => b.last_notified_at - a.last_notified_at);
      return { now, tables, alerts, counts };
    }
  }

  // =================================================================== queue
  class Queue {
    constructor() { this.entries = []; this.history = []; this.seq = 0; }
    add(name, size, now, source = "host", patience = null) {
      const e = { id: ++this.seq, name: String(name || "").trim().slice(0, 40) || "Guest",
                  size: Math.max(1, Math.min(20, Math.round(size) || 1)), added_at: now, source,
                  patience_sec: patience, note: "" };
      this.entries.push(e);
      return e;
    }
    remove(id, now, reason = "removed", tableId = null) {
      const i = this.entries.findIndex((e) => e.id === id);
      if (i < 0) return null;
      const [e] = this.entries.splice(i, 1);
      this.history.push({ id: e.id, name: e.name, size: e.size, reason, table_id: tableId, waited_sec: now - e.added_at, at: now });
      if (this.history.length > 50) this.history.shift();
      return e;
    }
    expire(now) {
      const gone = this.entries.filter((e) => e.patience_sec !== null && now - e.added_at >= e.patience_sec);
      gone.forEach((e) => this.remove(e.id, now, "walked_away"));
      return gone.length;
    }
  }

  // =============================================================== predictor
  let STATS = { by_seats_hour: {}, by_seats: {}, cleaning_sec: null };
  const avgSession = (seats, hour) => STATS.by_seats_hour[`${seats}|${hour}`] || STATS.by_seats[seats] || 3000;
  const avgCleaning = () => STATS.cleaning_sec || 180;

  function timeToFree(t, now) {
    if (t.state === "FREE") return 0;
    if (t.state === "NEEDS_CLEANING") return Math.max(30, avgCleaning() - t.elapsed_sec);
    const start = t.session_start || t.state_since || now;
    const rem = avgSession(t.seats, new Date(start * 1000).getHours()) - (now - start);
    return Math.max(120, rem) + avgCleaning();
  }

  function predict(tables, queue, now) {
    const etas = Object.fromEntries(tables.map((t) => [t.id, timeToFree(t, now)]));
    const predictions = tables.filter((t) => t.state !== "FREE")
      .map((t) => ({ table_id: t.id, name: t.name, seats: t.seats, state: t.state,
                     eta_sec: Math.round(etas[t.id]), eta_at: now + etas[t.id] }))
      .sort((a, b) => a.eta_sec - b.eta_sec || a.seats - b.seats);
    const avail = { ...etas };
    const byId = Object.fromEntries(tables.map((t) => [t.id, t]));
    const hour = new Date(now * 1000).getHours();
    const view = queue.map((e) => {
      const row = { ...e, waited_sec: now - e.added_at };
      const fits = tables.filter((t) => t.seats >= e.size).map((t) => t.id);
      if (!fits.length) return { ...row, suggested_table: null, suggested_name: null, est_wait_sec: null, note: "No table large enough — combine tables" };
      const earliest = Math.min(...fits.map((id) => avail[id]));
      const near = fits.filter((id) => avail[id] <= earliest + 300);
      const best = near.sort((a, b) => byId[a].seats - byId[b].seats || avail[a] - avail[b])[0];
      const wait = avail[best];
      avail[best] = wait + avgSession(byId[best].seats, hour) + avgCleaning();
      return { ...row, suggested_table: best, suggested_name: byId[best].name, est_wait_sec: Math.round(wait) };
    });
    return [predictions, view];
  }

  // =============================================================== simulator
  const FLOOR_W = 1120, FLOOR_H = 680, HEADER_H = 44, ENTRANCE_X = 90, STATION_X = 1035, WAITER_SPEED = 140;
  const PRIORITY = { serve: 0, check: 1, clean: 2 };
  const SIZES = [1, 2, 3, 4, 5, 6], WEIGHTS = [8, 40, 14, 26, 6, 6];
  const SURNAMES = ["Sharma", "Iyer", "Khan", "Reddy", "Patel", "Das", "Mehta", "Nair", "Singh", "Gupta", "Rao", "Kapoor", "Joshi", "Menon", "Bose", "Fernandes", "Verma", "Pillai"];

  function bump(h, a, b, ramp = 0.75) {
    if (h < a - ramp || h > b + ramp) return 0;
    if (h < a) return 0.5 - 0.5 * Math.cos((Math.PI * (h - (a - ramp))) / ramp);
    if (h > b) return 0.5 + 0.5 * Math.cos((Math.PI * (h - b)) / ramp);
    return 1;
  }
  const arrivalRate = (h) => (h < 11 || h > 23.5 ? 0 : 2 + 15 * bump(h, 12.25, 14.75) + 21 * bump(h, 19.25, 22) + 3 * bump(h, 17.5, 18.75, 0.5));
  function weighted(vals, w) {
    let r = rand() * w.reduce((a, b) => a + b, 0);
    for (let i = 0; i < vals.length; i++) { r -= w[i]; if (r <= 0) return vals[i]; }
    return vals[vals.length - 1];
  }

  function buildLayout() {
    const out = [], x0 = 200, x1 = 900, y0 = HEADER_H + 62, y1 = FLOOR_H - 72;
    const cw = (x1 - x0) / 3, rh = (y1 - y0) / 3, seatsByRow = [2, 4, 4, 6];
    const size = { 2: [64, 64], 4: [84, 84], 6: [132, 76] };
    let n = 1;
    for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) {
      const seats = seatsByRow[r], [w, h] = size[seats];
      out.push({ id: `T${n}`, name: `T${n}`, seats, cx: x0 + c * cw, cy: y0 + r * rh, w, h, phase: "clean", party: null });
      n++;
    }
    return out;
  }
  const seatPositions = (t) => {
    const rx = t.w / 2 + 13, ry = t.h / 2 + 13, out = [];
    for (let i = 0; i < t.seats; i++) {
      const a = Math.PI + (2 * Math.PI * i) / t.seats + (t.seats === 4 ? Math.PI / t.seats : 0);
      out.push([t.cx + rx * Math.cos(a), t.cy + ry * Math.sin(a)]);
    }
    return out;
  };

  class Simulator {
    constructor(queue) {
      const s = CFG.sim;
      this.queue = queue;
      this.speed = s.speed;
      const [hh, mm] = s.start.split(":").map(Number);
      const d = new Date(); d.setHours(hh, mm, 0, 0);
      this.time = d.getTime() / 1000 - s.warmup_min * 60;
      this.tables = buildLayout();
      this.byId = Object.fromEntries(this.tables.map((t) => [t.id, t]));
      this.waiters = Array.from({ length: s.waiters }, (_, i) => {
        const home = [STATION_X, HEADER_H + 120 + i * 44];
        return { id: i + 1, home, pos: home, state: "idle", task: null, src: home, dst: home, dep: 0, arr: 0, busy: 0 };
      });
      this.tasks = [];
      this.stats = { arrivals: 0, seated: 0, walkaways: 0 };
      this.engine = null;
    }
    layout() {
      return this.tables.map((t) => ({ id: t.id, name: t.name, seats: t.seats,
        polygon: [[t.cx - t.w / 2, t.cy - t.h / 2], [t.cx + t.w / 2, t.cy - t.h / 2], [t.cx + t.w / 2, t.cy + t.h / 2], [t.cx - t.w / 2, t.cy + t.h / 2]] }));
    }
    minutes([a, b]) { return uniform(a, b) * 60; }
    delay(n, sl, p) { return this.minutes(rand() < p ? sl : n); }
    hour() { const d = new Date(this.time * 1000); return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600; }
    advance(dt) {
      let rem = dt;
      while (rem > 1e-9) {
        const step = Math.min(1, rem);
        rem -= step;
        this.time += step;
        this.step(step);
        if (this.engine) this.engine.update(this.time, this.observe());
      }
    }
    addTask(t) { this.tasks.push(t); this.tasks.sort((a, b) => a.due - b.due); }
    step(dt) {
      const now = this.time, s = CFG.sim;
      if (rand() < (arrivalRate(this.hour()) / 3600) * dt) {
        this.queue.add(choice(SURNAMES), weighted(SIZES, WEIGHTS), now, "sim", uniform(15, 45) * 60);
        this.stats.arrivals++;
      }
      this.stats.walkaways += this.queue.expire(now);
      for (const e of [...this.queue.entries]) {
        const fits = this.tables.filter((t) => t.phase === "clean" && t.seats >= e.size)
          .sort((a, b) => a.seats - b.seats || Number(a.id.slice(1)) - Number(b.id.slice(1)));
        if (!fits.length) continue;
        const t = fits[0];
        this.queue.remove(e.id, now, "seated", t.id);
        t.party = { id: e.id, size: e.size, seated: now, leave: null };
        t.phase = "occupied";
        this.stats.seated++;
        this.addTask({ table: t.id, kind: "serve", due: now + this.delay(s.resp, s.resp_slow, s.slow_prob), dur: uniform(40, 90), party: e.id });
      }
      for (const t of this.tables) {
        if (t.party && t.party.leave !== null && now >= t.party.leave) {
          t.party = null; t.phase = "dirty";
          this.tasks = this.tasks.filter((k) => k.table !== t.id);
          this.addTask({ table: t.id, kind: "clean", due: now + this.delay(s.clean, s.clean_slow, s.clean_slow_prob), dur: uniform(45, 120) });
        }
      }
      for (const w of this.waiters) {
        if (w.state === "walking" || w.state === "returning") {
          const f = Math.min(1, (now - w.dep) / Math.max(1e-6, w.arr - w.dep));
          w.pos = [w.src[0] + (w.dst[0] - w.src[0]) * f, w.src[1] + (w.dst[1] - w.src[1]) * f];
          if (f >= 1) { if (w.state === "walking" && w.task) { w.state = "at_table"; w.busy = now + w.task.dur; } else w.state = "idle"; }
        } else if (w.state === "at_table" && now >= w.busy) {
          this.complete(w, now);
          this.send(w, w.home, now, "returning");
        }
        if (w.state === "idle" || w.state === "returning") {
          const due = this.tasks.filter((k) => k.due <= now);
          if (due.length) {
            const task = due.sort((a, b) => PRIORITY[a.kind] - PRIORITY[b.kind] || a.due - b.due)[0];
            this.tasks.splice(this.tasks.indexOf(task), 1);
            w.task = task;
            const t = this.byId[task.table];
            this.send(w, [t.cx + t.w / 2 + 22, t.cy], now, "walking");
          }
        }
      }
    }
    send(w, dst, now, state) {
      const d = Math.hypot(dst[0] - w.pos[0], dst[1] - w.pos[1]);
      Object.assign(w, { src: w.pos, dst, state, dep: now, arr: now + d / WAITER_SPEED });
    }
    complete(w, now) {
      const task = w.task; w.task = null;
      if (!task) return;
      const t = this.byId[task.table];
      if (task.kind === "serve" && t.party && t.party.id === task.party) {
        t.party.leave = now + this.minutes(CFG.sim.dining) * (0.9 + 0.04 * t.party.size);
        const n = 1 + Math.floor(rand() * 3);
        for (let i = 0; i < n; i++) this.addTask({ table: t.id, kind: "check", due: now + uniform(0.2, 0.95) * (t.party.leave - now), dur: uniform(15, 40), party: t.party.id });
      } else if (task.kind === "clean" && t.phase === "dirty") t.phase = "clean";
    }
    observe() {
      const obs = {};
      for (const t of this.tables) {
        let guests = 0;
        if (t.party) for (let i = 0; i < t.party.size; i++) if (rand() > CFG.sim.miss_prob) guests++;
        if (!t.party && rand() < CFG.sim.ghost_prob) guests = 1;
        const staff = this.waiters.filter((w) => w.state === "at_table" && w.task && w.task.table === t.id).length;
        obs[t.id] = { guests, staff };
      }
      return obs;
    }
    manualClean(tid) {
      const t = this.byId[tid];
      if (t && t.phase === "dirty") { t.phase = "clean"; this.tasks = this.tasks.filter((k) => !(k.table === tid && k.kind === "clean")); }
    }

    // ------------------------------------------------------------ rendering
    render(ctx, snap) {
      const COL = { FREE: "34,197,94", SEATED_WAITING: "239,68,68", SERVED: "249,115,22", NEEDS_CLEANING: "234,179,8" };
      const text = (s, x, y, size, color, weight = 500) => { ctx.font = `${weight} ${size}px Inter, system-ui, sans-serif`; ctx.fillStyle = color; ctx.fillText(s, x, y); };
      ctx.fillStyle = "rgb(15,23,32)"; ctx.fillRect(0, 0, FLOOR_W, FLOOR_H);
      ctx.strokeStyle = "rgb(22,30,40)"; ctx.lineWidth = 1;
      for (let x = 0; x < FLOOR_W; x += 40) { ctx.beginPath(); ctx.moveTo(x + 0.5, HEADER_H); ctx.lineTo(x + 0.5, FLOOR_H); ctx.stroke(); }
      for (let y = HEADER_H; y < FLOOR_H; y += 40) { ctx.beginPath(); ctx.moveTo(0, y + 0.5); ctx.lineTo(FLOOR_W, y + 0.5); ctx.stroke(); }
      ctx.fillStyle = "rgb(28,38,52)";
      ctx.fillRect(0, HEADER_H, ENTRANCE_X + 30, FLOOR_H - HEADER_H);
      ctx.fillRect(STATION_X - 45, HEADER_H, FLOOR_W - STATION_X + 45, FLOOR_H - HEADER_H);
      text("ENTRANCE", 14, HEADER_H + 26, 12, "#94a3b8", 600);
      text("SERVICE", STATION_X - 32, HEADER_H + 26, 12, "#94a3b8", 600);
      ctx.fillStyle = "rgb(45,55,70)"; ctx.fillRect(STATION_X - 30, FLOOR_H - 120, FLOOR_W - 15 - STATION_X + 30, 100);
      text("KITCHEN", STATION_X - 18, FLOOR_H - 64, 12, "#cbd5e1", 600);

      const byId = Object.fromEntries(snap.tables.map((t) => [t.id, t]));
      const alertBy = Object.fromEntries(snap.alerts.map((a) => [a.table_id, a]));
      const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 160);
      for (const t of this.tables) {
        const info = byId[t.id] || { state: "FREE", elapsed_sec: 0 };
        const c = COL[info.state];
        const x = t.cx - t.w / 2, y = t.cy - t.h / 2;
        ctx.fillStyle = `rgba(${c},.35)`; ctx.fillRect(x, y, t.w, t.h);
        const a = alertBy[t.id];
        ctx.lineWidth = a && a.severity === "critical" ? 2 + 3 * pulse : 2;
        ctx.strokeStyle = `rgb(${c})`; ctx.strokeRect(x, y, t.w, t.h);
        const seats = seatPositions(t);
        ctx.lineWidth = 1; ctx.strokeStyle = "#475569";
        seats.forEach(([sx, sy]) => { ctx.beginPath(); ctx.arc(sx, sy, 9, 0, 7); ctx.stroke(); });
        if (t.party) { ctx.fillStyle = "#3b82f6"; seats.slice(0, t.party.size).forEach(([sx, sy]) => { ctx.beginPath(); ctx.arc(sx, sy, 8, 0, 7); ctx.fill(); }); }
        const el = Math.floor(info.elapsed_sec);
        ctx.textAlign = "center";
        text(t.name, t.cx, t.cy - 4, 15, "#fff", 700);
        text(`${String(Math.floor(el / 60)).padStart(2, "0")}:${String(el % 60).padStart(2, "0")}`, t.cx, t.cy + 15, 13, "#e2e8f0", 500);
        ctx.textAlign = "left";
        if (a) { ctx.fillStyle = a.type === "waiter" ? "#ff0000" : "#ffd700"; ctx.beginPath(); ctx.arc(x + t.w - 6, y + 6, 7, 0, 7); ctx.fill(); }
      }
      for (const w of this.waiters) {
        ctx.fillStyle = "#a855f7"; ctx.beginPath(); ctx.arc(w.pos[0], w.pos[1], 10, 0, 7); ctx.fill();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(w.pos[0], w.pos[1], 12, 0, 7); ctx.stroke();
        ctx.textAlign = "center"; text("W", w.pos[0], w.pos[1] + 4, 11, "#fff", 700); ctx.textAlign = "left";
      }
      const q = this.queue.entries;
      q.slice(0, 12).forEach((e, i) => {
        const x = 30 + (i % 2) * 40, y = HEADER_H + 70 + Math.floor(i / 2) * 44;
        ctx.fillStyle = "#3b82f6"; ctx.beginPath(); ctx.arc(x, y, 13, 0, 7); ctx.fill();
        ctx.textAlign = "center"; text(String(e.size), x, y + 4, 12, "#fff", 700); ctx.textAlign = "left";
      });
      if (q.length > 12) text(`+${q.length - 12}`, 30, HEADER_H + 70 + 6 * 44 + 4, 13, "#e2e8f0");
      text(`Queue: ${q.length}`, 14, FLOOR_H - 18, 13, "#e2e8f0");

      ctx.fillStyle = "rgb(10,15,23)"; ctx.fillRect(0, 0, FLOOR_W, HEADER_H);
      const d = new Date(this.time * 1000);
      text("SIMULATION", 14, 29, 17, "#eab308", 700);
      text(`${d.toLocaleDateString("en-GB", { weekday: "short" })} ${d.toTimeString().slice(0, 8)}   speed ${this.speed}x`, 160, 29, 15, "#e2e8f0");
      let lx = 560;
      [["Free", "FREE"], ["Waiting", "SEATED_WAITING"], ["Served", "SERVED"], ["Cleaning", "NEEDS_CLEANING"]].forEach(([l, k]) => {
        ctx.fillStyle = `rgb(${COL[k]})`; ctx.beginPath(); ctx.arc(lx, 23, 7, 0, 7); ctx.fill();
        text(l, lx + 12, 28, 13, "#cbd5e1"); lx += 105;
      });
      ctx.fillStyle = "#3b82f6"; ctx.beginPath(); ctx.arc(lx, 23, 7, 0, 7); ctx.fill(); text("Guest", lx + 12, 28, 13, "#cbd5e1");
      ctx.fillStyle = "#a855f7"; ctx.beginPath(); ctx.arc(lx + 85, 23, 7, 0, 7); ctx.fill(); text("Staff", lx + 97, 28, 13, "#cbd5e1");
    }
  }

  // =================================================================== app
  const notifications = [];
  let notifySeq = 0;
  const queue = new Queue();
  const sim = new Simulator(queue);
  const engine = new Engine(sim.layout(), CFG.engine, sim.time, (a, kind) => {
    notifications.push({ seq: ++notifySeq, kind, alert: { ...a } });
    if (notifications.length > 100) notifications.shift();
  });
  sim.engine = engine;
  sim.advance(CFG.sim.warmup_min * 60);

  let last = performance.now();
  setInterval(() => {
    const nowm = performance.now();
    const dt = Math.min(5, (nowm - last) / 1000); // cap catch-up after a background tab
    last = nowm;
    sim.advance(dt * sim.speed);
  }, 100);

  function state(sinceSeq = 0) {
    const now = sim.time;
    const snap = engine.snapshot(now);
    const [predictions, queueView] = predict(snap.tables, queue.entries, now);
    return {
      ...snap,
      mode: "simulation", restaurant: CFG.restaurant, server_time: Date.now() / 1000,
      notify_seq: notifySeq,
      notifications: notifications.filter((n) => n.seq > sinceSeq),
      sim: { speed: sim.speed, clock: now, queue_length: queue.entries.length, stats: { ...sim.stats } },
      live: null, notice: null,
      queue: queueView, predictions, queue_history: queue.history.slice(-15),
      thresholds: { waiter_alert_sec: CFG.engine.waiter_alert_sec, clean_alert_sec: CFG.engine.clean_alert_sec, escalate_sec: CFG.engine.escalate_sec },
    };
  }

  return {
    state,
    get notifySeq() { return notifySeq; },
    setStats(s) { if (s) STATS = s; },
    setSpeed(x) { sim.speed = Math.min(120, Math.max(0.25, Number(x) || 1)); return sim.speed; },
    ack: (id) => engine.ack(id, sim.time),
    markClean: (tid) => { const ok = engine.markClean(tid, sim.time); if (ok) sim.manualClean(tid); return ok; },
    addParty: (name, size) => queue.add(name, size, sim.time, "host"),
    removeParty: (id, reason, tid) => queue.remove(id, sim.time, reason, tid),
    render: (ctx) => sim.render(ctx, engine.snapshot(sim.time)),
    FLOOR_W, FLOOR_H,
  };
})();
