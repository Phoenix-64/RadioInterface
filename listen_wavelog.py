import asyncio
import json
import logging
import time
import websockets

logging.basicConfig(level=logging.INFO)


class WaveLogServer:
    def __init__(self, port: int):
        self.port = port
        self.clients: set = set()

    async def start(self) -> None:
        logging.info(f"[WaveLog] Starting WebSocket server on ws://localhost:{self.port}")
        async with websockets.serve(self.handle_client, "localhost", self.port):
            await asyncio.Future()  # run forever

    async def handle_client(self, websocket, path=None) -> None:
        self.clients.add(websocket)
        logging.info("[WaveLog] Client connected")

        await websocket.send(json.dumps({
            "type": "welcome",
            "message": "Connected to Python CAT bridge"
        }))

        try:
            async for message in websocket:
                logging.info(f"[WaveLog] RX RAW: {message}")

                try:
                    data = json.loads(message)
                    logging.info(f"[WaveLog] RX JSON: {data}")
                except json.JSONDecodeError:
                    logging.warning("[WaveLog] RX not valid JSON")

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)
            logging.info("[WaveLog] Client disconnected")

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
            except Exception:
                dead_clients.add(ws)

        self.clients.difference_update(dead_clients)

        logging.info(f"[WaveLog] Broadcast: {message}")


async def main():
    server = WaveLogServer(port=54322)
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())