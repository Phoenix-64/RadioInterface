import asyncio
import websockets
import json
FREQ_PROP = "device_vfo_frequency"
MODE_PROP = "demodulator"
async def listen():
    uri = "ws://127.0.0.1:5454/"

    try:
        async with websockets.connect(uri) as websocket:
            print(f"Connected to {uri}")
            msg = json.dumps({
                "event_type": "iq_stream_enable",
                "value": "true"
            })
            await websocket.send(msg)
            while True:
                message = await websocket.recv()
                #data = json.loads(message)
                # = data.get("event_type")
                #prop = data.get("property")
                #value = data.get("value")
                print(f"Received: {message}")
                #if event_type == "property_changed" and prop == FREQ_PROP:
                    #print(f'Frequency changed: {value}')
                #elif event_type == "property_changed" and prop == MODE_PROP:
                    #print(f'Mode changed: {value}')

    except Exception as e:
        print(f"Connection error: {e}")


if __name__ == "__main__":
    asyncio.run(listen())