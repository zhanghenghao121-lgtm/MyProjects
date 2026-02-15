import os
import json
import uuid
import base64
import re
import requests
from django.core.files.storage import default_storage
from rest_framework.views import APIView
from rest_framework.response import Response
from users.models import AuthToken
from common.cos_utils import upload_file_to_cos

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_CHAT_MODEL = os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "").strip()
DOUBAO_API_URL = os.environ.get(
    "DOUBAO_API_URL",
    "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
).strip()
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "").strip()
DOUBAO_CHAT_MODEL = os.environ.get("DOUBAO_CHAT_MODEL", "doubao-seed-2-0-mini-260215").strip()
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_IMAGE_SIZE = 500 * 1024 * 1024


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


def _strip_md_markers(text: str) -> str:
    return (text or "").strip().strip("*").strip()


def _extract_section(text: str, title_keywords: tuple[str, ...]) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    starts = []
    for i, line in enumerate(lines):
        for kw in title_keywords:
            if re.match(rf"^([#\-\*\d\.\)\s]*){re.escape(kw)}[:：]?\s*$", line):
                starts.append((i, kw))
                break
    if not starts:
        return ""
    start_idx = starts[0][0] + 1
    end_idx = len(lines)
    for i in range(start_idx, len(lines)):
        l = lines[i]
        if re.match(r"^([#\-\*\d\.\)\s]*)(结论|关键参数|依据来源|缺失信息)[:：]?\s*$", l):
            end_idx = i
            break
    return "\n".join([ln for ln in lines[start_idx:end_idx] if ln]).strip()


def _parse_params_to_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-").lstrip("*").strip()
        if not line:
            continue
        if ":" in line:
            key, val = line.split(":", 1)
        elif "：" in line:
            key, val = line.split("：", 1)
        else:
            rows.append((_strip_md_markers(line), "资料未提供"))
            continue
        key = _strip_md_markers(key)
        val = _strip_md_markers(val)
        if key:
            rows.append((key, val or "资料未提供"))
    return rows


def _normalize_reply_markdown(content: str) -> str:
    text = (content or "").strip()
    text = re.sub(r"\b[\w\-]+\.(docx|pdf|md|txt)\b", "知识文档", text, flags=re.IGNORECASE)
    text = text.replace("根据提供的资料，", "")
    text = text.replace("根据提供资料，", "")
    text = text.replace("根据资料，", "")
    if not text:
        return "我这边暂时没拿到有效信息，你可以换个问法试试。"

    # Prefer natural plain-text output for chat bubbles.
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in {"| --- | --- |", "---", "——"}:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(\d+[\.\)]\s*|[-*]\s*)", "", line)
        line = line.replace("**", "").replace("__", "")
        if line:
            lines.append(line)

    plain = "\n".join(lines)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    plain = re.sub(r"[ \t]{2,}", " ", plain).strip()

    if "|" in plain:
        plain = plain.replace("|", " ")
        plain = re.sub(r"[ ]{2,}", " ", plain).strip()

    return plain or "我这边暂时没拿到有效信息，你可以换个问法试试。"


