#!/usr/bin/env python3
"""
Bidirectional synchroniser between rigctld and SDRconnect for frequency and mode.
- Single persistent connection to rigctld, used for both polling and setting (with a lock).
- Single persistent WebSocket to SDRconnect, used for both listening and sending.
- Polls rigctld every second: sends 'f', reads; sends 'm', reads (consumes extra passband line).
- Listens for SDRconnect property changes (push notifications).
- Propagates changes to the other side.
- Uses last_sent_* variables to avoid echo loops (same method as frequency).
- Uses default passbands (2400 for SSB, 5000 for FM, 500 for CW) when setting rig mode.
"""

import asyncio
import socket
import json
from typing import Optional

import websockets

# ----------------------------------------------------------------------
RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532
SDR_WS_URL = "ws://127.0.0.1:5454/"
POLL_INTERVAL = 1.0  # seconds

# Mode mapping between Hamlib (rigctld) and SDRconnect
HAMLIB_TO_SDR = {
    "USB": "USB",
    "LSB": "LSB",
    "AM": "AM",
    "CW": "CW",
    "CWR": "CW",          # map CWR to CW
    "FM": "NFM",           # assume narrow FM (adjust as needed)
    "WFM": "WFM",
    "SAM": "SAM",          # synchronous AM
    "NFM": "NFM",          # already NFM
}
SDR_TO_HAMLIB = {v: k for k, v in HAMLIB_TO_SDR.items()}
SDR_TO_HAMLIB["NFM"] = "FM"
SDR_TO_HAMLIB["WFM"] = "WFM"
SDR_TO_HAMLIB["SAM"] = "SAM"
SDR_TO_HAMLIB["USB"] = "USB"
SDR_TO_HAMLIB["LSB"] = "LSB"
SDR_TO_HAMLIB["AM"] = "AM"
SDR_TO_HAMLIB["CW"] = "CW"

# Default passbands for rigctld mode set commands (matching the test program)
DEFAULT_PASSBAND = {
    "USB": 2400,
    "LSB": 2400,
    "AM": 6000,
    "CW": 500,
    "FM": 5000,
    "WFM": 150000,
    "SAM": 6000,
    "NFM": 5000,
}

# Property names in SDRconnect
SDR_FREQ_PROP = "device_vfo_frequency"
SDR_MODE_PROP = "demodulator"

# ----------------------------------------------------------------------
# Shared registers (protected by lock)
state_lock = asyncio.Lock()

rig_freq: Optional[str] = None          # last known rig frequency (digits)
rig_mode: Optional[str] = None          # last known rig mode (e.g. "USB")

sdr_freq: Optional[str] = None          # last known SDR frequency
sdr_mode: Optional[str] = None          # last known SDR mode

# Last sent values to avoid echo
last_sent_to_rig_freq: Optional[str] = None
last_sent_to_rig_mode: Optional[str] = None
last_sent_to_sdr_freq: Optional[str] = None
last_sent_to_sdr_mode: Optional[str] = None

# Rig connection shared variables
rig_reader = None
rig_writer = None
rig_lock = asyncio.Lock()               # protects rig socket operations
rig_connected = False

# SDR connection shared variables
sdr_websocket = None
sdr_lock = asyncio.Lock()               # protects SDR sends
sdr_connected = False

# ----------------------------------------------------------------------
# Rigctld helpers (using shared connection)

