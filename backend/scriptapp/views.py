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
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    obj_or_arr = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if obj_or_arr:
        try:
            return json.loads(obj_or_arr.group(1))
        except Exception:
            return None
    return None


def _parse_dict_maybe(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _normalize_name_list(raw):
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[，,、/|]", raw) if x.strip()]
    if not isinstance(raw, list):
        return []
    items = []
    for value in raw:
        txt = str(value).strip()
        if txt and txt not in items:
            items.append(txt)
    return items


def _parse_time_range_seconds(time_range: str):
    text = str(time_range or "").strip()
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if len(nums) >= 2 and nums[1] > nums[0]:
        return nums[0], nums[1]
    return 0, 15


def _normalize_shot_type(raw, text):
    val = str(raw or "").strip()
    merged = f"{val} {text or ''}"
    base = "中景"
    for candidate in ("远景", "中景", "近景", "特写"):
        if candidate in merged:
            base = candidate
            break
    if "过肩" in merged or "OTS" in merged.upper():
        return f"{base}（过肩镜头 OTS）"
    return base


def _normalize_dynamic_elements(raw, fallback_text):
    if isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[，,、/|；;]", raw) if x.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = []
    if items:
        return list(dict.fromkeys(items))

    text = str(fallback_text or "")
    inferred = []
    keyword_pairs = [
        (("云", "云层"), "云层缓慢流动"),
        (("风", "衣角", "头发"), "风吹动头发与衣角"),
        (("雾", "烟", "烟尘"), "雾气/烟尘缓慢变化"),
        (("光", "光影"), "光影层次变化"),
    ]
    for keys, label in keyword_pairs:
        if any(k in text for k in keys):
            inferred.append(label)
    if not inferred:
        inferred.append("自然风动与环境光影变化")
    return inferred


def _normalize_beats(raw, start_sec, end_sec):
    beats = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                s = item.get("start")
                e = item.get("end")
                content = str(item.get("content") or item.get("desc") or "").strip()
                if isinstance(s, int) and isinstance(e, int) and e > s and content:
                    beats.append((s, e, content))
            elif isinstance(item, str):
                m = re.match(r"\s*(\d+)\s*s?\s*[-~—–]\s*(\d+)\s*s?\s*[:：]\s*(.+)\s*$", item)
                if m:
                    s, e, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
                    if e > s and content:
                        beats.append((s, e, content))
    elif isinstance(raw, str):
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        for line in lines:
            m = re.match(r"\s*(\d+)\s*s?\s*[-~—–]\s*(\d+)\s*s?\s*[:：]\s*(.+)\s*$", line)
            if m:
                s, e, content = int(m.group(1)), int(m.group(2)), m.group(3).strip()
                if e > s and content:
                    beats.append((s, e, content))

    if len(beats) >= 3:
        beats = sorted(beats, key=lambda x: x[0])
        return beats[:5]

    # fallback to 4 continuous segments
    duration = max(10, end_sec - start_sec)
    b0 = start_sec
    b1 = start_sec + round(duration * 0.2)
    b2 = start_sec + round(duration * 0.47)
    b3 = start_sec + round(duration * 0.8)
    b4 = end_sec if end_sec > b3 else start_sec + duration
    if not (b0 < b1 < b2 < b3 < b4):
        b0, b1, b2, b3, b4 = 0, 3, 7, 12, 15
    return [
        (b0, b1, "建立空间与人物关系，主体进入画面并开始动作"),
        (b1, b2, "主体动作推进，环境动态元素持续变化"),
        (b2, b3, "情绪或对话核心动作展开，保持单一机位"),
        (b3, b4, "动作收束并形成下个分镜的衔接势能"),
    ]


def _normalize_scene_item(item):
    if not isinstance(item, dict):
        return None
    time_range = str(item.get("time_range") or item.get("time") or "").strip()
    start_sec, end_sec = _parse_time_range_seconds(time_range)
    source_scene = str(item.get("scene_desc") or item.get("scene") or "").strip()
    source_prompt = str(item.get("prompt") or "").strip()
    shot_type = _normalize_shot_type(item.get("shot_type"), f"{source_scene}\n{source_prompt}")
    dynamic_elements = _normalize_dynamic_elements(
        item.get("dynamic_elements"),
        f"{source_scene}\n{source_prompt}",
    )
    beats = _normalize_beats(item.get("beats"), start_sec, end_sec)

    beat_lines = [f"- {s}s-{e}s：{desc}" for s, e, desc in beats]
    structured_scene_desc = (
        f"镜头类型：{shot_type}\n"
        f"场景动态元素：{' / '.join(dynamic_elements)}\n"
        "分镜内容：\n"
        + "\n".join(beat_lines)
    )
    structured_prompt = (
        f"总时长：{start_sec}s-{end_sec}s\n"
        f"镜头类型：{shot_type}\n"
        f"场景动态元素：{' / '.join(dynamic_elements)}\n"
        "分镜内容：\n"
        + "\n".join(beat_lines)
    )

    return {
        "time_range": time_range or f"{start_sec}-{end_sec}s",
        "characters": _normalize_name_list(item.get("characters") or []),
        "props": _normalize_name_list(item.get("props") or item.get("objects") or []),
        "scene_desc": structured_scene_desc,
        "prompt": structured_prompt,
    }


def _normalize_image_map(raw):
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        k = str(key).strip()
        v = str(value or "").strip()
        if k:
            cleaned[k] = v
    return cleaned


def _serialize_scene(scene: ScriptScene):
    return {
        "id": scene.id,
        "script_id": scene.script_id,
        "time_range": scene.time_range,
        "prompt": scene.prompt,
        "scene_desc": scene.scene_desc,
        "scene_image_url": scene.scene_image_url,
        "characters": scene.characters or [],
        "character_images": scene.character_images or {},
        "props": scene.props or [],
        "prop_images": scene.prop_images or {},
        "created_at": scene.created_at.isoformat(),
        "updated_at": scene.updated_at.isoformat(),
    }


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


def _call_model(messages, temperature=0.2):
    if not DOUBAO_API_KEY:
        raise RuntimeError("未配置 DOUBAO_API_KEY")
    resp = requests.post(
        DOUBAO_API_URL,
        headers={
            "Authorization": f"Bearer {DOUBAO_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": SCRIPT_PARSE_MODEL,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message") or resp.text
        except Exception:
            err = resp.text
        raise RuntimeError(f"模型调用失败: {err or 'unknown error'}")
    return (
        resp.json()
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )


def _extract_entities_with_ai(script_text: str):
    prompt = (
        "你是剧本结构提取助手。"
        "请从剧本文本中提取：人物、场景、物品。"
        "只返回 JSON 对象，不要任何解释。格式："
        '{"characters":[""],"scenes":[""],"props":[""]}'
    )
    content = _call_model(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": script_text},
        ],
        temperature=0.1,
    )
    parsed = _extract_json_block(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("实体提取格式异常")
    return {
        "characters": _normalize_name_list(parsed.get("characters") or []),
        "scenes": _normalize_name_list(parsed.get("scenes") or []),
        "props": _normalize_name_list(parsed.get("props") or []),
    }


def _match_scene_image(scene_desc: str, scene_images: dict):
    if not scene_images:
        return ""
    desc = (scene_desc or "").strip()
    if not desc:
        return next((v for v in scene_images.values() if v), "")
    if desc in scene_images and scene_images[desc]:
        return scene_images[desc]
    for key, url in scene_images.items():
        if not url:
            continue
        if key in desc or desc in key:
            return url
    return ""


def _parse_script_with_ai(script_text: str, user_prompt: str, entities: dict):
    entity_text = (
        f"人物候选: {', '.join(entities.get('characters', [])) or '无'}\n"
        f"场景候选: {', '.join(entities.get('scenes', [])) or '无'}\n"
        f"物品候选: {', '.join(entities.get('props', [])) or '无'}"
    )
    extra = user_prompt.strip() if isinstance(user_prompt, str) else ""
    system_prompt = (
        "你是一名专业影视分镜编剧与导演，熟悉真实影视拍摄与剪辑逻辑。\n\n"
        "请将我提供的剧本拆分为多个分镜，并严格遵守以下规则：\n\n"
        "【整体分镜规则】\n"
        "1. 每个分镜约 10–18 秒，平均约 15 秒\n"
        "2. 每个分镜只对应一个镜头（不切机位）\n"
        "3. 每个分镜的场景必须是“动态场景”，至少包含一种持续变化的环境元素，例如：\n"
        "   - 云层缓慢流动\n"
        "   - 风吹动人物头发或衣角\n"
        "   - 雾气、烟尘、光影变化\n"
        "4. 全部内容必须可视化，不允许心理描写或抽象描述\n\n"
        "【镜头类型规则】\n"
        "1. 远景镜头仅用于交代空间，全片远景不超过 2 个\n"
        "2. 大部分分镜使用中景和近景\n"
        "3. 情绪、动作、对话以中景 / 近景完成\n\n"
        "【人物对话与机位规则】\n"
        "1. 人物对话场景中：\n"
        "   - 约 30% 的对话使用“背后过肩镜头（OTS）”\n"
        "2. 过肩镜头定义为：\n"
        "   - 两名人物处于同一空间\n"
        "   - 镜头位于其中一人背后或肩部后方\n"
        "   - 画面前景可看到该人物的肩部或背部轮廓\n"
        "   - 正在说话的人面对镜头进行对白\n"
        "3. 不要所有对话都使用正面镜头，合理穿插过肩镜头以增强真实交流感\n\n"
        "【分镜内部秒级拆分规则】\n"
        "1. 每个分镜内部拆分为 3–5 个连续时间段，例如：\n"
        "   - 0s–3s\n"
        "   - 3s–7s\n"
        "   - 7s–12s\n"
        "   - 12s–15s\n"
        "2. 每个时间段只描述一个清晰、可被画面直接看到的动作或变化\n"
        "3. 时间段必须首尾连续，不跳秒、不重叠\n\n"
        "【分镜输出格式（严格遵守）】\n"
        "分镜 X：\n"
        "- 总时长：Xs–Xs\n"
        "- 镜头类型：远景 / 中景 / 近景 / 特写（如为过肩镜头请标注）\n"
        "- 场景动态元素：明确写出（如云动 / 风动 / 雾动等）\n"
        "- 分镜内容：\n"
        "  - 0s–3s：……\n"
        "  - 3s–7s：……\n"
        "  - 7s–12s：……\n"
        "  - 12s–15s：……\n\n"
        "最终返回时仅输出 JSON 数组，不要解释，不要 markdown。"
        "每个数组元素字段必须是：time_range, characters(数组), scene_desc, props(数组), prompt。"
        "同时建议额外输出：shot_type, dynamic_elements(数组), beats(数组)。"
        "beats 数组每项格式为 {start,end,content}。"
        "其中 scene_desc 需要包含镜头类型、动态场景元素和分镜内容；"
        "prompt 需要包含可直接用于生成画面的分镜描述。"
        "characters 和 props 请尽量使用给定候选名称。"
    )
    user_content = (
        f"{entity_text}\n\n"
        f"用户补充要求: {extra or '无'}\n\n"
        f"剧本文本:\n{script_text}"
    )
    content = _call_model(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )
    rows = _extract_json_block(content)
    if not isinstance(rows, list):
        raise RuntimeError("分镜解析格式异常，未得到 JSON 数组")
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
def extract_entities(request, script_id):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    script = get_object_or_404(Script, id=script_id)
    if script.user_id and script.user_id != user.id:
        return Response({"msg": "无权限访问该剧本"}, status=403)
    try:
        entities = _extract_entities_with_ai(script.content)
    except requests.Timeout:
        return Response({"msg": "实体提取超时，请重试"}, status=504)
    except Exception as exc:
        return Response({"msg": str(exc)}, status=502)
    return Response({"script_id": script.id, "title": script.title, **entities})


@api_view(["POST"])
def parse_script(request, script_id):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=401)
    script = get_object_or_404(Script, id=script_id)
    if script.user_id and script.user_id != user.id:
        return Response({"msg": "无权限访问该剧本"}, status=403)

    user_prompt = (request.data.get("user_prompt") or "").strip()
    character_images = _normalize_image_map(_parse_dict_maybe(request.data.get("character_images")))
    scene_images = _normalize_image_map(_parse_dict_maybe(request.data.get("scene_images")))
    prop_images = _normalize_image_map(_parse_dict_maybe(request.data.get("prop_images")))
    entities = {
        "characters": list(character_images.keys()),
        "scenes": list(scene_images.keys()),
        "props": list(prop_images.keys()),
    }

    try:
        parsed = _parse_script_with_ai(script.content, user_prompt, entities)
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
                scene_desc=item["scene_desc"],
                prompt=item["prompt"],
                characters=item["characters"],
                props=item["props"],
                character_images={name: character_images.get(name, "") for name in item["characters"] if character_images.get(name)},
                prop_images={name: prop_images.get(name, "") for name in item["props"] if prop_images.get(name)},
                scene_image_url=_match_scene_image(item["scene_desc"], scene_images),
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
    scene_desc = request.data.get("scene_desc")
    scene_image_url = request.data.get("scene_image_url")
    characters = request.data.get("characters")
    character_images = request.data.get("character_images")
    props = request.data.get("props")
    prop_images = request.data.get("prop_images")

    if prompt is not None:
        scene.prompt = str(prompt).strip()
    if scene_desc is not None:
        scene.scene_desc = str(scene_desc).strip()
    if scene_image_url is not None:
        scene.scene_image_url = str(scene_image_url).strip()
    if isinstance(characters, list):
        scene.characters = _normalize_name_list(characters)
    if isinstance(character_images, dict):
        scene.character_images = _normalize_image_map(character_images)
    if isinstance(props, list):
        scene.props = _normalize_name_list(props)
    if isinstance(prop_images, dict):
        scene.prop_images = _normalize_image_map(prop_images)

    scene.save(
        update_fields=[
            "prompt",
            "scene_desc",
            "scene_image_url",
            "characters",
            "character_images",
            "props",
            "prop_images",
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
