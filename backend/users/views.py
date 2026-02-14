
from django.contrib.auth import authenticate, login, logout
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from .models import AuthToken
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.conf import settings
import os
import random
import io
import base64
import re
from PIL import Image, ImageDraw, ImageFont
from django.middleware.csrf import get_token
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from django.utils import timezone
from datetime import timedelta

User = get_user_model()
EMAIL_CODE_TTL_SECONDS = 300
USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9\u4e00-\u9fff]+$')
PASSWORD_PATTERN = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{6,}$')
CAPTCHA_SESSION_FALLBACK = os.environ.get('CAPTCHA_SESSION_FALLBACK', 'false').lower() == 'true'


def _normalize_email(email):
    return (email or '').strip().lower()


def _email_code_key(purpose, email, username=''):
    return f"email_code:{purpose}:{username}:{email}"


def _is_valid_email(email):
    normalized = _normalize_email(email)
    if not normalized.endswith('.com'):
        return False
    try:
        validate_email(normalized)
        return True
    except ValidationError:
        return False


def _is_valid_username(username):
    return bool(USERNAME_PATTERN.fullmatch((username or '').strip()))


def _is_valid_password(password):
    return bool(PASSWORD_PATTERN.fullmatch((password or '').strip()))


def _send_email_code(purpose, email, username=''):
    code = ''.join(random.choices('0123456789', k=6))
    cache.set(_email_code_key(purpose, email, username), code, EMAIL_CODE_TTL_SECONDS)
    send_mail(
        '邮箱验证码',
        f'您的验证码是：{code}，5分钟内有效。',
        settings.DEFAULT_FROM_EMAIL,
        [email],
        fail_silently=False,
    )
    return code


def _serialize_user_profile(user, request):
    avatar_url = ''
    if user.avatar:
        avatar_url = request.build_absolute_uri(user.avatar.url)
    days_remaining = 0
    if user.username_changed_at:
        next_change = user.username_changed_at + timedelta(days=30)
        if next_change > timezone.now():
            days_remaining = (next_change - timezone.now()).days + 1
    return {
        'username': user.username,
        'email': user.email,
        'signature': user.signature or '',
        'animerole': user.animerole or 'npc',
        'avatar_url': avatar_url,
        'username_change_days_remaining': max(0, days_remaining),
    }


