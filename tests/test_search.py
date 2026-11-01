"""联网搜索模块的单元测试。"""

import asyncio

from app.core.search import (
    SearchResult,
    WebSearchClient,
    format_search_context,
    needs_search,
    parse_results,
)


def test_needs_search_for_explicit_request():
    assert needs_search("帮我查一下明天的天气")
    assert needs_search("最近的新闻")
    assert needs_search("这个多少钱")


def test_needs_search_for_live_question():
    assert needs_search("今天天气怎么样？")
    assert needs_search("最近有什么好看的电影吗")


def test_needs_search_skips_casual_chat():
    assert not needs_search("你好呀")
    assert not needs_search("今天好累啊")
    assert not needs_search("最近怎么样")
    assert not needs_search("晚安")
    assert not needs_search("")


def test_parse_results_skips_invalid_items():
    data = {
        "results": [
            {
                "title": "标题",
                "url": "https://example.com/a",
                "summary": "摘要",
                "site_name": "示例站",
                "date": "2026-11-01T00:00:00+00:00",
            },
            {"title": "", "summary": "", "url": "https://example.com/b"},
            "not a dict",
        ]
    }
    results = parse_results(data)
    assert len(results) == 1
    assert results[0].title == "标题"
    assert results[0].date == "2026-11-01"


def test_parse_results_handles_bad_payload():
    assert parse_results({}) == []
    assert parse_results({"results": None}) == []
    assert parse_results(None) == []


def test_format_search_context_includes_title_and_link():
    text = format_search_context(
        [
            SearchResult(
                title="今日新闻",
                url="https://example.com/news",
                summary="某地发布了新政策。",
                site_name="示例站",
                date="2026-11-01",
            )
        ]
    )
    assert "【联网搜索结果" in text
    assert "今日新闻" in text
    assert "示例站" in text
    assert "https://example.com/news" in text


def test_format_search_context_limits_length():
    results = [
        SearchResult(
            title=f"标题{index}",
            url="https://example.com/" + "a" * 100,
            summary="内容" * 200,
        )
        for index in range(10)
    ]
    text = format_search_context(results)
    assert len(text) <= 1200
    assert "标题1" in text


def test_client_without_key_returns_empty():
    client = WebSearchClient("", "https://api.deepseek.com")

    async def run():
        results = await client.search("新闻")
        await client.aclose()
        return results

    assert asyncio.run(run()) == []
