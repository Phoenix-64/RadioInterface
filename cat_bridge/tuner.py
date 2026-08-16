"""HTTP server for receiving Wavelog frequency tune requests."""

import asyncio
import logging
import re
from typing import Callable, Awaitable


class WavelogTuneServer:
    """
    Minimal HTTP server that handles Wavelog cluster tune callbacks.

    Wavelog sends an OPTIONS preflight (CORS), then:
        GET /{frequency_hz}/{mode} HTTP/1.1
    e.g. GET /7010600/cw HTTP/1.1
    """

    _CORS = (
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Methods: GET, OPTIONS\r\n"
        "Access-Control-Allow-Headers: *\r\n"
        "Access-Control-Allow-Private-Network: true\r\n"
    )

    def __init__(
        self,
        port: int,
        on_tune: Callable[[int, str], Awaitable[None]],
    ) -> None:
        self.port = port
        self._on_tune = on_tune

    async def start(self) -> None:
        server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.port
        )
        logging.info(f"[TUNE] Listening on http://127.0.0.1:{self.port}")
        async with server:
            await server.serve_forever()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.read(4096)
            if not raw:
                return

            first_line = raw.decode(errors="ignore").split("\r\n")[0]
            parts = first_line.split()
            if len(parts) < 2:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                return

            method, path = parts[0], parts[1]

            if method == "OPTIONS":
                writer.write(("HTTP/1.1 204 No Content\r\n" + self._CORS + "\r\n").encode())
                await writer.drain()
                return

            if method == "GET":
                match = re.match(r"^/(\d+)/([a-zA-Z0-9]+)", path)
                if match:
                    freq = int(match.group(1))
                    mode = match.group(2).upper()
                    logging.info(f"[TUNE] Cluster spot: {freq} Hz / {mode}")
                    try:
                        await self._on_tune(freq, mode)
                    except Exception as e:
                        logging.warning(f"[TUNE] Callback error: {e}")
                else:
                    logging.warning(f"[TUNE] Unrecognised path: {path}")

                writer.write(
                    ("HTTP/1.1 200 OK\r\n" + self._CORS + "Content-Length: 0\r\n\r\n").encode()
                )
                await writer.drain()
                return

            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()

        except Exception as e:
            logging.warning(f"[TUNE] Handler error: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass