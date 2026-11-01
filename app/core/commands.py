"""斜杠指令：/help /reset /remember /memories /forget /ping。"""

from __future__ import annotations

from typing import Optional, Tuple

from ..config import Settings
from .memory import MemoryStore

HELP_TEXT = """我是大肥鱼~ 可以这样跟我说话：
· 直接聊天：私聊我，或在群里 @我 / 叫我的名字
· /help 看看这份说明
· /ping 看看我在不在
· /reset 清空当前对话上下文（群里只有管理员能用）
· /remember 内容 —— 私聊我，让我记住一件事
· /memories —— 私聊我，看看我记住了什么
· /forget —— 私聊我，忘掉所有记住的事"""

_PRIVATE_ONLY = {"remember", "memories", "forget"}


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """把 /help 这类文本解析成 (指令名, 参数)；不是指令返回 None。"""
    text = text.strip()
    if not text.startswith("/"):
        return None
    body = text[1:].strip()
    if not body:
        return None
    parts = body.split(maxsplit=1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


async def handle_command(
    name: str,
    args: str,
    *,
    chat_key: str,
    is_private: bool,
    sender_id: int,
    settings: Settings,
    memory: MemoryStore,
) -> Optional[str]:
    """执行指令并返回要发送的文本。"""
    if name in {"help", "h", "?"}:
        return HELP_TEXT
    if name == "ping":
        return "pong！我在呢~"
    if name == "reset":
        if not is_private and sender_id != settings.admin_qq:
            return "重置对话只有管理员能操作哦~"
        await memory.reset_context(chat_key)
        return "好，我把这段对话忘掉啦，我们重新开始吧~"
    if name in _PRIVATE_ONLY and not is_private:
        return "这个指令私聊我才能用哦~"
    if name == "remember":
        content = args.strip()[:200]
        if not content:
            return "要记住什么呢？用法：/remember 你想让我记住的事"
        await memory.add_memory(chat_key, content)
        return f"记住啦：{content}"
    if name == "memories":
        items = await memory.list_memories(chat_key, limit=20)
        if not items:
            return "我还没记住什么呢，用 /remember 告诉我就好啦~"
        lines = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(items))
        return f"我记住这些啦：\n{lines}"
    if name == "forget":
        await memory.clear_memories(chat_key)
        return "好，我把记住的事都忘掉啦~"
    return "没有这个指令哦，发 /help 看看我会什么~"
