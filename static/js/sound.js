// Alert sounds + browser notifications.
// Uses /static/sounds/alert.mp3 if you drop one in; otherwise synthesises tones with WebAudio.
// Browsers block audio until the user interacts, so the dashboard shows an "Enable alerts" button.
window.TSAlerts = (() => {
  "use strict";
  const KEY = "tablesense.alerts.enabled";
  let ctx = null;
  let customAudio = null;
  let enabled = false;
  try { enabled = localStorage.getItem(KEY) === "1"; } catch (_) { /* storage unavailable */ }

  fetch("/static/sounds/alert.mp3", { method: "HEAD" })
    .then((r) => { if (r.ok) customAudio = new Audio("/static/sounds/alert.mp3"); })
    .catch(() => {});

  function audioCtx() {
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }

  function tone(freq, start, dur, type = "sine", gain = 0.18) {
    const c = audioCtx();
    if (!c) return;
    const t0 = c.currentTime + start;
    const osc = c.createOscillator();
    const g = c.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g).connect(c.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.05);
  }

  const PATTERNS = {
    // waiter needed: bright two-tone
    waiter: () => { tone(880, 0, 0.18); tone(1175, 0.2, 0.28); },
    // escalation: urgent triple
    critical: () => { [0, 0.18, 0.36].forEach((t) => tone(1320, t, 0.14, "square", 0.12)); tone(990, 0.6, 0.35, "square", 0.1); },
    // cleaning: soft single chime
    cleaning: () => { tone(660, 0, 0.35, "triangle", 0.16); },
  };

  function play(kind) {
    if (!enabled) return;
    if (customAudio && kind !== "cleaning") {
      customAudio.currentTime = 0;
      customAudio.play().catch(() => PATTERNS[kind]?.());
      return;
    }
    PATTERNS[kind]?.();
  }

  function notify(title, body, tag) {
    if (!enabled || !("Notification" in window) || Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, { body, tag, renotify: true, silent: true });
      n.onclick = () => { window.focus(); n.close(); };
    } catch (_) { /* some mobile browsers only allow notifications from a service worker */ }
  }

  async function enable() {
    enabled = true;
    try { localStorage.setItem(KEY, "1"); } catch (_) {}
    audioCtx();
    tone(880, 0, 0.12, "sine", 0.08);
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (_) {}
    }
  }

  function disable() {
    enabled = false;
    try { localStorage.setItem(KEY, "0"); } catch (_) {}
  }

  return {
    play, notify, enable, disable,
    get enabled() { return enabled; },
    // Audio needs a user gesture each page load even if previously enabled.
    unlock: () => { if (enabled) audioCtx(); },
  };
})();
