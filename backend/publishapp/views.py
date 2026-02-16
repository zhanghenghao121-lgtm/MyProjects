import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from common.cos_utils import upload_file_to_cos
from users.models import AuthToken

HOT_API_URL = os.environ.get("PUBLISH_HOT_API_URL", "https://v2.xxapi.cn/api/douyinhot").strip()
DOUBAO_API_URL = os.environ.get(
    "DOUBAO_API_URL",
    "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
).strip()
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "").strip()
COPY_MODEL_ID = os.environ.get("PUBLISH_COPY_MODEL", "doubao-seed-2-0-pro-260215").strip()
VIDEO_MODEL_ID = os.environ.get("PUBLISH_VIDEO_MODEL", "doubao-seedance-1-5-pro-251215").strip()
VIDEO_CREATE_URL = os.environ.get(
    "PUBLISH_VIDEO_CREATE_URL",
    "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
).strip()
VIDEO_QUERY_URL_TEMPLATE = os.environ.get(
    "PUBLISH_VIDEO_QUERY_URL_TEMPLATE",
    "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}",
).strip()
VIDEO_API_KEY = os.environ.get("PUBLISH_VIDEO_API_KEY", DOUBAO_API_KEY).strip()
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("PUBLISH_REQUEST_TIMEOUT_SECONDS", "120"))
POLL_INTERVAL_SECONDS = int(os.environ.get("PUBLISH_POLL_INTERVAL_SECONDS", "4"))
POLL_MAX_ATTEMPTS = int(os.environ.get("PUBLISH_POLL_MAX_ATTEMPTS", "45"))
PUBLISH_USE_COS = os.environ.get("PUBLISH_USE_COS", "true").lower() == "true"
# Force all generated videos to 5 seconds.
VIDEO_DURATION_SECONDS = 5
VIDEO_RATIO = os.environ.get("PUBLISH_VIDEO_RATIO", "16:9").strip()
VIDEO_WATERMARK = os.environ.get("PUBLISH_VIDEO_WATERMARK", "false").lower() == "true"
VIDEO_RESOLUTION = os.environ.get("PUBLISH_VIDEO_RESOLUTION", "1080p").strip() or "1080p"
MAX_KEYWORDS_PER_BATCH = int(os.environ.get("PUBLISH_MAX_KEYWORDS_PER_BATCH", "5"))


class SensitiveContentError(RuntimeError):
    pass


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


