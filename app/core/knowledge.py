"""人格语料检索（RAG）：多文件语料 + FAISS 向量索引，异常时回退字面检索。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Tuple

import faiss
import numpy as np

from .embeddings import BailianEmbeddings, EmbeddingError

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]")
_SKIP_HEADINGS = ("使用说明", "资料出处")

# 单个语料块最多 600 字，块间重叠 100 字，避免长小节被硬切后语义断裂
_MAX_CHUNK_CHARS = 600
_CHUNK_OVERLAP = 100
_MAX_DISPLAY_CHARS = 400
_MIN_BODY_CHARS = 10
_MIN_BIGRAM_OVERLAP = 2
# 字面兜底只适合小语料；语料大了直接跳过，避免每条消息都做全量扫描
_LITERAL_FALLBACK_MAX_CHUNKS = 2000
_EMBEDDING_WARN_INTERVAL = 300.0
# 建索引时按小组取向量再入库，避免几万段的 Python 浮点列表一次性驻留内存
_EMBED_GROUP_SIZE = 200

_INDEX_VERSION = 1
# 切块规则变化时旧索引必须重建，防止缓存和实际切块对不上
_CHUNKER = "md-v2-600-100"

_INDEX_FILE = "index.faiss"
_CHUNKS_FILE = "chunks.jsonl"
_MANIFEST_FILE = "manifest.json"


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text.lower())


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _split_body(body: str) -> List[str]:
    """把超长小节切成带重叠的块，尽量在换行或句末标点处断开。"""
    if len(body) <= _MAX_CHUNK_CHARS:
        return [body]
    pieces: List[str] = []
    start = 0
    length = len(body)
    while start < length:
        end = min(start + _MAX_CHUNK_CHARS, length)
        cut = end
        if end < length:
            window = body[start:end]
            pos = max(
                window.rfind("\n"),
                window.rfind("。"),
                window.rfind("！"),
                window.rfind("？"),
            )
            if pos >= _MAX_CHUNK_CHARS // 2:
                cut = start + pos + 1
        piece = body[start:cut].strip()
        if piece:
            pieces.append(piece)
        if cut >= length:
            break
        next_start = cut - _CHUNK_OVERLAP
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return pieces


def _chunk_id(file: str, heading: str, index: int, body: str) -> int:
    """用内容生成稳定的 int64 id，内容不变时增量索引可以直接复用向量。"""
    payload = f"{file}\x00{heading}\x00{index}\x00{body}".encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


@dataclass(frozen=True)
class _Chunk:
    """检索时真正需要的语料信息；向量本身存放在 FAISS 索引里。"""

    id: int
    file: str
    heading: str
    display: str


class KnowledgeBase:
    """加载语料目录，建立 FAISS 索引，优先按向量相似度检索。"""

    def __init__(
        self,
        path: Path,
        min_score: float = 0.12,
        *,
        embedder: BailianEmbeddings | None = None,
        index_dir: Path | None = None,
        embedding_min_score: float = 0.40,
    ) -> None:
        self._path = Path(path)
        self._min_score = min_score
        self._embedder = embedder
        self._index_dir = (
            Path(index_dir)
            if index_dir is not None
            else self._path.parent / ".knowledge_index"
        )
        self._embedding_min_score = embedding_min_score
        self._index: Optional[faiss.IndexIDMap] = None
        self._records: Dict[int, _Chunk] = {}
        self._file_hashes: Dict[str, str] = {}
        self._manifest_model = ""
        self._dims = 0
        self._started = False
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_lock = asyncio.Lock()
        self._last_embedding_warning = 0.0
        self._fallback_skip_logged = False

    async def start(self, wait: bool = False) -> None:
        """检查语料变化：能直接复用索引就复用，需要重算时放后台执行。"""
        if self._started:
            if wait and self._refresh_task is not None:
                await self._refresh_task
            return
        self._started = True
        cached = self._load_cache()
        chunks, embed_inputs, file_hashes = self._scan()

        if (
            cached
            and self._embedder is not None
            and self._manifest_model == self._embedder.model
            and self._dims_compatible()
            and self._file_hashes == file_hashes
        ):
            logger.info(
                "语料索引已就绪：%s 段，%s 个文件（%s）",
                len(self._records),
                len(file_hashes),
                self._index_dir,
            )
            return

        if not chunks:
            self._records = {}
            self._index = None
            self._file_hashes = file_hashes
            self._remove_cache()
            logger.info("语料目录为空：%s", self._path)
            return

        if self._embedder is None:
            # 没有向量能力时保留字面检索，不建立索引
            self._records = {chunk.id: chunk for chunk in chunks}
            self._file_hashes = file_hashes
            logger.info("知识库未启用向量索引，按字面检索：%s 段", len(self._records))
            return

        self._refresh_task = asyncio.create_task(
            self._refresh(chunks, embed_inputs, file_hashes),
            name="knowledge-index",
        )
        if wait:
            await self._refresh_task

    async def retrieve(
        self, query: str, top_k: int = 2, min_score: float = 0.0
    ) -> List[str]:
        """返回与 query 最相关的若干语料片段；没有命中返回空列表。"""
        if not query or top_k <= 0:
            return []
        if not self._started:
            await self.start()
        if self._index is not None and self._embedder is not None:
            try:
                return await self._retrieve_embedding(query, top_k)
            except Exception as exc:  # 向量异常时不能让正常聊天失败
                self._warn_embedding_failure(exc)
        return self._retrieve_literal(query, top_k, min_score)

    def _dims_compatible(self) -> bool:
        if self._embedder is None or not self._embedder.dimensions:
            return True
        return self._dims == self._embedder.dimensions

    def _scan(self) -> Tuple[List[_Chunk], Dict[int, str], Dict[str, str]]:
        """扫描语料目录，返回语料块、向量输入和文件指纹。"""
        root = self._path
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = sorted(path for path in root.rglob("*.md") if path.is_file())
        else:
            return [], {}, {}

        chunks: List[_Chunk] = []
        embed_inputs: Dict[int, str] = {}
        file_hashes: Dict[str, str] = {}
        seen_ids: set[int] = set()
        for file_path in files:
            try:
                raw = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                logger.warning("读取语料失败，已跳过 %s：%s", file_path, exc)
                continue
            content = _COMMENT_RE.sub("", raw)
            relative = (
                file_path.name
                if root.is_file()
                else file_path.relative_to(root).as_posix()
            )
            file_hashes[relative] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            for part in _SECTION_RE.split(content)[1:]:
                lines = part.split("\n", 1)
                heading = lines[0].strip()
                body = lines[1].strip() if len(lines) > 1 else ""
                if not heading or len(body) < _MIN_BODY_CHARS:
                    continue
                if any(mark in heading for mark in _SKIP_HEADINGS):
                    continue
                for index, piece in enumerate(_split_body(body)):
                    display_body = piece[:_MAX_DISPLAY_CHARS].rstrip()
                    if len(piece) > _MAX_DISPLAY_CHARS:
                        display_body += "…"
                    chunk_id = _chunk_id(relative, heading, index, piece)
                    if chunk_id in seen_ids:
                        continue
                    seen_ids.add(chunk_id)
                    chunks.append(
                        _Chunk(
                            chunk_id,
                            relative,
                            heading,
                            f"【{heading}】\n{display_body}",
                        )
                    )
                    # 向量输入保留完整正文，避免语义被显示截断
                    embed_inputs[chunk_id] = f"【{heading}】\n{piece}"
        return chunks, embed_inputs, file_hashes

    def _load_cache(self) -> bool:
        if self._embedder is None:
            return False
        index_path = self._index_dir / _INDEX_FILE
        chunks_path = self._index_dir / _CHUNKS_FILE
        manifest_path = self._index_dir / _MANIFEST_FILE
        if not (
            index_path.is_file()
            and chunks_path.is_file()
            and manifest_path.is_file()
        ):
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != _INDEX_VERSION:
                return False
            if manifest.get("chunker") != _CHUNKER:
                return False
            if manifest.get("model") != self._embedder.model:
                return False
            dims = int(manifest.get("dimensions") or 0)
            if self._embedder.dimensions and dims != self._embedder.dimensions:
                return False

            records: Dict[int, _Chunk] = {}
            with chunks_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    chunk_id = int(item["id"])
                    records[chunk_id] = _Chunk(
                        chunk_id,
                        str(item["file"]),
                        str(item["heading"]),
                        str(item["display"]),
                    )

            index = faiss.read_index(str(index_path))
            if not dims:
                dims = int(index.d)
            if index.ntotal != len(records):
                return False
            if index.d != dims:
                return False

            self._index = index
            self._records = records
            self._dims = dims
            self._manifest_model = str(manifest.get("model") or "")
            self._file_hashes = {
                str(key): str(value)
                for key, value in (manifest.get("files") or {}).items()
            }
            return True
        except Exception as exc:  # 缓存损坏时按没有缓存处理，不能阻止启动
            logger.warning("读取语料索引失败，将重新建立：%s", exc)
            self._index = None
            self._records = {}
            self._dims = 0
            self._manifest_model = ""
            self._file_hashes = {}
            return False

    async def _refresh(
        self,
        chunks: List[_Chunk],
        embed_inputs: Dict[int, str],
        file_hashes: Dict[str, str],
    ) -> None:
        async with self._refresh_lock:
            try:
                if (
                    self._index is None
                    or not self._dims_compatible()
                    or self._manifest_model != self._embedder.model
                ):
                    await self._rebuild(chunks, embed_inputs, file_hashes)
                else:
                    await self._update(chunks, embed_inputs, file_hashes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("语料索引更新失败，继续使用旧索引：%s", exc)

    async def _rebuild(
        self,
        chunks: List[_Chunk],
        embed_inputs: Dict[int, str],
        file_hashes: Dict[str, str],
    ) -> None:
        if not chunks:
            self._records = {}
            self._index = None
            self._file_hashes = file_hashes
            self._remove_cache()
            return
        # 先记录语料块，向量接口失败时仍能用字面检索兜底
        self._records = {chunk.id: chunk for chunk in chunks}
        started = time.monotonic()
        texts = [embed_inputs[chunk.id] for chunk in chunks]
        ids = np.asarray([chunk.id for chunk in chunks], dtype="int64")
        index = None
        dims = 0
        async for offset, array in self._embed_in_groups(texts):
            if array.ndim != 2 or array.shape[1] == 0:
                raise EmbeddingError("语料向量维度异常")
            if dims == 0:
                dims = int(array.shape[1])
                if self._embedder.dimensions and dims != self._embedder.dimensions:
                    raise EmbeddingError(
                        f"语料向量维度 {dims} 与配置 {self._embedder.dimensions} 不一致"
                    )
                index = faiss.IndexIDMap(faiss.IndexFlatIP(dims))
            elif array.shape[1] != dims:
                raise EmbeddingError(
                    f"语料向量维度 {array.shape[1]} 与首批 {dims} 不一致"
                )
            faiss.normalize_L2(array)
            index.add_with_ids(array, ids[offset:offset + len(array)])
        if index is None:
            raise EmbeddingError("没有可入库的语料向量")
        self._index = index
        self._records = {chunk.id: chunk for chunk in chunks}
        self._dims = dims
        self._manifest_model = self._embedder.model
        self._file_hashes = file_hashes
        self._persist()
        logger.info(
            "语料索引建立完成：%s 段，耗时 %.1f 秒",
            len(chunks),
            time.monotonic() - started,
        )

    async def _update(
        self,
        chunks: List[_Chunk],
        embed_inputs: Dict[int, str],
        file_hashes: Dict[str, str],
    ) -> None:
        old_ids = set(self._records)
        new_ids = {chunk.id for chunk in chunks}
        added = [chunk for chunk in chunks if chunk.id not in old_ids]
        removed = old_ids - new_ids
        started = time.monotonic()

        if added:
            texts = [embed_inputs[chunk.id] for chunk in added]
            ids = np.asarray([chunk.id for chunk in added], dtype="int64")
            async for offset, array in self._embed_in_groups(texts):
                if array.ndim != 2 or array.shape[1] != self._dims:
                    raise EmbeddingError(
                        f"新增语料向量维度 {array.shape} 与索引 {self._dims} 不一致"
                    )
                faiss.normalize_L2(array)
                self._index.add_with_ids(array, ids[offset:offset + len(array)])

        if removed:
            self._index.remove_ids(
                np.asarray(sorted(removed), dtype="int64")
            )

        self._records = {chunk.id: chunk for chunk in chunks}
        self._file_hashes = file_hashes
        self._persist()
        if added or removed:
            logger.info(
                "语料索引已增量更新：新增 %s 段，删除 %s 段，复用 %s 段，耗时 %.1f 秒",
                len(added),
                len(removed),
                len(chunks) - len(added),
                time.monotonic() - started,
            )

    async def _embed_in_groups(
        self, texts: List[str]
    ) -> AsyncIterator[Tuple[int, np.ndarray]]:
        """按小组取向量，边算边入库，控制大语料建索引时的内存峰值。"""
        for start in range(0, len(texts), _EMBED_GROUP_SIZE):
            group = texts[start:start + _EMBED_GROUP_SIZE]
            vectors = await self._embedder.embed(group)
            if len(vectors) != len(group):
                raise EmbeddingError("向量数量与输入文本数量不一致")
            yield start, np.asarray(vectors, dtype="float32")

    def _persist(self) -> None:
        if self._index is None:
            self._remove_cache()
            return
        self._index_dir.mkdir(parents=True, exist_ok=True)
        index_path = self._index_dir / _INDEX_FILE
        chunks_path = self._index_dir / _CHUNKS_FILE
        manifest_path = self._index_dir / _MANIFEST_FILE

        temp_index = index_path.with_name(index_path.name + ".tmp")
        faiss.write_index(self._index, str(temp_index))
        os.replace(temp_index, index_path)

        temp_chunks = chunks_path.with_name(chunks_path.name + ".tmp")
        with temp_chunks.open("w", encoding="utf-8", newline="\n") as handle:
            for chunk in self._records.values():
                json.dump(
                    {
                        "id": chunk.id,
                        "file": chunk.file,
                        "heading": chunk.heading,
                        "display": chunk.display,
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
        os.replace(temp_chunks, chunks_path)

        manifest = {
            "version": _INDEX_VERSION,
            "chunker": _CHUNKER,
            "model": self._manifest_model,
            "dimensions": self._dims,
            "metric": "ip",
            "files": self._file_hashes,
            "chunks": len(self._records),
            "updated_at": int(time.time()),
        }
        temp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
        temp_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp_manifest, manifest_path)

    def _remove_cache(self) -> None:
        for name in (_INDEX_FILE, _CHUNKS_FILE, _MANIFEST_FILE):
            try:
                (self._index_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("删除语料索引文件失败 %s：%s", name, exc)

    async def _retrieve_embedding(self, query: str, top_k: int) -> List[str]:
        query_vectors = await self._embedder.embed([query])
        if not query_vectors:
            raise EmbeddingError("查询向量为空")
        array = np.asarray(query_vectors, dtype="float32")
        if array.ndim != 2 or array.shape[1] != self._dims:
            raise EmbeddingError(
                f"查询向量维度 {array.shape} 与索引 {self._dims} 不一致"
            )
        faiss.normalize_L2(array)
        k = min(top_k, int(self._index.ntotal))
        if k <= 0:
            return []
        scores, ids = self._index.search(array, k)
        results: List[str] = []
        for score, chunk_id in zip(scores[0], ids[0]):
            if chunk_id < 0 or float(score) < self._embedding_min_score:
                continue
            chunk = self._records.get(int(chunk_id))
            if chunk is None:
                continue
            results.append(chunk.display)
            if len(results) >= top_k:
                break
        return results

    def _retrieve_literal(
        self, query: str, top_k: int, min_score: float
    ) -> List[str]:
        if not self._records:
            return []
        if len(self._records) > _LITERAL_FALLBACK_MAX_CHUNKS:
            if not self._fallback_skip_logged:
                self._fallback_skip_logged = True
                logger.info(
                    "语料超过 %s 段，字面兜底已跳过；请确认向量索引可用",
                    _LITERAL_FALLBACK_MAX_CHUNKS,
                )
            return []
        query_grams = _bigrams(_normalize(query))
        if not query_grams:
            return []
        threshold = max(self._min_score, min_score)
        min_overlap = min(_MIN_BIGRAM_OVERLAP, len(query_grams))
        scored: List[Tuple[float, str]] = []
        for chunk in self._records.values():
            text = _normalize(chunk.display)
            overlap = sum(1 for gram in query_grams if gram in text)
            if overlap < min_overlap:
                continue
            score = overlap / len(query_grams)
            if score >= threshold:
                scored.append((score, chunk.display))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [text for _score, text in scored[:top_k]]

    def _warn_embedding_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_embedding_warning < _EMBEDDING_WARN_INTERVAL:
            return
        self._last_embedding_warning = now
        logger.warning("向量检索失败，已回退字面检索：%s", exc)

    async def aclose(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._embedder is not None:
            await self._embedder.aclose()
