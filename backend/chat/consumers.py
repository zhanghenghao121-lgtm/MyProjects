import json
from urllib.parse import parse_qs, urlparse

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from chat.models import ChatMessage
from users.models import AuthToken


ROOM_GROUP = 'chat_room_global'


def _public_base_url():
    return getattr(settings, 'PUBLIC_BACKEND_BASE_URL', 'http://localhost:8000').rstrip('/')


def _abs_url(url: str) -> str:
    if not url:
        return ''
    if url.startswith('http://') or url.startswith('https://'):
        return url
    if not url.startswith('/'):
        url = '/' + url
    return _public_base_url() + url


def _is_safe_image_url(url: str) -> bool:
    raw = (url or '').strip()
    if not raw:
        return False
    if raw.startswith('/media/'):
        return True
    parsed = urlparse(raw)
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        token = self._extract_token()
        user = await self._get_user_by_token(token)
        if not user:
            await self.close(code=4001)
            return

        self.user = user
        self.user_id = user.id
        self.animerole = (getattr(user, 'animerole', '') or 'npc').strip() or 'npc'
        self.username = user.username
        self.avatar_url = _abs_url(user.avatar.url) if getattr(user, 'avatar', None) else ''

        await self.channel_layer.group_add(ROOM_GROUP, self.channel_name)
        await self.accept()

        await self.send(text_data=json.dumps({
            'type': 'connected',
            'user': {
                'id': self.user_id,
                'username': self.username,
                'animerole': self.animerole,
                'avatar_url': self.avatar_url,
            },
        }))

        await self.channel_layer.group_send(
            ROOM_GROUP,
            {
                'type': 'chat.system',
                'actor_id': self.user_id,
                'content': f'{self.animerole}已进入聊天室',
            },
        )

    async def disconnect(self, close_code):
        if getattr(self, 'user_id', None):
            await self.channel_layer.group_send(
                ROOM_GROUP,
                {
                    'type': 'chat.system',
                    'actor_id': self.user_id,
                    'content': f'{self.animerole}已离开聊天室',
                },
            )
            await self.channel_layer.group_discard(ROOM_GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        action = (payload.get('action') or '').strip()
        if action == 'send_text':
            content = (payload.get('content') or '').strip()
            reply_preview = self._clean_reply_preview(payload.get('reply_preview'))
            if not content:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'code': 'empty_message',
                    'message': '消息内容不能为空',
                }))
                return
            if len(content) > 1000:
                content = content[:1000]
            msg = await self._create_text_message(content, reply_preview)
            await self.channel_layer.group_send(
                ROOM_GROUP,
                {'type': 'chat.message', 'message': msg},
            )
            return

        if action == 'send_image':
            image_url = (payload.get('image_url') or '').strip()
            reply_preview = self._clean_reply_preview(payload.get('reply_preview'))
            if not image_url:
                return
            if len(image_url) > 1000:
                return
            if not _is_safe_image_url(image_url):
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'code': 'invalid_image_url',
                    'message': '图片地址不合法',
                }))
                return
            msg = await self._create_image_message(image_url, reply_preview)
            await self.channel_layer.group_send(
                ROOM_GROUP,
                {'type': 'chat.message', 'message': msg},
            )
            return

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
        }))

    async def chat_system(self, event):
        # Join/leave notices are shown to other users only.
        if event.get('actor_id') == getattr(self, 'user_id', None):
            return
        await self.send(text_data=json.dumps({
            'type': 'system_message',
            'message': {
                'id': None,
                'type': ChatMessage.TYPE_SYSTEM,
                'content': event.get('content') or '',
                'image_url': '',
                'created_at': '',
                'user': {
                    'id': None,
                    'username': '',
                    'animerole': '',
                    'avatar_url': '',
                },
            },
        }))

    def _clean_reply_preview(self, value):
        text = (value or '').strip()
        if len(text) > 120:
            text = text[:120]
        return text

    def _extract_token(self):
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        params = parse_qs(query_string)
        return (params.get('token') or [''])[0].strip()

    @database_sync_to_async
    def _get_user_by_token(self, token):
        if not token:
            return None
        try:
            return AuthToken.objects.select_related('user').get(key=token).user
        except AuthToken.DoesNotExist:
            return None

    @database_sync_to_async
    def _create_text_message(self, content, reply_preview=''):
        msg = ChatMessage.objects.create(
            user=self.user,
            animerole=self.animerole,
            message_type=ChatMessage.TYPE_TEXT,
            content=content,
            reply_preview=reply_preview,
        )
        return self._serialize_message(msg)

    @database_sync_to_async
    def _create_image_message(self, image_url, reply_preview=''):
        msg = ChatMessage.objects.create(
            user=self.user,
            animerole=self.animerole,
            message_type=ChatMessage.TYPE_IMAGE,
            image_url=image_url,
            reply_preview=reply_preview,
        )
        return self._serialize_message(msg)

    def _serialize_message(self, msg: ChatMessage):
        image_url = msg.image_url or ''
        if msg.image:
            image_url = _abs_url(msg.image.url)
        return {
            'id': msg.id,
            'type': msg.message_type,
            'content': msg.content or '',
            'image_url': _abs_url(image_url),
            'reply_preview': msg.reply_preview or '',
            'created_at': msg.created_at.isoformat(),
            'user': {
                'id': msg.user_id,
                'username': self.username,
                'animerole': msg.animerole or 'npc',
                'avatar_url': self.avatar_url,
            },
        }