def _extract_hot_keyword(payload: dict) -> Optional[str]:
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            word = str(first.get("word") or "").strip()
            if word:
                return word
    if isinstance(data, dict):
        for key in ("word", "keyword", "title"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return None


def _fetch_hot_keyword() -> str:
    resp = requests.get(HOT_API_URL, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"热点接口请求失败: HTTP {resp.status_code}")
    payload = resp.json()
    keyword = _extract_hot_keyword(payload if isinstance(payload, dict) else {})
    if not keyword:
        raise RuntimeError("热点接口未返回有效关键词")
    return keyword


def _fetch_hot_list(limit: int = 20):
    resp = requests.get(HOT_API_URL, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"热点接口请求失败: HTTP {resp.status_code}")
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    words = []
    for item in data:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if word and word not in words:
            words.append(word)
        if len(words) >= limit:
            break
    return words


def _safe_filename(keyword: str) -> str:
    text = str(keyword or "").strip()
    if not text:
        return "hot-video"
    text = text.replace("/", "_").replace("\\", "_").replace(":", "_")
    text = text.replace("*", "_").replace("?", "_").replace('"', "_")
    text = text.replace("<", "_").replace(">", "_").replace("|", "_")
    text = " ".join(text.split())
    return text[:80] or "hot-video"


def _generate_copy(keyword: str) -> str:
    if not DOUBAO_API_KEY:
        raise RuntimeError("未配置 DOUBAO_API_KEY")
    system_prompt = (
        "你是一名短视频导演与编剧，请根据热点关键词生成一段可直接用于文生视频模型的中文提示词，"
        "整体风格为幽默、夸张的二次元日漫风格。"
        "画面主体清晰明确，围绕热点关键词进行戏剧化演绎：主角为一名二次元角色或拟人化形象，"
        "做出与热点强相关的搞笑动作或反差行为；镜头采用电影级运镜（如快速推进、轻微摇镜、跟拍或突然定格），"
        "在 8–12 秒内完成一个完整笑点；环境为贴合热点的夸张场景（如办公室、街头、虚拟世界或幻想空间），"
        "整体氛围轻松沙雕、节奏明快；光影风格为高饱和度动漫光效，边缘描线清晰，辅以夸张特效"
        "（速度线、表情符号、文字拟声、能量闪光等），突出喜剧效果；画面构图适配 16:9 横版、1080p 分辨率，"
        "主体居中偏前景，背景适度虚化，确保短视频平台观看时信息集中、第一眼有冲击力。"
        "只输出一段可直接用于文生视频模型的中文提示词，不要解释。"
    )
    user_prompt = f"热点关键词：{keyword}\n请输出一段高质量中文视频生成文案。"
    resp = requests.post(
        DOUBAO_API_URL,
        headers={
            "Authorization": f"Bearer {DOUBAO_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": COPY_MODEL_ID,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "stream": False,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"文案生成失败: HTTP {resp.status_code}")
    text = (
        resp.json()
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    final_text = str(text or "").strip()
    if not final_text:
        raise RuntimeError("文案生成为空")
    return final_text


def _rewrite_copy_for_safety(keyword: str, copy_text: str) -> str:
    """Rewrite prompt into a safer, generic scene when provider content policy blocks it."""
    if not DOUBAO_API_KEY:
        return (
            f"二次元日漫风格，围绕“{keyword}”的幽默夸张短片，主角为二次元拟人化角色，"
            "在贴合热点的场景里做出反差搞笑动作，电影级运镜，节奏明快，16:9，1080p。"
        )
    system_prompt = (
        "你是内容安全改写助手。将输入视频文案改写为合规中性版本。"
        "不要出现政治、医疗、灾难、暴力、未成年人危险、色情、仇恨等敏感要素。"
        "保留视觉表达与镜头描述，且保持幽默夸张的二次元日漫风格。"
        "仅输出一段可用于文生视频的中文文案。"
    )
    user_prompt = (
        f"热点关键词：{keyword}\n"
        f"原始文案：{copy_text}\n"
        "请输出更安全的改写版。"
    )
    resp = requests.post(
        DOUBAO_API_URL,
        headers={
            "Authorization": f"Bearer {DOUBAO_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": COPY_MODEL_ID,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.5,
            "stream": False,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        return (
            f"二次元日漫风格，围绕“{keyword}”的幽默夸张短片，主角为二次元拟人化角色，"
            "在贴合热点的场景里做出反差搞笑动作，电影级运镜，节奏明快，16:9，1080p。"
        )
    safe_text = (
        resp.json()
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    safe_text = str(safe_text or "").strip()
    return safe_text or (
        f"二次元日漫风格，围绕“{keyword}”的幽默夸张短片，主角为二次元拟人化角色，"
        "在贴合热点的场景里做出反差搞笑动作，电影级运镜，节奏明快，16:9，1080p。"
    )


def _ensure_anime_style(copy_text: str) -> str:
    text = str(copy_text or "").strip()
    style_hint = "二次元日漫风格，高饱和动漫光效，幽默夸张喜剧表达。"
    if not text:
        return style_hint
    if "二次元" in text or "日漫" in text or "动漫" in text:
        return text
    return f"{text} {style_hint}"


def _extract_video_url(payload):
    if isinstance(payload, dict):
        for key in ("video_url", "url", "file_url", "output_url", "download_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip().lower().startswith("http") and ".mp4" in value.lower():
                return value.strip()
        for value in payload.values():
            found = _extract_video_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_video_url(item)
            if found:
                return found
    return ""


def _submit_video_task(copy_text: str) -> str:
    if not VIDEO_API_KEY:
        raise RuntimeError("未配置 PUBLISH_VIDEO_API_KEY（或 DOUBAO_API_KEY）")
    payload = {
        "model": VIDEO_MODEL_ID,
        "content": [{"type": "text", "text": _ensure_anime_style(copy_text)}],
        "ratio": VIDEO_RATIO,
        "duration": VIDEO_DURATION_SECONDS,
        "watermark": VIDEO_WATERMARK,
    }
    # Some model versions may support explicit resolution; keep it optional.
    if VIDEO_RESOLUTION:
        payload["resolution"] = VIDEO_RESOLUTION

    resp = requests.post(
        VIDEO_CREATE_URL,
        headers={
            "Authorization": f"Bearer {VIDEO_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        err_text = resp.text[:500]
        try:
            err_payload = resp.json()
            err_code = (
                err_payload.get("error", {}).get("code")
                or err_payload.get("code")
                or ""
            )
            err_msg = (
                err_payload.get("error", {}).get("message")
                or err_payload.get("message")
                or err_text
            )
        except Exception:
            err_code = ""
            err_msg = err_text
        if err_code == "InputTextSensitiveContentDetected":
            raise SensitiveContentError(f"视频文案触发风控: {err_msg}")
        raise RuntimeError(f"视频任务创建失败: HTTP {resp.status_code}, {err_msg}")
    payload = resp.json()
    task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("视频任务创建返回缺少任务ID")
    return task_id


def _query_video_task(task_id: str):
    url = VIDEO_QUERY_URL_TEMPLATE.format(task_id=task_id)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {VIDEO_API_KEY}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"视频任务查询失败: HTTP {resp.status_code}")
    payload = resp.json()
    status_text = str(payload.get("status") or payload.get("task_status") or "").lower()
    return payload, status_text


def _wait_for_video_url(task_id: str) -> str:
    success_states = {"succeeded", "success", "completed", "done"}
    fail_states = {"failed", "error", "canceled", "cancelled"}

    for _ in range(POLL_MAX_ATTEMPTS):
        payload, status_text = _query_video_task(task_id)
        if status_text in fail_states:
            raise RuntimeError(f"视频生成失败: {json.dumps(payload, ensure_ascii=False)[:300]}")
        if status_text in success_states:
            video_url = _extract_video_url(payload)
            if video_url:
                return video_url
        # Some providers return URL before final succeeded status.
        early_url = _extract_video_url(payload)
        if early_url:
            return early_url
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError("视频生成超时，请稍后重试")


def _format_size(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    value = float(size_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"


def _head_content_length(url: str) -> int:
    # Prefer HEAD first.
    try:
        resp = requests.head(url, timeout=20, allow_redirects=True)
        raw = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if raw and str(raw).isdigit():
            return int(raw)
    except Exception:
        pass

    # Fallback 1: GET with Range; some providers return Content-Range.
    try:
        resp = requests.get(
            url,
            headers={"Range": "bytes=0-0"},
            timeout=20,
            allow_redirects=True,
            stream=True,
        )
        content_range = resp.headers.get("Content-Range") or resp.headers.get("content-range") or ""
        # Example: bytes 0-0/1234567
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        raw = resp.headers.get("Content-Length") or resp.headers.get("content-length")
        if raw and str(raw).isdigit():
            return int(raw)
    except Exception:
        pass

    # Fallback 2: stream a limited chunk to at least return non-zero size hint.
    try:
        max_probe = 8 * 1024 * 1024
        got = 0
        with requests.get(url, stream=True, timeout=30, allow_redirects=True) as resp:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                got += len(chunk)
                if got >= max_probe:
                    break
        return got
    except Exception:
        return 0


def _upload_video_to_cos(video_url: str, keyword: str) -> str:
    if not PUBLISH_USE_COS:
        return ""
    with requests.get(video_url, stream=True, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        if resp.status_code != 200:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as fp:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fp.write(chunk)
            fp.flush()
            title = _safe_filename(keyword)
            key = f"publish/videos/{time.strftime('%Y%m%d')}/{title}-{uuid.uuid4().hex[:8]}.mp4"
            return upload_file_to_cos(fp.name, key) or ""


def _generate_single_video(keyword: str):
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"keyword": "", "error": "关键词为空"}
    try:
        copy_text = _generate_copy(keyword)
        try:
            task_id = _submit_video_task(copy_text)
            used_safe_fallback = False
        except SensitiveContentError:
            copy_text = _rewrite_copy_for_safety(keyword, copy_text)
            task_id = _submit_video_task(copy_text)
            used_safe_fallback = True
        source_video_url = _wait_for_video_url(task_id)
        cos_video_url = _upload_video_to_cos(source_video_url, keyword)
        final_video_url = cos_video_url or source_video_url
        size_bytes = _head_content_length(final_video_url)
        title = _safe_filename(keyword)
        return {
            "keyword": keyword,
            "video_title": title,
            "file_name": f"{title}.mp4",
            "copy_text": copy_text,
            "task_id": task_id,
            "video_url": final_video_url,
            "source_video_url": source_video_url,
            "video_ratio": VIDEO_RATIO,
            "video_resolution": "1080p",
            "video_format": "mp4",
            "video_duration": VIDEO_DURATION_SECONDS,
            "video_size_bytes": size_bytes,
            "video_size_human": _format_size(size_bytes),
            "stored_on_cos": bool(cos_video_url),
            "used_safe_fallback": used_safe_fallback,
        }
    except Exception as exc:
        return {"keyword": keyword, "error": str(exc)}


class PublishHealthAPIView(APIView):
    def get(self, request):
        return Response({"msg": "publish.app ready"})


@api_view(["GET"])
def hot_list(request):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        words = _fetch_hot_list(limit=20)
    except Exception as exc:
        return Response({"msg": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response({"words": words})


@api_view(["POST"])
def auto_publish(request):
    user = _resolve_user(request)
    if not user:
        return Response({"msg": "未登录"}, status=status.HTTP_401_UNAUTHORIZED)
    raw_keywords = request.data.get("keywords")
    if isinstance(raw_keywords, list):
        keywords = []
        for item in raw_keywords:
            word = str(item or "").strip()
            if word and word not in keywords:
                keywords.append(word)
    else:
        single = str(request.data.get("keyword") or "").strip()
        keywords = [single] if single else []

    if not keywords:
        try:
            keywords = [_fetch_hot_keyword()]
        except Exception as exc:
            return Response({"msg": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    keywords = keywords[:MAX_KEYWORDS_PER_BATCH]

    start_ts = time.time()
    results = [None] * len(keywords)
    worker_count = min(5, len(keywords))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_generate_single_video, word): idx
            for idx, word in enumerate(keywords)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = {"keyword": keywords[idx], "error": str(exc)}

    ok_count = len([r for r in results if r and not r.get("error")])
    elapsed = round(time.time() - start_ts, 2)
    return Response(
        {
            "msg": f"自动发布流程完成（成功 {ok_count}/{len(results)}）",
            "count": len(results),
            "ok_count": ok_count,
            "elapsed_seconds": elapsed,
            "results": results,
        }
    )
