// TableSense analytics page: fetches /api/analytics/weekly and renders tiles, cards and charts.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const C = {
    s1: css("--series-1"), s2: css("--series-2"),
    text: css("--text-secondary"), muted: css("--text-muted"), primary: css("--text-primary"),
    grid: css("--grid"), surface: "#111a2e",
  };
  // Sequential (blue) ramp for the dark surface: near-zero recedes toward the surface.
  const HEAT = ["#152238", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 1) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d }));
  const money = (cur, v) => `${cur}${Math.round(v).toLocaleString("en-IN")}`;

  let weeks = 1;
  try { weeks = Number(localStorage.getItem("tablesense.analytics.weeks")) || 1; } catch (_) {}
  let data = null;
  const charts = {};

  // ---------------------------------------------------------- Chart.js base
  Chart.defaults.color = C.text;
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.font.size = 11;
  Chart.defaults.borderColor = C.grid;
  Chart.defaults.animation.duration = 300;

  const tooltip = {
    backgroundColor: "#1b2540", borderColor: "rgba(255,255,255,.1)", borderWidth: 1,
    titleColor: C.primary, bodyColor: C.primary, padding: 8, cornerRadius: 8,
    boxWidth: 8, boxHeight: 8, usePointStyle: true,
  };
  const legend = { display: true, position: "top", align: "end",
    labels: { usePointStyle: true, pointStyle: "rectRounded", boxWidth: 8, boxHeight: 8, color: C.primary, padding: 12 } };
  const scales = (yTitle, extra = {}) => ({
    x: { grid: { display: false }, border: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 8 } },
    y: { beginAtZero: true, grid: { color: C.grid }, border: { display: false },
         title: { display: !!yTitle, text: yTitle, color: C.muted }, ...extra },
  });

  // Crosshair for line charts.
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const a = chart.tooltip?.getActiveElements?.();
      if (!a || !a.length || chart.config.type !== "line") return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.strokeStyle = "rgba(226,232,240,.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(a[0].element.x, chartArea.top);
      ctx.lineTo(a[0].element.x, chartArea.bottom);
      ctx.stroke();
      ctx.restore();
    },
  };
  // Dashed "TableSense went live" marker on daily charts.
  const goLive = {
    id: "goLive",
    afterDraw(chart) {
      const idx = chart.options.plugins.goLive?.index;
      if (idx == null || idx < 0) return;
      const x = chart.scales.x.getPixelForValue(idx) - (chart.scales.x.width / chart.data.labels.length) / 2;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(226,232,240,.4)";
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.text;
      ctx.font = "500 10.5px Inter, sans-serif";
      ctx.fillText("TableSense live →", x + 4, chartArea.top + 10);
      ctx.restore();
    },
  };
  // Direct labels at the end of each line (≤4 series).
  const endLabels = {
    id: "endLabels",
    afterDatasetsDraw(chart) {
      if (chart.config.type !== "line") return;
      const { ctx } = chart;
      ctx.save();
      ctx.font = "600 11px Inter, sans-serif";
      ctx.fillStyle = C.primary;
      const labels = [];
      chart.data.datasets.forEach((ds, i) => {
        const meta = chart.getDatasetMeta(i);
        if (meta.hidden) return;
        for (let j = ds.data.length - 1; j >= 0; j--) {
          if (ds.data[j] != null) {
            const p = meta.data[j];
            labels.push({ text: ds.short || ds.label, x: p.x + 8, y: p.y + 4 });
            break;
          }
        }
      });
      // De-collide: keep at least 13px between labels when lines converge.
      labels.sort((a, b) => a.y - b.y);
      for (let i = 1; i < labels.length; i++) {
        if (labels[i].y - labels[i - 1].y < 13) labels[i].y = labels[i - 1].y + 13;
      }
      labels.forEach((l) => ctx.fillText(l.text, l.x, l.y));
      ctx.restore();
    },
  };
  // Dashed average reference line on the utilisation chart.
  const avgLine = {
    id: "avgLine",
    afterDatasetsDraw(chart) {
      const v = chart.options.plugins.avgLine?.value;
      if (v == null) return;
      const y = chart.scales.y.getPixelForValue(v);
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = "rgba(226,232,240,.55)";
      ctx.beginPath();
      ctx.moveTo(chartArea.left, y);
      ctx.lineTo(chartArea.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.primary;
      ctx.font = "600 11px Inter, sans-serif";
      const label = `avg ${v}%`;
      ctx.fillText(label, chartArea.right - ctx.measureText(label).width, y - 5);
      ctx.restore();
    },
  };
  Chart.register(crosshair, goLive, endLabels, avgLine);

  const bar = (label, values, color, extra = {}) => ({
    label, data: values, backgroundColor: color, hoverBackgroundColor: color,
    borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: false, maxBarThickness: 28, ...extra,
  });

  function makeOrUpdate(id, config) {
    if (charts[id]) {
      charts[id].data = config.data;
      Object.assign(charts[id].options.plugins, config.options.plugins || {});
      charts[id].update();
      return;
    }
    charts[id] = new Chart($(id), config);
  }

  // ------------------------------------------------------------- rendering
  function renderKpis(k) {
    const tiles = [
      ["Guests served", num(k.covers, 0), "", `${num(k.covers_per_day, 0)}/day · ${num(k.parties, 0)} parties`],
      ["Avg table turn", num(k.avg_turn_min), "min", "seating → table ready"],
      ["Waiter response", num(k.avg_response_min), "min", "seated → first visit"],
      ["Cleaning delay", num(k.avg_cleaning_min), "min", "guests leave → cleaned"],
      ["Utilisation", num(k.utilisation_pct), "%", "of opening hours"],
      ["Alerts", num(k.alerts, 0), "", `${num(k.waiter_alerts, 0)} waiter · ${num(k.cleaning_alerts, 0)} cleaning`],
      ["Walk-aways", num(k.walkaways, 0), "parties", `${num(k.walkaway_covers, 0)} guests lost to waiting`],
    ];
    $("kpis").innerHTML = tiles.map(([l, v, u, s]) =>
      `<div class="kpi"><div class="k-label">${l}</div><div class="k-value">${v}<span class="k-unit">${u}</span></div><div class="k-sub">${s}</div></div>`).join("");
  }

  function renderRevenue(r) {
    if (!r || !r.available) {
      $("revenue").innerHTML = `<p class="text-sm text-slate-400">Not enough history yet: the before/after comparison needs about two weeks of data on each side of go-live.</p>`;
      $("rev-note").textContent = "";
      return;
    }
    const d = new Date(r.go_live * 1000);
    $("rev-note").textContent = `vs. the 2 weeks before go-live (${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })})`;
    const rows = [
      ["Avg table turn", "turn_min", "min", true],
      ["Waiter response", "response_min", "min", true],
      ["Cleaning delay", "cleaning_min", "min", true],
      ["Waiter alerts / week", "waiter_alerts_per_week", "", true],
    ];
    const tr = rows.map(([label, key, unit, lowerIsBetter]) => {
      const b = r.before[key], a = r.after[key];
      const diff = a - b;
      const pct = b ? Math.round((diff / b) * 100) : 0;
      const good = lowerIsBetter ? diff < 0 : diff > 0;
      const arrow = diff < 0 ? "↓" : diff > 0 ? "↑" : "";
      return `<tr><td>${label}</td><td class="text-slate-400">${num(b)} ${unit}</td><td>${num(a)} ${unit}</td>
        <td class="${good ? "delta-good" : "delta-bad"}">${arrow} ${Math.abs(pct)}%</td></tr>`;
    }).join("");
    $("revenue").innerHTML = `
      <div class="grid sm:grid-cols-2 gap-4 items-center">
        <div>
          <div class="text-[11px] text-slate-400 font-medium">Extra revenue from faster table turns</div>
          <div class="hero mt-1">+${money(r.currency, r.extra_revenue_per_week)}<span class="text-base font-medium text-slate-400"> / week</span></div>
          <div class="text-sm text-slate-300 mt-1">≈ <b>${money(r.currency, r.extra_revenue_per_month)}</b> per month</div>
          <p class="text-xs text-slate-400 mt-3 leading-relaxed">Tables now turn <b class="text-slate-200">${num(r.turn_saved_min)} min faster</b>, which frees room for
          <b class="text-slate-200">+${num(r.extra_covers_per_week, 0)} extra guests a week</b> in peak hours, at an average bill of ${money(r.currency, r.avg_bill)}.</p>
        </div>
        <table class="cmp">
          <thead><tr><th></th><th>Before</th><th>After</th><th>Change</th></tr></thead>
          <tbody>${tr}</tbody>
        </table>
      </div>`;
  }

  function renderStaffing(s) {
    $("staff-note").textContent = `${s.tables_per_waiter} tables per waiter`;
    const cell = (n, peak) => `<div class="staff-cell"><div class="n">${n}<span class="text-xs font-medium text-slate-400"> waiter${n === 1 ? "" : "s"}</span></div>
      <div class="waiter-dots">${"<i></i>".repeat(n)}</div><div class="text-[10.5px] text-slate-500 mt-1">peak ≈ ${num(peak)} tables busy</div></div>`;
    $("staffing").innerHTML = `<div class="staff-grid">
      <div class="h">Shift</div><div class="h">Mon–Thu</div><div class="h">Fri–Sun</div>
      ${s.shifts.map((r) => `<div class="staff-cell"><div class="font-semibold text-sm">${esc(r.shift)}</div><div class="text-[11px] text-slate-500">${esc(r.hours)}</div></div>
        ${cell(r.weekday_waiters, r.weekday_peak_tables)}${cell(r.weekend_waiters, r.weekend_peak_tables)}`).join("")}
    </div>
    <p class="text-[11px] text-slate-500 mt-3">Waiters = ⌈typical peak occupied tables ÷ ${s.tables_per_waiter}⌉, from the busiest 15-minute periods of each shift.</p>`;
  }

  function renderHeatmap(h) {
    const max = Math.max(1, ...h.values.flat());
    const bucket = (v) => (v <= 0 ? 0 : 1 + Math.min(4, Math.floor((v / max) * 5)));
    let html = `<div></div>${h.hours.map((hr) => `<div class="hhead">${String(hr).padStart(2, "0")}</div>`).join("")}`;
    h.days.forEach((day, i) => {
      html += `<div class="hlabel">${day}</div>`;
      h.hours.forEach((hr, j) => {
        const v = h.values[i][j];
        html += `<div class="cell" style="background:${HEAT[bucket(v)]}" data-tip="${day} ${String(hr).padStart(2, "0")}:00 · <b>${num(v)}</b> guests"></div>`;
      });
    });
    $("heatmap").innerHTML = html;
    $("heat-legend").innerHTML = `<span>0</span><span class="heat-scale">${HEAT.map((c) => `<i style="background:${c}"></i>`).join("")}</span><span>${num(max, 0)} guests</span>`;
  }

  function renderCharts(d) {
    const daily = d.daily;
    const goIdx = d.revenue?.available ? daily.dates.indexOf(new Date(d.revenue.go_live * 1000).toLocaleDateString("en-CA")) : -1;
    const hourLabels = d.peak_hours.hours.map((h) => `${String(h).padStart(2, "0")}:00`);

    makeOrUpdate("peak", {
      type: "bar",
      data: { labels: hourLabels, datasets: [bar("Avg guests", d.peak_hours.avg_covers, C.s1)] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip }, scales: scales("guests / hour") },
    });
    makeOrUpdate("turn", {
      type: "bar",
      data: { labels: daily.labels, datasets: [bar("Avg turn (min)", daily.turn_min, C.s1)] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip, goLive: { index: goIdx } }, scales: scales("minutes") },
    });
    makeOrUpdate("service", {
      type: "line",
      data: {
        labels: daily.labels,
        datasets: [
          { label: "Waiter response", short: "Waiter", data: daily.response_min, borderColor: C.s1, backgroundColor: C.s1, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderColor: C.surface, pointHoverBorderWidth: 2, spanGaps: true, tension: 0.25 },
          { label: "Cleaning delay", short: "Cleaning", data: daily.cleaning_min, borderColor: C.s2, backgroundColor: C.s2, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderColor: C.surface, pointHoverBorderWidth: 2, spanGaps: true, tension: 0.25 },
        ],
      },
      options: {
        maintainAspectRatio: false, layout: { padding: { right: 56 } },
        interaction: { mode: "index", intersect: false },
        plugins: { legend, tooltip: { ...tooltip, callbacks: { label: (c) => ` ${c.dataset.label}: ${num(c.parsed.y)} min` } }, goLive: { index: goIdx } },
        scales: scales("minutes"),
      },
    });
    makeOrUpdate("alerts", {
      type: "bar",
      data: {
        labels: daily.labels,
        datasets: [
          // 2px surface-coloured top border = visual gap between stacked segments
          bar("Waiter needed", daily.alerts_waiter, C.s1, { borderRadius: 0, borderColor: C.surface, borderWidth: { top: 2 } }),
          bar("Needs cleaning", daily.alerts_cleaning, C.s2, { borderColor: C.surface, borderWidth: { top: 0 } }),
        ],
      },
      options: {
        maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend, tooltip, goLive: { index: goIdx } },
        scales: { ...scales("alerts"), x: { ...scales().x, stacked: true }, y: { ...scales("alerts").y, stacked: true } },
      },
    });
    const ut = d.utilisation.tables;
    makeOrUpdate("util", {
      type: "bar",
      data: { labels: ut.map((t) => `${t.name} · ${t.seats}`), datasets: [bar("Utilisation %", ut.map((t) => t.pct), C.s1)] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { ...tooltip, callbacks: { label: (c) => ` ${num(c.parsed.y)}% occupied` } }, avgLine: { value: d.utilisation.overall } },
        scales: scales("% of open hours", { max: 100, ticks: { callback: (v) => `${v}%` } }),
      },
    });
  }

  function render(d) {
    data = d;
    $("error").classList.add("hidden");
    const s = new Date(d.range.start * 1000), e = new Date((d.range.end - 1) * 1000);
    const f = (x) => x.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    $("range").textContent = `${f(s)} – ${f(e)} · ${d.range.days} days`;
    renderKpis(d.kpis);
    renderRevenue(d.revenue);
    renderStaffing(d.staffing);
    renderHeatmap(d.heatmap);
    renderCharts(d);
    refreshOpenTables();
  }

  // -------------------------------------------------------- table views
  function tableFor(id) {
    const ch = charts[id];
    if (!ch) return "";
    const ds = ch.data.datasets;
    const head = `<tr><th></th>${ds.map((x) => `<th>${esc(x.label)}</th>`).join("")}</tr>`;
    const body = ch.data.labels.map((l, i) => `<tr><td>${esc(l)}</td>${ds.map((x) => `<td>${num(x.data[i])}</td>`).join("")}</tr>`).join("");
    return `<div class="table-wrap"><table class="data-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  }
  function refreshOpenTables() {
    document.querySelectorAll("[data-table-for].on").forEach((b) => {
      const box = b.closest(".chart-card").querySelector(".tbl-view");
      if (box) box.innerHTML = tableFor(b.dataset.tableFor);
    });
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-table-for]");
    if (!b) return;
    const card = b.closest(".chart-card");
    const box = card.querySelector(".chart-box");
    let tv = card.querySelector(".tbl-view");
    if (!tv) { tv = document.createElement("div"); tv.className = "tbl-view hidden"; card.appendChild(tv); }
    const on = !b.classList.contains("on");
    b.classList.toggle("on", on);
    b.textContent = on ? "Chart" : "Table";
    box.classList.toggle("hidden", on);
    tv.classList.toggle("hidden", !on);
    if (on) tv.innerHTML = tableFor(b.dataset.tableFor);
  });

  // Heatmap tooltip
  const tip = $("tip");
  document.addEventListener("mousemove", (e) => {
    const c = e.target.closest?.("[data-tip]");
    if (!c) { tip.classList.add("hidden"); return; }
    tip.innerHTML = c.dataset.tip;
    tip.classList.remove("hidden");
    const x = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 12);
    tip.style.left = `${x}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });

  // ----------------------------------------------------------------- data
  function setWeeks(w) {
    weeks = w;
    try { localStorage.setItem("tablesense.analytics.weeks", String(w)); } catch (_) {}
    document.querySelectorAll("#weeks button").forEach((b) => b.classList.toggle("on", Number(b.dataset.weeks) === w));
    $("csv-daily").href = `/api/analytics/export.csv?weeks=${w}&kind=daily`;
    $("csv-events").href = `/api/analytics/export.csv?weeks=${w}&kind=events`;
    load();
  }
  async function load() {
    try {
      const res = await fetch(`/api/analytics/weekly?weeks=${weeks}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      render(await res.json());
    } catch (err) {
      $("error").textContent = `Could not load analytics (${err.message}). Is the server running?`;
      $("error").classList.remove("hidden");
    }
  }
  $("weeks").addEventListener("click", (e) => {
    const b = e.target.closest("[data-weeks]");
    if (b) setWeeks(Number(b.dataset.weeks));
  });
  fetch("/api/state").then((r) => r.json()).then((s) => { $("restaurant").textContent = s.restaurant; }).catch(() => {});
  const urlWeeks = Number(new URLSearchParams(location.search).get("weeks"));
  if (urlWeeks) weeks = urlWeeks;
  setWeeks([1, 2, 4].includes(weeks) ? weeks : 1);
  setInterval(load, 60000);
})();
