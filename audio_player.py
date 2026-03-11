import asyncio
import json
import numpy as np
import sounddevice as sd
import websockets

SDRCONNECT_HOST = "127.0.0.1"
SDRCONNECT_PORT = 5454       # SDRconnect WebSocket port
SAMPLE_RATE = 48000          # match SDRconnect IQ sample rate

# --- List audio devices ---
print("Available audio output devices:")
for i, dev in enumerate(sd.query_devices()):
    if dev['max_output_channels'] > 0:
        print(f"{i}: {dev['name']} ({dev['max_output_channels']} channels)")

device_index = int(input("Select output device index: "))

# --- Configure SDRconnect sequence ---
SELECTED_DEVICE_INDEX = 0  # change if multiple devices

async def main():
    uri = f"ws://{SDRCONNECT_HOST}:{SDRCONNECT_PORT}"
    async with websockets.connect(uri) as ws:
        print("Connected to SDRconnect")

        # 3️⃣ Enable IQ streaming
        msg = json.dumps({
            "event_type": "iq_stream_enable",
            "value": "true"
        })
        await ws.send(msg)
        print("Enabled IQ streaming")

        # --- Open audio output (I=left, Q=right) ---
        stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=2,
            dtype="int16",
            device=device_index
        )
        stream.start()
        print("Audio stream started")

        async for message in ws:
            if isinstance(message, bytes):

                # First 2 bytes = payload type
                payload_type = int.from_bytes(message[:2], 'little')
                if payload_type != 2:  # 2 = IQ stream
                    continue

                iq_data = np.frombuffer(message[2:], dtype=np.int16)
                print(f"Received IQ samples: {len(iq_data)}")
                print(f"First 8 samples: {iq_data[:8]}")

                if len(iq_data) % 2 != 0:
                    iq_data = iq_data[:-1]
                stereo = iq_data.reshape(-1, 2)
                stream.write(stereo)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")