def _get_user_from_token(request):
    """Extract user from Bearer token, return None if missing/invalid."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        token_key = auth_header.split(' ', 1)[1]
        try:
            token_obj = AuthToken.objects.select_related('user').get(key=token_key)
            return token_obj.user
        except Exception:
            return None
    return None


#注册
@api_view(['POST'])
def register(request):
    username = (request.data.get('username') or '').strip()
    password = (request.data.get('password') or '').strip()
    email = _normalize_email(request.data.get('email'))
    email_code = (request.data.get('email_code') or '').strip()

    if not username or not password or not email or not email_code:
        return Response({'msg': '请填写完整信息'}, status=400)

    if not _is_valid_email(email):
        return Response({'msg': '邮箱格式不正确，需以 .com 结尾'}, status=400)
    if not _is_valid_username(username):
        return Response({'msg': '用户名只能包含中英文和数字，不能有空格或特殊符号'}, status=400)
    if not _is_valid_password(password):
        return Response({'msg': '密码需至少6位，且包含大小写英文和数字'}, status=400)

    if User.objects.filter(username=username).exists():
        return Response({'msg': '用户已存在'}, status=400)
    if User.objects.filter(email=email).exists():
        return Response({'msg': '邮箱已被使用'}, status=400)

    cached_code = cache.get(_email_code_key('register', email))
    if not cached_code or email_code != str(cached_code):
        return Response({'msg': '邮箱验证码错误或已过期'}, status=400)

    User.objects.create_user(
        username=username,
        password=password,
        email=email,
    )
    cache.delete(_email_code_key('register', email))
    return Response({'msg': '注册成功'})


#登录
@api_view(['POST'])
def login_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    captcha = request.data.get('captcha')
    captcha_token = request.data.get('captchaToken')

    if not _is_valid_username(username):
        return Response({'msg': '用户名格式不正确'}, status=400)
    if not _is_valid_password(password):
        return Response({'msg': '密码格式不正确'}, status=400)

    is_captcha_ok = False
    if captcha_token and captcha:
        signer = TimestampSigner()
        try:
            signed_code = signer.unsign(captcha_token, max_age=300)
            is_captcha_ok = str(captcha).strip() == str(signed_code)
        except (BadSignature, SignatureExpired):
            is_captcha_ok = False
    if not is_captcha_ok and CAPTCHA_SESSION_FALLBACK:
        session_code = request.session.get('captcha_code')
        if not session_code or not captcha or str(captcha).strip() != str(session_code):
            return Response({'msg': '验证码错误'}, status=400)
    elif not is_captcha_ok:
        return Response({'msg': '验证码错误或已过期，请刷新验证码重试'}, status=400)

    user = authenticate(username=username, password=password)
    if not user:
        return Response({'msg': '账号或密码错误'}, status=400)

    login(request, user)
    # 单用户只保留一个有效 token，先清理旧的
    AuthToken.objects.filter(user=user).delete()
    token = AuthToken.objects.create(user=user, key=AuthToken.generate_token())
    return Response({'msg': '登录成功', 'token': token.key})

#校验
@api_view(['GET'])
def profile(request):
    # SPA uses Bearer token primarily; keep session auth as fallback.
    user = _get_user_from_token(request)
    if not user and request.user.is_authenticated:
        user = request.user
    if not user:
        return Response({'msg': '未登录'}, status=401)

    if not user.email:
        user.email = f"{random.randint(100000000, 999999999)}@163.com"
        user.save(update_fields=['email'])

    return Response(_serialize_user_profile(user, request))


@api_view(['POST'])
def update_profile(request):
    user = _get_user_from_token(request)
    if not user and request.user.is_authenticated:
        user = request.user
    if not user:
        return Response({'msg': '未登录'}, status=401)

    new_username = (request.data.get('username') or '').strip()
    signature = (request.data.get('signature') or '').strip()
    animerole = (request.data.get('animerole') or '').strip()
    avatar = request.FILES.get('avatar')

    if not new_username:
        return Response({'msg': '用户名不能为空'}, status=400)
    if not _is_valid_username(new_username):
        return Response({'msg': '用户名只能包含中英文和数字，不能有空格或特殊符号'}, status=400)
    if len(signature) > 20:
        return Response({'msg': '个性签名不能超过20个字'}, status=400)
    if not animerole:
        animerole = 'npc'
    if len(animerole) > 20:
        return Response({'msg': '角色名不能超过20个字'}, status=400)
    if not USERNAME_PATTERN.fullmatch(animerole):
        return Response({'msg': '角色名只能包含中英文和数字，不能有空格或特殊符号'}, status=400)

    if avatar and avatar.size > 5 * 1024 * 1024:
        return Response({'msg': '头像大小不能超过5MB'}, status=400)

    if new_username != user.username:
        if user.username_changed_at and user.username_changed_at + timedelta(days=30) > timezone.now():
            remaining = (user.username_changed_at + timedelta(days=30) - timezone.now()).days + 1
            return Response({'msg': f'用户名每月只能修改一次，还需等待{remaining}天'}, status=400)
        if User.objects.exclude(id=user.id).filter(username=new_username).exists():
            return Response({'msg': '用户名已存在'}, status=400)
        user.username = new_username
        user.username_changed_at = timezone.now()

    user.signature = signature
    user.animerole = animerole
    if avatar:
        user.avatar = avatar
    user.save()

    return Response({'msg': '保存成功', 'profile': _serialize_user_profile(user, request)})


#退出登录
@api_view(['POST'])
def logout_view(request):
    # 删除 token
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        token_key = auth_header.split(' ', 1)[1]
        AuthToken.objects.filter(key=token_key).delete()
    logout(request)
    return Response({'msg': '退出成功'})


@api_view(['GET'])
def captcha(request):
    """生成数字验证码并以 base64 图片返回，同时将验证码存入 session。"""
    code = ''.join(random.choices('0123456789', k=4))
    # Default flow uses signed captchaToken to avoid DB session dependency.
    if CAPTCHA_SESSION_FALLBACK:
        try:
            request.session['captcha_code'] = code
        except Exception:
            pass
    signer = TimestampSigner()
    captcha_token = signer.sign(code)

    width, height = 120, 40
    image = Image.new('RGB', (width, height), (245, 246, 250))
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        font = ImageFont.load_default()

    # Pillow compatibility: textbbox may vary across versions/fonts.
    try:
        bbox = draw.textbbox((0, 0), code, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    except Exception:
        text_width, text_height = draw.textsize(code, font=font)
    x = (width - text_width) / 2
    y = (height - text_height) / 2
    draw.text((x, y), code, fill=(34, 34, 34), font=font)

    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    img_str = base64.b64encode(buffer.getvalue()).decode()

    return Response({'image': f'data:image/png;base64,{img_str}', 'captchaToken': captcha_token})


@api_view(['GET'])
def csrf(request):
    """Issue a CSRF token cookie for SPA clients."""
    return Response({'csrfToken': get_token(request)})


@api_view(['POST'])
def send_register_email_code(request):
    email = _normalize_email(request.data.get('email'))
    if not email:
        return Response({'msg': '邮箱不能为空'}, status=400)
    if not _is_valid_email(email):
        return Response({'msg': '邮箱格式不正确，需以 .com 结尾'}, status=400)
    if User.objects.filter(email=email).exists():
        return Response({'msg': '邮箱已被使用'}, status=400)
    try:
        _send_email_code('register', email)
    except Exception:
        return Response({'msg': '邮件发送失败，请检查邮箱配置'}, status=500)
    return Response({'msg': '验证码已发送'})


@api_view(['POST'])
def send_reset_email_code(request):
    username = (request.data.get('username') or '').strip()
    email = _normalize_email(request.data.get('email'))
    if not username or not email:
        return Response({'msg': '用户名和邮箱不能为空'}, status=400)
    if not _is_valid_username(username):
        return Response({'msg': '用户名格式不正确'}, status=400)
    if not _is_valid_email(email):
        return Response({'msg': '邮箱格式不正确，需以 .com 结尾'}, status=400)
    user = User.objects.filter(username=username).first()
    if not user:
        return Response({'msg': '用户不存在'}, status=404)
    if not user.email:
        random_email = f"{random.randint(100000000, 999999999)}@163.com"
        user.email = random_email
        user.save(update_fields=['email'])
    if _normalize_email(user.email) != email:
        return Response({'msg': '用户名和邮箱不匹配'}, status=400)
    try:
        _send_email_code('reset', email, username)
    except Exception:
        return Response({'msg': '邮件发送失败，请检查邮箱配置'}, status=500)
    return Response({'msg': '验证码已发送'})


@api_view(['POST'])
def reset_password(request):
    username = (request.data.get('username') or '').strip()
    email = _normalize_email(request.data.get('email'))
    new_password = (request.data.get('new_password') or '').strip()
    email_code = (request.data.get('email_code') or '').strip()

    if not username or not email or not new_password or not email_code:
        return Response({'msg': '请填写完整信息'}, status=400)
    if not _is_valid_username(username):
        return Response({'msg': '用户名格式不正确'}, status=400)
    if not _is_valid_password(new_password):
        return Response({'msg': '密码需至少6位，且包含大小写英文和数字'}, status=400)
    if not _is_valid_email(email):
        return Response({'msg': '邮箱格式不正确，需以 .com 结尾'}, status=400)

    user = User.objects.filter(username=username).first()
    if not user:
        return Response({'msg': '用户不存在'}, status=404)
    if _normalize_email(user.email) != email:
        return Response({'msg': '用户名和邮箱不匹配'}, status=400)

    cached_code = cache.get(_email_code_key('reset', email, username))
    if not cached_code or email_code != str(cached_code):
        return Response({'msg': '邮箱验证码错误或已过期'}, status=400)

    user.set_password(new_password)
    user.save(update_fields=['password'])
    cache.delete(_email_code_key('reset', email, username))
    return Response({'msg': '密码重置成功'})
