import json
from channels.generic.websocket import AsyncWebsocketConsumer

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        await self.send(text_data=json.dumps({"type": "connected"}))

    async def receive(self, text_data=None, bytes_data=None):
        # echo
        await self.send(text_data=text_data or "")

    async def disconnect(self, close_code):
        pass
