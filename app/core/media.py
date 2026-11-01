"""把 QQ 图片下载下来，转成模型能直接读取的 data URL。"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

_MAX_BYTES = 5 * 1024 * 1024


async def to_data_url(url: str, timeout: float = 10.0) -> Optional[str]:
    """下载图片并编码为 data URL；失败或超限时返回 None。"""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError:
        return None
    data = response.content
    if not data or len(data) > _MAX_BYTES:
        return None
    mime = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
