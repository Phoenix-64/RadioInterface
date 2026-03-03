import asyncio
import re
import json
import logging
import socket
import time
import threading
from typing import Optional, Set

import websockets

# ================= CONFIG =================
SDR_WS_URL = "ws://127.0.0.1:5454/"
RIG_HOST = "192.168.1.18"
RIG_PORT = 4532
WAVELOG_WS_PORT = 54322
POLL_INTERVAL = 0.1  # seconds
TUNE_DURATION = 6

FREQ_PROP = "device_vfo_frequency"
MODE_PROP = "demodulator"
AUDIO_MUTE_PROP = "audio_mute"

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ================= RIGCTLD =================
class RigCtl:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        logging.info(f"[RIG] Connected to {self.host}:{self.port}")

    def send_command(self, cmd: str) -> str:
        if not self.sock:
            raise RuntimeError("RIG socket not connected")

        with self._lock:
            self.sock.sendall((cmd + "\n").encode())

            data = b""
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in chunk:
                    break

            return data.decode(errors="ignore").strip()

    def set_frequency(self, freq: int) -> None:
        #resp = self.send_command(f"F {freq}")
        logging.info(f"[RIG] Set frequency to {freq} Hz ->  NIL")

    def set_mode(self, mode: str) -> None:
        # Some rigs/rigctld expect FM instead of NFM etc — normalize before sending.
        if mode == "NFM":
            mode = "FM"
        resp = self.send_command(f"M {mode} 0")
        logging.info(f"[RIG] Set mode to {mode} -> {resp}")

    def get_tx(self) -> str:
        """Return TX status: '0' for RX, '1' for TX"""
        return self.send_command("t").strip()

    # -----------------------
    # New: read functions
    # -----------------------
    def _normalize_mode(self, raw: str) -> str:
        """Normalize various mode tokens to a small canonical set."""
        if raw is None:
            return ""
        m = raw.strip().upper()
        # Common normalization rules; extend as needed.
        if m == "NFM":
            return "FM"
        if m in ("DIGU", "DIGL"):
            return "DATA"
        # keep USB/LSB/AM/FM/CW/RTTY/DATA etc as-is
        return m

    def get_frequency(self) -> Optional[int]:
        """
        Query the rig for current frequency and return it as an int (Hz).
        Returns None if the response couldn't be parsed.
        """
        try:
            resp = self.send_command("f")
        except Exception as e:
            logging.warning(f"[RIG] Failed to query frequency: {e}")
            return None

        if not resp:
            logging.warning("[RIG] Empty response when querying frequency")
            return None

        # Try to find the first integer in the response
        m = re.search(r"(\d+)", resp)
        if m:
            try:
                freq = int(m.group(1))
                logging.debug(f"[RIG] Queried frequency -> {freq} (raw='{resp}')")
                return freq
            except ValueError:
                pass

        logging.warning(f"[RIG] Couldn't parse frequency from response: '{resp}'")
        return None

    def get_mode(self) -> Optional[str]:
        """
        Query the rig for current mode and return a normalized mode string
        (e.g. 'FM', 'USB', 'LSB', 'AM', 'CW', 'RTTY', 'DATA').
        Returns None if the response couldn't be parsed.
        """
        try:
            resp = self.send_command("m")
        except Exception as e:
            logging.warning(f"[RIG] Failed to query mode: {e}")
            return None

        if not resp:
            logging.warning("[RIG] Empty response when querying mode")
            return None

        raw = resp.strip().upper()
        # Search for known mode tokens (order matters for substrings)
        known_modes = ["AM", "CW", "LSB", "USB", "RTTY", "FM", "FMW", "PKTLSB", "PKTUSB"]

        for token in known_modes:
            if token in raw:
                normalized = self._normalize_mode(token)
                logging.debug(f"[RIG] Queried mode -> {normalized} (raw='{resp}')")
                return normalized

        # fallback: try to extract alphabetic token
        m = re.search(r"([A-Z]+)", raw)
        if m:
            normalized = self._normalize_mode(m.group(1))
            logging.debug(f"[RIG] Queried mode (fallback) -> {normalized} (raw='{resp}')")
            return normalized

        logging.warning(f"[RIG] Couldn't parse mode from response: '{resp}'")
        return None


