import asyncio

from app.core.memory import MemoryStore

CHAT = "private:10001"


def test_message_roundtrip(tmp_path):
    asyncio.run(_message_roundtrip(tmp_path))


async def _message_roundtrip(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    await memory.add_message(CHAT, "user", "你好")
    await memory.add_message(CHAT, "assistant", "你好呀~")
    rows = await memory.recent(CHAT, 10)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "你好"


def test_summary_and_unsummarized(tmp_path):
    asyncio.run(_summary_and_unsummarized(tmp_path))


async def _summary_and_unsummarized(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    first = await memory.add_message(CHAT, "user", "第一句")
    await memory.add_message(CHAT, "assistant", "第二句")

    summary, covered = await memory.latest_summary(CHAT)
    assert summary == "" and covered == 0

    rows = await memory.unsummarized(CHAT, covered, 10)
    assert len(rows) == 2 and rows[0]["id"] == first

    await memory.save_summary(CHAT, "聊了第一句和第二句", rows[-1]["id"])
    summary, covered = await memory.latest_summary(CHAT)
    assert summary == "聊了第一句和第二句"
    assert await memory.unsummarized(CHAT, covered, 10) == []


def test_memories_add_list_clear(tmp_path):
    asyncio.run(_memories_add_list_clear(tmp_path))


async def _memories_add_list_clear(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    await memory.add_memory(CHAT, "喜欢吃火锅")
    await memory.add_memory(CHAT, "正在准备考研")
    items = await memory.list_memories(CHAT)
    assert items == ["正在准备考研", "喜欢吃火锅"]

    await memory.clear_memories(CHAT)
    assert await memory.list_memories(CHAT) == []


def test_reset_context_keeps_memories(tmp_path):
    asyncio.run(_reset_context_keeps_memories(tmp_path))


async def _reset_context_keeps_memories(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    await memory.add_message(CHAT, "user", "你好")
    await memory.add_memory(CHAT, "喜欢吃火锅")
    await memory.reset_context(CHAT)
    assert await memory.recent(CHAT, 10) == []
    assert await memory.list_memories(CHAT) == ["喜欢吃火锅"]
