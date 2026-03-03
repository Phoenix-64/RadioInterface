import socket
import asyncio
import websockets
import json
import time

# RIGCTLD config
RIG_HOST = "127.0.0.1"
RIG_PORT = 4532
POLL_INTERVAL = 0.5  # seconds

# SDRconnect WebSocket config
SDR_WS_URL = "ws://127.0.0.1:5454/"
AUDIO_MUTE_PROP = "audio_mute"

# ---------------- RIGCTLD ----------------

def rig_send_command(sock, cmd):
    """Send a command and return the stripped response"""
    sock.sendall((cmd + "\n").encode())
    raw = sock.recv(1024)
    return raw.decode().strip()

def rig_get_tx(sock):
    """Return TX status from rigctld (usually '0' for RX, '1' for TX)"""
    resp = rig_send_command(sock, "t")
    return resp.strip()

# ---------------- SDRCONNECT ----------------

async def set_sdr_mute(websocket, enable: bool):
    """Set SDRconnect audio mute/unmute"""
    value = "true" if enable else "false"
    msg = json.dumps({
        "event_type": "set_property",
        "property": AUDIO_MUTE_PROP,
        "value": value
    })
    await websocket.send(msg)
    print(f"[SDR] Audio mute set to {value}")

# ---------------- MAIN LOOP ----------------

async def monitor_tx():
    try:
        # Connect to SDRconnect WebSocket
        async with websockets.connect(SDR_WS_URL) as ws:
            print(f"[SDR] Connected to {SDR_WS_URL}")

            # Connect to rigctld TCP socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((RIG_HOST, RIG_PORT))
                print(f"[RIG] Connected to {RIG_HOST}:{RIG_PORT}")

                last_tx = None

                while True:
                    tx_status = rig_get_tx(sock)
                    if tx_status not in ["0", "1"]:
                        print(f"[RIG] Unexpected TX value: {tx_status}")
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    tx = tx_status == "1"  # True if transmitting

                    # Only update SDRconnect if status changed
                    if tx != last_tx:
                        await set_sdr_mute(ws, tx)
                        last_tx = tx

                    await asyncio.sleep(POLL_INTERVAL)

    except Exception as e:
        print("[ERROR]", e)
        print("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)
        await monitor_tx()

if __name__ == "__main__":
    asyncio.run(monitor_tx())