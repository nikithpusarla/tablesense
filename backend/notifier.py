"""Alert dispatch: dashboard (always, via WebSocket state) + optional WhatsApp via Twilio.

Secrets are never hardcoded — Twilio credentials are read from environment / ``.env``:
``TWILIO_SID``, ``TWILIO_TOKEN``, ``TWILIO_FROM`` (e.g. ``whatsapp:+14155238886``),
``TWILIO_TO`` (comma-separated, e.g. ``whatsapp:+9198xxxxxxx``).
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

log = logging.getLogger(__name__)


class Notifier:
    """Receives alert lifecycle callbacks from the engine and fans them out."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        wa = cfg.get("whatsapp") or {}
        self.whatsapp_enabled = bool(wa.get("enabled", False))
        self.restaurant = (cfg.get("restaurant") or {}).get("name", "TableSense")
        # Dashboard picks these up to play sound / browser notifications exactly once.
        self.recent: deque[dict[str, Any]] = deque(maxlen=100)
        self._seq = 0
        self._lock = threading.Lock()
        if self.whatsapp_enabled and not self._twilio_creds():
            log.warning("WhatsApp enabled but TWILIO_* env vars missing - WhatsApp disabled")
            self.whatsapp_enabled = False

    def __call__(self, alert: Any, kind: str) -> None:
        """Engine ``on_alert`` callback."""
        with self._lock:
            self._seq += 1
            self.recent.append({"seq": self._seq, "kind": kind, "alert": alert.to_public()})
        if kind in ("new", "renotify", "escalate"):
            log.info("Notify [%s] %s", kind, alert.message)
            if self.whatsapp_enabled:
                threading.Thread(target=self.send_whatsapp,
                                 args=(f"[{self.restaurant}] {alert.message}",), daemon=True).start()

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [n for n in self.recent if n["seq"] > seq]

    @property
    def last_seq(self) -> int:
        return self._seq

    # ------------------------------------------------------------ WhatsApp
    @staticmethod
    def _twilio_creds() -> dict[str, str] | None:
        keys = ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "TWILIO_TO")
        vals = {k: os.getenv(k, "") for k in keys}
        return vals if all(vals.values()) else None

    def send_whatsapp(self, body: str) -> bool:
        """Send a WhatsApp message through Twilio's REST API (no SDK dependency)."""
        creds = self._twilio_creds()
        if not creds:
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{creds['TWILIO_SID']}/Messages.json"
        auth = base64.b64encode(f"{creds['TWILIO_SID']}:{creds['TWILIO_TOKEN']}".encode()).decode()
        ok = True
        for to in (x.strip() for x in creds["TWILIO_TO"].split(",") if x.strip()):
            data = urllib.parse.urlencode({"From": creds["TWILIO_FROM"], "To": to, "Body": body}).encode()
            req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    log.info("WhatsApp sent to %s (HTTP %s)", to, resp.status)
            except (urllib.error.URLError, OSError) as exc:
                ok = False
                log.error("WhatsApp send to %s failed: %s", to, exc)
        return ok
