import asyncio
import dataclasses
import json

from app.config import Settings
from app.core.engine import Engine
from app.core.llm import LLMError
from app.core.memory import MemoryStore
from app.core.search import SearchResult
from app.onebot.events import Attachment, MessageEvent


class FakeLLM:
    def __init__(self, reply: str = "你好呀~", tool_calls=(), tools_error=None) -> None:
        self.reply = reply
        self.tool_calls = list(tool_calls)
        self.tools_error = tools_error
        self.calls = 0
        self.tool_requests = 0
        self.messages: list = []

    async def request(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.messages = messages
        if tools and self.tools_error:
            raise self.tools_error
        if tools and self.tool_calls:
            self.tool_requests += 1
            return {"role": "assistant", "content": "", "tool_calls": self.tool_calls}
        return {"role": "assistant", "content": self.reply}

    async def chat(self, messages):
        self.calls += 1
        self.messages = messages
        return self.reply


def make_tool_call(query: str, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": query}, ensure_ascii=False),
        },
    }


class FailingLLM:
    async def request(self, messages, tools=None, tool_choice="auto"):
        raise LLMError("模拟失败")

    async def chat(self, messages):
        raise LLMError("模拟失败")


class FakeSearch:
    def __init__(self, results=(), error=None) -> None:
        self.results = list(results)
        self.error = error
        self.queries: list = []

    async def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


class FakeLinks:
    def __init__(self, text: str = "", error=None) -> None:
        self.text = text
        self.error = error
        self.links: list = []

    async def read_many(self, links):
        self.links = list(links)
        if self.error:
            raise self.error
        return self.text


class FakeVoice:
    def __init__(self, transcript: str = "", path=None, tts_enabled: bool = True) -> None:
        self.transcript = transcript
        self.path = path
        self._tts_enabled = tts_enabled
        self.texts: list = []

    @property
    def tts_enabled(self) -> bool:
        return self._tts_enabled

    async def transcribe(self, attachment):
        return self.transcript

    async def synthesize(self, text):
        self.texts.append(text)
        return self.path


class FakeSender:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_group_msg(self, group_id, text, quote_message_id=None):
        self.sent.append(("group", group_id, text, quote_message_id))

    async def send_private_msg(self, user_id, text):
        self.sent.append(("private", user_id, text, None))

    async def send_group_record(self, group_id, file, quote_message_id=None):
        self.sent.append(("group_record", group_id, file, quote_message_id))

    async def send_private_record(self, user_id, file):
        self.sent.append(("private_record", user_id, file, None))


def make_settings() -> Settings:
    return dataclasses.replace(
        Settings(),
        rate_min_interval=0.0,
        rate_per_minute=1000,
        group_reply_probability=0.0,
    )


def make_event(
    *,
    text: str,
    message_type: str = "private",
    at_me: bool = False,
    message_id: int = 1,
    user_id: int = 10001,
    group_id: int | None = None,
    sender_name: str = "小明",
    has_media: bool = False,
    attachments: tuple = (),
) -> MessageEvent:
    return MessageEvent(
        message_type=message_type,
        sub_type="normal",
        message_id=message_id,
        user_id=user_id,
        group_id=group_id,
        self_id=99999,
        sender_name=sender_name,
        text=text,
        at_me=at_me,
        has_media=has_media,
        attachments=attachments,
    )


async def prepare(tmp_path, llm=None, search=None, voice=None):
    llm = llm or FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm, search, voice=voice)
    return memory, engine, llm


def test_private_message_replies_and_stores(tmp_path):
    asyncio.run(_private_message_replies_and_stores(tmp_path))


