"""消息里链接的解析：B 站视频取标题/简介/热评/弹幕，普通网页取标题和正文摘要。"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https?://[^\s<>\"'（）()【】\[\]{}，。！？、；：]+", re.IGNORECASE
)
_TRAILING = ".,;:!?)]}>\"'，。！？、；：」』）】”’"
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_BILI_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")

_MAX_LINK_CHARS = 900
_MAX_BODY_CHARS = 500
_MAX_DESC_CHARS = 200
_MAX_REPLY_CHARS = 70
_MAX_DANMAKU = 10

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
_BILI_REFERER = {"Referer": "https://www.bilibili.com/"}

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|169\.254\.|::1)",
    re.IGNORECASE,
)


def _is_safe_url(url: str) -> bool:
    """只抓 http/https 的公网地址，避免群里发内网链接让机器人乱访问。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    host = (parts.hostname or "").strip()
    if not host or "@" in parts.netloc:
        return False
    return not _PRIVATE_HOST_RE.match(host)


def _trim_url(url: str) -> str:
    return url.strip().rstrip(_TRAILING)


def extract_links(text: str, attachments: Iterable[Any] = (), max_links: int = 2) -> List[str]:
    """从消息文字和卡片附件里提取链接，去重并限制数量。"""
    if max_links <= 0:
        return []
    links: List[str] = []

    def add(candidate: str) -> None:
        url = _trim_url(candidate)
        if not url.startswith(("http://", "https://")):
            return
        if url in links or len(links) >= max_links:
            return
        links.append(url)

    for match in _URL_RE.finditer(text or ""):
        add(match.group(0))
    for attachment in attachments:
        if getattr(attachment, "kind", "") != "other":
            continue
        add(str(getattr(attachment, "url", "") or ""))
    return links


