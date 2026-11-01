import asyncio
import json

import httpx

from app.core.link import (
    LinkReader,
    extract_danmaku,
    extract_links,
    format_bilibili,
    format_webpage,
    parse_bilibili_video,
    parse_replies,
    parse_tags,
)
from app.onebot.events import Attachment, parse_event


def test_extract_links_trims_and_dedupes():
    text = "看这个 https://b23.tv/AbCd12，好笑。还有 https://example.com/a 和 https://example.com/a"
    links = extract_links(text, max_links=2)
    assert links == ["https://b23.tv/AbCd12", "https://example.com/a"]


def test_extract_links_ignores_images_and_caps_count():
    attachments = (
        Attachment("image", "图片", "https://example.com/a.png"),
        Attachment("other", "卡片", "https://b23.tv/xyz"),
        Attachment("other", "卡片", "https://example.com/b"),
        Attachment("other", "卡片", "https://example.com/c"),
    )
    links = extract_links("", attachments, max_links=2)
    assert links == ["https://b23.tv/xyz", "https://example.com/b"]


def test_json_card_url_reaches_event():
    card = {
        "app": "com.tencent.structmsg",
        "meta": {"news": {"jumpUrl": "https://b23.tv/AbCd12"}},
    }
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 10,
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 99999,
        "message": [{"type": "json", "data": {"data": json.dumps(card)}}],
    }
    event = parse_event(payload)
    assert event is not None
    assert event.has_media
    assert event.attachments[0].kind == "other"
    assert event.attachments[0].label == "卡片"
    assert event.attachments[0].url == "https://b23.tv/AbCd12"


def test_xml_card_url_reaches_event():
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 11,
        "user_id": 10002,
        "self_id": 99999,
        "message": [
            {
                "type": "xml",
                "data": {
                    "data": (
                        '<?xml version="1.0"?><msg><item><url>'
                        "https://b23.tv/XyZ789</url></item></msg>"
                    )
                },
            }
        ],
    }
    event = parse_event(payload)
    assert event is not None
    assert event.attachments[0].url == "https://b23.tv/XyZ789"


def test_cq_json_card_url_reaches_event():
    raw = (
        '[CQ:json,data={"app":"com.tencent.miniapp_01",'
        '"meta":{"detail_1":{"qqdocurl":"https://b23.tv/QwEr12"}}}]'
    )
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 12,
        "user_id": 10003,
        "self_id": 99999,
        "message": raw,
    }
    event = parse_event(payload)
    assert event is not None
    assert event.attachments[0].url == "https://b23.tv/QwEr12"


def test_format_webpage_uses_title_and_description():
    raw = (
        "<html><head><title>测试文章</title>"
        '<meta name="description" content="这是一篇测试文章的摘要。">'
        "</head><body><script>var a=1;</script>"
        "<p>正文第一段。</p><p>正文第二段。</p></body></html>"
    )
    text = format_webpage("https://example.com/a", raw)
    assert "【网页】测试文章" in text
    assert "这是一篇测试文章的摘要" in text
    assert "正文第一段" in text
    assert "var a=1" not in text


def test_format_webpage_returns_empty_for_thin_content():
    assert format_webpage("https://example.com/a", "<html><body>hi</body></html>") == ""


VIDEO_DATA = {
    "bvid": "BV1xx411c7mD",
    "aid": 12345,
    "cid": 999,
    "title": "测试视频标题",
    "desc": "这是一个测试视频的简介。",
    "duration": 125,
    "pubdate": 1700000000,
    "tname": "生活区",
    "owner": {"name": "测试UP主"},
    "stat": {"view": 123456, "like": 2345},
    "pages": [{"cid": 999}],
}


def test_parse_bilibili_video():
    info = parse_bilibili_video(VIDEO_DATA)
    assert info is not None
    assert info.bvid == "BV1xx411c7mD"
    assert info.aid == 12345
    assert info.cid == 999
    assert info.owner == "测试UP主"
    assert info.views == 123456


