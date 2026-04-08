"""Main CAT bridge orchestrator."""

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

        # Current known state
        self.current_frequency: Optional[int] = None
        self.current_mode: Optional[str] = None

        # Last rig values (poll comparison)
        self._last_rig_frequency: Optional[int] = None
        self._last_rig_mode: Optional[str] = None

        # Sync tracking
        self._last_sync_source: Optional[str] = None
        self._last_sync_time: float = 0.0

        # Frequency follow mode
        self.follow_frequency = True

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

        # SDR -> RIG frequency coalescing: latest value wins, intermediate steps dropped
        self._pending_sdr_freq: Optional[int] = None
        self._sdr_freq_event: asyncio.Event = asyncio.Event()



    async def start(self) -> None:
        """Start all background tasks."""
        self._loop = asyncio.get_running_loop()
        self.smeter.start()

        threading.Thread(target=self._capslock_ptt_listener, daemon=True).start()

        await asyncio.gather(
            self._rig_connection_manager(),
            self._sdr_connection_manager(),
            self._monitor_tx(),
            self._monitor_rig_state(),
            self._keyboard_listener(),
            self._sdr_freq_worker(),
            self.sdr.listen(
                on_frequency_change=self._on_frequency_change,
                on_mode_change=self._on_mode_change,
                on_audio_mute_change=self._on_audio_mute_change,
                on_signal_power=self._on_signal_power,
            ),
            self.wave.start()
        )

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
        """SDR frequency change event — coalesces rapid scroll steps."""

        if not self.follow_frequency:
            return

        if freq == self.current_frequency:
            return

        if self._sync_recent("sdr"):
            return

        # Update current_frequency immediately so duplicate events are dropped
        # while the worker is busy. The worker will pick up the latest value.
        self.current_frequency = freq
        self._pending_sdr_freq = freq
        self._sdr_freq_event.set()

    async def _sdr_freq_worker(self) -> None:
        """
        Consume pending SDR frequency changes one at a time.

        While the rig round-trip is in progress, any scroll steps that arrive
        simply overwrite _pending_sdr_freq. When the rig finishes, we send
        only the most recent value, skipping all intermediate steps.
        """
        while True:
            await self._sdr_freq_event.wait()
            self._sdr_freq_event.clear()

            freq = self._pending_sdr_freq
            if freq is None:
                continue

            self._pending_sdr_freq = None
            self._mark_sync("sdr")

            logging.info(f"[SYNC] SDR -> RIG freq {freq}")
            await self.rig.set_frequency(freq)
            await self.wave.broadcast_status(freq, self.current_mode or "")

            # If more scroll events arrived while we were waiting for the rig,
            # loop immediately instead of blocking on wait() again.
            if self._pending_sdr_freq is not None:
                self._sdr_freq_event.set()

    async def _on_mode_change(self, mode: str) -> None:
        """SDR mode change event."""

        if mode == self.current_mode:
            return

        if self._sync_recent("sdr"):
            return

        logging.info(f"[SYNC] SDR -> RIG mode {mode}")

        self.current_mode = mode
        self._mark_sync("sdr")

        await self.rig.set_mode(mode)
        await self.wave.broadcast_status(self.current_frequency or 0, mode)

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
        logging.info("[RIG] Rig state monitor started")

        while True:
            try:
                freq = await self.rig.get_frequency()
                mode = await self.rig.get_mode()

                # Frequency handling
                if freq is not None and freq != self.current_frequency:

                    if not self.follow_frequency:
                        # Track internally but do not push to SDR
                        self.current_frequency = freq
                        self._last_rig_frequency = freq

                    else:
                        if not self._sync_recent("rig"):

                            logging.info(f"[SYNC] RIG -> SDR freq {freq}")

                            self.current_frequency = freq
                            self._last_rig_frequency = freq
                            self._mark_sync("rig")

                            await self.sdr.set_frequency(freq)
                            await self.wave.broadcast_status(freq, self.current_mode or "")

                # Mode handling
                if mode and mode != self.current_mode:

                    if not self._sync_recent("rig"):

                        logging.info(f"[SYNC] RIG -> SDR mode {mode}")

                        self.current_mode = mode
                        self._last_rig_mode = mode
                        self._mark_sync("rig")

                        await self.sdr.set_mode(mode)
                        await self.wave.broadcast_status(self.current_frequency or 0, mode)

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

    # ================= Console commands =================

    async def _keyboard_listener(self) -> None:
        logging.info("[KEY] Commands: m | tune | ptt | s | f (toggle freq follow)")

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

                elif cmd == "f":
                    self.follow_frequency = not self.follow_frequency

                    if self.follow_frequency:
                        logging.info("[SYNC] Frequency FOLLOW ENABLED")

                        freq = await self.rig.get_frequency()
                        if freq:
                            self.current_frequency = freq
                            await self.sdr.set_frequency(freq)

                    else:
                        logging.info("[SYNC] Frequency FOLLOW DISABLED (SDR free tuning)")

            except Exception as e:
                logging.warning(f"[KEY] Error: {e}")

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