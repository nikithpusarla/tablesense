"""Build a static, simulation-only demo of TableSense into ``dist/``.

The output is plain HTML/JS/CSS that runs entirely in the browser (no Python server):
the simulator, table engine and queue predictor are ported to JS (``demo/demo-sim.js``)
and a small shim (``demo/demo-shim.js``) answers the dashboard's /ws and /api calls.
Analytics use four weeks of seeded history precomputed here into JSON.

Usage (from the project root)::

    .venv\\Scripts\\python tools\\build_static.py
    # then deploy the dist/ folder to Netlify / Vercel / GitHub Pages, or preview:
    .venv\\Scripts\\python -m http.server -d dist 8080
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import analytics  # noqa: E402
from backend.config import load_config  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.queue_predictor import HistoryStats  # noqa: E402
from backend.simulator import build_layout  # noqa: E402

log = logging.getLogger("build_static")
DIST = ROOT / "dist"
STATIC = ROOT / "static"
DEMO = ROOT / "demo"

BANNER = (
    '<div style="background:#1e293b;color:#cbd5e1;font:500 12px Inter,system-ui,sans-serif;'
    'text-align:center;padding:6px 12px;border-bottom:1px solid rgba(255,255,255,.06)">'
    "🧪 Live demo: a simulated 16-table restaurant running entirely in your browser. "
    "No camera and no server. The installed version connects to your existing CCTV.</div>"
)
DEMO_SCRIPTS = '<script src="./js/demo-sim.js"></script>\n  <script src="./js/demo-shim.js"></script>\n  '

SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>TableSense — Setup</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" />
<script src="https://cdn.tailwindcss.com/3.4.16"></script>
<link rel="stylesheet" href="./css/app.css" /></head>
<body class="bg-[#0b1120] text-slate-200 antialiased min-h-screen font-[Inter]">
__BANNER__
<main class="max-w-xl mx-auto px-5 py-16 space-y-5">
  <h1 class="text-2xl font-semibold">Camera setup</h1>
  <p class="text-slate-300">In the installed version, this page shows a snapshot from your CCTV camera. You click around
  each table to draw its zone and click on a waiter's shirt to teach TableSense your uniform colour.</p>
  <p class="text-slate-400 text-sm">It needs a connected camera or video, so it isn't part of this browser-only demo.</p>
  <div class="flex gap-3"><a class="btn-primary" href="./index.html">← Back to the live floor</a>
  <a class="btn-ghost" href="./analytics.html">View analytics</a></div>
</main></body></html>
"""


def transform(html: str) -> str:
    html = html.replace('href="/static/', 'href="./').replace('src="/static/', 'src="./')
    html = (html.replace('href="/"', 'href="./index.html"')
                .replace('href="/analytics"', 'href="./analytics.html"')
                .replace('href="/setup"', 'href="./setup.html"'))
    html = re.sub(r'<img id="feed"[^>]*>',
                  '<canvas id="feed" width="1120" height="680" class="w-full h-auto block" '
                  'aria-label="Simulated restaurant floor plan"></canvas>', html)
    html = re.sub(r"(<body[^>]*>)", r"\1\n  " + BANNER.replace("\\", "\\\\"), html, count=1)
    first_script = re.search(r'<script src="\./js/(sound|analytics)\.js"></script>', html)
    if first_script:
        html = html[:first_script.start()] + DEMO_SCRIPTS + html[first_script.start():]
    return html


def build_data(out: Path) -> None:
    """Seed four weeks of history into a temp DB and export analytics + predictor stats."""
    cfg = load_config()
    tables = [{"id": t.id, "name": t.name, "seats": t.seats} for t in build_layout()]
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "demo.db")
        analytics.seed_demo_history(db, tables, cfg, weeks=4)
        for weeks in (1, 2, 4):
            rep = analytics.weekly_report(db, tables, cfg, weeks=weeks)
            (out / f"analytics_{weeks}.json").write_text(json.dumps(rep), encoding="utf-8")
        hs = HistoryStats(db)
        hs._refresh()
        stats = {
            "by_seats_hour": {f"{s}|{h}": round(v) for (s, h), v in hs._by_seats_hour.items()},
            "by_seats": {str(s): round(v) for s, v in hs._by_seats.items()},
            "cleaning_sec": round(hs.avg_cleaning_sec()),
        }
        (out / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "js").mkdir(parents=True)
    (DIST / "data").mkdir()
    shutil.copytree(STATIC / "css", DIST / "css")
    for name in ("dashboard.js", "sound.js", "analytics.js"):
        shutil.copy2(STATIC / "js" / name, DIST / "js" / name)
    for name in ("demo-sim.js", "demo-shim.js"):
        shutil.copy2(DEMO / name, DIST / "js" / name)
    for page in ("index.html", "analytics.html"):
        html = transform((STATIC / page).read_text(encoding="utf-8"))
        (DIST / page).write_text(html, encoding="utf-8")
    (DIST / "setup.html").write_text(SETUP_PAGE.replace("__BANNER__", BANNER), encoding="utf-8")
    build_data(DIST / "data")
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    log.info("Static demo built in %s (%d files, %.0f KB)", DIST, len(list(DIST.rglob("*.*"))), size / 1024)
    log.info("Preview:  .venv\\Scripts\\python -m http.server -d dist 8080   ->  http://localhost:8080")


if __name__ == "__main__":
    main()
