"""DeepSeek 联网搜索接口（/v1/tools/search），复用同一个 API Key，不引入新依赖。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_QUERY_CHARS = 128
_MAX_SUMMARY_CHARS = 300
_MAX_CONTEXT_CHARS = 900

# 明确要求联网的说法：命中任意一个就搜索
_SEARCH_WORDS = (
    "搜一下",
    "搜索一下",
    "搜索",
    "搜搜",
    "查一下",
    "查查",
    "查下",
    "帮我查",
    "帮我搜",
    "百度一下",
    "谷歌一下",
    "官网",
    "最新",
    "新闻",
    "热搜",
    "多少钱",
    "价格",
    "报价",
)
# 时效性较强的话题词：和疑问词同时出现才搜索，避免把“今天好累”也当成查资料
_LIVE_WORDS = (
    "今天",
    "今日",
    "现在",
    "目前",
    "最近",
    "近期",
    "刚才",
    "昨天",
    "明天",
    "本周",
    "今年",
    "天气",
    "股价",
    "汇率",
    "比分",
    "赛程",
    "上映",
    "发布",
    "新版本",
    "更新",
    "附近",
    "多少钱",
)
_QUESTION_WORDS = (
    "?",
    "？",
    "吗",
    "什么",
    "怎么",
    "为什么",
    "多少",
    "哪",
    "谁",
    "几",
    "是不是",
    "有没有",
    "能不能",
    "要不要",
)
# 常见的寒暄问句，虽然含“最近/今天 + 怎么”，但不需要查资料
_CASUAL_PHRASES = (
    "最近怎么样",
    "最近好吗",
    "最近咋样",
    "今天怎么样",
    "现在怎么样",
    "最近过得",
)


def needs_search(text: str) -> bool:
    """按关键词判断是否需要联网：普通闲聊和问候不搜，省时间也省钱。"""
    text = (text or "").strip()
    if len(text) < 2:
        return False
    lowered = text.lower()
    if any(word in lowered for word in _SEARCH_WORDS):
        return True
    if any(phrase in text for phrase in _CASUAL_PHRASES):
        return False
    return any(word in text for word in _LIVE_WORDS) and any(
        word in text for word in _QUESTION_WORDS
    )


@dataclass(frozen=True)
class SearchResult:
    """一条网页搜索结果。"""

    title: str
    url: str
    summary: str = ""
    site_name: str = ""
    date: str = ""


def parse_results(data: Dict[str, Any]) -> List[SearchResult]:
    """把接口返回的 results 数组转成 SearchResult；结构异常时跳过。"""
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    results: List[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        summary = str(item.get("summary") or item.get("snippet") or "").strip()
        if not title and not summary:
            continue
        site_name = str(item.get("site_name") or "").strip()
        date = str(item.get("date") or "").strip()[:10]
        results.append(
            SearchResult(
                title=title or url,
                url=url,
                summary=summary,
                site_name=site_name,
                date=date,
            )
        )
    return results


def format_search_context(results: List[SearchResult]) -> str:
    """把搜索结果拼成一段可放进提示词的文字，并控制总长度。"""
    if not results:
        return ""
    lines = ["【联网搜索结果（实时网页，仅供参考）】"]
    used = 0
    for index, item in enumerate(results, start=1):
        summary = item.summary.replace("\n", " ").strip()
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS].rstrip() + "…"
        meta = "｜".join(part for part in (item.site_name, item.date) if part)
        heading = f"{index}. {item.title}"
        if meta:
            heading = f"{heading}（{meta}）"
        parts = [heading]
        if summary:
            parts.append(summary)
        if item.url:
            parts.append(f"链接：{item.url[:200]}")
        block = "\n".join(parts)
        if used + len(block) > _MAX_CONTEXT_CHARS:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


def _error_detail(response: httpx.Response) -> str:
    """提取接口返回的错误说明，便于定位 401/402 等问题。"""
    try:
        data = response.json()
    except ValueError:
        return response.text[:120].replace("\n", " ")
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:120]
    return str(error or data)[:120]


class WebSearchClient:
    """联网搜索客户端。失败只记日志并返回空列表，由上层继续正常聊天。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        count: int = 3,
        timeout: float = 15.0,
        retries: int = 1,
    ) -> None:
        self._api_key = api_key
        self._count = max(1, min(50, count))
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def search(self, query: str, count: int | None = None) -> List[SearchResult]:
        """搜索网页并返回结果；没有 Key、无结果或失败时返回空列表。"""
        query = (query or "").strip()[:_MAX_QUERY_CHARS]
        if not query:
            return []
        if not self._api_key:
            logger.warning("未配置 DEEPSEEK_API_KEY，跳过联网搜索")
            return []
        payload: Dict[str, Any] = {
            "query": query,
            "count": max(1, min(50, count or self._count)),
            "need_content": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        error = "未知错误"
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(
                    "/v1/tools/search", json=payload, headers=headers
                )
                if response.status_code in _RETRYABLE_STATUS and attempt < self._retries:
                    error = f"HTTP {response.status_code}"
                    await asyncio.sleep(1.0)
                    continue
                response.raise_for_status()
                return parse_results(response.json())
            except httpx.HTTPStatusError as exc:
                error = (
                    f"HTTP {exc.response.status_code}："
                    f"{_error_detail(exc.response)}"
                )
                if exc.response.status_code in _RETRYABLE_STATUS and attempt < self._retries:
                    await asyncio.sleep(1.0)
                    continue
                break
            except httpx.HTTPError as exc:
                error = f"网络错误：{exc}"
                if attempt < self._retries:
                    await asyncio.sleep(1.0)
                    continue
                break
            except ValueError:
                error = "返回格式异常"
                break
        logger.warning("联网搜索失败：%s", error)
        return []

    async def aclose(self) -> None:
        await self._client.aclose()