def _collapse(text: str) -> str:
    """压缩空白：行内多空格变一个，连续空行最多保留一个。"""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    text = _collapse(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


_BLOCKED_TAGS_RE = re.compile(
    r"<(script|style|noscript|iframe|svg|head)[^>]*>.*?</\1>", re.IGNORECASE | re.S
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.S)
_META_TAG_RE = re.compile(r"<meta\s+([^>]+?)\s*/?>", re.IGNORECASE | re.S)
_ATTR_RE = re.compile(r"([^\s=]+)\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)


def html_to_text(raw: str) -> str:
    """去掉脚本、注释和标签，还原实体，得到可读正文。"""
    if not raw:
        return ""
    text = _COMMENT_RE.sub("", raw)
    text = _BLOCKED_TAGS_RE.sub(" ", text)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    return _collapse(html_lib.unescape(text))


def _meta_attrs(raw: str) -> Dict[str, str]:
    """把 <meta ...> 里的属性和内容配成字典（小写键）。"""
    attrs: Dict[str, str] = {}
    for key, value in _ATTR_RE.findall(raw):
        attrs[key.strip().lower()] = html_lib.unescape(value).strip()
    return attrs


def parse_webpage(raw_html: str) -> Tuple[str, str]:
    """从 HTML 里取出标题和摘要（优先 og:title / description）。"""
    title = ""
    description = ""
    match = _TITLE_RE.search(raw_html)
    if match:
        title = html_to_text(match.group(1))
    for tag in _META_TAG_RE.findall(raw_html):
        attrs = _meta_attrs(tag)
        key = (attrs.get("name") or attrs.get("property") or "").lower()
        content = attrs.get("content") or ""
        if not content:
            continue
        if key in {"og:title", "twitter:title"} and not title:
            title = _collapse(content)
        elif key in {"description", "og:description", "twitter:description"} and not description:
            description = _collapse(content)
    return title, description


def format_webpage(url: str, raw_html: str, max_chars: int = _MAX_LINK_CHARS) -> str:
    """把网页 HTML 格式化成一段给模型看的文字；内容太少就返回空。"""
    title, description = parse_webpage(raw_html)
    body = html_to_text(raw_html)
    if title and title in body:
        body = body.replace(title, "", 1).strip()
    if not title and not description and len(body) < 40:
        return ""
    parts = [f"【网页】{title or url}"]
    if description:
        parts.append(f"摘要：{_truncate(description, _MAX_DESC_CHARS)}")
    if body:
        parts.append(f"正文摘录：{_truncate(body, _MAX_BODY_CHARS)}")
    parts.append(f"链接：{url[:200]}")
    return _truncate("\n".join(parts), max_chars)


@dataclass(frozen=True)
class VideoInfo:
    """B 站视频的基本信息，字段缺省时为空字符串或 0。"""

    bvid: str
    aid: int
    cid: int
    title: str
    owner: str
    desc: str
    duration: int
    views: int
    likes: int
    pubdate: int
    category: str


def parse_bilibili_video(data: Any) -> Optional[VideoInfo]:
    """把 /x/web-interface/view 返回的 data 转成 VideoInfo。"""
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    cid = 0
    if pages and isinstance(pages[0], dict):
        try:
            cid = int(pages[0].get("cid") or 0)
        except (TypeError, ValueError):
            cid = 0
    if not cid:
        try:
            cid = int(data.get("cid") or 0)
        except (TypeError, ValueError):
            cid = 0
    try:
        aid = int(data.get("aid") or 0)
    except (TypeError, ValueError):
        aid = 0
    try:
        duration = int(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    try:
        views = int(stat.get("view") or 0)
    except (TypeError, ValueError):
        views = 0
    try:
        likes = int(stat.get("like") or 0)
    except (TypeError, ValueError):
        likes = 0
    try:
        pubdate = int(data.get("pubdate") or 0)
    except (TypeError, ValueError):
        pubdate = 0
    return VideoInfo(
        bvid=str(data.get("bvid") or ""),
        aid=aid,
        cid=cid,
        title=title,
        owner=str(owner.get("name") or "").strip(),
        desc=_collapse(str(data.get("desc") or "")),
        duration=max(0, duration),
        views=max(0, views),
        likes=max(0, likes),
        pubdate=max(0, pubdate),
        category=str(data.get("tname") or "").strip(),
    )


def _human_count(number: int) -> str:
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if number >= 10_000:
        return f"{number / 10_000:.1f}万".replace(".0万", "万")
    return str(number)


def _duration_text(seconds: int) -> str:
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _date_text(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(timestamp))
    except (OSError, ValueError):
        return ""


def format_bilibili(
    info: VideoInfo,
    tags: Sequence[str] = (),
    replies: Sequence[Tuple[str, str]] = (),
    danmaku: Sequence[str] = (),
    url: str = "",
    max_chars: int = _MAX_LINK_CHARS,
) -> str:
    """把视频信息、标签、热评和弹幕拼成一段给模型看的文字。"""
    lines = [f"【B站视频】{info.title}"]
    meta = []
    if info.owner:
        meta.append(f"UP主：{info.owner}")
    if info.duration:
        meta.append(f"时长：{_duration_text(info.duration)}")
    if info.views:
        meta.append(f"播放：{_human_count(info.views)}")
    if info.likes:
        meta.append(f"点赞：{_human_count(info.likes)}")
    if info.category:
        meta.append(f"分区：{info.category}")
    published = _date_text(info.pubdate)
    if published:
        meta.append(f"发布：{published}")
    if meta:
        lines.append("｜".join(meta))
    if info.desc:
        lines.append(f"简介：{_truncate(info.desc, _MAX_DESC_CHARS)}")
    if tags:
        lines.append("标签：" + " / ".join(tags[:8]))
    if replies:
        hot = "；".join(
            f"{name}：{_truncate(text, _MAX_REPLY_CHARS)}" for name, text in replies[:3]
        )
        lines.append(f"热评：{hot}")
    if danmaku:
        lines.append("弹幕摘录：" + " / ".join(danmaku[:_MAX_DANMAKU]))
    if url:
        lines.append(f"链接：{url[:200]}")
    return _truncate("\n".join(lines), max_chars)


_DANMAKU_RE = re.compile(r"<d p=\"[^\"]*\">(.*?)</d>", re.S)


def extract_danmaku(raw_xml: str) -> List[str]:
    """从弹幕 XML 里取出文字，过滤重复和超长内容。"""
    items: List[str] = []
    seen = set()
    for match in _DANMAKU_RE.finditer(raw_xml or ""):
        text = _collapse(html_lib.unescape(match.group(1)))
        if not text or len(text) > 50 or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= _MAX_DANMAKU:
            break
    return items


def parse_replies(data: Any) -> List[Tuple[str, str]]:
    """从 /x/v2/reply 返回的 data 里取出前几条热评（昵称, 内容）。"""
    if not isinstance(data, dict):
        return []
    replies = data.get("replies")
    if not isinstance(replies, list):
        return []
    items: List[Tuple[str, str]] = []
    for item in replies:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), dict) else {}
        text = _collapse(str(content.get("message") or ""))
        if not text:
            continue
        member = item.get("member") if isinstance(item.get("member"), dict) else {}
        name = str(member.get("uname") or "网友").strip()
        items.append((name, text))
        if len(items) >= 3:
            break
    return items


def parse_tags(data: Any) -> List[str]:
    """从 /x/tag/archive/tags 返回的 data 里取出标签名。"""
    if not isinstance(data, list):
        return []
    tags: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tag_name") or "").strip()
        if name and name not in tags:
            tags.append(name)
    return tags


class LinkReader:
    """抓取链接内容。任何失败都只记日志并返回空串，不影响正常聊天。"""

    def __init__(
        self,
        timeout: float = 10.0,
        max_links: int = 2,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._timeout = max(1.0, timeout)
        self._max_links = max(0, max_links)
        self._client = client or httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True, headers=_HEADERS
        )
        self._buvid_ready = False
        self._buvid_lock = asyncio.Lock()

    async def read_many(self, links: Sequence[str]) -> str:
        """并发读取多条链接，拼成一段「【链接内容】」文字；全部失败时返回空。"""
        targets = [url for url in links if _is_safe_url(url)][: self._max_links]
        if not targets:
            return ""
        results = await asyncio.gather(*(self._read_safe(url) for url in targets))
        blocks = [item for item in results if item]
        if not blocks:
            return ""
        header = "【链接内容（程序抓取的页面文字，可能不完整）】"
        return "\n\n".join([header, *blocks])

    async def read(self, url: str) -> str:
        """读取单个链接；不安全的地址或失败时返回空串。"""
        if not _is_safe_url(url):
            return ""
        return await self._read_safe(url)

    async def _read_safe(self, url: str) -> str:
        try:
            return await self._read_one(url)
        except Exception as exc:  # 第三方页面异常不能影响聊天
            logger.debug("链接解析失败（%s）：%s", url, exc)
            return ""

    async def _read_one(self, url: str) -> str:
        target = await self._resolve_redirect(url)
        if any(host in urlsplit(target).netloc for host in _BILI_HOSTS):
            return await self._read_bilibili(target)
        return await self._read_web(target)

    async def _resolve_redirect(self, url: str) -> str:
        """b23.tv 这类短链先跟随跳转，拿到真实地址。"""
        if "b23.tv" not in url and "bili2233.cn" not in url:
            return url
        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            return url
        return str(response.url)

    async def _read_web(self, url: str) -> str:
        raw = await self._get_text(url)
        if not raw:
            return ""
        return format_webpage(url, raw)

    async def _read_bilibili(self, url: str) -> str:
        bvid = _BV_RE.search(url)
        aid = _AV_RE.search(url)
        if not bvid and not aid:
            return await self._read_web(url)
        await self._ensure_buvid()
        params = {"bvid": bvid.group(0)} if bvid else {"aid": aid.group(1)}
        data = await self._bili_api("https://api.bilibili.com/x/web-interface/view", params)
        info = parse_bilibili_video(data)
        if info is None:
            return await self._read_web(url)

        tasks = [
            self._bili_api("https://api.bilibili.com/x/tag/archive/tags", {"bvid": info.bvid}),
        ]
        if info.aid:
            tasks.append(
                self._bili_api(
                    "https://api.bilibili.com/x/v2/reply",
                    {"type": 1, "oid": info.aid, "sort": 2, "ps": 5, "pn": 1},
                )
            )
        else:
            tasks.append(asyncio.sleep(0, result=None))
        if info.cid:
            tasks.append(
                self._get_text(
                    "https://api.bilibili.com/x/v1/dm/list.so",
                    params={"oid": info.cid},
                )
            )
        else:
            tasks.append(asyncio.sleep(0, result=None))
        tags_data, reply_data, danmaku_raw = await asyncio.gather(*tasks)
        return format_bilibili(
            info,
            tags=parse_tags(tags_data),
            replies=parse_replies(reply_data),
            danmaku=extract_danmaku(str(danmaku_raw or "")),
            url=url,
        )

    async def _ensure_buvid(self) -> None:
        """B 站接口不带设备 Cookie 会返回 412，先取 buvid3/buvid4。"""
        if self._buvid_ready:
            return
        async with self._buvid_lock:
            if self._buvid_ready:
                return
            self._buvid_ready = True
            try:
                response = await self._client.get(
                    "https://api.bilibili.com/x/frontend/finger/spi"
                )
                data = response.json().get("data") or {}
            except (httpx.HTTPError, ValueError, AttributeError):
                return
            if not isinstance(data, dict):
                return
            for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
                value = str(data.get(key) or "").strip()
                if value:
                    self._client.cookies.set(name, value)

    async def _bili_api(self, url: str, params: Dict[str, Any]) -> Any:
        """调用 B 站 JSON 接口，code 非 0 或失败时返回 None。"""
        try:
            response = await self._client.get(url, params=params, headers=_BILI_REFERER)
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        return payload.get("data")

    async def _get_text(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        """下载文本内容，限制大小和非文本类型。"""
        try:
            response = await self._client.get(url, params=params)
            if response.status_code != 200:
                return ""
        except httpx.HTTPError:
            return ""
        if len(response.content) > 4 * 1024 * 1024:
            return ""
        content_type = (response.headers.get("content-type") or "").lower()
        if not any(token in content_type for token in ("text", "html", "xml", "json")):
            return ""
        return response.text

    async def aclose(self) -> None:
        await self._client.aclose()