# ================= SDRCONNECT =================
class SDRConnectClient:
    SUPPORTED_MODES = {"AM", "USB", "LSB", "CW", "NFM", "WFM"}  # SAM intentionally excluded

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None

    async def connect(self) -> None:
        self.websocket = await websockets.connect(self.ws_url)
        logging.info(f"[SDR] Connected to {self.ws_url}")

    # -------------------------------------------------
    # Internal helper
    # -------------------------------------------------
    async def _set_property(self, prop: str, value) -> None:
        if not self.websocket:
            logging.warning("[SDR] Cannot set property, websocket not connected")
            return

        msg = json.dumps({
            "event_type": "set_property",
            "property": prop,
            "value": value
        })

        await self.websocket.send(msg)
        logging.debug(f"[SDR] Set property {prop} -> {value}")

    # -------------------------------------------------
    # New: Frequency setter
    # -------------------------------------------------
    async def set_frequency(self, freq: int) -> None:
        """
        Set SDR VFO frequency in Hz.
        """
        try:
            freq = int(freq)
        except (TypeError, ValueError):
            logging.warning(f"[SDR] Invalid frequency value: {freq}")
            return

        await self._set_property(FREQ_PROP, str(freq))
        logging.info(f"[SDR] Frequency set to {freq} Hz")

    # -------------------------------------------------
    # New: Mode setter
    # -------------------------------------------------
    async def set_mode(self, mode: str) -> None:
        """
        Set SDR demodulator mode.
        Supported: AM, USB, LSB, CW, NFM, WFM
        SAM explicitly not supported.
        """
        if not mode:
            logging.warning("[SDR] Empty mode passed")
            return

        mode = mode.upper()

        # Normalize common variations
        if mode == "FM":
            mode = "NFM"  # Map rig FM to SDR NFM

        if mode == "SAM":
            logging.warning("[SDR] SAM mode not supported — ignoring request")
            return

        if mode not in self.SUPPORTED_MODES:
            logging.warning(f"[SDR] Unsupported mode: {mode}")
            return

        await self._set_property(MODE_PROP, mode)
        logging.info(f"[SDR] Mode set to {mode}")

    # -------------------------------------------------
    # Existing mute setter (unchanged)
    # -------------------------------------------------
    async def set_audio_mute(self, enable: bool) -> None:
        if not self.websocket:
            return
        value = "true" if enable else "false"

        await self._set_property(AUDIO_MUTE_PROP, value)
        logging.info(f"[SDR] Audio mute set to {value}")

    # -------------------------------------------------
    # Listener (unchanged)
    # -------------------------------------------------
    async def listen(self, on_frequency_change, on_mode_change, on_audio_mute_change) -> None:
        """Listen for property changes from SDRconnect"""
        if not self.websocket:
            return

        async for message in self.websocket:
            try:
                data = json.loads(message)
                prop = data.get("property")
                event_type = data.get("event_type")
                value = data.get("value")

                if event_type != "property_changed" or value is None:
                    continue

                if prop == FREQ_PROP:
                    await on_frequency_change(value)
                elif prop == MODE_PROP:
                    await on_mode_change(value)
                elif prop == AUDIO_MUTE_PROP:
                    await on_audio_mute_change(value)

            except Exception as e:
                logging.warning(f"[SDR] Failed to parse message: {e}")


# ================= WAVELOG =================
class WaveLogServer:
    def __init__(self, port: int):
        self.port = port
        self.clients: set = set()  # use plain set, type hint optional

    async def start(self) -> None:
        logging.info(f"[WaveLog] Starting WebSocket server on ws://localhost:{self.port}")
        async with websockets.serve(self.handle_client, "localhost", self.port):
            await asyncio.Future()  # run forever

    async def handle_client(self, websocket, path=None) -> None:
        """
        Handles a new WebSocket client.
        `websocket` is a connected WebSocket server object from websockets.serve
        `path` is optional, required by websockets.serve callback signature.
        """
        self.clients.add(websocket)
        logging.info("[WaveLog] Browser connected")
        await websocket.send(json.dumps({
            "type": "welcome",
            "message": "Connected to Python CAT bridge"
        }))
        try:
            await websocket.wait_closed()
        finally:
            self.clients.remove(websocket)
            logging.info("[WaveLog] Browser disconnected")

    async def broadcast_status(self, frequency: int, mode: str) -> None:
        if not self.clients:
            return
        message = {
            "type": "radio_status",
            "radio": True,
            "frequency": int(frequency),
            "mode": mode,
            "power": 0,
            "split": False,
            "vfoB": None,
            "modeB": None,
            "timestamp": int(time.time() * 1000)
        }
        dead_clients = set()
        for ws in self.clients:
            try:
                await ws.send(json.dumps(message))
            except:
                dead_clients.add(ws)
        self.clients.difference_update(dead_clients)
        logging.info(f"[WaveLog] Broadcast: {message}")


