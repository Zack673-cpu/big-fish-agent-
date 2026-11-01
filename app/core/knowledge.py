"""轻量 RAG：从本地语料库按字面相似度检索片段，不做向量化、不引入额外依赖。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")
_SKIP_HEADINGS = ("使用说明", "资料出处")

_MAX_CHUNK_CHARS = 400
_MIN_OVERLAP = 2


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text.lower())


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


class KnowledgeBase:
    """加载一个 Markdown 语料文件，按 `##` 小节切块并提供检索。"""

    def __init__(self, path: Path, min_score: float = 0.12) -> None:
        self._path = Path(path)
        self._min_score = min_score
        self._chunks: List[Tuple[str, str]] = []
        self._loaded = False

    def _load(self) -> None:
        self._loaded = True
        if not self._path.is_file():
            return
        raw = _COMMENT_RE.sub("", self._path.read_text(encoding="utf-8"))
        parts = _SECTION_RE.split(raw)
        # 第一段是文件标题和简介，没有正文小节标题，不单独入块
        for part in parts[1:]:
            lines = part.split("\n", 1)
            heading = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            if not heading or not body:
                continue
            if any(mark in heading for mark in _SKIP_HEADINGS):
                continue
            if len(body) > _MAX_CHUNK_CHARS:
                body = body[:_MAX_CHUNK_CHARS].rstrip() + "…"
            self._chunks.append((heading, f"【{heading}】\n{body}"))

    def retrieve(self, query: str, top_k: int = 2, min_score: float = 0.0) -> List[str]:
        """返回与 query 字面最相关的若干语料片段；没有命中返回空列表。"""
        if not query or top_k <= 0:
            return []
        if not self._loaded:
            self._load()
        if not self._chunks:
            return []
        query_grams = _bigrams(_normalize(query))
        if not query_grams:
            return []
        threshold = max(self._min_score, min_score)
        min_overlap = min(_MIN_OVERLAP, len(query_grams))
        scored: List[Tuple[float, str]] = []
        for heading, text in self._chunks:
            chunk_grams = _bigrams(_normalize(f"{heading}{text}"))
            overlap = len(query_grams & chunk_grams)
            if overlap < min_overlap:
                continue
            score = overlap / len(query_grams)
            if score >= threshold:
                scored.append((score, text))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [text for _score, text in scored[:top_k]]
