"""聊天记录与长期记忆的存储（SQLite）。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_key, id);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    content TEXT NOT NULL,
    covered_until INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_chat ON summaries (chat_key, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_chat ON memories (chat_key, id);
"""

_MAX_MEMORIES = 100


class MemoryStore:
    """每次操作单独开连接，避免跨线程共享 sqlite3 连接。"""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def add_message(self, chat_key: str, role: str, content: str) -> int:
        return await asyncio.to_thread(self._add_message_sync, chat_key, role, content)

    def _add_message_sync(self, chat_key: str, role: str, content: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (chat_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_key, role, content, time.time()),
            )
            return int(cursor.lastrowid)

    async def recent(self, chat_key: str, limit: int) -> List[Dict[str, str]]:
        """返回最近的对话，按时间正序。"""
        rows = await asyncio.to_thread(self._recent_sync, chat_key, limit)
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def _recent_sync(self, chat_key: str, limit: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_key = ? ORDER BY id DESC LIMIT ?",
                (chat_key, max(1, limit)),
            ).fetchall()
        return list(reversed(rows))

    async def latest_summary(self, chat_key: str) -> Tuple[str, int]:
        """返回 (摘要文本, 已覆盖到的消息 id)。没有摘要时返回 ("", 0)。"""
        row = await asyncio.to_thread(self._latest_summary_sync, chat_key)
        if row is None:
            return "", 0
        return str(row["content"]), int(row["covered_until"])

    def _latest_summary_sync(self, chat_key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT content, covered_until FROM summaries WHERE chat_key = ? ORDER BY id DESC LIMIT 1",
                (chat_key,),
            ).fetchone()

    async def unsummarized(self, chat_key: str, after_id: int, limit: int) -> List[Dict[str, object]]:
        """返回 id 大于 after_id 的消息，用于生成摘要。"""
        rows = await asyncio.to_thread(self._unsummarized_sync, chat_key, after_id, limit)
        return [
            {"id": int(row["id"]), "role": row["role"], "content": row["content"]}
            for row in rows
        ]

    def _unsummarized_sync(self, chat_key: str, after_id: int, limit: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT id, role, content FROM messages "
                "WHERE chat_key = ? AND id > ? ORDER BY id LIMIT ?",
                (chat_key, after_id, max(1, limit)),
            ).fetchall()

    async def save_summary(self, chat_key: str, content: str, covered_until: int) -> None:
        await asyncio.to_thread(self._save_summary_sync, chat_key, content, covered_until)

    def _save_summary_sync(self, chat_key: str, content: str, covered_until: int) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO summaries (chat_key, content, covered_until, created_at) VALUES (?, ?, ?, ?)",
                (chat_key, content, covered_until, time.time()),
            )
            conn.execute(
                "DELETE FROM summaries WHERE chat_key = ? AND id != ?",
                (chat_key, cursor.lastrowid),
            )

    async def reset_context(self, chat_key: str) -> None:
        """清空对话记录和摘要，保留长期记忆。"""
        await asyncio.to_thread(self._reset_context_sync, chat_key)

    def _reset_context_sync(self, chat_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE chat_key = ?", (chat_key,))
            conn.execute("DELETE FROM summaries WHERE chat_key = ?", (chat_key,))

    async def add_memory(self, chat_key: str, content: str) -> None:
        await asyncio.to_thread(self._add_memory_sync, chat_key, content)

    def _add_memory_sync(self, chat_key: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memories (chat_key, content, created_at) VALUES (?, ?, ?)",
                (chat_key, content, time.time()),
            )
            conn.execute(
                "DELETE FROM memories WHERE chat_key = ? AND id NOT IN "
                "(SELECT id FROM memories WHERE chat_key = ? ORDER BY id DESC LIMIT ?)",
                (chat_key, chat_key, _MAX_MEMORIES),
            )

    async def list_memories(self, chat_key: str, limit: int = 10) -> List[str]:
        rows = await asyncio.to_thread(self._list_memories_sync, chat_key, limit)
        return [str(row["content"]) for row in rows]

    def _list_memories_sync(self, chat_key: str, limit: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT content FROM memories WHERE chat_key = ? ORDER BY id DESC LIMIT ?",
                (chat_key, max(1, limit)),
            ).fetchall()

    async def clear_memories(self, chat_key: str) -> None:
        await asyncio.to_thread(self._clear_memories_sync, chat_key)

    def _clear_memories_sync(self, chat_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memories WHERE chat_key = ?", (chat_key,))