def test_format_bilibili_contains_all_parts():
    info = parse_bilibili_video(VIDEO_DATA)
    text = format_bilibili(
        info,
        tags=["测试", "生活"],
        replies=[("网友A", "这个视频不错")],
        danmaku=["哈哈", "来了"],
        url="https://www.bilibili.com/video/BV1xx411c7mD",
    )
    assert "【B站视频】测试视频标题" in text
    assert "UP主：测试UP主" in text
    assert "时长：2:05" in text
    assert "播放：12.3万" in text
    assert "标签：测试 / 生活" in text
    assert "热评：网友A：这个视频不错" in text
    assert "弹幕摘录：哈哈 / 来了" in text


def test_parse_tags_and_replies_tolerate_bad_data():
    assert parse_tags(None) == []
    assert parse_tags([{"tag_name": "a"}, {"foo": 1}]) == ["a"]
    assert parse_replies(None) == []
    assert parse_replies({"replies": [{"content": {"message": "hi"}, "member": {"uname": "u"}}]}) == [
        ("u", "hi")
    ]


def test_extract_danmaku_filters_and_unescapes():
    xml = (
        '<i><d p="1,1,25">哈哈</d><d p="1,1,25">哈哈</d>'
        '<d p="1,1,25">a &amp; b</d><d p="1,1,25"></d></i>'
    )
    assert extract_danmaku(xml) == ["哈哈", "a & b"]


class FakeResponse:
    def __init__(self, url: str, text: str, content_type: str = "text/html; charset=utf-8"):
        self.status_code = 200
        self.url = url
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        return json.loads(self.text)


class FakeClient:
    async def get(self, url, params=None, headers=None):
        if "/x/web-interface/view" in url:
            return FakeResponse(
                url,
                json.dumps({"code": 0, "data": VIDEO_DATA}),
                "application/json",
            )
        if "/x/tag/archive/tags" in url:
            return FakeResponse(
                url,
                json.dumps({"code": 0, "data": [{"tag_name": "测试"}]}),
                "application/json",
            )
        if "/x/v2/reply" in url:
            return FakeResponse(
                url,
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "replies": [
                                {
                                    "content": {"message": "好看"},
                                    "member": {"uname": "网友"},
                                }
                            ]
                        },
                    }
                ),
                "application/json",
            )
        if "/x/v1/dm/list.so" in url:
            return FakeResponse(url, '<i><d p="1">哈哈</d></i>', "text/xml")
        return FakeResponse(
            url,
            "<html><head><title>示例网页</title>"
            '<meta name="description" content="网页摘要内容。">'
            "</head><body><p>正文内容。</p></body></html>",
        )

    async def aclose(self):
        return None


class BrokenClient:
    async def get(self, url, params=None, headers=None):
        raise httpx.ConnectError("模拟断网", request=httpx.Request("GET", url))

    async def aclose(self):
        return None


def test_reader_formats_bilibili_video():
    reader = LinkReader(client=FakeClient())
    text = asyncio.run(reader.read("https://www.bilibili.com/video/BV1xx411c7mD"))
    assert "【B站视频】测试视频标题" in text
    assert "标签：测试" in text
    assert "热评：网友：好看" in text
    assert "弹幕摘录：哈哈" in text


def test_reader_formats_plain_webpage():
    reader = LinkReader(client=FakeClient())
    text = asyncio.run(reader.read("https://example.com/a"))
    assert "【网页】示例网页" in text
    assert "网页摘要内容" in text


def test_reader_returns_empty_on_network_error():
    reader = LinkReader(client=BrokenClient())
    text = asyncio.run(reader.read_many(["https://example.com/a"]))
    assert text == ""


def test_reader_rejects_private_hosts():
    reader = LinkReader(client=BrokenClient())
    assert asyncio.run(reader.read("http://127.0.0.1:8080/health")) == ""
    assert asyncio.run(reader.read("http://192.168.1.1/admin")) == ""
    assert asyncio.run(reader.read("http://[::1]/admin")) == ""


def test_reader_limits_link_count():
    reader = LinkReader(max_links=1, client=FakeClient())
    text = asyncio.run(
        reader.read_many(["https://example.com/a", "https://example.com/b"])
    )
    assert text.count("【网页】") == 1