async def _private_message_replies_and_stores(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(make_event(text="你好"), sender)

    assert llm.calls == 1
    assert sender.sent and sender.sent[0][0] == "private"
    rows = await memory.recent("private:10001", 10)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "你好"


def test_group_message_without_mention_is_ignored(tmp_path):
    asyncio.run(_group_message_without_mention_is_ignored(tmp_path))


async def _group_message_without_mention_is_ignored(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(
        make_event(text="今天天气不错", message_type="group", group_id=20001),
        sender,
    )

    assert llm.calls == 0
    assert sender.sent == []


def test_group_at_triggers_and_quotes(tmp_path):
    asyncio.run(_group_at_triggers_and_quotes(tmp_path))


async def _group_at_triggers_and_quotes(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(
        make_event(text="你好", message_type="group", group_id=20001, at_me=True),
        sender,
    )

    assert llm.calls == 1
    assert sender.sent[0][0] == "group"
    assert sender.sent[0][3] == 1
    rows = await memory.recent("group:20001", 10)
    assert rows[0]["content"] == "小明：你好"


def test_alias_triggers_in_group(tmp_path):
    asyncio.run(_alias_triggers_in_group(tmp_path))


async def _alias_triggers_in_group(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(
        make_event(text="大肥鱼在吗", message_type="group", group_id=20002),
        sender,
    )

    assert llm.calls == 1
    assert sender.sent
    assert llm.messages[-1]["content"] == "小明：在吗"


def test_help_command_skips_llm(tmp_path):
    asyncio.run(_help_command_skips_llm(tmp_path))


async def _help_command_skips_llm(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(make_event(text="/help"), sender)

    assert llm.calls == 0
    assert sender.sent
    assert "/remember" in sender.sent[0][2]


def test_remember_and_memories(tmp_path):
    asyncio.run(_remember_and_memories(tmp_path))


async def _remember_and_memories(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle(make_event(text="/remember 我最喜欢吃火锅"), sender)
    assert "记住" in sender.sent[-1][2]

    await engine.handle(make_event(text="/memories"), sender)
    assert "火锅" in sender.sent[-1][2]
    assert llm.calls == 0


def test_reset_clears_context(tmp_path):
    asyncio.run(_reset_clears_context(tmp_path))


async def _reset_clears_context(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle(make_event(text="你好"), sender)
    assert len(await memory.recent("private:10001", 10)) == 2

    await engine.handle(make_event(text="/reset"), sender)
    assert await memory.recent("private:10001", 10) == []


def test_llm_failure_gives_friendly_fallback(tmp_path):
    asyncio.run(_llm_failure_gives_friendly_fallback(tmp_path))


async def _llm_failure_gives_friendly_fallback(tmp_path):
    memory, engine, llm = await prepare(tmp_path, llm=FailingLLM())
    sender = FakeSender()
    await engine.handle(make_event(text="你好"), sender)

    assert sender.sent
    assert "脑子卡" in sender.sent[0][2]
    assert await memory.recent("private:10001", 10) == []


def test_private_media_without_text(tmp_path):
    asyncio.run(_private_media_without_text(tmp_path))


async def _private_media_without_text(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()
    await engine.handle(make_event(text="", has_media=True), sender)

    assert llm.calls == 1
    assert sender.sent
    assert llm.messages[-1]["content"] == "[只发送了附件]"


def test_group_replies_every_message_when_probability_one(tmp_path):
    asyncio.run(_group_replies_every_message_when_probability_one(tmp_path))


async def _group_replies_every_message_when_probability_one(tmp_path):
    settings = dataclasses.replace(
        make_settings(), group_reply_probability=1.0, group_cooldown=0.0
    )
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(settings, memory, llm)
    sender = FakeSender()

    await engine.handle(
        make_event(text="今天天气不错", message_type="group", group_id=20003),
        sender,
    )

    assert llm.calls == 1
    assert sender.sent and sender.sent[0][0] == "group"


def test_knowledge_injected_into_system_prompt(tmp_path):
    asyncio.run(_knowledge_injected_into_system_prompt(tmp_path))


async def _knowledge_injected_into_system_prompt(tmp_path):
    kb_path = tmp_path / "kb.md"
    kb_path.write_text("## 小伊卡\n\n天马小伊卡是风堇的伙伴。\n", encoding="utf-8")
    settings = dataclasses.replace(
        make_settings(), knowledge_file=kb_path, knowledge_top_k=1
    )
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(settings, memory, llm)
    sender = FakeSender()

    await engine.handle(make_event(text="小伊卡是谁"), sender)

    assert "天马小伊卡" in llm.messages[0]["content"]


def test_group_face_only_gets_reply(tmp_path):
    asyncio.run(_group_face_only_gets_reply(tmp_path))


async def _group_face_only_gets_reply(tmp_path):
    settings = dataclasses.replace(
        make_settings(), group_reply_probability=1.0, group_cooldown=0.0
    )
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(settings, memory, llm)
    sender = FakeSender()

    await engine.handle(
        make_event(text="", has_media=True, message_type="group", group_id=20004),
        sender,
    )

    assert llm.calls == 1
    assert sender.sent and sender.sent[0][0] == "group"


def test_face_label_reaches_model(tmp_path):
    asyncio.run(_face_label_reaches_model(tmp_path))


async def _face_label_reaches_model(tmp_path):
    settings = dataclasses.replace(
        make_settings(), group_reply_probability=1.0, group_cooldown=0.0
    )
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(settings, memory, llm)
    sender = FakeSender()

    await engine.handle(
        make_event(
            text="",
            message_type="group",
            group_id=20005,
            has_media=True,
            attachments=(Attachment("face", "/菜汪"),),
        ),
        sender,
    )

    assert llm.calls == 1
    assert "[QQ表情：/菜汪]" in llm.messages[-1]["content"]


def test_image_is_passed_as_multimodal_content(tmp_path, monkeypatch):
    asyncio.run(_image_is_passed_as_multimodal_content(tmp_path, monkeypatch))


async def _image_is_passed_as_multimodal_content(tmp_path, monkeypatch):
    async def fake_to_data_url(url, timeout=10.0):
        return "data:image/png;base64,AAAA"

    monkeypatch.setattr("app.core.engine.to_data_url", fake_to_data_url)
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm)
    sender = FakeSender()

    await engine.handle(
        make_event(
            text="看看这个",
            has_media=True,
            attachments=(Attachment("image", "图片", "https://example.com/a.png"),),
        ),
        sender,
    )

    assert llm.calls == 1
    content = llm.messages[-1]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)


def test_tool_call_searches_and_feeds_results_back(tmp_path):
    asyncio.run(_tool_call_searches_and_feeds_results_back(tmp_path))


async def _tool_call_searches_and_feeds_results_back(tmp_path):
    search = FakeSearch(
        [
            SearchResult(
                title="今日新闻",
                url="https://example.com/news",
                summary="某地发布了新的出行政策。",
                site_name="示例站",
            )
        ]
    )
    llm = FakeLLM(reply="我看到消息说……", tool_calls=[make_tool_call("Steam 下载榜 前十")])
    memory, engine, llm = await prepare(tmp_path, llm=llm, search=search)
    sender = FakeSender()

    await engine.handle(make_event(text="下载前十名是哪些游戏"), sender)

    assert search.queries == ["Steam 下载榜 前十"]
    assert llm.tool_requests == 1
    assert llm.calls == 2
    assert any(message.get("role") == "tool" for message in llm.messages)
    assert "某地发布了新的出行政策" in llm.messages[-1]["content"]
    assert sender.sent


def test_casual_chat_skips_web_search(tmp_path):
    asyncio.run(_casual_chat_skips_web_search(tmp_path))


async def _casual_chat_skips_web_search(tmp_path):
    search = FakeSearch(
        [SearchResult(title="新闻", url="https://example.com", summary="不该出现")]
    )
    memory, engine, llm = await prepare(tmp_path, search=search)
    sender = FakeSender()

    await engine.handle(make_event(text="今天好累啊"), sender)

    assert search.queries == []
    assert llm.calls == 1
    assert "不该出现" not in llm.messages[0]["content"]


def test_search_failure_does_not_break_reply(tmp_path):
    asyncio.run(_search_failure_does_not_break_reply(tmp_path))


async def _search_failure_does_not_break_reply(tmp_path):
    search = FakeSearch(error=RuntimeError("搜索接口挂了"))
    llm = FakeLLM(reply="没查到呢~", tool_calls=[make_tool_call("最近新闻")])
    memory, engine, llm = await prepare(tmp_path, llm=llm, search=search)
    sender = FakeSender()

    await engine.handle(make_event(text="最近有什么新闻吗"), sender)

    assert llm.calls == 2
    assert sender.sent
    assert "联网搜索结果" not in llm.messages[-1]["content"]


def test_search_tool_unsupported_falls_back_to_keywords(tmp_path):
    asyncio.run(_search_tool_unsupported_falls_back_to_keywords(tmp_path))


async def _search_tool_unsupported_falls_back_to_keywords(tmp_path):
    search = FakeSearch(
        [SearchResult(title="新闻", url="https://example.com", summary="新闻摘要")]
    )
    llm = FakeLLM(reply="我查了下……", tools_error=LLMError("tools 不支持"))
    memory, engine, llm = await prepare(tmp_path, llm=llm, search=search)
    sender = FakeSender()

    await engine.handle(make_event(text="最近有什么新闻吗"), sender)

    assert search.queries == ["最近有什么新闻吗"]
    assert "【联网搜索结果" in llm.messages[0]["content"]
    assert "新闻摘要" in llm.messages[0]["content"]


def poke_payload(**overrides) -> dict:
    payload = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "self_id": 99999,
        "user_id": 10001,
        "target_id": 99999,
    }
    payload.update(overrides)
    return payload


def test_poke_group_replies(tmp_path):
    asyncio.run(_poke_group_replies(tmp_path))


async def _poke_group_replies(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle_poke(poke_payload(group_id=20001), sender)

    assert llm.calls == 1
    assert sender.sent
    assert sender.sent[0][0] == "group"


def test_poke_private_replies(tmp_path):
    asyncio.run(_poke_private_replies(tmp_path))


async def _poke_private_replies(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle_poke(poke_payload(), sender)

    assert llm.calls == 1
    assert sender.sent
    assert sender.sent[0][0] == "private"


def test_poke_other_target_also_replies(tmp_path):
    asyncio.run(_poke_other_target_also_replies(tmp_path))


async def _poke_other_target_also_replies(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle_poke(poke_payload(target_id=12345, group_id=20001), sender)

    assert llm.calls == 1
    assert sender.sent and sender.sent[0][0] == "group"


def test_voice_transcript_reaches_model(tmp_path):
    asyncio.run(_voice_transcript_reaches_model(tmp_path))


async def _voice_transcript_reaches_model(tmp_path):
    memory, engine, llm = await prepare(tmp_path, voice=FakeVoice("今天好累呀"))
    sender = FakeSender()
    event = make_event(
        text="",
        has_media=True,
        attachments=(Attachment("record", "语音", "https://example.com/a.amr"),),
    )
    await engine.handle(event, sender)

    assert "[语音转文字]：今天好累呀" in llm.messages[-1]["content"]
    rows = await memory.recent("private:10001", 10)
    assert "[语音转文字]：今天好累呀" in rows[0]["content"]


def test_voice_marker_sends_record(tmp_path):
    asyncio.run(_voice_marker_sends_record(tmp_path))


async def _voice_marker_sends_record(tmp_path):
    llm = FakeLLM(reply="[语音] 晚安呀")
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    voice = FakeVoice(path="/tmp/voice.mp3")
    engine = Engine(make_settings(), memory, llm, voice=voice)
    sender = FakeSender()

    await engine.handle(make_event(text="说句晚安"), sender)

    assert voice.texts == ["晚安呀"]
    assert sender.sent == [("private_record", 10001, "/tmp/voice.mp3", None)]
    rows = await memory.recent("private:10001", 10)
    assert rows[1]["content"] == "晚安呀"


def test_voice_marker_falls_back_to_text(tmp_path):
    asyncio.run(_voice_marker_falls_back_to_text(tmp_path))


async def _voice_marker_falls_back_to_text(tmp_path):
    llm = FakeLLM(reply="[语音] 晚安呀")
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm, voice=FakeVoice(path=None))
    sender = FakeSender()

    await engine.handle(make_event(text="说句晚安"), sender)

    assert sender.sent and sender.sent[0][0] == "private"
    assert sender.sent[0][2] == "晚安呀"


def test_voice_reply_disabled_keeps_text(tmp_path):
    asyncio.run(_voice_reply_disabled_keeps_text(tmp_path))


async def _voice_reply_disabled_keeps_text(tmp_path):
    llm = FakeLLM(reply="[语音] 晚安呀")
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm, voice=FakeVoice(tts_enabled=False))
    sender = FakeSender()

    await engine.handle(make_event(text="说句晚安"), sender)

    assert sender.sent[0][0] == "private"
    assert "【语音回复】" not in llm.messages[0]["content"]


def test_link_context_injected_into_user_message(tmp_path):
    asyncio.run(_link_context_injected_into_user_message(tmp_path))


async def _link_context_injected_into_user_message(tmp_path):
    links = FakeLinks("【链接内容（程序抓取的页面文字，可能不完整）】\n【B站视频】测试视频")
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm, links=links)
    sender = FakeSender()

    await engine.handle(make_event(text="看看这个 https://b23.tv/AbCd12"), sender)

    assert links.links == ["https://b23.tv/AbCd12"]
    assert "【B站视频】测试视频" in llm.messages[-1]["content"]
    assert "【链接内容的使用方式】" in llm.messages[0]["content"]


def test_link_parse_disabled_skips_reader(tmp_path):
    asyncio.run(_link_parse_disabled_skips_reader(tmp_path))


async def _link_parse_disabled_skips_reader(tmp_path):
    links = FakeLinks("不该出现")
    settings = dataclasses.replace(make_settings(), link_parse_enabled=False)
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(settings, memory, llm, links=links)
    sender = FakeSender()

    await engine.handle(make_event(text="看看这个 https://b23.tv/AbCd12"), sender)

    assert links.links == []
    assert "不该出现" not in llm.messages[-1]["content"]


def test_link_parse_failure_does_not_break_reply(tmp_path):
    asyncio.run(_link_parse_failure_does_not_break_reply(tmp_path))


async def _link_parse_failure_does_not_break_reply(tmp_path):
    links = FakeLinks(error=RuntimeError("抓取挂了"))
    llm = FakeLLM()
    memory = MemoryStore(tmp_path / "bot.db")
    await memory.initialize()
    engine = Engine(make_settings(), memory, llm, links=links)
    sender = FakeSender()

    await engine.handle(make_event(text="看看这个 https://b23.tv/AbCd12"), sender)

    assert sender.sent
    assert "【链接内容的使用方式】" not in llm.messages[0]["content"]


def test_poke_self_is_ignored(tmp_path):
    asyncio.run(_poke_self_is_ignored(tmp_path))


async def _poke_self_is_ignored(tmp_path):
    memory, engine, llm = await prepare(tmp_path)
    sender = FakeSender()

    await engine.handle_poke(poke_payload(user_id=99999), sender)

    assert llm.calls == 0
    assert sender.sent == []
