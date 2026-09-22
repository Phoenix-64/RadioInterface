"""WaveLog API v2 client (push-only): posts live radio state to /api/v2/radio."""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

MAX_MODE_LEN = 10  # API v2 limit for mode / mode_rx


class WaveLogClient:
    """Pushes frequency/mode/split state to WaveLog. Coalesces rapid changes."""

    def __init__(self, base_url: str, token: str, radio_name: str,
                 min_interval: float = 0.5) -> None:
        self._url = base_url.rstrip("/") + "/index.php/api/v2/radio"
        self._token = token
        self._radio = radio_name
        self._min_interval = min_interval
        self._pending: Optional[Dict] = None
        self._last_sent: Optional[Dict] = None
        self._event = asyncio.Event()

    async def start(self) -> None:
        """Worker loop (runs forever)."""
        if not self._token:
            logging.warning("[WaveLog] No API v2 token configured - push disabled")
            await asyncio.Future()
        logging.info(f"[WaveLog] Pushing radio '{self._radio}' to {self._url}")

        while True:
            await self._event.wait()
            self._event.clear()
            payload = self._pending
            self._pending = None
            if payload is None or payload == self._last_sent:
                continue

            try:
                status, body = await asyncio.to_thread(self._post_sync, payload)
            except Exception as e:
                logging.warning(f"[WaveLog] Request failed: {e}")
                status, body = 0, ""

            if status in (200, 201):
                self._last_sent = payload
                delay = self._min_interval
                logging.info(f"[WaveLog] Pushed: {payload}")
            else:
                if status:
                    logging.warning(f"[WaveLog] HTTP {status}: {body[:300]}")
                delay = self._retry_delay(status, body)
                # Retry with the newest state (keep a newer one if it arrived)
                if self._pending is None:
                    self._pending = payload
                self._event.set()

            await asyncio.sleep(delay)

    async def push_status(self, tx_freq: int, rx_freq: Optional[int], mode: str) -> None:
        """Queue a state update. rx_freq is None when not in split."""
        mode = (mode or "")[:MAX_MODE_LEN] or None
        self._pending = {
            "radio": self._radio,
            "frequency": int(tx_freq),
            "frequency_rx": int(rx_freq) if rx_freq is not None else None,
            "mode": mode,
            "mode_rx": mode if rx_freq is not None else None,
        }
        self._event.set()

    # ---------- internals ----------
    def _post_sync(self, payload: Dict) -> Tuple[int, str]:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode(errors="ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="ignore")

    @staticmethod
    def _retry_delay(status: int, body: str) -> float:
        if status == 429:
            try:
                return float(json.loads(body)["error"]["details"]["retry_after"])
            except Exception:
                return 30.0
        if status in (400, 401, 403):
            return 60.0   # token/validation problem, don't hammer
        return 5.0