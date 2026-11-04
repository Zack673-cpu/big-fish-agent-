import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.core.llm import LLMError
from app.core.memory import MemoryStore
from app.core.proactive import ProactiveMessenger

TZ = timezone(timedelta(hours=8))


class FakeLLM:
    def __init__(self, reply: str = "记得喝口水呀~", error=None) -> None:
        self.reply = reply
        self.error = error
        self.calls = 0
        self.messages: list = []

    async def chat(self, messages):
        self.calls += 1
        self.messages = messages
        if self.error:
            raise self.error
        return self.reply


class FakeConnection:
    def __init__(self, error=None) -> None:
        self.error = error
        self.sent: list = []

    async def send_private_msg(self, user_id, text):
        if self.error:
            raise self.error
        self.sent.append((user_id, text))


def make_settings(**kwargs) -> Settings:
    values = {
        "admin_qq": 10001,
        "proactive_interval_minutes": 20,
        "proactive_interval_max_minutes": 120,
        "proactive_start_hour": 8,
        "proactive_end_hour": 23,
        "proactive_utc_offset": 8,
        "max_reply_chars": 60,
    }
    values.update(kwargs)
    return dataclasses.replace(Settings(), **values)


def make_messenger(settings=None, llm=None, memory=None, connection=None):
    return ProactiveMessenger(
        settings or make_settings(),
        llm or FakeLLM(),
        memory,
        lambda: connection,
    )


def at(hour: int, minute: int, day: int = 4) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_next_run_before_window_starts_at_window_begin():
    messenger = make_messenger()

    assert messenger.next_run(at(7, 10)) == at(8, 0)


def test_next_run_inside_window_is_between_configured_bounds():
    messenger = make_messenger()
    now = at(8, 5)

    target = messenger.next_run(now)

    assert now + timedelta(minutes=20) <= target <= now + timedelta(minutes=120)
    assert target < at(23, 0)


def test_next_run_near_end_moves_to_tomorrow():
    messenger = make_messenger()

    assert messenger.next_run(at(22, 45)) == at(8, 0, day=5)


def test_next_run_after_window_waits_until_tomorrow():
    messenger = make_messenger()

    assert messenger.next_run(at(23, 30)) == at(8, 0, day=5)


def test_next_run_uses_new_random_value_each_time(monkeypatch):
    messenger = make_messenger()
    now = at(8, 5)
    delays = iter([30, 60, 90])
    monkeypatch.setattr(
        "app.core.proactive.random.uniform", lambda low, high: next(delays) * 60
    )

    targets = [messenger.next_run(now) for _ in range(3)]

    assert targets == [at(8, 35), at(9, 5), at(9, 35)]


def test_owner_activity_resets_the_wait():
    asyncio.run(_owner_activity_resets_the_wait())


async def _owner_activity_resets_the_wait():
    messenger = make_messenger()
    calls = {"count": 0}

    def fake_next_run(now=None):
        calls["count"] += 1
        return datetime.now(TZ) + timedelta(hours=1)

    messenger.next_run = fake_next_run
    task = asyncio.create_task(messenger.run())
    await asyncio.sleep(0.1)
    assert calls["count"] == 1

    messenger.note_activity()
    await asyncio.sleep(0.1)
    assert calls["count"] == 2

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_send_once_sends_private_message_and_records_it(tmp_path):
    asyncio.run(_send_once_sends_private_message_and_records_it(tmp_path))


async def _send_once_sends_private_message_and_records_it(tmp_path):
    llm = FakeLLM(reply="记得喝口水呀~")
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    connection = FakeConnection()
    messenger = make_messenger(llm=llm, memory=memory, connection=connection)

    await messenger.send_once()

    assert connection.sent == [(10001, "记得喝口水呀~")]
    assert llm.calls == 1
    assert "主动消息" in llm.messages[-1]["content"]
    assert "大肥鱼" in llm.messages[0]["content"]
    rows = await memory.recent("private:10001", 10)
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "记得喝口水呀~"


def test_voice_marker_is_stripped_before_sending(tmp_path):
    asyncio.run(_voice_marker_is_stripped_before_sending(tmp_path))


async def _voice_marker_is_stripped_before_sending(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    connection = FakeConnection()
    messenger = make_messenger(
        llm=FakeLLM(reply="[语音] 晚安呀"), memory=memory, connection=connection
    )

    await messenger.send_once()

    assert connection.sent == [(10001, "晚安呀")]


def test_send_once_skips_when_napcat_not_connected(tmp_path):
    asyncio.run(_send_once_skips_when_napcat_not_connected(tmp_path))


async def _send_once_skips_when_napcat_not_connected(tmp_path):
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    messenger = make_messenger(llm=llm, memory=memory)

    await messenger.send_once()

    assert llm.calls == 0
    assert await memory.recent("private:10001", 10) == []


def test_send_once_skips_when_llm_fails(tmp_path):
    asyncio.run(_send_once_skips_when_llm_fails(tmp_path))


async def _send_once_skips_when_llm_fails(tmp_path):
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    connection = FakeConnection()
    messenger = make_messenger(
        llm=FakeLLM(error=LLMError("模拟失败")),
        memory=memory,
        connection=connection,
    )

    await messenger.send_once()

    assert connection.sent == []
    assert await memory.recent("private:10001", 10) == []


def test_send_once_skips_without_admin_qq(tmp_path):
    asyncio.run(_send_once_skips_without_admin_qq(tmp_path))


async def _send_once_skips_without_admin_qq(tmp_path):
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    connection = FakeConnection()
    messenger = make_messenger(
        settings=make_settings(admin_qq=0),
        llm=llm,
        memory=memory,
        connection=connection,
    )

    await messenger.send_once()

    assert llm.calls == 0
    assert connection.sent == []
