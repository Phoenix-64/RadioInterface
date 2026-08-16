"""WaveLog REST API v2 client."""

import logging
from typing import Optional

import aiohttp


class WaveLogServer:
    """Pushes radio state to WaveLog via the REST API v2 (POST /api/v2/radio)."""

    def __init__(self, url: str, api_key: str, radio_name: str) -> None:
        self._endpoint = url.rstrip("/") + "/index.php/api/v2/radio"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._radio_name = radio_name
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        """Open the shared HTTP session. Called once at startup."""
        self._session = aiohttp.ClientSession(headers=self._headers)
        logging.info(f"[WaveLog] REST client ready → {self._endpoint}")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def broadcast_status(self, frequency: int, mode: str) -> None:
        """Push current radio state to WaveLog."""
        if not self._session:
            logging.warning("[WaveLog] Session not started, skipping broadcast")
            return

        payload = {
            "radio": self._radio_name,
            "frequency": int(frequency),
            "mode": mode or None,
        }

        try:
            async with self._session.post(self._endpoint, json=payload) as resp:
                if resp.status in (200, 201):
                    logging.info(f"[WaveLog] Pushed: freq={frequency} mode={mode} ({resp.status})")
                else:
                    body = await resp.text()
                    logging.warning(f"[WaveLog] Push failed {resp.status}: {body}")
        except Exception as e:
            logging.warning(f"[WaveLog] Request error: {e}")