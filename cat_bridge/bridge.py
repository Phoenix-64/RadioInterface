"""Main CAT bridge orchestrator with split VFO support and global hotkeys."""

import asyncio
import logging
import threading
import time
from typing import Optional

import keyboard

from .config import settings
from .rig import RigCtl
from .sdr import SDRConnectClient
from .gui import SMeterDisplay
from .wavelog import WaveLogServer


SYNC_SUPPRESS = 0.35  # seconds


class CATBridge:
    """Orchestrates communication between rigctld, SDRConnect, and WaveLog."""

    def __init__(self) -> None:
        self.rig = RigCtl(settings.RIG_HOST, settings.RIG_PORT)
        self.sdr = SDRConnectClient(settings.SDR_WS_URL)
        self.wave = WaveLogServer(settings.WAVELOG_WS_PORT)
        self.smeter = SMeterDisplay()

        # Split VFO state
        self.split_mode = 1          # 1=combined, 2=split RX, 3=split TX, 4=split combined
        self.rx_freq: Optional[int] = None
        self.tx_freq: Optional[int] = None

        # Current known state (for compatibility)
        self.current_frequency: Optional[int] = None
        self.current_mode: Optional[str] = None

        # Last rig values (poll comparison)
        self._last_rig_frequency: Optional[int] = None
        self._last_rig_mode: Optional[str] = None

        # Sync tracking
        self._last_sync_source: Optional[str] = None
        self._last_sync_time: float = 0.0

        # Mute state
        self.auto_muted = False
        self.manual_override = False
        self.muting_enabled = True

        # TX state
        self.tx = False
        self.last_tx: Optional[bool] = None
        self._tuning = False

        # CapsLock PTT
        self.capslock_ptt_enabled = False
        self._ptt_pressed = False

        # Event loop reference
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # SDR -> RIG frequency coalescing
        self._pending_sdr_freq: Optional[int] = None
        self._sdr_freq_event: asyncio.Event = asyncio.Event()

        # GUI split display cache
        self._last_mode = 1
        self._last_rx = 0
        self._last_tx = 0
        self._split_offset: int = 0  # fixed offset in Hz for mode 4 (TX - RX)

    async def start(self) -> None:
        """Start all background tasks."""
        self._loop = asyncio.get_running_loop()
        self.smeter.start()

        # Initialise frequencies from rig (if connected)
        await self._initialise_frequencies()

        # Start global hotkey listener thread (for split control)
        threading.Thread(target=self._global_hotkey_listener, daemon=True).start()

        # Start CapsLock PTT listener (blocking keyboard library)
        threading.Thread(target=self._capslock_ptt_listener, daemon=True).start()

        await asyncio.gather(
            self._rig_connection_manager(),
            self._sdr_connection_manager(),
            self._monitor_tx(),
            self._monitor_rig_state(),
            self._keyboard_console(),
            self._sdr_freq_worker(),
            self.sdr.listen(
                on_frequency_change=self._on_frequency_change,
                on_mode_change=self._on_mode_change,
                on_audio_mute_change=self._on_audio_mute_change,
                on_signal_power=self._on_signal_power,
            ),
            self.wave.start()
        )

    async def _initialise_frequencies(self) -> None:
        """Read initial rig frequency and set both RX/TX equally."""
        try:
            freq = await self.rig.get_frequency()
            if freq:
                self.rx_freq = freq
                self.tx_freq = freq
                self.current_frequency = freq
                self._last_rig_frequency = freq
                logging.info(f"[INIT] Frequencies set to {freq} Hz")
                await self._send_split_update()
        except Exception as e:
            logging.warning(f"[INIT] Could not read initial frequency: {e}")

    # ================= Sync helpers =================

    def _sync_recent(self, source: str) -> bool:
        """Return True if a recent sync came from the opposite source."""
        if self._last_sync_source is None:
            return False
        if self._last_sync_source == source:
            return False
        return (time.time() - self._last_sync_time) < SYNC_SUPPRESS

    def _mark_sync(self, source: str) -> None:
        self._last_sync_source = source
        self._last_sync_time = time.time()

    # ================= GUI split display =================

    async def _send_split_update(self) -> None:
        """Send current split mode and frequencies to the GUI."""
        if self.rx_freq is None or self.tx_freq is None:
            return
        if (self.split_mode != self._last_mode or
            self.rx_freq != self._last_rx or
            self.tx_freq != self._last_tx):
            self.smeter.update_split_display(self.split_mode, self.rx_freq, self.tx_freq)
            self._last_mode = self.split_mode
            self._last_rx = self.rx_freq
            self._last_tx = self.tx_freq

    # ================= Split mode management =================

    async def _set_split_mode(self, new_mode: int) -> None:
        """Change split mode and update SDR/rig accordingly."""
        # If already in the target mode, still sync TX=RX if combined mode
        if new_mode == self.split_mode:
            if new_mode == 1:
                # Force TX to match RX (useful when pressing Ctrl+Q again)
                if self.rx_freq is not None and self.tx_freq != self.rx_freq:
                    self.tx_freq = self.rx_freq
                    await self.rig.set_frequency(self.tx_freq)
                    await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
                    logging.info(f"[SPLIT] Resynced TX to RX: {self.tx_freq}")
                    await self._send_split_update()
            elif new_mode == 4:
                # Re-capture offset if already in mode 4 (e.g., after external changes)
                if self.rx_freq is not None and self.tx_freq is not None:
                    self._split_offset = self.tx_freq - self.rx_freq
                    logging.info(f"[SPLIT] Re-captured offset for mode 4: {self._split_offset} Hz")
            return

        old_mode = self.split_mode
        self.split_mode = new_mode
        logging.info(f"[SPLIT] Mode {old_mode} -> {new_mode}")

        # Ensure frequencies are set
        if self.rx_freq is None or self.tx_freq is None:
            await self._initialise_frequencies()
            if self.rx_freq is None:
                return

        # Capture offset when entering mode 4
        if new_mode == 4:
            self._split_offset = self.tx_freq - self.rx_freq
            logging.info(f"[SPLIT] Captured offset for mode 4: {self._split_offset} Hz")

        # Update SDR display and rig according to new mode
        if new_mode == 1:  # Combined
            self.tx_freq = self.rx_freq
            await self.sdr.set_frequency(self.rx_freq)
            self.current_frequency = self.rx_freq
            await self.rig.set_frequency(self.tx_freq)
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.info(f"[SPLIT] Combined mode: RX={self.rx_freq} TX={self.tx_freq}")

        elif new_mode == 2:  # Split RX
            await self.sdr.set_frequency(self.rx_freq)
            self.current_frequency = self.rx_freq
            await self.rig.set_frequency(self.tx_freq)
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.info(f"[SPLIT] Split RX: RX={self.rx_freq} TX={self.tx_freq}")

        elif new_mode == 3:  # Split TX
            await self.sdr.set_frequency(self.tx_freq)
            self.current_frequency = self.tx_freq
            await self.rig.set_frequency(self.tx_freq)
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.info(f"[SPLIT] Split TX: RX={self.rx_freq} TX={self.tx_freq}")

        elif new_mode == 4:  # Split combined
            # Enforce the captured offset
            self.tx_freq = self.rx_freq + self._split_offset
            await self.sdr.set_frequency(self.rx_freq)
            self.current_frequency = self.rx_freq
            await self.rig.set_frequency(self.tx_freq)
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.info(f"[SPLIT] Split combined: RX={self.rx_freq} TX={self.tx_freq} (offset={self._split_offset})")

        await self._send_split_update()

    async def _apply_frequency_change(self, new_freq: int) -> None:
        """Handle SDR tuning event according to current split mode."""
        if self.rx_freq is None or self.tx_freq is None:
            await self._initialise_frequencies()
            if self.rx_freq is None:
                return

        if self.split_mode == 1:      # Combined: move both equally
            delta = new_freq - self.rx_freq
            self.rx_freq = new_freq
            self.tx_freq += delta
            await self.rig.set_frequency(self.tx_freq)
            self.current_frequency = self.rx_freq
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.debug(f"[SPLIT] Combined: RX={self.rx_freq} TX={self.tx_freq}")

        elif self.split_mode == 2:    # Split RX: only RX changes
            self.rx_freq = new_freq
            self.current_frequency = self.rx_freq
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.debug(f"[SPLIT] Split RX: RX={self.rx_freq} TX={self.tx_freq}")

        elif self.split_mode == 3:    # Split TX: only TX changes
            self.tx_freq = new_freq
            await self.rig.set_frequency(self.tx_freq)
            self.current_frequency = self.tx_freq
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.debug(f"[SPLIT] Split TX: RX={self.rx_freq} TX={self.tx_freq}")


        elif self.split_mode == 4:  # Split combined: enforce fixed offset
            delta = new_freq - self.rx_freq
            self.rx_freq = new_freq
            # TX must always be RX + fixed offset
            self.tx_freq = self.rx_freq + self._split_offset
            await self.rig.set_frequency(self.tx_freq)
            self.current_frequency = self.rx_freq
            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
            logging.debug(f"[SPLIT] Split combined: RX={self.rx_freq} TX={self.tx_freq} (offset={self._split_offset})")

        await self._send_split_update()

    # ================= Connection managers =================

    async def _rig_connection_manager(self) -> None:
        while True:
            if not self.rig.is_connected():
                try:
                    logging.info("[RIG] Attempting reconnect...")
                    await self.rig.connect()
                except Exception:
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(2)

    async def _sdr_connection_manager(self) -> None:
        while True:
            if not self.sdr.is_connected():
                try:
                    logging.info("[SDR] Attempting reconnect...")
                    await self.sdr.connect()
                except Exception:
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(2)

    # ================= SDR Event Handlers =================

    async def _on_frequency_change(self, freq: int) -> None:
        if freq <= 0:
            return
        if self._sync_recent("sdr"):
            return
        self._pending_sdr_freq = freq
        self._sdr_freq_event.set()

    async def _sdr_freq_worker(self) -> None:
        while True:
            await self._sdr_freq_event.wait()
            self._sdr_freq_event.clear()
            freq = self._pending_sdr_freq
            if freq is None:
                continue
            self._pending_sdr_freq = None
            self._mark_sync("sdr")
            logging.info(f"[SYNC] SDR tuning -> {freq} Hz (mode {self.split_mode})")
            await self._apply_frequency_change(freq)
            if self._pending_sdr_freq is not None:
                self._sdr_freq_event.set()

    async def _on_mode_change(self, mode: str) -> None:
        if mode == self.current_mode:
            return
        if self._sync_recent("sdr"):
            return
        logging.info(f"[SYNC] SDR -> RIG mode {mode}")
        self.current_mode = mode
        self._mark_sync("sdr")
        await self.rig.set_mode(mode)
        await self.wave.broadcast_status(self.tx_freq or 0, mode)

    async def _on_audio_mute_change(self, value: str) -> None:
        muted = (value == "true")
        if muted != self.auto_muted:
            self.manual_override = True
            self.auto_muted = False
            logging.info(f"[SDR] Manual override detected: mute={muted}")
        if self.manual_override and not muted and not self.tx:
            self.manual_override = False
            logging.info("[SDR] Manual override cleared")

    async def _on_signal_power(self, value: float) -> None:
        self.smeter.update_power(value)

    # ================= Rig polling =================

    async def _monitor_rig_state(self) -> None:
        """Poll rig for frequency and mode changes, update internal state and SDR."""
        logging.info("[RIG] Rig state monitor started")

        while True:
            try:
                freq = await self.rig.get_frequency()
                mode = await self.rig.get_mode()

                # Frequency handling
                if freq is not None and freq != self._last_rig_frequency:
                    self._last_rig_frequency = freq

                    if self.split_mode == 1:  # Combined mode
                        if not self._sync_recent("rig"):
                            logging.info(f"[SYNC] RIG -> SDR freq {freq}")
                            self.rx_freq = freq
                            self.tx_freq = freq
                            self.current_frequency = freq
                            self._mark_sync("rig")
                            await self.sdr.set_frequency(freq)
                            await self.wave.broadcast_status(freq, self.current_mode or "")
                            await self._send_split_update()

                    elif self.split_mode == 4:  # Split combined mode
                        # Rig frequency is TX, enforce fixed offset to compute RX
                        if not self._sync_recent("rig"):
                            logging.info(f"[SYNC] RIG -> SDR freq {freq} (mode 4, enforcing offset)")
                            self.tx_freq = freq
                            self.rx_freq = self.tx_freq - self._split_offset
                            self.current_frequency = self.rx_freq
                            self._mark_sync("rig")
                            await self.sdr.set_frequency(self.rx_freq)
                            await self.wave.broadcast_status(self.tx_freq, self.current_mode or "")
                            await self._send_split_update()

                    else:  # Split modes 2 and 3
                        # Only TX frequency changes (rig is TX)
                        self.tx_freq = freq
                        await self.wave.broadcast_status(freq, self.current_mode or "")
                        await self._send_split_update()

                # Mode handling (same for all modes)
                if mode and mode != self._last_rig_mode:
                    if not self._sync_recent("rig"):
                        logging.info(f"[SYNC] RIG -> SDR mode {mode}")
                        self.current_mode = mode
                        self._last_rig_mode = mode
                        self._mark_sync("rig")
                        await self.sdr.set_mode(mode)
                        await self.wave.broadcast_status(self.tx_freq or 0, mode)

            except Exception as e:
                logging.warning(f"[RIG] Monitor error: {e}")

            await asyncio.sleep(settings.POLL_INTERVAL)

    # ================= TX monitoring =================

    async def _monitor_tx(self) -> None:
        while True:
            tx_status = await self.rig.get_tx()
            tx = (tx_status == "1")
            self.tx = tx

            if tx != self.last_tx:
                if tx:
                    if self.split_mode == 3:
                        logging.info("[SPLIT] TX started while in split TX mode -> switching to split RX")
                        await self._set_split_mode(2)
                    if self.muting_enabled and not self.manual_override:
                        await self.sdr.set_audio_mute(True)
                        self.auto_muted = True
                        logging.info("[SDR] Program muted for TX")
                else:
                    if self.muting_enabled and self.auto_muted and not self.manual_override:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                        logging.info("[SDR] Program unmuted")

                self.last_tx = tx

            await asyncio.sleep(settings.POLL_INTERVAL)

    # ================= Console commands (text only) =================

    async def _keyboard_console(self) -> None:
        """Handle text commands from the terminal (m, tune, ptt, s)."""
        logging.info("[CONSOLE] Commands: m | tune | ptt | s")
        logging.info("  Global hotkeys: Ctrl+W, Ctrl+Q, Ctrl+E for split modes")

        while True:
            try:
                cmd = (await asyncio.to_thread(input)).strip().lower()

                if cmd == "m":
                    self.muting_enabled = not self.muting_enabled
                    if not self.muting_enabled:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                    logging.info(f"[KEY] Muting {'ENABLED' if self.muting_enabled else 'DISABLED'}")

                elif cmd == "tune":
                    asyncio.create_task(self._tune())

                elif cmd == "ptt":
                    self.capslock_ptt_enabled = not self.capslock_ptt_enabled
                    logging.info(f"[PTT] CapsLock {'ENABLED' if self.capslock_ptt_enabled else 'DISABLED'}")

                elif cmd == "s":
                    self.smeter.toggle_visibility()

                else:
                    logging.info(f"[CONSOLE] Unknown command: {cmd}")

            except Exception as e:
                logging.warning(f"[CONSOLE] Error: {e}")

    # ================= Global hotkeys (Ctrl+W, Ctrl+Q, Ctrl+E) =================

    def _global_hotkey_listener(self) -> None:
        """Register global hotkeys that work even when terminal is not focused."""
        logging.info("[HOTKEYS] Registering global hotkeys: Ctrl+W, Ctrl+Q, Ctrl+E")

        def do_ctrl_w():
            asyncio.run_coroutine_threadsafe(self._global_ctrl_w(), self._loop)

        def do_ctrl_q():
            asyncio.run_coroutine_threadsafe(self._global_ctrl_q(), self._loop)

        def do_ctrl_e():
            asyncio.run_coroutine_threadsafe(self._global_ctrl_e(), self._loop)

        keyboard.add_hotkey('ctrl+w', do_ctrl_w)
        keyboard.add_hotkey('ctrl+q', do_ctrl_q)
        keyboard.add_hotkey('ctrl+e', do_ctrl_e)

        # Keep thread alive
        threading.Event().wait()

    async def _global_ctrl_w(self) -> None:
        """Ctrl+W: cycle split mode (1->2->3->2->...)."""
        if self.split_mode == 1:
            await self._set_split_mode(2)
        elif self.split_mode == 2:
            await self._set_split_mode(3)
        elif self.split_mode == 3:
            await self._set_split_mode(2)
        elif self.split_mode == 4:
            await self._set_split_mode(2)

    async def _global_ctrl_q(self) -> None:
        """Ctrl+Q: reset to combined mode (1) and sync TX=RX."""
        await self._set_split_mode(1)

    async def _global_ctrl_e(self) -> None:
        """Ctrl+E: enter split combined mode (4)."""
        await self._set_split_mode(4)

    # ================= Tune carrier =================

    async def _tune(self) -> None:
        if self._tuning:
            return
        self._tuning = True
        try:
            original_mode = self.current_mode or "FM"
            prev_muting = self.muting_enabled
            self.muting_enabled = False
            await self.rig.set_mode("FM")
            await self.rig.send_command("T 1")
            await asyncio.sleep(settings.TUNE_DURATION)
            await self.rig.send_command("T 0")
            await self.rig.set_mode(original_mode)
            self.muting_enabled = prev_muting
        finally:
            self._tuning = False

    # ================= CapsLock PTT =================

    def _capslock_ptt_listener(self) -> None:
        logging.info("[PTT] CapsLock listener started")

        def ptt_down(_):
            if not self.capslock_ptt_enabled or self._ptt_pressed:
                return
            self._ptt_pressed = True
            asyncio.run_coroutine_threadsafe(
                self.rig.send_command("T 1"), self._loop
            )

        def ptt_up(_):
            if not self.capslock_ptt_enabled or not self._ptt_pressed:
                return
            self._ptt_pressed = False
            asyncio.run_coroutine_threadsafe(
                self.rig.send_command("T 0"), self._loop
            )

        keyboard.on_press_key("caps lock", ptt_down, suppress=True)
        keyboard.on_release_key("caps lock", ptt_up, suppress=True)

        threading.Event().wait()