import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from chat.models import ChatMessage
from users.models import AuthToken

User = get_user_model()


def _get_user_from_bearer(request):
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        token_key = auth_header.split(' ', 1)[1].strip()
        if not token_key:
            return None
        try:
            return AuthToken.objects.select_related('user').get(key=token_key).user
        except AuthToken.DoesNotExist:
            return None
    return None


def _serialize_message(request, msg: ChatMessage):
    avatar_url = ''
    if msg.user and getattr(msg.user, 'avatar', None):
        try:
            avatar_url = request.build_absolute_uri(msg.user.avatar.url)
        except Exception:
            avatar_url = ''
    image_url = msg.image_url or ''
    if msg.image:
        image_url = request.build_absolute_uri(msg.image.url)
    return {
        'id': msg.id,
        'type': msg.message_type,
        'content': msg.content or '',
        'image_url': image_url,
        'created_at': msg.created_at.isoformat(),
        'user': {
            'id': msg.user_id,
            'username': msg.user.username if msg.user else '',
            'animerole': msg.animerole or 'npc',
            'avatar_url': avatar_url,
        },
    }


@api_view(['GET'])
def room_meta(request):
    return Response({'title': '章鱼聊天室'})


@api_view(['GET'])
def room_history(request):
    user = _get_user_from_bearer(request) or (request.user if request.user.is_authenticated else None)
    if not user:
        return Response({'msg': '未登录'}, status=401)
    latest = list(
        ChatMessage.objects.select_related('user').order_by('-created_at')[:50]
    )
    latest.reverse()
    return Response({'messages': [_serialize_message(request, m) for m in latest]})


@api_view(['GET'])
def room_history_days(request):
    user = _get_user_from_bearer(request) or (request.user if request.user.is_authenticated else None)
    if not user:
        return Response({'msg': '未登录'}, status=401)
    days = request.query_params.get('days', '3')
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 3
    days = min(max(days, 1), 7)
    start_time = timezone.now() - timedelta(days=days)
    rows = (
        ChatMessage.objects.select_related('user')
        .filter(created_at__gte=start_time)
        .order_by('created_at')
    )
    return Response({'messages': [_serialize_message(request, m) for m in rows]})


@api_view(['POST'])
def upload_chat_image(request):
    user = _get_user_from_bearer(request) or (request.user if request.user.is_authenticated else None)
    if not user:
        return Response({'msg': '未登录'}, status=401)
    image = request.FILES.get('image')
    if not image:
        return Response({'msg': '请选择图片'}, status=400)
    if image.size > 5 * 1024 * 1024:
        return Response({'msg': '图片大小不能超过5MB'}, status=400)
    content_type = (getattr(image, 'content_type', '') or '').lower()
    if not content_type.startswith('image/'):
        return Response({'msg': '仅支持图片文件'}, status=400)

    ext = (image.name.rsplit('.', 1)[-1] if '.' in image.name else 'png').lower()
    file_name = f"chat/images/{uuid.uuid4().hex}.{ext}"
    stored_path = default_storage.save(file_name, image)
    image_url = request.build_absolute_uri(default_storage.url(stored_path))
    return Response({'image_url': image_url})
