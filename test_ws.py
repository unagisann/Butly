import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://localhost:8000/ws") as ws:
        await ws.send(json.dumps({"type": "chat_message", "payload": "こんにちは"}))
        print(await ws.recv())
        print(await ws.recv())

asyncio.run(test())
