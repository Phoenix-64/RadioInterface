"""WebSocket server for WaveLog browser client."""

import asyncio
import json
import logging
import time
from typing import Set

import websockets
from websockets import WebSocketServerProtocol


class WaveLogServer:
    """Simple WebSocket server that broadcasts radio status to browsers."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._clients: Set[WebSocketServerProtocol] = set()

    async def start(self) -> None:
        """Start the WebSocket server (runs forever)."""
        logging.info(f"[WaveLog] Starting WebSocket server on ws://localhost:{self.port}")
        async with websockets.serve(self._handle_client, "localhost", self.port):
            await asyncio.Future()  # run forever

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a new client connection."""
        self._clients.add(websocket)
        logging.info("[WaveLog] Browser connected")
        try:
            await websocket.send(json.dumps({
                "type": "welcome",
                "message": "Connected to Python CAT bridge"
            }))
            await websocket.wait_closed()
        finally:
            self._clients.remove(websocket)
            logging.info("[WaveLog] Browser disconnected")

    async def broadcast_status(self, frequency: int, mode: str) -> None:
        """Send current radio status to all connected clients."""
        if not self._clients:
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

        dead = set()
        for ws in self._clients:
            try:
                await ws.send(json.dumps(message))
            except Exception:
                dead.add(ws)

        self._clients.difference_update(dead)
        logging.info(f"[WaveLog] Broadcast: {message}")