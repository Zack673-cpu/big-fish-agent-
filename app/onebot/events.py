"""OneBot 11 消息事件解析。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

_IGNORED_TYPES = {"text", "at", "reply"}
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]")
_OTHER_LABELS = {
    "record": "语音",
    "video": "视频",
    "file": "文件",
    "json": "卡片",
    "xml": "卡片",
    "forward": "聊天记录",
}
_CARD_URL_KEYS = ("jumpUrl", "qqdocurl", "source_url", "url")
_HTTP_RE = re.compile(r"https?://[^\s<>\"'\u4e00-\u9fff]+", re.IGNORECASE)
_XML_URL_RE = re.compile(r"<url>(.*?)</url>", re.IGNORECASE | re.S)


@dataclass(frozen=True)
class Attachment:
    """消息里的非文字内容，供模型理解或作为提示。"""

    kind: str
    label: str = ""
    url: str = ""


def _decode_cq_value(text: str) -> str:
    """还原 CQ 码里转义过的逗号、方括号和 &。"""
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _first_url(text: str) -> str:
    match = _HTTP_RE.search(text or "")
    return match.group(0).rstrip(".,;:!?)]}>\"'") if match else ""


def _json_card_url(raw: str) -> str:
    """从分享卡片的 JSON 里找跳转链接，优先正式跳转字段。"""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _first_url(raw)
    preferred: list[str] = []
    others: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if key in _CARD_URL_KEYS:
                        preferred.append(value)
                    else:
                        others.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return preferred[0] if preferred else (others[0] if others else "")


def _xml_card_url(raw: str) -> str:
    """从 XML 卡片（如小程序、结构化分享）里找链接。"""
    for match in _XML_URL_RE.finditer(raw):
        value = _decode_cq_value(match.group(1)).strip()
        if value.startswith(("http://", "https://")):
            return value
    return _first_url(_decode_cq_value(raw))


def _record_source(data: Dict[str, Any]) -> str:
    """语音段优先用 http 地址，其次 NapCat 给的本地路径。"""
    url = str(data.get("url") or "")
    file = str(data.get("file") or "")
    if not url and file.startswith(("http://", "https://")):
        url = file
    if url.startswith(("http://", "https://")):
        return url
    return str(data.get("path") or file or url)


def _card_url(seg_type: str, data: Dict[str, Any]) -> str:
    """取出卡片里的链接；不是卡片或没有链接时返回空串。"""
    raw = data.get("data")
    if not isinstance(raw, str) or not raw.strip():
        raw = str(data.get("url") or "")
        return _first_url(raw)
    raw = _decode_cq_value(raw)
    if seg_type == "json":
        return _json_card_url(raw)
    return _xml_card_url(raw)


def _attachment(seg_type: str, data: Dict[str, Any]) -> Attachment:
    if seg_type == "image":
        label = "表情包" if str(data.get("sub_type") or "") == "1" else "图片"
        url = str(data.get("url") or "")
        if not url:
            candidate = str(data.get("file") or "")
            if candidate.startswith(("http://", "https://")):
                url = candidate
        return Attachment("image", label, url)
    if seg_type in {"face", "mface", "marketface"}:
        label = str(
            data.get("summary")
            or data.get("raw")
            or data.get("name")
            or data.get("id")
            or "未知表情"
        )
        return Attachment("face", label)
    if seg_type == "record":
        return Attachment("record", _OTHER_LABELS["record"], _record_source(data))
    if seg_type in {"json", "xml"}:
        return Attachment(
            "other",
            _OTHER_LABELS.get(seg_type, seg_type),
            _card_url(seg_type, data),
        )
    return Attachment("other", _OTHER_LABELS.get(seg_type, seg_type))


@dataclass(frozen=True)
class MessageEvent:
    """一次私聊或群聊消息。"""

    message_type: str
    sub_type: str
    message_id: int
    user_id: int
    group_id: Optional[int]
    self_id: int
    sender_name: str
    text: str
    at_me: bool
    has_media: bool
    attachments: Tuple[Attachment, ...] = ()
    unsupported: bool = False

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def is_private(self) -> bool:
        return self.message_type == "private"

    @property
    def chat_key(self) -> str:
        if self.is_group:
            return f"group:{self.group_id}"
        return f"private:{self.user_id}"


def _parse_segments(
    segments: list, self_id: int
) -> Tuple[str, bool, bool, Tuple[Attachment, ...]]:
    parts: list[str] = []
    attachments: list[Attachment] = []
    at_me = False
    has_media = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_type = segment.get("type")
        data = segment.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            if str(data.get("qq", "")) == str(self_id):
                at_me = True
        elif seg_type and seg_type not in _IGNORED_TYPES:
            # 表情、表情包、图片、语音、卡片等都算“有内容但没文字”
            has_media = True
            attachments.append(_attachment(str(seg_type), data))
    return "".join(parts), at_me, has_media, tuple(attachments)


def _parse_cq(raw: str, self_id: int) -> Tuple[str, bool, bool, Tuple[Attachment, ...]]:
    parts: list[str] = []
    attachments: list[Attachment] = []
    at_me = False
    has_media = False
    last = 0
    for match in _CQ_RE.finditer(raw):
        parts.append(raw[last:match.start()])
        cq_type = match.group(1)
        params = {}
        if match.group(2):
            # 卡片 JSON 里可能带未转义的逗号，只在逗号后紧跟 key= 时才拆
            for item in re.split(r",(?=[A-Za-z_]+=)", match.group(2)):
                if "=" in item:
                    key, value = item.split("=", 1)
                    params[key] = value
        if cq_type == "at" and params.get("qq") == str(self_id):
            at_me = True
        if cq_type not in _IGNORED_TYPES:
            has_media = True
            attachments.append(_attachment(cq_type, params))
        last = match.end()
    parts.append(raw[last:])
    text = _decode_cq_value("".join(parts))
    return text, at_me, has_media, tuple(attachments)


def parse_event(payload: Dict[str, Any]) -> Optional[MessageEvent]:
    """把 OneBot 上报事件解析成 MessageEvent；非消息或自己发的消息返回 None。"""
    if payload.get("post_type") != "message":
        return None
    message_type = payload.get("message_type")
    if message_type not in {"private", "group"}:
        return None
    try:
        user_id = int(payload.get("user_id"))
        self_id = int(payload.get("self_id") or 0)
    except (TypeError, ValueError):
        return None
    if user_id == self_id:
        return None

    group_id: Optional[int] = None
    if message_type == "group":
        try:
            group_id = int(payload.get("group_id"))
        except (TypeError, ValueError):
            return None

    message = payload.get("message", payload.get("raw_message", ""))
    raw_message = str(payload.get("raw_message") or "")
    if isinstance(message, list):
        text, at_me, has_media, attachments = _parse_segments(message, self_id)
        # NapCat 遇到不认识的消息元素（红包就是其一）会给空数组，只留一个空事件
        unsupported = not message and not raw_message
    else:
        text, at_me, has_media, attachments = _parse_cq(str(message), self_id)
        unsupported = not str(message).strip() and not raw_message

    sender = payload.get("sender") or {}
    sender_name = str(sender.get("card") or sender.get("nickname") or user_id)

    try:
        message_id = int(payload.get("message_id") or 0)
    except (TypeError, ValueError):
        message_id = 0

    return MessageEvent(
        message_type=str(message_type),
        sub_type=str(payload.get("sub_type") or ""),
        message_id=message_id,
        user_id=user_id,
        group_id=group_id,
        self_id=self_id,
        sender_name=sender_name,
        text=text,
        at_me=at_me,
        has_media=has_media,
        attachments=attachments,
        unsupported=unsupported,
    )