# ================= MAIN CONTROLLER =================
class CATBridge:
    def __init__(self):
        self.rig = RigCtl(RIG_HOST, RIG_PORT)
        self.sdr = SDRConnectClient(SDR_WS_URL)
        self.wave = WaveLogServer(WAVELOG_WS_PORT)

        self.current_frequency: Optional[int] = None
        self.current_mode: Optional[str] = None

        self._last_rig_frequency: Optional[int] = None
        self._last_rig_mode: Optional[str] = None

        self.auto_muted = False
        self.manual_override = False
        self.tx = False
        self.last_tx: Optional[bool] = None
        self.muting_enabled = True
        self.tune_duration = TUNE_DURATION
        self._tuning = False

    async def start(self):
        self.rig.connect()
        await self.sdr.connect()

        await asyncio.gather(
            self.monitor_tx(),
            self.monitor_rig_state(),   # <-- NEW
            self.keyboard_listener(),
            self.sdr.listen(
                on_frequency_change=self.on_frequency_change,
                on_mode_change=self.on_mode_change,
                on_audio_mute_change=self.on_audio_mute_change
            ),
            self.wave.start()
        )

    async def on_frequency_change(self, freq: int):
        self.current_frequency = freq
        self.rig.set_frequency(freq)
        await self.wave.broadcast_status(freq, self.current_mode or "")

    async def on_mode_change(self, mode: str):
        self.current_mode = mode
        self.rig.set_mode(mode)
        await self.wave.broadcast_status(self.current_frequency or 0, mode)

    async def on_audio_mute_change(self, value: str):
        muted = (value == "true")

        # Detect manual change (actual state differs from program intention)
        if muted != self.auto_muted:
            self.manual_override = True
            self.auto_muted = False      # Give up automatic control
            logging.info(f"[SDR] Manual override detected: mute={muted}")

        # Clear manual override if user unmutes during RX (normal listening)
        if self.manual_override and not muted and not self.tx:
            self.manual_override = False
            logging.info("[SDR] Manual override cleared (user unmuted during RX)")

    async def monitor_tx(self):
        while True:
            tx_status = await asyncio.to_thread(self.rig.get_tx)
            tx = (tx_status == "1")
            self.tx = tx

            if tx != self.last_tx:
                if tx:  # TX started
                    if self.muting_enabled and not self.manual_override:
                        await self.sdr.set_audio_mute(True)
                        self.auto_muted = True
                        logging.info("[SDR] Program muted for TX")
                else:  # TX ended
                    if self.muting_enabled and self.auto_muted and not self.manual_override:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                        logging.info("[SDR] Program unmuted after TX")

                self.last_tx = tx

            await asyncio.sleep(POLL_INTERVAL)

    async def monitor_rig_state(self):
        """
        Poll rig frequency and mode and propagate changes to SDR.
        """
        logging.info("[RIG] Rig state monitor started")

        while True:
            try:
                # Query rig in thread (blocking socket)
                freq = await asyncio.to_thread(self.rig.get_frequency)
                mode = await asyncio.to_thread(self.rig.get_mode)

                # -------------------------
                # Frequency change
                # -------------------------
                if freq is not None and freq != self._last_rig_frequency:
                    logging.info(f"[RIG] Frequency changed -> {freq}")

                    self._last_rig_frequency = freq
                    self.current_frequency = freq

                    # Push to SDR
                    await self.sdr.set_frequency(freq)

                    # Broadcast to WaveLog
                    await self.wave.broadcast_status(
                        freq,
                        self.current_mode or ""
                    )

                # -------------------------
                # Mode change
                # -------------------------
                if mode and mode != self._last_rig_mode:
                    logging.info(f"[RIG] Mode changed -> {mode}")

                    self._last_rig_mode = mode
                    self.current_mode = mode

                    # Push to SDR
                    await self.sdr.set_mode(mode)

                    # Broadcast to WaveLog
                    await self.wave.broadcast_status(
                        self.current_frequency or 0,
                        mode
                    )

            except Exception as e:
                logging.warning(f"[RIG] Monitor error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

    async def keyboard_listener(self):
        """
        Console commands:
          m      -> toggle automatic muting
          tune   -> transmit FM carrier for tune_duration seconds
        """
        logging.info("[KEY] Commands: 'm' = toggle mute | 'tune' = carrier")

        while True:
            try:
                cmd = (await asyncio.to_thread(input)).strip().lower()

                # --------------------
                # Toggle muting
                # --------------------
                if cmd == "m":
                    self.muting_enabled = not self.muting_enabled

                    if not self.muting_enabled:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                        logging.info("[KEY] Automatic muting DISABLED")
                    else:
                        logging.info("[KEY] Automatic muting ENABLED")

                # --------------------
                # Tune carrier
                # --------------------
                elif cmd == "tune":
                    asyncio.create_task(self.tune())

            except Exception as e:
                logging.warning(f"[KEY] Keyboard listener error: {e}")

    async def tune(self):
        """
        Switch to FM, transmit carrier for tune_duration seconds,
        then restore previous mode and RX state.
        """

        if self._tuning:
            logging.info("[TUNE] Already tuning")
            return

        self._tuning = True

        try:
            logging.info("[TUNE] Starting carrier")

            original_mode = self.current_mode or "FM"

            # Disable automatic muting temporarily
            prev_muting_state = self.muting_enabled
            self.muting_enabled = False

            # Switch to FM
            self.rig.set_mode("FM")

            # Force TX ON (rigctld command)
            await asyncio.to_thread(self.rig.send_command, "T 1")

            # Wait
            await asyncio.sleep(self.tune_duration)

            # Return to RX
            await asyncio.to_thread(self.rig.send_command, "T 0")

            # Restore mode
            self.rig.set_mode(original_mode)

            # Restore muting setting
            self.muting_enabled = prev_muting_state

            logging.info("[TUNE] Carrier finished")

        except Exception as e:
            logging.warning(f"[TUNE] Error: {e}")

        finally:
            self._tuning = False


# ================= ENTRY POINT =================
if __name__ == "__main__":
    bridge = CATBridge()
    asyncio.run(bridge.start())