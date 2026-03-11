import asyncio
import re
import json
import logging
import socket
import time
import threading
import keyboard
from typing import Optional, Set
import tkinter as tk
import queue

import websockets

# ================= CONFIG =================
SDR_WS_URL = "ws://127.0.0.1:5454/"
RIG_HOST = "192.168.1.18"
RIG_PORT = 4532
WAVELOG_WS_PORT = 54322
POLL_INTERVAL = 0.1  # seconds
TUNE_DURATION = 5

FREQ_PROP = "device_vfo_frequency"
MODE_PROP = "demodulator"
AUDIO_MUTE_PROP = "audio_mute"
SIGNAL_POWER_PROP = "signal_power"

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
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logging.info(f"[RIG] Connected to {self.host}:{self.port}")
        except Exception as e:
            self.sock = None
            logging.warning(f"[RIG] Connection failed: {e}")
            raise

    def is_connected(self) -> bool:
        return self.sock is not None

    def send_command(self, cmd: str) -> str:
        if not self.sock:
            raise RuntimeError("RIG socket not connected")

        try:
            with self._lock:
                self.sock.sendall((cmd + "\n").encode())

                data = b""
                while True:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("RIG connection lost")
                    data += chunk
                    if b"\n" in chunk:
                        break

                return data.decode(errors="ignore").strip()

        except Exception as e:
            logging.warning(f"[RIG] Connection lost: {e}")
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
            raise

    def set_frequency(self, freq: int) -> None:
        resp = self.send_command(f"F {freq}")
        logging.info(f"[RIG] Set frequency to {freq} Hz ->  {resp} ")

    def set_mode(self, mode: str) -> None:
        # Some rigs/rigctld expect FM instead of NFM etc — normalize before sending.
        if mode == "NFM":
            mode = "FM"
        elif mode == "LSB":
            mode = "PKTLSB"
        elif mode == "USB":
            mode = "PKTUSB"
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
        try:
            self.websocket = await websockets.connect(self.ws_url)
            logging.info(f"[SDR] Connected to {self.ws_url}")
        except Exception as e:
            self.websocket = None
            logging.warning(f"[SDR] Connection failed: {e}")
            raise

    def is_connected(self) -> bool:
        return self.websocket is not None

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
    async def listen(self, on_frequency_change, on_mode_change, on_audio_mute_change, on_signal_power):
        while True:
            if not self.websocket:
                await asyncio.sleep(1)
                continue

            try:
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
                        elif prop == SIGNAL_POWER_PROP:
                            await on_signal_power(value)

                    except Exception as e:
                        logging.warning(f"[SDR] Failed to parse message: {e}")
            except Exception as e:
                logging.warning(f"[SDR] Listener error: {e}")
                self.websocket = None
                await asyncio.sleep(2)


# ================= S-METER GUI =================

# ================= S-METER GUI =================

