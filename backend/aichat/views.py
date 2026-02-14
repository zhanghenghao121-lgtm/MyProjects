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
from .rag import owner_stats, rebuild_local_docs, search_relevant
from common.cos_utils import upload_file_to_cos

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_CHAT_MODEL = os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
DEEPSEEK_VISION_MODEL = os.environ.get("DEEPSEEK_VISION_MODEL", "").strip()
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
    # Redact knowledge-file names and boilerplate source disclosure wording.
    text = re.sub(r"\b[\w\-]+\.(docx|pdf|md|txt)\b", "知识文档", text, flags=re.IGNORECASE)
    text = text.replace("根据提供的资料，", "")
    text = text.replace("根据提供资料，", "")
    text = text.replace("根据资料，", "")
    if not text:
        return "## 结论\n- 暂无有效回复内容。"

    conclusion = _extract_section(text, ("结论",))
    params_text = _extract_section(text, ("关键参数", "参数", "规格"))
    missing = _extract_section(text, ("缺失信息", "不足", "未提供"))

    if not conclusion:
        # Fallback: first non-empty line as conclusion
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        conclusion = lines[0] if lines else "未检索到可用结论。"
        body = "\n".join(lines[1:]).strip()
        if body and not params_text:
            params_text = body

    rows = _parse_params_to_rows(params_text)
    if not rows:
        rows = [("说明", _strip_md_markers(params_text) if params_text else "资料未提供")]

    missing_text = _strip_md_markers(missing) if missing else "无"

    table_lines = ["| 参数 | 值 |", "| --- | --- |"]
    for k, v in rows:
        kk = k.replace("|", "\\|")
        vv = v.replace("|", "\\|")
        table_lines.append(f"| {kk} | {vv} |")

    return (
        "## 结论\n"
        f"- {_strip_md_markers(conclusion)}\n\n"
        "## 关键参数\n"
        f"{chr(10).join(table_lines)}\n\n"
        "## 缺失信息\n"
        f"- {missing_text}"
    )


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
        return cos_url or file_url

    def _build_attachment_payload(self, request):
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
            if DEEPSEEK_VISION_MODEL:
                image_bytes = image.read()
                image.seek(0)
                image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                image_data_url = f"data:{content_type};base64,{image_b64}"

            image_url = self._save_upload(request, image, "images")
            notes.append(
                f"[用户上传图片] 名称: {image.name}; 大小: {image.size} bytes; 链接: {image_url}"
            )

        if generic_file:
            if generic_file.size > MAX_FILE_SIZE:
                return None, Response({"msg": "文件不能超过10MB"}, status=400)
            file_url = self._save_upload(request, generic_file, "files")
            notes.append(
                f"[用户上传文件] 名称: {generic_file.name}; 大小: {generic_file.size} bytes; 链接: {file_url}"
            )

        return {"notes": notes, "image_data_url": image_data_url, "has_image": has_image}, None

    def post(self, request):
        user = _resolve_user(request)
        if not user:
            return Response({"msg": "未登录"}, status=401)

        if not DEEPSEEK_API_KEY:
            return Response({"msg": "未配置 DEEPSEEK_API_KEY，请联系管理员"}, status=500)

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

        attachment_payload, attachment_error = self._build_attachment_payload(request)
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

        rag_docs = []
        rag_error = ""
        try:
            rag_docs = search_relevant(user.id, user_text)
        except Exception as exc:
            rag_error = str(exc)

        context_block = ""
        if rag_docs:
            lines = []
            for idx, doc in enumerate(rag_docs, start=1):
                source = (doc.metadata or {}).get("source_name", "unknown")
                page = (doc.metadata or {}).get("page")
                page_info = f", page={page}" if page is not None else ""
                content = (doc.page_content or "").strip().replace("\n\n", "\n")
                # Keep context concise and structured for the model.
                content = content[:1200]
                lines.append(f"[{idx}] source={source}{page_info}\n{content}")
            context_block = "\n\n".join(lines)

        system_prompt_text = (
            "你是章鱼助手。请输出专业、简洁、结构化答案，不要角色扮演和口癖。"
            "若用户问参数/规格，优先使用表格；若信息不足，明确标记“资料未提供”。"
            "不要复述原始检索片段，不要输出 'source=' 这类中间格式。"
            "若用户上传了图片/文件，消息里会给出附件信息和链接，你要结合附件信息与用户文字一起回答。"
        )
        if context_block:
            system_prompt_text += (
                "\n\n以下是从知识库检索到的资料片段。你必须优先依据这些资料回答，"
                "不要捏造未出现的参数；不确定时明确写“资料未提供该项”。"
                "当问题包含“型号/编号/参数/规格/尺寸”等词时，必须先从片段逐项抽取后再回答，"
                "不能直接给出“没有信息”的结论。"
                "\n\n请按以下格式输出："
                "\n1) 结论"
                "\n2) 关键参数（可用表格）"
                "\n3) 缺失信息（若有）"
                "\n\n知识库片段如下：\n"
                f"{context_block}"
            )
        elif rag_error:
            system_prompt_text += f"\n\n[检索系统提示] 当前知识库检索失败：{rag_error}"
        else:
            system_prompt_text += (
                "\n\n当前未检索到相关知识库片段。请明确告知“未在知识库命中相关内容”，"
                "并引导用户提供文档中的关键词、型号或补充资料。"
            )

        system_prompt = {
            "role": "system",
            "content": system_prompt_text,
        }

        model_name = DEEPSEEK_VISION_MODEL if image_data_url else DEEPSEEK_CHAT_MODEL

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
                DEEPSEEK_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
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
            return Response({"msg": "调用 deepseek 超时"}, status=504)
        except Exception as exc:
            return Response({"msg": str(exc)}, status=500)


class RagStatsAPIView(APIView):
    def get(self, request):
        user = _resolve_user(request)
        if not user:
            return Response({"msg": "未登录"}, status=401)
        try:
            return Response(owner_stats(user.id))
        except Exception as exc:
            return Response({"msg": f"获取知识库状态失败: {exc}"}, status=500)


class RagRebuildAPIView(APIView):
    def post(self, request):
        user = _resolve_user(request)
        if not user:
            return Response({"msg": "未登录"}, status=401)
        try:
            result = rebuild_local_docs()
            return Response({"msg": "RAG 向量库重建完成", **result})
        except Exception as exc:
            return Response({"msg": f"RAG 重建失败: {exc}"}, status=500)
