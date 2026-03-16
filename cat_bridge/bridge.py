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

        # Current known state (authoritative bridge state)
        self.current_frequency: Optional[int] = None
        self.current_mode: Optional[str] = None

        # Last values from rig (polling comparison)
        self._last_rig_frequency: Optional[int] = None
        self._last_rig_mode: Optional[str] = None

        # Sync control
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

        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
            self.sdr.listen(
                on_frequency_change=self._on_frequency_change,
                on_mode_change=self._on_mode_change,
                on_audio_mute_change=self._on_audio_mute_change,
                on_signal_power=self._on_signal_power,
            ),
            self.wave.start()
        )

    # ========== Sync Helpers ==========

    def _sync_recent(self, source: str) -> bool:
        """Check if a recent sync came from the opposite source."""
        if self._last_sync_source is None:
            return False

        if self._last_sync_source == source:
            return False

        return (time.time() - self._last_sync_time) < SYNC_SUPPRESS

    def _mark_sync(self, source: str) -> None:
        self._last_sync_source = source
        self._last_sync_time = time.time()

    # ========== Connection managers ==========

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

    # ========== SDR Event Handlers ==========

    async def _on_frequency_change(self, freq: int) -> None:
        if freq == self.current_frequency:
            return

        if self._sync_recent("sdr"):
            return

        logging.info(f"[SYNC] SDR -> RIG freq {freq}")

        self.current_frequency = freq
        self._mark_sync("sdr")

        await self.rig.set_frequency(freq)
        await self.wave.broadcast_status(freq, self.current_mode or "")

    async def _on_mode_change(self, mode: str) -> None:
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

    # ========== Rig State Polling ==========

    async def _monitor_rig_state(self) -> None:
        logging.info("[RIG] Rig state monitor started")

        while True:
            try:
                freq = await self.rig.get_frequency()
                mode = await self.rig.get_mode()

                if freq is not None and freq != self.current_frequency:
                    if not self._sync_recent("rig"):
                        logging.info(f"[SYNC] RIG -> SDR freq {freq}")

                        self.current_frequency = freq
                        self._last_rig_frequency = freq
                        self._mark_sync("rig")

                        await self.sdr.set_frequency(freq)
                        await self.wave.broadcast_status(freq, self.current_mode or "")

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

    # ========== TX Monitoring ==========

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

    # ========== Console Commands ==========

    async def _keyboard_listener(self) -> None:
        logging.info("[KEY] Commands: m | tune | ptt | s")

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

            except Exception as e:
                logging.warning(f"[KEY] Error: {e}")

    # ========== Tune Carrier ==========

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

    # ========== CapsLock PTT Thread ==========

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