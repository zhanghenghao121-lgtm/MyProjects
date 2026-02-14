import os
import re
import requests
from bs4 import BeautifulSoup
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
