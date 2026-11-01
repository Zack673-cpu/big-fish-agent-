"""文本清洗、别名识别与分段发送。"""

from __future__ import annotations

import re
from typing import Iterable, List

_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_SEGMENT_PUNCT = "。！？!?；;"
_SECONDARY_PUNCT = "，,、:： "


def clean_text(text: str) -> str:
    """去掉不可见字符，统一换行，压缩空白。"""
    if not text:
        return ""
    text = _INVISIBLE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def contains_alias(text: str, aliases: Iterable[str]) -> bool:
    """文本里是否出现机器人名字或别名（忽略大小写）。"""
    lowered = text.lower()
    return any(alias.lower() in lowered for alias in aliases if alias)


def remove_aliases(text: str, aliases: Iterable[str]) -> str:
    """删掉名字/别名，并清理开头残留的标点和多余空格。"""
    for alias in sorted((a for a in aliases if a), key=len, reverse=True):
        text = re.sub(re.escape(alias), "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"^[\s,，:：、!！~～]+", "", text).strip()


def _find_cut(text: str, limit: int) -> int:
    """在 limit 内找一个合适的断句位置，找不到就硬切。"""
    for marks in (_SEGMENT_PUNCT, _SECONDARY_PUNCT):
        index = max(text.rfind(mark, 0, limit) for mark in marks)
        if index >= limit // 3:
            return index + 1
    return limit


def _split_long(paragraph: str, max_chars: int) -> List[str]:
    pieces: List[str] = []
    rest = paragraph.strip()
    while len(rest) > max_chars:
        cut = _find_cut(rest, max_chars)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def _cut_at_sentence(text: str, limit: int) -> str:
    """在 limit 内尽量停在句末标点，实在没有标点才硬截。"""
    for mark in _SEGMENT_PUNCT:
        index = text.rfind(mark, 0, limit)
        if index >= limit // 2:
            return text[: index + 1].strip()
    return text[:limit].rstrip() + "…"


def split_text(text: str, max_chars: int, max_segments: int) -> List[str]:
    """把回复切成最多 max_segments 段，每段不超过 max_chars 字。

    超出的部分并进最后一段，避免把话说到一半就断掉；
    只有明显超长（超过 3 倍单段上限）时才在句末截断。
    """
    text = clean_text(text)
    if not text:
        return []
    units: List[str] = []
    for paragraph in text.split("\n"):
        if paragraph.strip():
            units.extend(_split_long(paragraph, max_chars))
    segments: List[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 1 + len(unit) <= max_chars:
            current = f"{current} {unit}"
        else:
            segments.append(current)
            current = unit
    if current:
        segments.append(current)
    if len(segments) > max_segments:
        head = segments[: max_segments - 1]
        tail = " ".join(segments[max_segments - 1:])
        if len(tail) > max_chars * 3:
            tail = _cut_at_sentence(tail, max_chars * 3)
        head.append(tail)
        segments = head
    return segments
