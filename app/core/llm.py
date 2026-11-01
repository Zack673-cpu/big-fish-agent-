"""DeepSeek（OpenAI 兼容）聊天接口封装。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import httpx

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """大模型调用失败。"""


def _error_detail(response: httpx.Response) -> str:
    """提取接口返回的错误说明，便于定位 400 的具体原因。"""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200].replace("\n", " ")
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str(error or data)[:200]


class LLMClient:
    """异步聊天客户端。失败时抛 LLMError，由上层做友好兜底。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        retries: int = 1,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def chat(self, messages: List[Dict[str, Any]]) -> str:
        """发送对话并返回回复文本。"""
        if not self._api_key:
            raise LLMError("未配置 DEEPSEEK_API_KEY")
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 1024,
            "stream": False,
        }
        error = "未知错误"
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(
                    "/chat/completions", json=payload, headers=self._headers()
                )
                if response.status_code in _RETRYABLE_STATUS and attempt < self._retries:
                    error = f"DeepSeek 返回 HTTP {response.status_code}"
                    await asyncio.sleep(1.2)
                    continue
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return str(content).strip()
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    f"DeepSeek 返回 HTTP {exc.response.status_code}：{_error_detail(exc.response)}"
                ) from exc
            except httpx.HTTPError as exc:
                error = f"网络错误：{exc}"
                if attempt < self._retries:
                    await asyncio.sleep(1.2)
                    continue
                raise LLMError(error) from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise LLMError("DeepSeek 返回格式异常") from exc
        raise LLMError(error)

    async def aclose(self) -> None:
        await self._client.aclose()
