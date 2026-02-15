import os
import re
from datetime import date, timedelta, timezone, datetime
import requests
from bs4 import BeautifulSoup
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response


class HotspotListAPIView(APIView):
    """
    兼容前端原有的 AI 热点列表接口，占位返回空数组，后续可接入真实源。
    """

    def get(self, request):
        return Response([])


class SkillsLeaderboardAPIView(APIView):
    """
    爬取 skills.sh / trending / hot，解析榜单返回 JSON。
    """

    VIEW_MAP = {
        "all": "https://skills.sh/",
        "trending": "https://skills.sh/trending",
        "hot": "https://skills.sh/hot",
    }

    def get(self, request):
        view = request.query_params.get("view", "all").lower()
        target = self.VIEW_MAP.get(view, self.VIEW_MAP["all"])

        html = ""
        try:
            resp = requests.get(target, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            # 留到后面的本地兜底
            html = ""

        items = self._parse_leaderboard(html, view)
        if items:
            return Response({"items": items, "view": view})

        return Response({"msg": "未能解析榜单，请稍后重试"}, status=502)

    def _parse_leaderboard(self, html: str, view: str):
        items = self._try_parse_all(html)
        if items:
            return items

        local_html = self._load_local_html(view)
        if local_html:
            items = self._try_parse_all(local_html)
            if items:
                return items

        return []

    def _try_parse_all(self, html: str):
        if not html:
            return []
        # 1) 原始（去掉转义）做 JSON 片段解析
        html_clean = html.replace('\\"', '"')
        items = self._parse_from_json_like(html_clean)
        if items:
            return items
        # 2) 将 HTML 去标签后的纯文本再试一次 JSON 片段解析
        text = BeautifulSoup(html_clean, "html.parser").get_text("\n", strip=True)
        text = text.replace('\\"', '"')
        items = self._parse_from_json_like(text)
        if items:
            return items
        # 3) 文本模式（适配 r.jina.ai Markdown 风格）
        items = self._parse_from_text(text)
        return items

    def _load_local_html(self, view: str):
        base = os.environ.get("SKILLS_HTML_DIR", "/Users/zhanghenghao/Documents/document")
        name_map = {
            "all": "skills.sh.html",
            "trending": "skills2.html",
            "hot": "skills3.html",
        }
        filename = name_map.get(view, "skills.sh.html")
        path = os.path.join(base, filename)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            return ""

    def _parse_from_json_like(self, html: str):
        patterns = [
            re.compile(
                r'"source":"(?P<source>[^"]+?)".{0,600}?"name":"(?P<name>[^"]+?)".{0,300}?"installs":(?P<installs>\d+)',
                re.DOTALL,
            ),
            re.compile(
                r'"name":"(?P<name>[^"]+?)".{0,600}?"source":"(?P<source>[^"]+?)".{0,300}?"installs":(?P<installs>\d+)',
                re.DOTALL,
            ),
        ]
        items = []
        for pat in patterns:
            items.clear()
            for idx, m in enumerate(pat.finditer(html), start=1):
                items.append(
                    {
                        "rank": idx,
                        "name": m.group("name"),
                        "source": m.group("source"),
                        "installs": int(m.group("installs")),
                    }
                )
                if idx >= 100:
                    break
            if items:
                break
        return items

    def _parse_from_text(self, text: str):
        pattern = re.compile(r"\b(\d+)\s+###\s+([^\s]+)\s+([^\s]+)\s+([0-9.,Kk]+)\b")
        items = []
        for m in pattern.finditer(text):
            rank = int(m.group(1))
            name = m.group(2)
            source = m.group(3)
            installs_raw = m.group(4).replace(",", "")
            installs = (
                int(float(installs_raw[:-1]) * 1000)
                if installs_raw.lower().endswith("k")
                else int(float(installs_raw))
            )
            items.append({"rank": rank, "name": name, "source": source, "installs": installs})
        items.sort(key=lambda x: x["rank"])
        return items[:100]


class GithubHotProjectsAPIView(APIView):
    GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
    API_VERSION = "2022-11-28"
    LANG_ALLOWLIST = {"all", "python", "javascript"}

    def get(self, request):
        lang = (request.query_params.get("lang") or "all").lower().strip()
        if lang not in self.LANG_ALLOWLIST:
            return Response({"msg": "lang 仅支持 all/python/javascript"}, status=400)

        days = self._safe_int(request.query_params.get("days"), default=7, min_v=1, max_v=30)
        per_page = self._safe_int(request.query_params.get("per_page"), default=20, min_v=5, max_v=50)
        cache_seconds = self._safe_int(
            os.environ.get("GITHUB_HOT_CACHE_SECONDS"), default=600, min_v=60, max_v=3600
        )
        cache_key = f"aihotspot:github:hot:{lang}:{days}:{per_page}"
        backup_key = f"{cache_key}:backup"

        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        query = self._build_query(days, lang)
        try:
            payload = self._fetch(query=query, per_page=per_page)
            items = self._map_items(payload.get("items", []))
            result = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "lang": lang,
                "days": days,
                "total_count": int(payload.get("total_count") or 0),
                "items": items,
            }
            cache.set(cache_key, result, timeout=cache_seconds)
            cache.set(backup_key, result, timeout=None)
            return Response(result)
        except Exception as exc:
            backup = cache.get(backup_key)
            if backup:
                backup["stale"] = True
                backup["msg"] = f"GitHub 拉取失败，已返回缓存: {exc}"
                return Response(backup)
            return Response({"msg": f"GitHub 拉取失败: {exc}"}, status=502)

    def _safe_int(self, raw, default, min_v, max_v):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(min_v, min(max_v, value))

    def _build_query(self, days: int, lang: str):
        since = (date.today() - timedelta(days=days)).isoformat()
        if lang == "all":
            return f"stars:>200 pushed:>{since}"
        if lang == "python":
            return f"stars:>100 pushed:>{since} language:Python"
        return f"stars:>100 pushed:>{since} language:JavaScript"

    def _fetch(self, query: str, per_page: int):
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        user_agent = (os.environ.get("GITHUB_USER_AGENT") or "aihotspot-github-ranking").strip()
        timeout = self._safe_int(os.environ.get("GITHUB_API_TIMEOUT"), default=20, min_v=5, max_v=60)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION,
            "User-Agent": user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
            "page": 1,
        }
        resp = requests.get(self.GITHUB_SEARCH_URL, headers=headers, params=params, timeout=timeout)
        if resp.status_code >= 400:
            text = (resp.text or "")[:300]
            raise RuntimeError(f"{resp.status_code} {text}")
        return resp.json()

    def _map_items(self, raw_items):
        items = []
        for idx, repo in enumerate(raw_items or [], start=1):
            owner = repo.get("owner") or {}
            items.append(
                {
                    "rank": idx,
                    "id": repo.get("id"),
                    "full_name": repo.get("full_name") or "",
                    "html_url": repo.get("html_url") or "",
                    "description": repo.get("description") or "",
                    "language": repo.get("language") or "",
                    "stargazers_count": int(repo.get("stargazers_count") or 0),
                    "forks_count": int(repo.get("forks_count") or 0),
                    "open_issues_count": int(repo.get("open_issues_count") or 0),
                    "pushed_at": repo.get("pushed_at") or "",
                    "owner": {
                        "login": owner.get("login") or "",
                        "avatar_url": owner.get("avatar_url") or "",
                    },
                }
            )
        return items
