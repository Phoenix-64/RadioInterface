"""SDRConnect WebSocket client."""

import asyncio
import json
import logging
from typing import Callable, Awaitable, Optional, Set

import websockets
from websockets import WebSocketClientProtocol

from .config import settings


class SDRConnectClient:
    """Client for SDRConnect WebSocket API."""

    SUPPORTED_MODES = {"AM", "USB", "LSB", "CW", "NFM", "WFM"}  # SAM excluded

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._websocket: Optional[WebSocketClientProtocol] = None
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Establish WebSocket connection."""
        try:
            self._websocket = await websockets.connect(self.ws_url)
            logging.info(f"[SDR] Connected to {self.ws_url}")
        except Exception as e:
            self._websocket = None
            logging.warning(f"[SDR] Connection failed: {e}")
            raise

    def is_connected(self) -> bool:
        """Return True if websocket is connected."""
        return self._websocket is not None

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def _set_property(self, prop: str, value) -> None:
        """Send a set_property message."""
        if not self._websocket:
            logging.warning("[SDR] Cannot set property, websocket not connected")
            return

        msg = json.dumps({
            "event_type": "set_property",
            "property": prop,
            "value": value
        })
        await self._websocket.send(msg)
        logging.debug(f"[SDR] Set property {prop} -> {value}")

    async def set_frequency(self, freq: int) -> None:
        """Set VFO frequency in Hz."""
        try:
            freq = int(freq)
        except (TypeError, ValueError):
            logging.warning(f"[SDR] Invalid frequency value: {freq}")
            return
        await self._set_property(settings.FREQ_PROP, str(freq))
        logging.info(f"[SDR] Frequency set to {freq} Hz")

    async def set_mode(self, mode: str) -> None:
        """Set demodulator mode (normalized)."""
        if not mode:
            logging.warning("[SDR] Empty mode passed")
            return

        mode = mode.upper()
        if mode == "FM":
            mode = "NFM"          # Map rig FM to SDR NFM
        if mode == "SAM":
            logging.warning("[SDR] SAM mode not supported — ignoring request")
            return
        if mode not in self.SUPPORTED_MODES:
            logging.warning(f"[SDR] Unsupported mode: {mode}")
            return

        await self._set_property(settings.MODE_PROP, mode)
        logging.info(f"[SDR] Mode set to {mode}")

    async def set_audio_mute(self, enable: bool) -> None:
        """Mute or unmute SDR audio."""
        value = "true" if enable else "false"
        await self._set_property(settings.AUDIO_MUTE_PROP, value)
        logging.info(f"[SDR] Audio mute set to {value}")

    async def listen(
        self,
        on_frequency_change: Callable[[int], Awaitable[None]],
        on_mode_change: Callable[[str], Awaitable[None]],
        on_audio_mute_change: Callable[[str], Awaitable[None]],
        on_signal_power: Callable[[float], Awaitable[None]],
    ) -> None:
        """
        Listen for property changes from SDR and invoke callbacks.
        Runs until connection is lost; reconnects are handled by the caller.
        """
        while True:
            if not self._websocket:
                await asyncio.sleep(1)
                continue

            try:
                async for message in self._websocket:
                    try:
                        data = json.loads(message)
                        prop = data.get("property")
                        event_type = data.get("event_type")
                        value = data.get("value")

                        if event_type != "property_changed" or value is None:
                            continue

                        if prop == settings.FREQ_PROP:
                            await on_frequency_change(int(value))
                        elif prop == settings.MODE_PROP:
                            await on_mode_change(value)
                        elif prop == settings.AUDIO_MUTE_PROP:
                            await on_audio_mute_change(value)
                        elif prop == settings.SIGNAL_POWER_PROP:
                            await on_signal_power(float(value))

                    except Exception as e:
                        logging.warning(f"[SDR] Failed to parse message: {e}")

            except Exception as e:
                logging.warning(f"[SDR] Listener error: {e}")
                self._websocket = None
                await asyncio.sleep(2)