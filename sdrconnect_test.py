#!/usr/bin/env python3
"""
Continuous poller and setter for SDRconnect WebSocket API.
Connects to ws://127.0.0.1:5454/, prints frequency and mode every second,
and periodically changes frequency and mode in a loop:
- frequency toggles between 7 MHz and 8 MHz
- mode cycles LSB, USB, NFM, CW (mapped from LSB, USB, FM, CW)
"""

import asyncio
import websockets
import json
import datetime
import time

WS_URL = "ws://127.0.0.1:5454/"
POLL_INTERVAL = 1.0       # seconds between get_property requests
CHANGE_INTERVAL = 5.0     # seconds between setting changes

# Properties we are interested in
FREQ_PROP = "device_vfo_frequency"
MODE_PROP = "demodulator"

# Frequency toggling (in Hz as string)
FREQ_1 = "7000000"
FREQ_2 = "8000000"
current_freq_target = FREQ_1

# Mode cycling (SDRconnect mode names)
# Mapping from our conceptual modes (LSB, USB, FM, CW) to SDRconnect demodulator values
MODES = ["LSB", "USB", "NFM", "CW"]   # FM -> NFM
mode_index = 0

# Global variables to hold latest known values
current_freq = None
current_mode = None

async def send_get_property(websocket, prop):
    """Send a get_property request for the given property."""
    msg = json.dumps({"event_type": "get_property", "property": prop})
    await websocket.send(msg)

async def set_frequency(websocket, freq):
    """Set VFO frequency in SDRconnect."""
    msg = json.dumps({
        "event_type": "set_property",
        "property": FREQ_PROP,
        "value": freq
    })
    await websocket.send(msg)
    print(f"[SDR] Set frequency to {freq} Hz")

async def set_mode(websocket, mode):
    """Set demodulator mode in SDRconnect."""
    msg = json.dumps({
        "event_type": "set_property",
        "property": MODE_PROP,
        "value": mode
    })
    await websocket.send(msg)
    print(f"[SDR] Set mode to {mode}")

async def poll_properties(websocket):
    """Background task: send get_property requests every POLL_INTERVAL seconds."""
    while True:
        try:
            await send_get_property(websocket, FREQ_PROP)
            await send_get_property(websocket, MODE_PROP)
            await asyncio.sleep(POLL_INTERVAL)
        except websockets.exceptions.ConnectionClosed:
            break
        except Exception as e:
            print(f"Polling error: {e}")
            break

async def periodic_setter(websocket):
    """Background task: periodically change frequency and mode."""
    global current_freq_target, mode_index
    while True:
        try:
            await asyncio.sleep(CHANGE_INTERVAL)

            # Toggle frequency
            current_freq_target = FREQ_2 if current_freq_target == FREQ_1 else FREQ_1
            await set_frequency(websocket, current_freq_target)

            # Cycle mode
            mode = MODES[mode_index]
            await set_mode(websocket, mode)
            mode_index = (mode_index + 1) % len(MODES)

        except websockets.exceptions.ConnectionClosed:
            break
        except Exception as e:
            print(f"Setter error: {e}")
            break

def print_status():
    """Print current frequency and mode if both are known."""
    if current_freq is not None and current_mode is not None:
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{timestamp}] Freq: {current_freq} Hz | Mode: {current_mode}")

async def handle_messages(websocket):
    """Process incoming messages (text)."""
    global current_freq, current_mode
    async for message in websocket:
        if isinstance(message, bytes):
            continue  # ignore binary (spectrum, audio, IQ)

        try:
            data = json.loads(message)
            event_type = data.get("event_type")
            prop = data.get("property")
            value = data.get("value")

            if event_type in ("get_property_response", "property_changed"):
                if prop == FREQ_PROP:
                    current_freq = value
                    print_status()
                elif prop == MODE_PROP:
                    current_mode = value
                    print_status()
        except json.JSONDecodeError:
            print(f"Invalid JSON: {message}")

async def run():
    """Main coroutine: connect and manage the WebSocket session."""
    while True:
        try:
            async with websockets.connect(WS_URL) as websocket:
                print(f"Connected to {WS_URL}")

                # Start background tasks
                poll_task = asyncio.create_task(poll_properties(websocket))
                setter_task = asyncio.create_task(periodic_setter(websocket))

                # Process incoming messages until connection closes
                await handle_messages(websocket)

                # If we get here, the connection was closed
                poll_task.cancel()
                setter_task.cancel()
                print("Disconnected. Reconnecting...")

        except (ConnectionRefusedError, OSError, websockets.exceptions.WebSocketException) as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
        except KeyboardInterrupt:
            print("\nExiting.")
            break

if __name__ == "__main__":
    asyncio.run(run())