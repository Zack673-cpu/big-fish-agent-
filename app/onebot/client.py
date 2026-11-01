"""OneBot 11 反向 WebSocket 连接封装：发消息、等待 echo 响应。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.websockets import WebSocket, WebSocketState


class OneBotError(Exception):
    """OneBot 接口调用失败。"""


def _record_file(file: str) -> str:
    """本地路径转成 NapCat 认的 file:// 地址，其余形式原样传。"""
    value = str(file or "")
    if value.startswith(("http://", "https://", "file://", "base64://")):
        return value
    return Path(value).resolve().as_uri()


class OneBotConnection:
    """一条已经建立的反向 WS 连接。"""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._pending: Dict[str, asyncio.Future] = {}
        self._seq = 0

    async def resolve(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理 echo 响应；若 payload 是事件则原样返回。"""
        if "echo" not in payload:
            return payload
        echo = str(payload.get("echo"))
        future = self._pending.pop(echo, None)
        if future is not None and not future.done():
            if payload.get("status") == "failed":
                message = payload.get("message") or payload.get("wording") or "调用失败"
                future.set_exception(OneBotError(str(message)))
            else:
                future.set_result(payload)
        return None

    async def call_action(
        self, action: str, params: Dict[str, Any], timeout: float = 10.0
    ) -> Dict[str, Any]:
        self._seq += 1
        echo = f"req-{self._seq}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        try:
            await self._ws.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"调用 {action} 超时") from exc
        finally:
            self._pending.pop(echo, None)

    async def send_group_msg(
        self, group_id: int, text: str, quote_message_id: Optional[int] = None
    ) -> None:
        message: List[Dict[str, Any]] = []
        if quote_message_id:
            message.append({"type": "reply", "data": {"id": str(quote_message_id)}})
        message.append({"type": "text", "data": {"text": text}})
        await self.call_action("send_group_msg", {"group_id": group_id, "message": message})

    async def send_private_msg(self, user_id: int, text: str) -> None:
        await self.call_action(
            "send_private_msg",
            {"user_id": user_id, "message": [{"type": "text", "data": {"text": text}}]},
        )

    async def send_group_record(
        self, group_id: int, file: str, quote_message_id: Optional[int] = None
    ) -> None:
        message: List[Dict[str, Any]] = []
        if quote_message_id:
            message.append({"type": "reply", "data": {"id": str(quote_message_id)}})
        message.append({"type": "record", "data": {"file": _record_file(file)}})
        await self.call_action("send_group_msg", {"group_id": group_id, "message": message})

    async def send_private_record(self, user_id: int, file: str) -> None:
        await self.call_action(
            "send_private_msg",
            {"user_id": user_id, "message": [{"type": "record", "data": {"file": _record_file(file)}}]},
        )

    def close_pending(self, reason: str = "连接已关闭") -> None:
        """连接断开时，让所有等待中的调用立即失败。"""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(OneBotError(reason))
        self._pending.clear()

    async def close(self) -> None:
        self.close_pending("连接已关闭")
        if self._ws.client_state == WebSocketState.CONNECTED:
            await self._ws.close()
