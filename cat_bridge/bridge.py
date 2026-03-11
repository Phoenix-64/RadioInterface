"""Main CAT bridge orchestrator."""

import asyncio
import logging
import threading
from typing import Optional

import keyboard

from .config import settings
from .rig import RigCtl
from .sdr import SDRConnectClient
from .gui import SMeterDisplay
from .wavelog import WaveLogServer


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

        # Last values from rig (to detect changes)
        self._last_rig_frequency: Optional[int] = None
        self._last_rig_mode: Optional[str] = None

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

    async def start(self) -> None:
        """Start all background tasks."""
        self.smeter.start()

        # Start CapsLock listener in a thread (blocking keyboard library)
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

    # ========== Connection managers ==========

    async def _rig_connection_manager(self) -> None:
        """Maintain rig connection."""
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
        """Maintain SDR connection."""
        while True:
            if not self.sdr.is_connected():
                try:
                    logging.info("[SDR] Attempting reconnect...")
                    await self.sdr.connect()
                except Exception:
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(2)

    # ========== Event handlers from SDR ==========

    async def _on_frequency_change(self, freq: int) -> None:
        """SDR frequency changed -> update rig and WaveLog."""
        self.current_frequency = freq
        await self.rig.set_frequency(freq)
        await self.wave.broadcast_status(freq, self.current_mode or "")

    async def _on_mode_change(self, mode: str) -> None:
        """SDR mode changed -> update rig and WaveLog."""
        self.current_mode = mode
        await self.rig.set_mode(mode)
        await self.wave.broadcast_status(self.current_frequency or 0, mode)

    async def _on_audio_mute_change(self, value: str) -> None:
        """SDR mute state changed (possibly by user)."""
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

    async def _on_signal_power(self, value: float) -> None:
        """SDR signal power update -> refresh S-meter."""
        self.smeter.update_power(value)

    # ========== Rig state monitoring ==========

    async def _monitor_rig_state(self) -> None:
        """Poll rig for frequency and mode changes, update SDR."""
        logging.info("[RIG] Rig state monitor started")

        while True:
            try:
                freq = await self.rig.get_frequency()
                mode = await self.rig.get_mode()

                if freq is not None and freq != self._last_rig_frequency:
                    logging.info(f"[RIG] Frequency changed -> {freq}")
                    self._last_rig_frequency = freq
                    self.current_frequency = freq
                    await self.sdr.set_frequency(freq)
                    await self.wave.broadcast_status(freq, self.current_mode or "")

                if mode and mode != self._last_rig_mode:
                    logging.info(f"[RIG] Mode changed -> {mode}")
                    self._last_rig_mode = mode
                    self.current_mode = mode
                    await self.sdr.set_mode(mode)
                    await self.wave.broadcast_status(self.current_frequency or 0, mode)

            except Exception as e:
                logging.warning(f"[RIG] Monitor error: {e}")

            await asyncio.sleep(settings.POLL_INTERVAL)

    # ========== TX monitoring and muting ==========

    async def _monitor_tx(self) -> None:
        """Poll TX status and control SDR mute accordingly."""
        while True:
            tx_status = await self.rig.get_tx()
            tx = (tx_status == "1")
            self.tx = tx

            if tx != self.last_tx:
                if tx:  # TX started
                    if self.muting_enabled and not self.manual_override:
                        await self.sdr.set_audio_mute(True)
                        self.auto_muted = True
                        logging.info("[SDR] Program muted for TX")
                else:   # TX ended
                    if self.muting_enabled and self.auto_muted and not self.manual_override:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                        logging.info("[SDR] Program unmuted after TX")

                self.last_tx = tx

            await asyncio.sleep(settings.POLL_INTERVAL)

    # ========== Console commands ==========

    async def _keyboard_listener(self) -> None:
        """Listen for console commands: 'm', 'tune', 'ptt'."""
        logging.info("[KEY] Commands: 'm' = toggle mute | 'tune' = carrier | 'ptt' = toggle CapsLock PTT")

        while True:
            try:
                cmd = (await asyncio.to_thread(input)).strip().lower()

                if cmd == "m":
                    self.muting_enabled = not self.muting_enabled
                    if not self.muting_enabled:
                        await self.sdr.set_audio_mute(False)
                        self.auto_muted = False
                        logging.info("[KEY] Automatic muting DISABLED")
                    else:
                        logging.info("[KEY] Automatic muting ENABLED")

                elif cmd == "tune":
                    asyncio.create_task(self._tune())

                elif cmd == "ptt":
                    self.capslock_ptt_enabled = not self.capslock_ptt_enabled
                    logging.info(f"[PTT] CapsLock PTT {'ENABLED' if self.capslock_ptt_enabled else 'DISABLED'}")

            except Exception as e:
                logging.warning(f"[KEY] Keyboard listener error: {e}")

    # ========== Tune function ==========

    async def _tune(self) -> None:
        """Transmit FM carrier for tune_duration seconds, then restore."""
        if self._tuning:
            logging.info("[TUNE] Already tuning")
            return

        self._tuning = True
        try:
            logging.info("[TUNE] Starting carrier")
            original_mode = self.current_mode or "FM"
            prev_muting = self.muting_enabled
            self.muting_enabled = False

            await self.rig.set_mode("FM")
            await self.rig.send_command("T 1")   # TX on

            await asyncio.sleep(settings.TUNE_DURATION)

            await self.rig.send_command("T 0")   # TX off
            await self.rig.set_mode(original_mode)

            self.muting_enabled = prev_muting
            logging.info("[TUNE] Carrier finished")

        except Exception as e:
            logging.warning(f"[TUNE] Error: {e}")
        finally:
            self._tuning = False

    # ========== CapsLock PTT (blocking keyboard thread) ==========

    def _capslock_ptt_listener(self) -> None:
        """Listen for CapsLock key presses in a background thread."""
        logging.info("[PTT] CapsLock PTT listener started (disabled by default)")

        def ptt_down(_) -> None:
            if not self.capslock_ptt_enabled or self._ptt_pressed:
                return
            self._ptt_pressed = True
            try:
                if self.rig.is_connected():
                    # Use asyncio.run_coroutine_threadsafe if needed,
                    # but here we run the command synchronously in thread.
                    asyncio.run_coroutine_threadsafe(
                        self.rig.send_command("T 1"), asyncio.get_running_loop()
                    )
                    logging.info("[PTT] TX ON")
            except Exception as err:
                logging.warning(f"[PTT] TX start failed: {err}")

        def ptt_up(_) -> None:
            if not self.capslock_ptt_enabled or not self._ptt_pressed:
                return
            self._ptt_pressed = False
            try:
                if self.rig.is_connected():
                    asyncio.run_coroutine_threadsafe(
                        self.rig.send_command("T 0"), asyncio.get_running_loop()
                    )
                    logging.info("[PTT] TX OFF")
            except Exception as err:
                logging.warning(f"[PTT] TX stop failed: {err}")

        keyboard.on_press_key("caps lock", ptt_down, suppress=True)
        keyboard.on_release_key("caps lock", ptt_up, suppress=True)
        # Keep thread alive
        threading.Event().wait()