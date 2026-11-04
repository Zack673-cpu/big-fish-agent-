"""百炼 Embedding 客户端的单元测试（用 MockTransport，不请求真实接口）。"""

import asyncio
import json

import httpx

from app.core.embeddings import BailianEmbeddings, EmbeddingError


def make_embedder(handler, *, dimensions: int = 2) -> BailianEmbeddings:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://example.invalid/compatible-mode/v1",
    )
    return BailianEmbeddings(
        "test-key",
        "https://example.invalid/compatible-mode/v1",
        "text-embedding-v4",
        dimensions,
        client=client,
    )


def test_embed_orders_results_and_batches_requests():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        count = len(payload["input"])
        # 故意倒序返回，客户端应按 index 还原输入顺序
        data = [
            {"index": index, "embedding": [float(index), 1.0]}
            for index in range(count - 1, -1, -1)
        ]
        return httpx.Response(200, json={"data": data})

    embedder = make_embedder(handler)
    texts = [f"文本{i}" for i in range(13)]

    vectors = asyncio.run(embedder.embed(texts))

    # 批次会并发发送，顺序不保证，但都必须发出且结果按输入顺序还原
    assert sorted(len(payload["input"]) for payload in requests) == [3, 10]
    assert len(vectors) == 13
    assert vectors[0] == [0.0, 1.0]
    assert vectors[9] == [9.0, 1.0]
    assert vectors[10] == [0.0, 1.0]


def test_embed_reports_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "Incorrect API key provided."}},
        )

    embedder = make_embedder(handler)

    try:
        asyncio.run(embedder.embed(["测试"]))
    except EmbeddingError as exc:
        assert "401" in str(exc)
        assert "Incorrect API key" in str(exc)
    else:
        raise AssertionError("接口报错时应抛出 EmbeddingError")
