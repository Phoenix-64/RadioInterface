"""RigCtl client for communicating with rigctld."""

import logging
import re
import socket
import asyncio
from typing import Optional


class RigCtl:
    """TCP client for rigctld (hamlib)."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish connection to rigctld."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5)
            # Run blocking connect in thread
            await asyncio.to_thread(self._sock.connect, (self.host, self.port))
            self._sock.settimeout(None)
            logging.info(f"[RIG] Connected to {self.host}:{self.port}")
        except Exception as e:
            self._sock = None
            logging.warning(f"[RIG] Connection failed: {e}")
            raise

    def is_connected(self) -> bool:
        """Return True if socket is connected."""
        return self._sock is not None

    async def send_command(self, cmd: str) -> str:
        """
        Send a command to rigctld and return the response.

        Raises:
            RuntimeError: if not connected.
            ConnectionError: if the connection is lost.
        """
        if not self._sock:
            raise RuntimeError("RIG socket not connected")

        try:
            async with self._lock:
                # Send command
                await asyncio.to_thread(self._sock.sendall, (cmd + "\n").encode())

                # Read until newline
                data = b""
                while True:
                    chunk = await asyncio.to_thread(self._sock.recv, 4096)
                    if not chunk:
                        raise ConnectionError("RIG connection lost")
                    data += chunk
                    if b"\n" in chunk:
                        break

                return data.decode(errors="ignore").strip()

        except Exception as e:
            logging.warning(f"[RIG] Connection lost: {e}")
            self._close_socket()
            raise

    def _close_socket(self) -> None:
        """Close the socket and set to None."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ========== High-level commands ==========

    async def set_frequency(self, freq: int) -> None:
        """Set VFO frequency in Hz."""
        resp = await self.send_command(f"F {freq}")
        logging.info(f"[RIG] Set frequency to {freq} Hz -> {resp}")

    async def set_mode(self, mode: str) -> None:
        """Set operating mode (normalizes for rigctld)."""
        # Some rigs expect FM instead of NFM etc.
        if mode == "NFM":
            mode = "FM"
        elif mode == "LSB":
            mode = "PKTLSB"
        elif mode == "USB":
            mode = "PKTUSB"
        resp = await self.send_command(f"M {mode} 0")
        logging.info(f"[RIG] Set mode to {mode} -> {resp}")

    async def get_tx(self) -> str:
        """Return TX status: '0' for RX, '1' for TX."""
        return await self.send_command("t")

    async def get_frequency(self) -> Optional[int]:
        """Query current frequency, return Hz or None."""
        try:
            resp = await self.send_command("f")
        except Exception as e:
            logging.warning(f"[RIG] Failed to query frequency: {e}")
            return None

        if not resp:
            logging.warning("[RIG] Empty response when querying frequency")
            return None

        # Find first integer in response
        match = re.search(r"(\d+)", resp)
        if match:
            try:
                freq = int(match.group(1))
                logging.debug(f"[RIG] Queried frequency -> {freq} (raw='{resp}')")
                return freq
            except ValueError:
                pass

        logging.warning(f"[RIG] Couldn't parse frequency from response: '{resp}'")
        return None

    async def get_mode(self) -> Optional[str]:
        """Query current mode, return normalized string or None."""
        try:
            resp = await self.send_command("m")
        except Exception as e:
            logging.warning(f"[RIG] Failed to query mode: {e}")
            return None

        if not resp:
            logging.warning("[RIG] Empty response when querying mode")
            return None

        raw = resp.strip().upper()
        known_modes = ["AM", "CW", "LSB", "USB", "RTTY", "FM", "FMW", "PKTLSB", "PKTUSB"]

        for token in known_modes:
            if token in raw:
                normalized = self._normalize_mode(token)
                logging.debug(f"[RIG] Queried mode -> {normalized} (raw='{resp}')")
                return normalized

        # Fallback: extract alphabetic token
        match = re.search(r"([A-Z]+)", raw)
        if match:
            normalized = self._normalize_mode(match.group(1))
            logging.debug(f"[RIG] Queried mode (fallback) -> {normalized} (raw='{resp}')")
            return normalized

        logging.warning(f"[RIG] Couldn't parse mode from response: '{resp}'")
        return None

    @staticmethod
    def _normalize_mode(raw: str) -> str:
        """Normalize various mode tokens to a small canonical set."""
        if raw is None:
            return ""
        m = raw.strip().upper()
        if m == "NFM":
            return "FM"
        if m in ("DIGU", "DIGL"):
            return "DATA"
        return m