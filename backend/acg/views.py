import base64
import hashlib
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from rest_framework.decorators import api_view
from rest_framework.response import Response


ACG_BASE_URL = "https://acg.rip"
MAX_FETCH_PAGES = 30


def _to_abs_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if not href.startswith("/"):
        href = f"/{href}"
    return f"{ACG_BASE_URL}{href}"


def _extract_rows(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    rows = soup.select("table tr")
    page_results = []
    for row in rows:
        title_a = row.select_one("td.title span.title a")
        action_a = row.select_one("td.action a")
        size_td = row.select_one("td.size")
        if not title_a or not action_a or not size_td:
            continue
        title = title_a.get_text(strip=True)
        url = _to_abs_url(action_a.get("href", "").strip())
        size = size_td.get_text(strip=True)
        if not title or not url or not size:
            continue
        page_results.append({"title": title, "url": url, "size": size})
    return page_results


def _fetch_search_page(headers, keyword: str, page: int | None = None, page_param: str | None = None):
    params = {"term": keyword}
    if page and page_param:
        params[page_param] = page
    resp = requests.get(
        f"{ACG_BASE_URL}/",
        headers=headers,
        params=params,
        timeout=15,
    )
    return resp


def _parse_int(data: bytes, idx: int) -> int:
    end = data.index(b"e", idx)
    return end + 1


def _parse_bytes(data: bytes, idx: int) -> int:
    colon = data.index(b":", idx)
    length = int(data[idx:colon])
    return colon + 1 + length


def _parse_list(data: bytes, idx: int) -> int:
    i = idx + 1
    while data[i:i + 1] != b"e":
        i = _parse_any(data, i)
    return i + 1


def _parse_dict(data: bytes, idx: int) -> int:
    i = idx + 1
    while data[i:i + 1] != b"e":
        i = _parse_bytes(data, i)
        i = _parse_any(data, i)
    return i + 1


def _parse_any(data: bytes, idx: int) -> int:
    token = data[idx:idx + 1]
    if token == b"i":
        return _parse_int(data, idx + 1)
    if token == b"l":
        return _parse_list(data, idx)
    if token == b"d":
        return _parse_dict(data, idx)
    if token.isdigit():
        return _parse_bytes(data, idx)
    raise ValueError("invalid bencode")


def _extract_info_bytes(torrent_bytes: bytes) -> bytes:
    if not torrent_bytes.startswith(b"d"):
        raise ValueError("invalid torrent format")
    i = 1
    while torrent_bytes[i:i + 1] != b"e":
        key_start = i
        key_end = _parse_bytes(torrent_bytes, i)
        key = torrent_bytes[torrent_bytes.index(b":", key_start) + 1:key_end]
        val_start = key_end
        val_end = _parse_any(torrent_bytes, val_start)
        if key == b"info":
            return torrent_bytes[val_start:val_end]
        i = val_end
    raise ValueError("missing info section")


def _build_magnet(infohash_hex: str, display_name: str) -> str:
    dn = quote(display_name or "download")
    return f"magnet:?xt=urn:btih:{infohash_hex}&dn={dn}"


def _build_thunder(url: str) -> str:
    raw = f"AA{url}ZZ".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"thunder://{encoded}"


@api_view(["GET"])
def list_resources(request):
    keyword = (request.query_params.get("q") or "").strip()
    if not keyword:
        return Response([])

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": ACG_BASE_URL,
    }
    try:
        first_resp = _fetch_search_page(headers, keyword)
    except requests.RequestException as e:
        return Response({"msg": f"抓取失败: {e}"}, status=502)
    if first_resp.status_code != 200:
        return Response({"msg": "抓取失败"}, status=502)

    first_page_results = _extract_rows(first_resp.text)
    results = []
    seen_urls = set()
    for item in first_page_results:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        results.append(item)

    # Probe which pagination parameter works on this site.
    page_param = None
    if first_page_results:
        for candidate in ("page", "p"):
            try:
                page2_resp = _fetch_search_page(headers, keyword, page=2, page_param=candidate)
            except requests.RequestException:
                continue
            if page2_resp.status_code != 200:
                continue
            page2_results = _extract_rows(page2_resp.text)
            if any(item["url"] not in seen_urls for item in page2_results):
                page_param = candidate
                for item in page2_results:
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])
                    results.append(item)
                break

    if page_param:
        for page in range(3, MAX_FETCH_PAGES + 1):
            try:
                page_resp = _fetch_search_page(headers, keyword, page=page, page_param=page_param)
            except requests.RequestException:
                break
            if page_resp.status_code != 200:
                break
            page_results = _extract_rows(page_resp.text)
            if not page_results:
                break
            before = len(results)
            for item in page_results:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                results.append(item)
            if len(results) == before:
                # No new rows, likely reached the end or page parameter ignored.
                break

    return Response(results)


@api_view(["POST"])
def download_resource(request):
    torrent_url = (request.data.get("url") or "").strip()
    title = (request.data.get("title") or "download").strip()
    if not torrent_url:
        return Response({"msg": "缺少下载地址"}, status=400)

    if torrent_url.startswith("magnet:?"):
        magnet_url = torrent_url
    else:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
            "Referer": ACG_BASE_URL,
        }
        resp = requests.get(torrent_url, headers=headers, timeout=20)
        if resp.status_code != 200 or not resp.content:
            return Response({"msg": "获取 torrent 文件失败"}, status=502)
        try:
            info_bytes = _extract_info_bytes(resp.content)
            infohash_hex = hashlib.sha1(info_bytes).hexdigest()
        except Exception:
            return Response({"msg": "解析 torrent 失败"}, status=500)
        magnet_url = _build_magnet(infohash_hex, title)

    thunder_url = _build_thunder(magnet_url)
    return Response(
        {
            "title": title,
            "magnet_url": magnet_url,
            "thunder_url": thunder_url,
        }
    )
