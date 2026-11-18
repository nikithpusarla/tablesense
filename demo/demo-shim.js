// Static demo shim: lets the unchanged dashboard/analytics scripts run without a server.
// * WebSocket('/ws')  -> pushes TSDemo.state() every second
// * fetch('/api/...') -> routed to the in-browser simulator or bundled JSON (data/*.json)
// * <canvas id="feed"> -> animated floor plan (replaces the server's MJPEG stream)
(() => {
  "use strict";
  const realFetch = window.fetch.bind(window);
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  const notFound = () => json({ detail: "Not available in the demo" }, 404);

  // Historical averages for the predictor (precomputed by tools/build_static.py).
  realFetch("data/stats.json").then((r) => (r.ok ? r.json() : null)).then((s) => window.TSDemo?.setStats(s)).catch(() => {});

  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const path = url.pathname.replace(/^.*?(\/api\/|\/static\/)/, "$1");
    const method = (init.method || "GET").toUpperCase();
    if (path.startsWith("/static/")) return new Response("", { status: 404 });
    if (!path.startsWith("/api/")) return realFetch(input, init);
    const body = init.body ? JSON.parse(init.body) : {};
    const D = window.TSDemo;
    let m;
    if (path === "/api/state") return json(D.state(D.notifySeq));
    if (path === "/api/sim/speed" && method === "POST") return json({ ok: true, speed: D.setSpeed(body.speed) });
    if ((m = path.match(/^\/api\/alerts\/(\d+)\/ack$/))) return D.ack(Number(m[1])) ? json({ ok: true }) : notFound();
    if ((m = path.match(/^\/api\/tables\/([^/]+)\/mark_clean$/))) return D.markClean(decodeURIComponent(m[1])) ? json({ ok: true }) : notFound();
    if (path === "/api/queue" && method === "POST") {
      if (!(body.size >= 1 && body.size <= 20)) return json({ detail: "size must be 1-20" }, 422);
      return json({ ok: true, id: D.addParty(body.name, body.size).id });
    }
    if ((m = path.match(/^\/api\/queue\/(\d+)$/)) && method === "DELETE") {
      const ok = D.removeParty(Number(m[1]), url.searchParams.get("reason") || "removed", url.searchParams.get("table_id"));
      return ok ? json({ ok: true }) : notFound();
    }
    if (path === "/api/analytics/weekly") {
      const w = [1, 2, 4].includes(Number(url.searchParams.get("weeks"))) ? Number(url.searchParams.get("weeks")) : 1;
      return realFetch(`data/analytics_${w}.json`);
    }
    return notFound();
  };

  // Fake WebSocket for /ws: one full state push per second, like the server.
  const RealWS = window.WebSocket;
  window.WebSocket = class DemoSocket {
    constructor(url) {
      if (!String(url).endsWith("/ws")) return new RealWS(url);
      this.readyState = 0;
      this.seq = window.TSDemo.notifySeq;
      setTimeout(() => {
        this.readyState = 1;
        this.onopen && this.onopen({});
        const push = () => {
          const s = window.TSDemo.state(this.seq);
          this.seq = s.notify_seq;
          this.onmessage && this.onmessage({ data: JSON.stringify(s) });
        };
        push();
        this.timer = setInterval(push, 1000);
      }, 50);
    }
    send() {}
    close() { clearInterval(this.timer); this.readyState = 3; }
  };

  document.addEventListener("DOMContentLoaded", () => {
    // Floor-plan canvas in place of the MJPEG <img>.
    const feed = document.getElementById("feed");
    if (feed && feed.tagName === "CANVAS" && window.TSDemo) {
      const ctx = feed.getContext("2d");
      let lastDraw = 0;
      const loop = (t) => {
        if (t - lastDraw > 90) { window.TSDemo.render(ctx); lastDraw = t; }
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    }

    // Analytics CSV export: build from the bundled JSON instead of the server.
    const daily = document.getElementById("csv-daily");
    if (daily) {
      daily.addEventListener("click", async (e) => {
        e.preventDefault();
        const w = new URL(daily.href, location.href).searchParams.get("weeks") || "1";
        const d = (await (await realFetch(`data/analytics_${w}.json`)).json()).daily;
        const rows = [["date", "covers", "avg_turn_min", "avg_waiter_response_min", "avg_cleaning_delay_min", "waiter_alerts", "cleaning_alerts"]];
        d.dates.forEach((day, i) => rows.push([day, d.covers[i], d.turn_min[i] ?? "", d.response_min[i] ?? "", d.cleaning_min[i] ?? "", d.alerts_waiter[i], d.alerts_cleaning[i]]));
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([rows.map((r) => r.join(",")).join("\n")], { type: "text/csv" }));
        a.download = `tablesense_demo_daily_${w}w.csv`;
        a.click();
      }, true);
    }
    const raw = document.getElementById("csv-events");
    if (raw) raw.remove();
  });
})();