class ChatAPIView(APIView):
    def _normalize_messages(self, raw_messages):
        if isinstance(raw_messages, str):
            try:
                raw_messages = json.loads(raw_messages)
            except Exception:
                raw_messages = []
        if not isinstance(raw_messages, list):
            return []
        trimmed_messages = []
        for msg in raw_messages[-12:]:
            role = (msg or {}).get("role")
            content = (msg or {}).get("content")
            if role not in ("user", "assistant", "system"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            trimmed_messages.append({"role": role, "content": content.strip()})
        return trimmed_messages

    def _save_upload(self, request, upload, subdir):
        ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "bin").lower()
        file_name = f"aichat/{subdir}/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, upload)
        file_url = request.build_absolute_uri(default_storage.url(saved_path))
        # Try to mirror to COS for public access; fall back to local URL.
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
            raise RuntimeError("COS upload failed in COS_ONLY_MODE")
        return file_url

    def _build_attachment_payload(self, request, enable_vision=False):
        notes = []
        image_data_url = ""
        has_image = False
        image = request.FILES.get("image")
        generic_file = request.FILES.get("file")

        if image:
            has_image = True
            if image.size > MAX_IMAGE_SIZE:
                return None, Response({"msg": "图片不能超过500MB"}, status=400)
            content_type = (getattr(image, "content_type", "") or "").lower()
            if not content_type.startswith("image/"):
                return None, Response({"msg": "发图片仅支持图片类型"}, status=400)

            # 仅在配置了视觉模型时才构造多模态内容。
            if enable_vision:
                image_bytes = image.read()
                image.seek(0)
                image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                image_data_url = f"data:{content_type};base64,{image_b64}"

            try:
                image_url = self._save_upload(request, image, "images")
            except RuntimeError:
                return None, Response({"msg": "图片上传到 COS 失败，请稍后重试"}, status=502)
            notes.append(
                f"[用户上传图片] 名称: {image.name}; 大小: {image.size} bytes; 链接: {image_url}"
            )

        if generic_file:
            if generic_file.size > MAX_FILE_SIZE:
                return None, Response({"msg": "文件不能超过10MB"}, status=400)
            try:
                file_url = self._save_upload(request, generic_file, "files")
            except RuntimeError:
                return None, Response({"msg": "文件上传到 COS 失败，请稍后重试"}, status=502)
            notes.append(
                f"[用户上传文件] 名称: {generic_file.name}; 大小: {generic_file.size} bytes; 链接: {file_url}"
            )

        return {"notes": notes, "image_data_url": image_data_url, "has_image": has_image}, None

    def post(self, request):
        user = _resolve_user(request)
        if not user:
            return Response({"msg": "未登录"}, status=401)

        role = (request.data.get("role") or "octopus").strip().lower()
        role_configs = {
            "octopus": {
                "label": "章鱼",
                "api_url": DEEPSEEK_API_URL,
                "api_key": DEEPSEEK_API_KEY,
                "chat_model": DEEPSEEK_CHAT_MODEL,
                "vision_model": DEEPSEEK_VISION_MODEL,
                "system_prompt": (
                    "你是章鱼助手。用自然、口语化的中文回答，像真人聊天。"
                    "先直接回答核心问题，默认控制在1-3句，避免冗长。"
                    "非必要不要使用Markdown标题、表格、模板化分段。"
                    "只有在用户明确要求时再分点。"
                    "不确定就直说，不要编造。"
                    "若用户上传了图片/文件，消息中会给出附件说明和链接，请结合用户问题一起回答。"
                ),
            },
            "doubaoyu": {
                "label": "豆包鱼",
                "api_url": DOUBAO_API_URL,
                "api_key": DOUBAO_API_KEY,
                "chat_model": DOUBAO_CHAT_MODEL,
                "vision_model": "",
                "system_prompt": (
                    "你是豆包鱼助手。用自然、口语化的中文回答，像真人聊天。"
                    "先直接回答核心问题，默认控制在1-3句，避免冗长。"
                    "非必要不要使用Markdown标题、表格、模板化分段。"
                    "只有在用户明确要求时再分点。"
                    "不确定就直说，不要编造。"
                    "若用户上传了图片/文件，消息中会给出附件说明和链接，请结合用户问题一起回答。"
                ),
            },
        }
        role_config = role_configs.get(role)
        if not role_config:
            return Response({"msg": "不支持的角色"}, status=400)
        if not role_config["api_key"]:
            return Response({"msg": f"未配置 {role_config['label']} API Key，请联系管理员"}, status=500)

        messages = request.data.get("messages")
        if messages is None:
            messages = request.data.get("messages_json")
        trimmed_messages = self._normalize_messages(messages)

        # multipart/form-data 场景下，message 可能被上游网关或解析器处理为缺失
        # 这里用多来源兜底，避免误判“消息为空”。
        raw_message = request.data.get("message")
        if raw_message is None:
            raw_message = request.data.get("content")
        user_text = (raw_message.strip() if isinstance(raw_message, str) else "")
        if not user_text and trimmed_messages:
            for item in reversed(trimmed_messages):
                if item.get("role") == "user" and item.get("content"):
                    user_text = item["content"].strip()
                    break
        if not user_text:
            return Response({"msg": "必须输入消息内容"}, status=400)

        if not trimmed_messages:
            trimmed_messages = [{"role": "user", "content": user_text}]

        attachment_payload, attachment_error = self._build_attachment_payload(
            request,
            enable_vision=bool(role_config["vision_model"]),
        )
        if attachment_error:
            return attachment_error
        attachment_notes = attachment_payload["notes"]
        image_data_url = attachment_payload["image_data_url"]
        has_image = attachment_payload["has_image"]
        if attachment_notes:
            merged_user_text = user_text + "\n\n" + "\n".join(attachment_notes)
            if has_image and not image_data_url:
                merged_user_text += (
                    "\n\n[系统提示] 当前后端未配置视觉模型，无法直接识别图片像素内容。"
                    "请结合图片内容补充文字描述后再提问。"
                )
            if trimmed_messages and trimmed_messages[-1]["role"] == "user":
                trimmed_messages[-1]["content"] = merged_user_text
            else:
                trimmed_messages.append({"role": "user", "content": merged_user_text})

        system_prompt_text = role_config["system_prompt"]
        system_prompt = {
            "role": "system",
            "content": system_prompt_text,
        }

        model_name = role_config["vision_model"] if image_data_url else role_config["chat_model"]

        payload_messages = [system_prompt]
        for msg in trimmed_messages:
            payload_messages.append({"role": msg["role"], "content": msg["content"]})

        if image_data_url and payload_messages and payload_messages[-1].get("role") == "user":
            text_content = payload_messages[-1].get("content", "")
            payload_messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_content},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }

        payload = {
            "model": model_name,
            "messages": payload_messages,
            "stream": False,
            "temperature": 0.2,
        }

        try:
            resp = requests.post(
                role_config["api_url"],
                json=payload,
                headers={
                    "Authorization": f"Bearer {role_config['api_key']}",
                    "Content-Type": "application/json",
                },
                timeout=45,
            )
            if resp.status_code != 200:
                err_msg = "请求失败"
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message") or err_data.get("msg") or err_msg
                except Exception:
                    err_msg = resp.text or err_msg
                if image_data_url:
                    err_msg = f"图片识别调用失败（模型: {model_name}）: {err_msg}"
                else:
                    err_msg = f"模型接口错误（模型: {model_name}）: {err_msg}"
                return Response({"msg": err_msg}, status=resp.status_code)
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            return Response({"reply": _normalize_reply_markdown(content)})
        except requests.Timeout:
            return Response({"msg": f"调用 {role_config['label']} 超时"}, status=504)
        except Exception as exc:
            return Response({"msg": str(exc)}, status=500)