async def rig_send_command(cmd: str) -> str:
    """Send a command on the shared rig socket and return the stripped response."""
    global rig_reader, rig_writer, rig_connected
    if not rig_connected or rig_writer is None:
        raise ConnectionError("Rig not connected")
    writer = rig_writer
    reader = rig_reader
    writer.write((cmd + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    raw = line.decode().strip()
    print(f"[RIG RAW] Command '{cmd}' -> '{raw}'")
    return raw

def is_valid_frequency(resp: str) -> bool:
    return resp.isdigit()

async def rig_get_frequency() -> Optional[str]:
    """Send 'f' and return frequency if valid."""
    try:
        resp = await rig_send_command("f")
        if is_valid_frequency(resp):
            return resp
        else:
            print(f"[RIG] Invalid frequency response: {resp}")
            return None
    except Exception as e:
        print(f"[RIG] Error getting frequency: {e}")
        return None

async def rig_get_mode() -> Optional[str]:
    """
    Send 'm' and return mode (first token only, ignore passband).
    rigctld may send the passband on a separate line; we consume it immediately.
    """
    global rig_reader, rig_writer, rig_connected
    if not rig_connected or rig_writer is None:
        print("[RIG] Not connected, cannot get mode")
        return None
    try:
        writer = rig_writer
        reader = rig_reader
        writer.write(b"m\n")
        await writer.drain()

        # Read first line (the mode part)
        line = await reader.readline()
        resp = line.decode().strip()
        print(f"[RIG RAW] Command 'm' -> '{resp}'")

        # Try to consume an immediate extra line (likely the passband)
        try:
            extra_line = await asyncio.wait_for(reader.readline(), timeout=0.02)
            if extra_line:
                extra = extra_line.decode().strip()
                if extra.isdigit():
                    print(f"[RIG] Consumed extra passband line: {extra}")
                else:
                    print(f"[RIG] Consumed unexpected extra line after 'm': {extra}")
        except asyncio.TimeoutError:
            # No immediate extra line — normal
            pass

        if resp.startswith("RPRT"):
            print(f"[RIG] Invalid mode response: {resp}")
            return None
        else:
            mode = resp.split()[0] if resp else None
            return mode
    except Exception as e:
        print(f"[RIG] Error getting mode: {e}")
        return None

async def rig_set_frequency(freq: str):
    """Set frequency on rig using shared connection."""
    if not is_valid_frequency(freq):
        print(f"[RIG] Not setting invalid frequency: {freq}")
        return
    try:
        resp = await rig_send_command(f"F {freq}")
        if resp != "RPRT 0":
            print(f"[RIG] Set frequency returned: {resp}")
    except Exception as e:
        print(f"[RIG] Failed to set frequency: {e}")

async def rig_set_mode(mode: str):
    """Set mode with default passband on rig using shared connection."""
    passband = DEFAULT_PASSBAND.get(mode, 0)
    try:
        resp = await rig_send_command(f"M {mode} {passband}")
        if resp != "RPRT 0":
            print(f"[RIG] Set mode returned: {resp}")
    except Exception as e:
        print(f"[RIG] Failed to set mode: {e}")

# ----------------------------------------------------------------------
# Rig poller task (manages the persistent rig connection)

async def rig_poller():
    global rig_reader, rig_writer, rig_connected, rig_freq, rig_mode
    global last_sent_to_sdr_freq, last_sent_to_sdr_mode

    while True:
        try:
            # Establish persistent connection
            reader, writer = await asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT)
            async with rig_lock:
                rig_reader = reader
                rig_writer = writer
                rig_connected = True
            print("[RIG] Connected to rigctld")

            # Initial read
            async with rig_lock:
                freq = await rig_get_frequency()
                mode = await rig_get_mode()
            async with state_lock:
                if freq is not None:
                    old_freq = rig_freq
                    rig_freq = freq
                    if old_freq != rig_freq:
                        print(f"[RIG] Initial frequency: {rig_freq}")
                if mode is not None:
                    old_mode = rig_mode
                    rig_mode = mode
                    if old_mode != rig_mode:
                        print(f"[RIG] Initial mode: {rig_mode}")

            # Polling loop
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                async with rig_lock:
                    new_freq = await rig_get_frequency()
                    new_mode = await rig_get_mode()

                async with state_lock:
                    # Frequency change?
                    if new_freq is not None and new_freq != rig_freq and new_freq != last_sent_to_sdr_freq:
                        print(f"[RIG] Frequency changed: {rig_freq} -> {new_freq}")
                        rig_freq = new_freq
                        asyncio.create_task(sdr_set_frequency(new_freq))
                        last_sent_to_sdr_freq = new_freq

                    # Mode change?
                    if new_mode is not None and new_mode != rig_mode and new_mode != last_sent_to_sdr_mode:
                        print(f"[RIG] Mode changed: {rig_mode} -> {new_mode}")
                        rig_mode = new_mode
                        sdr_mode_val = HAMLIB_TO_SDR.get(rig_mode)
                        if sdr_mode_val:
                            asyncio.create_task(sdr_set_mode(sdr_mode_val))
                            last_sent_to_sdr_mode = sdr_mode_val

        except (ConnectionRefusedError, OSError) as e:
            print(f"[RIG] Connection failed: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[RIG] Unexpected error: {e}. Reconnecting...")
            await asyncio.sleep(2)
        finally:
            async with rig_lock:
                rig_connected = False
                if rig_writer:
                    rig_writer.close()
                    await rig_writer.wait_closed()
                rig_reader = None
                rig_writer = None

# ----------------------------------------------------------------------
# SDRconnect tasks (manages persistent WebSocket)

async def sdr_set_frequency(freq: str):
    """Send set_property for frequency on shared SDR websocket."""
    if not freq.isdigit():
        print(f"[SDR] Not sending invalid frequency: {freq}")
        return
    global sdr_websocket, sdr_connected
    if not sdr_connected or sdr_websocket is None:
        print("[SDR] Not connected, cannot set frequency")
        return
    try:
        msg = json.dumps({
            "event_type": "set_property",
            "property": SDR_FREQ_PROP,
            "value": freq
        })
        print(f"[SDR RAW] Sending: {msg}")
        async with sdr_lock:
            await sdr_websocket.send(msg)
        print(f"[SDR] Set frequency = {freq}")
        # After successful send, update sdr_freq register
        async with state_lock:
            global sdr_freq
            sdr_freq = freq
    except Exception as e:
        print(f"[SDR] Failed to set frequency: {e}")

async def sdr_set_mode(mode: str):
    """Send set_property for mode on shared SDR websocket."""
    global sdr_websocket, sdr_connected
    if not sdr_connected or sdr_websocket is None:
        print("[SDR] Not connected, cannot set mode")
        return
    try:
        msg = json.dumps({
            "event_type": "set_property",
            "property": SDR_MODE_PROP,
            "value": mode
        })
        print(f"[SDR RAW] Sending: {msg}")
        async with sdr_lock:
            await sdr_websocket.send(msg)
        print(f"[SDR] Set mode = {mode}")
        async with state_lock:
            global sdr_mode
            sdr_mode = mode
    except Exception as e:
        print(f"[SDR] Failed to set mode: {e}")

async def sdr_handler():
    global sdr_websocket, sdr_connected, sdr_freq, sdr_mode
    global last_sent_to_rig_freq, last_sent_to_rig_mode

    while True:
        try:
            async with websockets.connect(SDR_WS_URL) as ws:
                async with sdr_lock:
                    sdr_websocket = ws
                    sdr_connected = True
                print("[SDR] Connected to SDRconnect")

                # Request initial values
                await ws.send(json.dumps({"event_type": "get_property", "property": SDR_FREQ_PROP}))
                await ws.send(json.dumps({"event_type": "get_property", "property": SDR_MODE_PROP}))

                # Listen for messages
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    print(f"[SDR RAW] Received: {message}")
                    try:
                        data = json.loads(message)
                        event_type = data.get("event_type")
                        prop = data.get("property")
                        value = data.get("value")

                        if event_type in ("property_changed", "get_property_response"):
                            if prop == SDR_FREQ_PROP:
                                async with state_lock:
                                    if value != sdr_freq and value != last_sent_to_rig_freq:
                                        print(f"[SDR] Frequency changed: {sdr_freq} -> {value}")
                                        sdr_freq = value
                                        asyncio.create_task(rig_set_frequency_from_sdr(value))
                                        last_sent_to_rig_freq = value

                            elif prop == SDR_MODE_PROP:
                                async with state_lock:
                                    if value != sdr_mode and value != last_sent_to_rig_mode:
                                        print(f"[SDR] Mode changed: {sdr_mode} -> {value}")
                                        sdr_mode = value
                                        hamlib_mode = SDR_TO_HAMLIB.get(value)
                                        if hamlib_mode:
                                            asyncio.create_task(rig_set_mode_from_sdr(hamlib_mode))
                                            last_sent_to_rig_mode = hamlib_mode

                    except json.JSONDecodeError:
                        print(f"[SDR] Invalid JSON: {message}")

        except (ConnectionRefusedError, OSError, websockets.exceptions.WebSocketException) as e:
            print(f"[SDR] Connection failed: {e}. Retrying in 5s...")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[SDR] Unexpected error: {e}. Reconnecting...")
            await asyncio.sleep(2)
        finally:
            async with sdr_lock:
                sdr_connected = False
                sdr_websocket = None

# Functions to set rig from SDR (use the shared rig connection with lock)
async def rig_set_frequency_from_sdr(freq: str):
    async with rig_lock:
        if rig_connected:
            await rig_set_frequency(freq)
            # Note: we do NOT update rig_freq here; let polling detect it and avoid echo via last_sent
        else:
            print("[RIG] Not connected, cannot set frequency")

async def rig_set_mode_from_sdr(mode: str):
    async with rig_lock:
        if rig_connected:
            await rig_set_mode(mode)
        else:
            print("[RIG] Not connected, cannot set mode")

# ----------------------------------------------------------------------
async def main():
    tasks = [
        asyncio.create_task(rig_poller()),
        asyncio.create_task(sdr_handler()),
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nShutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())