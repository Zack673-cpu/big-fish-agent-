"""百炼（DashScope）文本向量客户端，走 OpenAI 兼容的 /embeddings 接口。"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Sequence

import httpx

logger = logging.getLogger(__name__)

# text-embedding-v4 单次最多 10 条，超过时自动分批
_MAX_BATCH = 10
# 建大索引时并发请求几批，缩短首次建索引时间；过高容易触发限流
_MAX_CONCURRENCY = 4
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0


class EmbeddingError(Exception):
    """向量接口调用失败；retryable 表示是否值得重试。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _error_detail(response: httpx.Response) -> str:
    """提取接口返回的错误说明，便于定位 400/401 的具体原因。"""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200].replace("\n", " ")
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str(error or data)[:200]


class BailianEmbeddings:
    """百炼文本向量客户端；失败时抛 EmbeddingError，由检索层回退字面匹配。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int = 1024,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dimensions = max(0, dimensions)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout
        )

    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """把一批文本转成向量，保持输入顺序；分批并发请求。"""
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingError("未配置 DASHSCOPE_API_KEY")
        batches = [
            list(texts[start:start + _MAX_BATCH])
            for start in range(0, len(texts), _MAX_BATCH)
        ]
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def run(index: int, batch: List[str]) -> tuple[int, List[List[float]]]:
            async with semaphore:
                return index, await self._embed_with_retry(batch)

        results = await asyncio.gather(
            *(run(index, batch) for index, batch in enumerate(batches))
        )
        vectors: List[List[float]] = []
        for _index, part in sorted(results, key=lambda item: item[0]):
            vectors.extend(part)
        return vectors

    async def _embed_with_retry(self, texts: List[str]) -> List[List[float]]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._embed_batch(texts)
            except EmbeddingError as exc:
                if not exc.retryable or attempt >= _RETRY_ATTEMPTS:
                    raise
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "百炼向量请求失败，%.1f 秒后重试（第 %s 次）：%s",
                    delay,
                    attempt,
                    exc,
                )
                await asyncio.sleep(delay)

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        payload: Dict[str, object] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        try:
            response = await self._client.post(
                "/embeddings",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise EmbeddingError(
                f"百炼返回 HTTP {code}：{_error_detail(exc.response)}",
                status=code,
                retryable=code in _RETRY_STATUS,
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"网络错误：{exc}", retryable=True) from exc
        except ValueError as exc:
            raise EmbeddingError("百炼返回格式异常") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise EmbeddingError("百炼返回的向量数量与输入不一致")
        ordered = sorted(items, key=lambda item: item.get("index", 0))
        vectors: List[List[float]] = []
        for item in ordered:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list) or not embedding:
                raise EmbeddingError("百炼返回空向量")
            vectors.append([float(value) for value in embedding])
        return vectors

    async def aclose(self) -> None:
        await self._client.aclose()
