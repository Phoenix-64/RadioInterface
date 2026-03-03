import asyncio
import websockets
import json
import time

PORT = 54322

# Your current radio state
FREQUENCY = 7155000
MODE = "CW"
POWER = 30


async def handle_client(websocket):
    print("Browser connected")

    # Send welcome message (WaveLog expects it)
    await websocket.send(json.dumps({
        "type": "welcome",
        "message": "Connected to Python WaveLog bridge"
    }))

    try:
        while True:
            message = {
                "type": "radio_status",
                "radio": True,
                "frequency": FREQUENCY,
                "mode": MODE,
                "power": POWER,
                "split": False,
                "vfoB": None,
                "modeB": None,
                "timestamp": int(time.time() * 1000)
            }

            await websocket.send(json.dumps(message))
            print("Sent:", message)

            await asyncio.sleep(2)  # send update every 2 sec

    except websockets.exceptions.ConnectionClosed:
        print("Browser disconnected")


async def main():
    print(f"Starting WebSocket server on ws://localhost:{PORT}")
    async with websockets.serve(handle_client, "localhost", PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())