# TableSense

Smart table management from the CCTV a restaurant already has. TableSense watches the
dining floor with a lightweight AI model (YOLO11n + ByteTrack, CPU only) and turns it into a
live table board: who's waiting for a waiter, which tables need cleaning, which will free up
next, and what it all means for revenue.

![Live floor dashboard](docs/dashboard.png)

| | |
|---|---|
| **Live floor** | Every table coloured by state, with timers: 🟢 free → 🔴 waiting for a waiter → 🟠 served → 🟡 needs cleaning |
| **Alerts** | "Table T4 waiting 3 min with no waiter" with a sound, a browser notification, reminders every 2 min, escalation after 5 min, and optional WhatsApp |
| **Host queue** | Add walk-ins; get a suggested table and an estimated wait for each party, plus a "freeing up next" list |
| **Analytics** | Footfall heatmap, peak hours, turn times, waiter response, cleaning delay, utilisation, staffing recommendations, revenue impact, CSV export |
| **Simulation mode** | A full 16-table restaurant with no camera at all, at 1x, 5x or 20x speed, for demos |
| **Privacy** | No video or faces are stored, only table events. Optional face blur on the displayed feed |

---

## 1. Quick start (Windows)

1. Install **Python 3.11–3.13** from [python.org](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
   (3.13 is recommended; PyTorch wheels for 3.14 can lag behind.)
2. Double-click **`run.bat`**. The first run creates `.venv\` and installs everything, including CPU-only PyTorch (about 200 MB). That takes 3–10 minutes.
3. Open **http://localhost:8000**. You'll see the restaurant in **simulation mode** at 19:00, already busy.

The port comes from `server.port` in `config\config.yaml` (default 8000). Port busy? Change it there, or for one
session open a terminal in this folder and run:

```bat
set PORT=8077
run.bat
```

Other commands: `run.bat test` runs the tests, and `run.bat demo` builds the browser-only demo (see §10).

**Phones:** waiters can open `http://<your-PC-IP>:8000` on the same Wi-Fi. Find the IP with `ipconfig`.
Allow Python through Windows Firewall when prompted.

---

## 2. Simulation vs live

Edit `config\config.yaml`, then restart:

```yaml
mode: simulation   # no camera, no AI: the demo restaurant
mode: live         # real video: file, RTSP camera/NVR, or webcam
source: "videos/demo.mp4"   # used when mode: live
```

If live mode can't start (missing video file, AI model unavailable), TableSense **falls back
to simulation** and shows a yellow banner explaining why.

Both modes feed the same table engine, so the dashboard, alerts and analytics behave identically.

---

## 3. Getting a demo CCTV video

**Two test clips are already in `videos\`** (see `videos\README.txt` for sources and licences):
`demo.mp4` is a high-angle restaurant view (used by default when `mode: live`), and `sushi_bar.mp4` is an eye-level
sushi bar with chefs in white uniforms (good for trying the uniform picker). For `demo.mp4`, set `live.anchor_y: 0.7`.

To get more:

You need a clip of a dining area from a **fixed, high camera angle** (like a real CCTV mount):

1. Go to [pexels.com/videos](https://www.pexels.com/videos/) or [pixabay.com/videos](https://pixabay.com/videos/)
   and search for **"restaurant cctv"**, **"cafe interior"**, **"restaurant overhead"** or **"people eating restaurant"**.
   Pick one with a static camera, several tables, and people sitting down and leaving.
2. Download it in HD (720p or 1080p is plenty) and save it as **`videos\demo.mp4`**.
3. Set `mode: live` and restart. The video loops forever.

> Using your own restaurant's footage? Export a 5–10 minute clip from the NVR (USB backup, MP4) and drop it in `videos\`.
> Only use footage you have the rights to.

---

## 4. Drawing table zones and the staff uniform (Setup page)

Open **http://localhost:8000/setup**. In simulation mode it grabs a frame from your configured video source.

**Tables tab**
1. Click **+ Draw a table zone**, then click around the **area where guests sit**: the table *and* its chairs.
   Click the first (yellow) point or press **Enter** to close the shape.
2. Name it (e.g. `T4`, `Window 2`) and set the number of seats.
3. Drag corners to adjust, or drag inside a zone to move it. **Del** deletes the selected zone, **Esc** cancels, and **Ctrl+Z** undoes a point.
4. Click **Save zones**. In live mode they take effect immediately, with no restart needed.

Tips: leave the walkway *outside* zones. The AI uses each person's **feet position** (bottom of
their box), so zones should cover the floor area under the chairs, not just the tabletop.

**Staff uniform tab**
1. Click on a waiter's shirt. Repeat on 2–3 waiters in different lighting.
2. Matching pixels light up purple. If lots of guests light up too, lower the hue tolerance or re-sample.
3. Pick a method and click **Save uniform colour**:
   - **Combined** (recommended): uniform colour plus posture and motion.
   - **Uniform only**: best when uniforms are a distinctive colour.
   - **Posture only**: no uniform. Seated and still inside a zone means a guest; standing and moving means staff.

Black, white and grey uniforms are detected automatically and matched by brightness instead of
colour. Dark guest clothing and shadows can match those too, so keep **Combined** in that case.

Zones are saved to `config\zones.json` as fractions of the image (0–1), so they survive a change
of camera resolution. See `config\zones.example.json`.

---

## 5. Connecting a real camera / NVR (RTSP)

Set `source` to the camera's RTSP URL. **Use the sub-stream:** it's lower resolution, so it uses
far less CPU and works just as well for detection.

**Hikvision** (cameras and NVRs):
```yaml
# Direct camera: 101 = main stream, 102 = sub-stream
source: "rtsp://admin:Password123@192.168.1.64:554/Streaming/Channels/102"
# Via NVR, camera on channel 5: <channel><stream> → 501 main, 502 sub
source: "rtsp://admin:Password123@192.168.1.100:554/Streaming/Channels/502"
```

**CP Plus** (Dahua-based; also Dahua, Amcrest):
```yaml
# subtype=0 main stream, subtype=1 sub-stream
source: "rtsp://admin:Password123@192.168.1.108:554/cam/realmonitor?channel=1&subtype=1"
```

**Webcam:** `source: 0`

Checklist if it won't connect:
- **Test the URL in VLC first** (Media → Open Network Stream). If VLC can't play it, TableSense can't either.
- RTSP must be enabled on the NVR (Hikvision: Configuration → Network → Advanced → Integration Protocol).
- **Special characters in the password must be URL-encoded:** `@` → `%40`, `#` → `%23`, `:` → `%3A`.
- The PC must be on the same network as the NVR (or have port 554 forwarded).
- TableSense uses RTSP over TCP and reconnects automatically (1 s → 30 s backoff). Credentials are masked in logs.

---

## 6. Tuning

All settings live in `config\config.yaml`; restart after editing (Setup-page changes apply live).

| Setting | Default | Raise it if… | Lower it if… |
|---|---|---|---|
| `engine.seat_confirm_sec` | 8 | people passing by trigger "seated" | real seating is detected too slowly |
| `engine.leave_confirm_sec` | 20 | tables flip to "cleaning" while guests visit the washroom | tables stay occupied long after guests leave |
| `engine.staff_serve_sec` | 3 | waiters walking past mark tables as served | quick waiter visits are missed |
| `engine.staff_clean_sec` | 5 | passing staff mark dirty tables clean | real cleaning isn't registered |
| `engine.debounce_window_sec` | 3 | states flicker | changes react too slowly |
| `alerts.waiter_alert_sec` | 180 | alerts feel too eager | you want earlier warnings |
| `alerts.clean_alert_sec` | 120 | same, for cleaning | same |
| `detector.conf` | 0.35 | chairs/coats are detected as people | people are missed (dim light, far tables) |
| `detector.process_every_n` | 3 | the CPU is maxed out | tracking IDs jump around |
| `detector.imgsz` | 640 | (larger = better for far tables, slower) | the laptop is slow: try 480 |
| `detector.threads` | 0 (auto: half the cores, max 4) | a big desktop CPU is underused | other apps on the PC feel sluggish |
| `live.zone_overlap_min` | 0.3 | people at neighbouring tables are counted | seated guests at zone edges are missed |
| `live.staff_adjacent_px` | 40 | staff at the next table count as visiting | waiters serving from the aisle are missed |
| `staff.uniform_min_ratio` | 0.30 | guests in similar colours are tagged staff | waiters aren't recognised |
| `staff.posture.seated_max_aspect` | 1.6 | standing people count as seated | seated people count as standing (steep camera angle) |
| `staff.posture.stationary_rel` | 0.35 | eating/gesturing diners get tagged as staff | waiters standing at a table look like guests |
| `live.anchor_y` | 1.0 | (1.0 = feet, the default) | diners are counted at the table *in front of* theirs (steep ceiling cameras: try 0.7) |

On a typical laptop CPU, YOLO11n takes 30–100 ms per frame, so one camera at `process_every_n: 3` uses
roughly one CPU core. The camera FPS and AI FPS are shown on the live feed.

**Run the PC on mains power.** On battery, Windows slows background programs down a lot (we measured
inference going from ~50 ms to over 1 s). TableSense opts out of Windows power throttling at startup and
caps its own CPU threads, which fixes most of this. If the AI still drops below 1.5 frames/s, the dashboard shows a
yellow warning with what to do. For a permanent install, set the Windows power plan to **Best performance** and
disable sleep.

---

## 7. Alerts, WhatsApp and sound

- Click **Enable alerts** once on the dashboard. Browsers block sound until you interact with the page. This also turns on browser notifications.
- Drop an `alert.mp3` into `static\sounds\` to replace the built-in tones.
- **WhatsApp** (Twilio): copy `.env.example` to `.env`, fill in `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` and `TWILIO_TO`, then set
  `whatsapp.enabled: true` in config. Secrets are only read from `.env`, never from config or code.
  The free [Twilio WhatsApp sandbox](https://www.twilio.com/docs/whatsapp/sandbox) works for testing.

---

## 8. Analytics

![Analytics](docs/analytics.png)

- On first start, `analytics.seed_demo_data: true` writes **4 weeks of realistic demo history** so the charts are full.
  The history includes a go-live point two weeks ago (slower service before it), which drives the before/after revenue card.
- **For a real deployment**, set `seed_demo_data: false` and delete `data\tablesense.db` to start clean.
- **Staffing**: waiters per shift = ⌈typical peak occupied tables ÷ `analytics.tables_per_waiter`⌉.
- **Revenue impact**: extra covers per week from faster turns × `analytics.avg_bill`. This is capped at the guests actually
  lost to walk-aways, so it never claims more than the demand that existed.
- Daily averages use complete days only (today appears tomorrow). The Export buttons download a daily summary or raw events.

---

## 9. Privacy

- **Nothing visual is stored.** Frames stay in memory only; SQLite holds table events (table, event type, time, duration, guest count).
- `privacy.face_blur: true` blurs the head region of every detected person on the displayed feed.
- The MJPEG feed is served only by this app. Don't expose the port to the internet without putting authentication in front of it.

---

## 10. Hosted demo link (browser-only build)

`run.bat demo` (or `.venv\Scripts\python tools\build_static.py`) writes a **static, simulation-only** copy into `dist\`.
The simulator, table engine and queue predictor are ported to JavaScript (`demo\demo-sim.js`), and
analytics use pre-computed sample data. There's no server, camera or Python involved, so it's ideal for a shareable link.

- **Preview locally:** `.venv\Scripts\python -m http.server -d dist 8080`, then open http://localhost:8080
- **Netlify:** drag the `dist` folder onto [app.netlify.com/drop](https://app.netlify.com/drop).
- **Vercel:** `npm i -g vercel`, then `vercel deploy dist --prod`.
- **GitHub Pages:** push `dist\` contents to a `gh-pages` branch.

Rebuild after changing the dashboard. `demo\demo-sim.js` mirrors `backend\simulator.py`,
`table_engine.py` and `queue_predictor.py`; the Python versions are the reference.

---

## 11. 5-minute client demo script

**Before the meeting:** run `run.bat` in simulation mode, open the dashboard full-screen, click **Enable alerts**,
and have the analytics page open in a second tab. (No laptop? Use the hosted demo link.)

| Time | Show | Say |
|---|---|---|
| 0:00 | Dashboard at **1x** | "This is your dining floor, built from the CCTV you already have. Every table is coloured by what's happening: green free, red waiting for a waiter, orange served, yellow needs cleaning. The timers show how long each table has been in that state." |
| 0:45 | Click **20x** | "Let me fast-forward a Friday dinner. Watch the tables fill up and waiters walk out from the service area." |
| 1:15 | First red alert, with sound | "Here: Table 9 has been waiting 3 minutes and nobody has gone over. Your manager gets this on their phone, even on WhatsApp. After 5 minutes it escalates." Click **Acknowledge**. |
| 2:00 | Yellow table → tap → **Mark clean** | "Tables that need cleaning get flagged too. Every minute a clean table sits empty is lost revenue." |
| 2:30 | Host queue: add "Sharma, 4" | "A walk-in arrives. The host types the name, and TableSense suggests the best table and quotes an honest wait: '~12 minutes'. No guessing." Point at **Freeing up next**. |
| 3:15 | Open **Analytics**, 4 weeks | "Every event becomes data. Here are your real peak hours, the heatmap by day, and how fast your team responds." |
| 3:45 | Revenue card + dashed go-live line | "Before TableSense, a waiter took 3.7 minutes to reach a new table. Now it's 2.4. Tables turn 3 minutes faster, which is about **₹50,000 a week** in extra covers at your average bill." |
| 4:15 | Staffing card | "And it tells you how many waiters you need per shift: three on weekday afternoons, four at weekend dinner." |
| 4:40 | Back to the dashboard on a phone | "Waiters can see it on their phones. It runs on a normal PC, uses your existing cameras, and stores no video or faces, only table events." |

Questions owners usually ask:
- **"Does it need new cameras?"** No. Any IP camera or NVR with RTSP works (Hikvision, CP Plus, Dahua…).
- **"Do I need a powerful PC?"** No. It runs on a normal laptop CPU, with no GPU needed.
- **"What about privacy?"** No video or faces are stored. Face blur is available.
- **"How long does setup take?"** About 15 minutes per camera: draw the tables, click a waiter's shirt, done.

---

## 12. Project layout

```
tablesense/
  backend/
    __main__.py          `python -m backend`: starts the server on config's host/port
    main.py              FastAPI app: routes, WebSocket, MJPEG, startup wiring
    perf.py              Windows power-throttling opt-out, CPU thread limits
    config.py            config.yaml / zones.json loading (comment-preserving saves)
    table_engine.py      per-table state machine, debouncing, alerts  ← core logic
    simulator.py         simulation mode + floor-plan renderer
    queue_predictor.py   host queue + next-free predictions
    analytics.py         weekly stats, staffing, revenue, demo-history seeder, CSV
    video_source.py      file / RTSP / webcam capture thread (latest frame only)
    detector.py          YOLO11n + ByteTrack (persons only)
    staff_classifier.py  uniform colour / posture / combined (pluggable base class)
    live_pipeline.py     camera → detection → zones → classifier → engine; annotated frames
    notifier.py          dashboard notifications + Twilio WhatsApp
    db.py                SQLite schema & helpers
  static/                index.html, analytics.html, setup.html, js/, css/, sounds/
  demo/                  JS port for the static demo (demo-sim.js, demo-shim.js)
  tools/build_static.py  builds dist/ (browser-only demo)
  config/                config.yaml, zones.json (created by Setup), zones.example.json
  data/tablesense.db     events & alerts (created on first run)
  models/                yolo11n.pt (downloaded on first live run)
  videos/                your demo clips
  tests/                 pytest suite
```

**API:** `GET /`, `/setup`, `/analytics`; `WS /ws` (full state every second); `GET /video_feed` (MJPEG);
`GET /api/state`; `POST /api/alerts/{id}/ack`; `POST /api/tables/{id}/mark_clean`;
`GET|POST /api/queue`, `DELETE /api/queue/{id}?reason=seated|removed`; `POST /api/sim/speed`;
`GET /api/analytics/weekly?weeks=1|2|4`; `GET /api/analytics/export.csv?weeks=&kind=daily|events`;
`GET|POST /api/zones`; `POST /api/uniform`; `GET /api/snapshot`; `GET /api/source_frame`.
Interactive docs at `/docs`.

**Several cameras or restaurants:** run one instance per camera, each on its own port, using the
`TABLESENSE_CONFIG`, `TABLESENSE_ZONES` and `TABLESENSE_DB` environment variables.

---

## 13. Tests

```bat
run.bat test
```

There are 40 tests:
- the table state machine, driven by fake detections: seating, serving, leaving, cleaning, flicker resistance, alert reminders and escalation, hot reload, SQLite persistence
- queue predictions
- zone assignment and the staff classifier
- the simulator → engine loop
- analytics consistency
- an API smoke test

---

## 14. Known limitations / next steps

- **Staff vs guest** uses colour and posture heuristics. They work well with distinctive uniforms; for
  mixed or dark uniforms, train a small classifier on person crops and plug it in via
  `staff_classifier.TrainedModelClassifier`.
- **One camera per instance.** Large floors need one TableSense instance per camera (see §12), or a multi-camera merge.
- **Zone quality matters most.** Overlapping zones or zones that include walkways cause most errors.
  Spend a few minutes on Setup with a real busy frame.
- **No user accounts.** Run it on the restaurant's local network, or behind a reverse proxy with auth if exposed.
