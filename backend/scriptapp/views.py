import json
import os
import re
import uuid

import requests
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from common.cos_utils import upload_file_to_cos
from users.models import AuthToken
from .models import Script, ScriptScene

DOUBAO_API_URL = os.environ.get(
    "DOUBAO_API_URL",
    "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
).strip()
SCRIPT_PARSE_MODEL = os.environ.get(
    "SCRIPT_PARSE_MODEL",
    os.environ.get("DOUBAO_CHAT_MODEL", "doubao-seed-2-0-mini-260215"),
).strip()
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "").strip()


def _get_user_from_bearer(request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token_key = auth_header.split(" ", 1)[1].strip()
        if not token_key:
            return None
        try:
            return AuthToken.objects.select_related("user").get(key=token_key).user
        except AuthToken.DoesNotExist:
            return None
    return None


def _resolve_user(request):
    return _get_user_from_bearer(request) or (request.user if request.user.is_authenticated else None)


def _extract_json_block(raw: str):
    text = (raw or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text, re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    array_block = re.search(r"(\[[\s\S]*\])", text)
    if array_block:
        try:
            return json.loads(array_block.group(1))
        except Exception:
            return []
    return []


def _read_upload_text(upload):
    ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "").lower()
    if ext not in {"txt", "md"}:
        raise ValueError("仅支持 .txt / .md 文件")
    raw = upload.read()
    upload.seek(0)
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    raise ValueError("文件编码无法识别，请使用 UTF-8 或 GBK")


def _normalize_scene_item(item):
    if not isinstance(item, dict):
        return None
    time_range = str(item.get("time_range") or item.get("time") or "").strip()
    raw_chars = item.get("characters") or []
    if isinstance(raw_chars, str):
        raw_chars = [c.strip() for c in re.split(r"[，,、/]", raw_chars) if c.strip()]
    if not isinstance(raw_chars, list):
        raw_chars = []
    characters = []
    for name in raw_chars:
        txt = str(name).strip()
        if txt and txt not in characters:
            characters.append(txt)
    scene_desc = str(item.get("scene_desc") or item.get("scene") or "").strip()
    prompt = str(item.get("prompt") or "").strip()
    scene_image_url = str(item.get("scene_image_url") or item.get("image_url") or "").strip()
    return {
        "time_range": time_range,
        "characters": characters,
        "scene_desc": scene_desc,
        "prompt": prompt,
        "scene_image_url": scene_image_url,
    }


def _serialize_scene(scene: ScriptScene):
    return {
        "id": scene.id,
        "script_id": scene.script_id,
        "time_range": scene.time_range,
        "characters": scene.characters or [],
        "character_images": scene.character_images or {},
        "scene_desc": scene.scene_desc,
        "prompt": scene.prompt,
        "scene_image_url": scene.scene_image_url,
        "user_remark": scene.user_remark,
        "created_at": scene.created_at.isoformat(),
        "updated_at": scene.updated_at.isoformat(),
    }


def _parse_script_with_ai(script_text: str):
    if not DOUBAO_API_KEY:
        raise RuntimeError("未配置 DOUBAO_API_KEY")
    if not script_text.strip():
        raise RuntimeError("剧本文本为空")
    prompt = (
        "你是专业分镜编剧助手。请把用户提供的剧本拆分为多个约15秒的分镜段。"
        "仅返回 JSON 数组，不要附加解释，不要 markdown。"
        "每个元素必须包含字段：time_range, characters(数组), scene_desc, prompt, scene_image_url。"
        "scene_image_url 若无可留空字符串。"
    )
    payload = {
        "model": SCRIPT_PARSE_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": script_text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    resp = requests.post(
        DOUBAO_API_URL,
        headers={
            "Authorization": f"Bearer {DOUBAO_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message") or resp.text
        except Exception:
            err = resp.text
        raise RuntimeError(f"模型解析失败: {err or 'unknown error'}")
    content = (
        resp.json()
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    rows = _extract_json_block(content)
    if not isinstance(rows, list):
        raise RuntimeError("模型返回格式异常，未得到 JSON 数组")
    normalized = []
    for row in rows:
        item = _normalize_scene_item(row)
        if item:
            normalized.append(item)
    if not normalized:
        raise RuntimeError("模型未返回有效分镜数据")
    return normalized


def _save_upload_and_maybe_cos(request, upload, subdir):
    ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "bin").lower()
    file_name = f"scriptapp/{subdir}/{uuid.uuid4().hex}.{ext}"
    saved_path = default_storage.save(file_name, upload)
    local_url = request.build_absolute_uri(default_storage.url(saved_path))
    cos_url = None
    try:
        local_path = default_storage.path(saved_path)
    except NotImplementedError:
        local_path = None
    if local_path:
        cos_url = upload_file_to_cos(local_path, file_name)
    cos_only = os.environ.get("COS_ONLY_MODE", "false").lower() == "true"
    if cos_url:
        if cos_only:
            default_storage.delete(saved_path)
        return cos_url
    if cos_only:
        raise RuntimeError("COS_ONLY_MODE 下图片上传失败")
    return local_url


class ScriptHealthAPIView(APIView):
    """
    Script.app entry placeholder.
    Future script-analysis APIs should live under this app.
    """

    def get(self, request):
        return Response({"msg": "script.app ready"})


@api_view(["POST"])
def upload_script(request):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)

    title = (request.data.get("title") or "").strip()
    content = (request.data.get("content") or "").strip()
    file = request.FILES.get("file")

    if file:
        try:
            content = _read_upload_text(file)
        except ValueError as exc:
            return Response({"msg": str(exc)}, status=400)
        if not title:
            raw_name = file.name.rsplit(".", 1)[0]
            title = raw_name.strip() or "未命名剧本"

    if not content:
        return Response({"msg": "请上传剧本文件或输入剧本文本"}, status=400)
    if not title:
        title = "未命名剧本"

    script = Script.objects.create(user=user, title=title[:200], content=content)
    return Response(
        {
            "script_id": script.id,
            "title": script.title,
            "content": script.content,
            "created_at": script.created_at.isoformat(),
        }
    )


@api_view(["POST"])
def parse_script(request, script_id):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    script = get_object_or_404(Script, id=script_id)
    if script.user_id and script.user_id != user.id:
        return Response({"msg": "无权限访问该剧本"}, status=403)
    try:
        parsed = _parse_script_with_ai(script.content)
    except requests.Timeout:
        return Response({"msg": "模型解析超时，请重试"}, status=504)
    except Exception as exc:
        return Response({"msg": str(exc)}, status=502)

    ScriptScene.objects.filter(script=script).delete()
    rows = []
    for item in parsed:
        rows.append(
            ScriptScene(
                script=script,
                time_range=item["time_range"],
                characters=item["characters"],
                scene_desc=item["scene_desc"],
                prompt=item["prompt"],
                scene_image_url=item["scene_image_url"],
                character_images={name: "" for name in item["characters"]},
            )
        )
    ScriptScene.objects.bulk_create(rows)
    scenes = ScriptScene.objects.filter(script=script).order_by("id")
    return Response(
        {
            "script_id": script.id,
            "title": script.title,
            "scenes": [_serialize_scene(scene) for scene in scenes],
        }
    )


@api_view(["GET"])
def list_scenes(request, script_id):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    script = get_object_or_404(Script, id=script_id)
    if script.user_id and script.user_id != user.id:
        return Response({"msg": "无权限访问该剧本"}, status=403)
    scenes = ScriptScene.objects.filter(script=script).order_by("id")
    return Response(
        {
            "script_id": script.id,
            "title": script.title,
            "content": script.content,
            "scenes": [_serialize_scene(scene) for scene in scenes],
        }
    )


@api_view(["PATCH"])
def update_scene(request, scene_id):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    scene = get_object_or_404(ScriptScene, id=scene_id)
    if scene.script.user_id and scene.script.user_id != user.id:
        return Response({"msg": "无权限修改该分镜"}, status=403)

    prompt = request.data.get("prompt")
    user_remark = request.data.get("user_remark")
    scene_image_url = request.data.get("scene_image_url")
    character_images = request.data.get("character_images")
    characters = request.data.get("characters")
    scene_desc = request.data.get("scene_desc")

    if prompt is not None:
        scene.prompt = str(prompt).strip()
    if user_remark is not None:
        scene.user_remark = str(user_remark).strip()
    if scene_image_url is not None:
        scene.scene_image_url = str(scene_image_url).strip()
    if scene_desc is not None:
        scene.scene_desc = str(scene_desc).strip()
    if isinstance(characters, list):
        scene.characters = [str(name).strip() for name in characters if str(name).strip()]
    if isinstance(character_images, dict):
        cleaned = {}
        for key, value in character_images.items():
            k = str(key).strip()
            if not k:
                continue
            cleaned[k] = str(value or "").strip()
        scene.character_images = cleaned

    scene.save(
        update_fields=[
            "prompt",
            "user_remark",
            "scene_image_url",
            "scene_desc",
            "characters",
            "character_images",
            "updated_at",
        ]
    )
    return Response({"msg": "保存成功", "scene": _serialize_scene(scene)})


@api_view(["POST"])
def upload_image(request):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    image = request.FILES.get("file") or request.FILES.get("image")
    if not image:
        return Response({"msg": "请选择图片"}, status=400)
    content_type = (getattr(image, "content_type", "") or "").lower()
    if not content_type.startswith("image/"):
        return Response({"msg": "仅支持图片文件"}, status=400)
    if image.size > 10 * 1024 * 1024:
        return Response({"msg": "图片大小不能超过10MB"}, status=400)
    try:
        url = _save_upload_and_maybe_cos(request, image, "images")
    except RuntimeError as exc:
        return Response({"msg": str(exc)}, status=502)
    return Response({"url": url})