class SMeterDisplay:

    def __init__(self):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

        self.samples = []
        self.avg_window = 0.7

    def start(self):
        self.thread.start()

    def update_power(self, dbm: float):
        self.queue.put((time.time(), dbm))

    def dbm_to_s(self, dbm):
        s = 9 + (dbm + 73) / 6
        if s < 0:
            s = 0
        return s

    def format_s(self, s):
        if s <= 9:
            return f"S{int(round(s))}"
        over = (s - 9) * 6
        return f"S9+{int(over)}"

    def dbm_to_meter(self, dbm):
        if dbm < -121:
            dbm = -121
        if dbm > -33:
            dbm = -33
        return (dbm + 121) / 88

    def _run(self):
        root = tk.Tk()

        # Remove title bar / window decorations
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        # make window draggable
        self._offset_x = 0
        self._offset_y = 0

        def start_move(event):
            self._offset_x = event.x
            self._offset_y = event.y

        def do_move(event):
            x = root.winfo_pointerx() - self._offset_x
            y = root.winfo_pointery() - self._offset_y
            root.geometry(f"+{x}+{y}")

        root.bind("<Button-1>", start_move)
        root.bind("<B1-Motion>", do_move)


        # Dark theme colors
        bg_main = "#2b2b2b"
        bg_meter = "#3a3a3a"
        text_color = "#d0d0d0"
        tick_color = "#9a9a9a"
        bar_color = "#4CAF50"

        root.configure(bg=bg_main)

        width = 420
        height = 20

        self.canvas = tk.Canvas(
            root,
            width=width,
            height=height,
            bg=bg_meter,
            highlightthickness=0
        )
        self.canvas.pack(padx=8, pady=6)

        self.bar = self.canvas.create_rectangle(
            0, 0, 0, height,
            fill=bar_color,
            width=0
        )

        # scale positions
        scale_dbm = [
            -121,-115,-109,-103,-97,-91,-85,-79,-73,
            -67,-61,-55,-49,-43,-37,-33
        ]

        for dbm in scale_dbm:
            x = self.dbm_to_meter(dbm) * width
            self.canvas.create_line(
                x,
                10,
                x,
                height,
                fill=tick_color
            )

        labels = [
            "S1","S2","S3","S4","S5","S6","S7","S8","S9",
            "+10","+20","+30","+40","+50","+60"
        ]

        for i,label in enumerate(labels):
            dbm = scale_dbm[i+1]
            x = self.dbm_to_meter(dbm) * width
            self.canvas.create_text(
                x,
                6,
                text=label,
                fill=text_color,
                font=("Segoe UI",8)
            )

        info = tk.Frame(root, bg=bg_main)
        info.pack(pady=(2,6))

        self.dbm_label = tk.Label(
            info,
            text="-120 dBm",
            font=("Segoe UI",11),
            fg=text_color,
            bg=bg_main
        )
        self.dbm_label.pack(side="left", padx=12)

        self.s_inst_label = tk.Label(
            info,
            text="S0",
            font=("Segoe UI",11),
            fg=text_color,
            bg=bg_main
        )
        self.s_inst_label.pack(side="left", padx=12)

        self.s_avg_label = tk.Label(
            info,
            text="S0 avg",
            font=("Segoe UI",11),
            fg=text_color,
            bg=bg_main
        )
        self.s_avg_label.pack(side="left", padx=12)

        self._update_loop(root)
        root.mainloop()

    def _update_loop(self, root):
        now = time.time()
        try:
            while True:
                t, dbm = self.queue.get_nowait()
                self.samples.append((t, dbm))
        except queue.Empty:
            pass

        self.samples = [
            (t,v) for (t,v) in self.samples
            if now - t < self.avg_window
        ]

        if self.samples:
            inst_dbm = self.samples[-1][1]
            avg_dbm = sum(v for (_,v) in self.samples) / len(self.samples)

            s_inst = self.dbm_to_s(inst_dbm)
            s_avg = self.dbm_to_s(avg_dbm)

            self.dbm_label.config(text=f"{inst_dbm:.1f} dBm")
            self.s_inst_label.config(text=self.format_s(s_inst))
            self.s_avg_label.config(text=f"{self.format_s(s_avg)} avg")

            meter = self.dbm_to_meter(inst_dbm)
            width = int(meter * 420)
            self.canvas.coords(self.bar, 0, 0, width, 20)

        root.after(40, lambda: self._update_loop(root))

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

        self.capslock_ptt_enabled = False

        self.smeter = SMeterDisplay()

    async def start(self):

        self.smeter.start()

        threading.Thread(
            target=self.start_capslock_ptt,
            daemon=True
        ).start()

        await asyncio.gather(
            self.rig_connection_manager(),
            self.sdr_connection_manager(),
            self.monitor_tx(),
            self.monitor_rig_state(),
            self.keyboard_listener(),
            self.sdr.listen(
                on_frequency_change=self.on_frequency_change,
                on_mode_change=self.on_mode_change,
                on_audio_mute_change=self.on_audio_mute_change,
                on_signal_power=self.on_signal_power
            ),
            self.wave.start()

        )

    async def rig_connection_manager(self):
        while True:
            if not self.rig.is_connected():
                try:
                    logging.info("[RIG] Attempting reconnect...")
                    await asyncio.to_thread(self.rig.connect)
                except Exception:
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(2)

    async def sdr_connection_manager(self):
        while True:
            if not self.sdr.is_connected():
                try:
                    logging.info("[SDR] Attempting reconnect...")
                    await self.sdr.connect()
                except Exception:
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(2)

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
          ptt    -> toggle CapsLock PTT
        """

        logging.info("[KEY] Commands: 'm' = toggle mute | 'tune' = carrier | 'ptt' = toggle CapsLock PTT")

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

                # --------------------
                # Toggle CapsLock PTT
                # --------------------
                elif cmd == "ptt":
                    self.capslock_ptt_enabled = not self.capslock_ptt_enabled

                    if self.capslock_ptt_enabled:
                        logging.info("[PTT] CapsLock PTT ENABLED")
                    else:
                        logging.info("[PTT] CapsLock PTT DISABLED")

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

    def start_capslock_ptt(self):

        logging.info("[PTT] CapsLock PTT listener started (disabled by default)")

        self._ptt_pressed = False

        def ptt_down(e):

            if not self.capslock_ptt_enabled:
                return

            if self._ptt_pressed:
                return

            self._ptt_pressed = True

            try:
                if self.rig.is_connected():
                    self.rig.send_command("T 1")
                    logging.info("[PTT] TX ON")
            except Exception as err:
                logging.warning(f"[PTT] TX start failed: {err}")

        def ptt_up(e):

            if not self.capslock_ptt_enabled:
                return

            if not self._ptt_pressed:
                return

            self._ptt_pressed = False

            try:
                if self.rig.is_connected():
                    self.rig.send_command("T 0")
                    logging.info("[PTT] TX OFF")
            except Exception as err:
                logging.warning(f"[PTT] TX stop failed: {err}")

        keyboard.on_press_key("caps lock", ptt_down, suppress=True)
        keyboard.on_release_key("caps lock", ptt_up, suppress=True)

    async def on_signal_power(self, value):

        try:
            dbm = float(value)
        except:
            return

        self.smeter.update_power(dbm)


# ================= ENTRY POINT =================
if __name__ == "__main__":
    bridge = CATBridge()
    asyncio.run(bridge.start())