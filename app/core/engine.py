"""消息处理引擎：触发判断、上下文组装、调用大模型、回复与记忆。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Set

from ..config import Settings
from . import commands
from .embeddings import BailianEmbeddings
from .knowledge import KnowledgeBase
from .link import LinkReader, extract_links
from .llm import LLMClient, LLMError
from .media import to_data_url
from .memory import MemoryStore
from .persona import WEB_SEARCH_GUIDE, build_system_prompt
from .search import WebSearchClient, format_search_context, needs_search
from .text import clean_text, contains_alias, remove_aliases, split_text
from .trigger import RateLimiter, Trigger
from .voice import VoiceService, pop_voice_marker
from .weather import WeatherClient

logger = logging.getLogger(__name__)

# 交给模型的联网搜索工具：由模型自己判断要不要查、查什么
_SEARCH_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时网页信息：新闻、榜单排名、价格、天气、赛事、新游戏、新版本、附近店铺等",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，尽量具体，不超过 128 字",
                }
            },
            "required": ["query"],
        },
    },
}
# 交给模型的天气工具：走结构化预报，避免拿网页旧数据回答
_WEATHER_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "查询中国城市未来 3 天的天气预报（天气现象、最高/最低气温、降水概率）。"
            "对方问天气、气温、下雨、带伞或穿什么时优先调用，不要用 web_search 查天气。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "常见中文城市名，如武汉、北京；不要带区县、湖泊或景点名",
                }
            },
            "required": ["city"],
        },
    },
}
_MAX_TOOL_CALLS = 2

# 红包气氛：不一定真抢到，只图个热闹，所以不声明金额
_RED_PACKET_REPLIES = (
    "咦，红包的气息～谢谢老板，新年快乐！",
    "恭喜发财，万事顺意，新年快乐呀～",
    "红包一响，好运登场，新年快乐！",
)


def _tool_query(call: Dict[str, Any]) -> str:
    """从模型返回的 tool_call 里取出搜索关键词；格式不对就返回空。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("query") or "").strip()


def _tool_name(call: Dict[str, Any]) -> str:
    """取出 tool_call 的工具名；格式不对就返回空。"""
    try:
        return str(call["function"]["name"] or "")
    except (KeyError, TypeError, ValueError):
        return ""


def _tool_city(call: Dict[str, Any]) -> str:
    """从天气 tool_call 里取出城市名。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("city") or "").strip()[:40]

SUMMARY_SYSTEM_PROMPT = """你在为一段 QQ 聊天整理“剧情摘要”，供后续聊天参考。请把旧摘要和新对话合并成一段不超过 400 字的中文摘要，保留：聊天对象的关键信息（昵称、喜好、计划）、聊过的重要话题和结论、还没完成的事。去掉寒暄和重复内容。只输出摘要正文，不要标题、列表符号或解释。"""


class MessageSender(Protocol):
    """发送消息所需的最小接口，方便测试时替换。"""

    async def send_group_msg(
        self, group_id: int, text: str, quote_message_id: int | None = None
    ) -> None: ...

    async def send_private_msg(self, user_id: int, text: str) -> None: ...

    async def send_group_record(
        self, group_id: int, file: str, quote_message_id: int | None = None
    ) -> None: ...

    async def send_private_record(self, user_id: int, file: str) -> None: ...


class Engine:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        llm: LLMClient,
        search: WebSearchClient | None = None,
        links: LinkReader | None = None,
        voice: VoiceService | None = None,
        weather: WeatherClient | None = None,
    ) -> None:
        self._settings = settings
        self._memory = memory
        self._llm = llm
        self._search = search
        self._links = links
        self._voice = voice
        self._weather = weather
        embedder = None
        if settings.embedding_enabled and settings.embedding_api_key:
            embedder = BailianEmbeddings(
                settings.embedding_api_key,
                settings.embedding_base_url,
                settings.embedding_model,
                settings.embedding_dimensions,
                settings.embedding_timeout,
            )
        self._knowledge = KnowledgeBase(
            settings.knowledge_path,
            embedder=embedder,
            index_dir=settings.knowledge_index_dir,
            embedding_min_score=settings.embedding_min_score,
        )
        self._trigger = Trigger(
            RateLimiter(settings.rate_min_interval, settings.rate_per_minute),
            settings.group_reply_probability,
            settings.group_cooldown,
        )
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._summary_locks: Dict[str, asyncio.Lock] = {}
        self._summary_tasks: Set[asyncio.Task] = set()
        self._red_packet_last: Dict[str, float] = {}
        self._last_send_at: Dict[str, float] = {}
        self._activity_listener: Optional[Callable[[], None]] = None

    def set_activity_listener(self, listener: Callable[[], None]) -> None:
        """注册号主互动的回调，用于重置主动消息计时。"""
        self._activity_listener = listener

    async def start(self) -> None:
        """启动时检查语料索引；需要重建时在后台执行，不阻塞机器人上线。"""
        await self._knowledge.start()

    async def aclose(self) -> None:
        """停止索引任务并关闭知识库持有的网络客户端。"""
        await self._knowledge.aclose()

    def _notify_activity(self) -> None:
        if self._activity_listener is None:
            return
        try:
            self._activity_listener()
        except Exception as exc:  # 回调失败不能影响正常聊天
            logger.warning("重置主动消息计时失败：%s", exc)

    def _note_activity(self, event) -> None:
        """号主私聊发消息时通知主动消息任务重新计时。"""
        if not event.is_private or event.user_id != self._settings.admin_qq:
            return
        self._notify_activity()

    async def handle(self, event, sender: MessageSender) -> None:
        """处理一条已经解析好的消息事件。"""
        self._note_activity(event)
        chat_key = event.chat_key
        text = clean_text(event.text)
        alias_hit = contains_alias(text, self._settings.aliases)
        if alias_hit:
            text = clean_text(remove_aliases(text, self._settings.aliases))

        # 群聊里没点名的斜杠文本不当指令，也不打扰别人
        if event.is_group and not (event.at_me or alias_hit) and text.startswith("/"):
            return

        if event.unsupported:
            await self._react_red_packet(sender, event)
            return

        if event.is_private or event.at_me or alias_hit:
            command = commands.parse_command(text)
            if command is not None:
                reply = await commands.handle_command(
                    command[0],
                    command[1],
                    chat_key=chat_key,
                    is_private=event.is_private,
                    sender_id=event.user_id,
                    settings=self._settings,
                    memory=self._memory,
                )
                if reply:
                    await self._send(sender, event, reply, quote=True)
                return

        if not self._trigger.decide(
            chat_key,
            is_private=event.is_private,
            at_me=event.at_me,
            has_alias=alias_hit,
        ):
            return

        if not text and not event.has_media:
            if event.at_me or alias_hit:
                await self._send(sender, event, "我在呀，怎么啦~", quote=True)
            return

        knowledge = await self._knowledge.retrieve(
            text, self._settings.knowledge_top_k
        )
        transcript = await self._transcribe(event.attachments)
        history_text = self._compose_history_text(event, text, transcript)
        link_context = await self._read_links(text, event.attachments)
        llm_content = await self._build_llm_content(
            history_text, event.attachments, link_context
        )

        async with self._chat_lock(chat_key):
            summary, _covered = await self._memory.latest_summary(chat_key)
            memories = await self._memory.list_memories(chat_key)
            recent = await self._memory.recent(chat_key, self._settings.context_messages)
            prompt = [
                {
                    "role": "system",
                    "content": build_system_prompt(
                        bot_name=self._settings.bot_name,
                        max_chars=self._settings.max_reply_chars,
                        is_group=event.is_group,
                        summary=summary,
                        memories=memories,
                        knowledge=knowledge,
                        search_tool=self._search is not None,
                        weather_tool=self._weather is not None,
                        link_context=bool(link_context),
                        voice_reply=self._voice is not None and self._voice.tts_enabled,
                        max_segments=self._settings.max_reply_segments,
                    ),
                }
            ]
            prompt.extend(recent)
            prompt.append({"role": "user", "content": llm_content})

            failed = False
            try:
                answer = await self._answer(prompt, text)
            except LLMError as exc:
                logger.warning("调用大模型失败：%s", exc)
                answer = "哎呀，我刚刚脑子卡了一下，等会儿再跟我说一次好不好~"
                failed = True
            answer = clean_text(answer)
            answer, wants_voice = pop_voice_marker(answer)

            if answer and not failed:
                await self._memory.add_message(chat_key, "user", history_text)
                await self._memory.add_message(chat_key, "assistant", answer)

            sent_as_voice = False
            if wants_voice and answer and not failed:
                sent_as_voice = await self._send_voice(sender, event, answer)
            if not sent_as_voice:
                segments = split_text(
                    answer,
                    self._settings.max_reply_chars,
                    self._settings.max_reply_segments,
                )
                for index, segment in enumerate(segments):
                    await self._send(sender, event, segment, quote=index == 0)

            if answer and not failed:
                self._schedule_summary(chat_key)

    async def _answer(self, prompt: List[Dict[str, Any]], text: str) -> str:
        """让模型按需调用搜索或天气工具，再生成最终回复。"""
        tools: List[Dict[str, Any]] = []
        if self._search is not None:
            tools.append(_SEARCH_TOOL)
        if self._weather is not None:
            tools.append(_WEATHER_TOOL)
        if not tools:
            return await self._llm.chat(prompt)
        try:
            message = await self._llm.request(prompt, tools=tools)
        except LLMError as exc:  # 模型或网关不支持工具时，退回关键词判断
            if self._search is None:
                logger.warning("工具调用不可用，直接聊天：%s", exc)
                return await self._llm.chat(prompt)
            logger.warning("联网搜索工具不可用，改用关键词判断：%s", exc)
            return await self._chat_with_keywords(prompt, text)
        calls = message.get("tool_calls") or []
        if not calls:
            return str(message.get("content") or "").strip()
        return await self._answer_with_tools(prompt, message, calls)

    async def _answer_with_tools(
        self,
        prompt: List[Dict[str, Any]],
        message: Dict[str, Any],
        calls: List[Dict[str, Any]],
    ) -> str:
        """执行模型要求的工具调用，把结果交给模型生成最终回复。"""
        follow: List[Dict[str, Any]] = list(prompt)
        assistant: Dict[str, Any] = {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        }
        if message.get("reasoning_content"):
            assistant["reasoning_content"] = message["reasoning_content"]
        follow.append(assistant)
        for index, call in enumerate(calls):
            content = await self._run_tool(call, index)
            follow.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": content,
                }
            )
        return await self._llm.chat(follow)

    async def _run_tool(self, call: Dict[str, Any], index: int) -> str:
        """按工具名分发；次数用完后不再执行外部请求。"""
        if index >= _MAX_TOOL_CALLS:
            return "工具调用次数已用完。"
        name = _tool_name(call)
        if name == "get_weather":
            return await self._weather_context(call)
        if name == "web_search":
            return await self._search_context(call)
        return "未知工具。"

    async def _weather_context(self, call: Dict[str, Any]) -> str:
        if self._weather is None:
            return "天气工具不可用。"
        city = _tool_city(call)
        if not city:
            return "没有提供城市名。"
        try:
            report = await self._weather.forecast(city)
        except Exception as exc:  # 天气查询失败不能影响正常聊天
            logger.warning("天气查询异常：%s", exc)
            report = None
        if report is None:
            return f"没有查到「{city}」的天气数据。"
        return report.format_context()

    async def _search_context(self, call: Dict[str, Any]) -> str:
        query = _tool_query(call)
        results = []
        if query and self._search is not None:
            try:
                results = await self._search.search(query)
            except Exception as exc:  # 搜索失败不能影响正常聊天
                logger.warning("联网搜索异常：%s", exc)
                results = []
        return format_search_context(results) or "没有搜到可用的结果。"

    async def _chat_with_keywords(
        self, prompt: List[Dict[str, Any]], text: str
    ) -> str:
        """工具不可用时的兜底：按关键词命中就先把网页摘要塞进提示词。"""
        if not text or not needs_search(text):
            return await self._llm.chat(prompt)
        try:
            results = await self._search.search(text)
        except Exception as exc:  # 搜索失败不能影响正常聊天
            logger.warning("联网搜索异常：%s", exc)
            results = []
        web_context = format_search_context(results)
        if not web_context:
            return await self._llm.chat(prompt)
        messages: List[Dict[str, Any]] = list(prompt)
        messages[0] = {
            "role": "system",
            "content": f"{messages[0]['content']}\n\n{WEB_SEARCH_GUIDE}\n{web_context}",
        }
        return await self._llm.chat(messages)

    def _compose_history_text(self, event, text: str, transcript: str = "") -> str:
        """把表情/图片/语音等附件转成文字说明，供模型和聊天记录使用。"""
        notes: list[str] = []
        for attachment in event.attachments:
            if attachment.kind == "face":
                notes.append(f"[QQ表情：{attachment.label}]")
            elif attachment.kind == "record":
                notes.append(f"[语音转文字]：{transcript}" if transcript else "[语音]")
            else:
                notes.append(f"[{attachment.label or '附件'}]")
        body = text
        if notes:
            body = f"{body} {' '.join(notes)}".strip()
        if not body:
            body = "[只发送了附件]"
        return f"{event.sender_name}：{body}" if event.is_group else body

    async def _transcribe(self, attachments) -> str:
        """把第一条语音附件转成文字；未启用或失败时返回空串。"""
        if self._voice is None or not self._settings.voice_asr_enabled:
            return ""
        for attachment in attachments:
            if attachment.kind != "record" or not attachment.url:
                continue
            try:
                return await self._voice.transcribe(attachment)
            except Exception as exc:  # 语音识别失败不能影响正常聊天
                logger.warning("语音识别异常：%s", exc)
                return ""
        return ""

    async def _send_voice(self, sender: MessageSender, event, text: str) -> bool:
        """把回复合成为语音并发送；失败返回 False，由调用方改发文字。"""
        if self._voice is None or not self._settings.voice_tts_enabled:
            return False
        if len(text) > self._settings.voice_tts_max_chars:
            return False
        try:
            path = await self._voice.synthesize(text)
        except Exception as exc:  # 合成异常不能影响正常聊天
            logger.warning("语音合成异常：%s", exc)
            return False
        if not path:
            return False
        await self._wait_before_send(event.chat_key)
        quote = self._settings.quote_reply
        try:
            if event.is_group:
                await sender.send_group_record(
                    event.group_id, path, quote_message_id=event.message_id if quote else None
                )
            else:
                await sender.send_private_record(event.user_id, path)
        except Exception as exc:
            logger.warning("语音发送失败，改用文字：%s", exc)
            return False
        return True

    async def _read_links(self, text: str, attachments) -> str:
        """把消息里的 B 站/网页链接抓成文字；失败或没链接时返回空串。"""
        if self._links is None or not self._settings.link_parse_enabled:
            return ""
        links = extract_links(text, attachments, self._settings.link_parse_max_links)
        if not links:
            return ""
        try:
            return await self._links.read_many(links)
        except Exception as exc:  # 链接抓取失败不能影响正常聊天
            logger.warning("链接解析异常：%s", exc)
            return ""

    async def _build_llm_content(
        self, history_text: str, attachments, extra_text: str = ""
    ) -> str | list:
        """有图片时下载并转成 data URL，让模型直接看图；否则只发文字。"""
        body = f"{history_text}\n\n{extra_text}" if extra_text else history_text
        urls = [item.url for item in attachments if item.kind == "image" and item.url][:2]
        images = [data for data in [await to_data_url(url) for url in urls] if data]
        if not images:
            return body
        parts: list[dict] = [{"type": "text", "text": body}]
        parts.extend({"type": "image_url", "image_url": {"url": data}} for data in images)
        return parts

    async def handle_poke(self, payload: dict, sender: MessageSender) -> None:
        """处理戳一戳：由模型即兴回应；戳别人也会接一句。"""
        try:
            self_id = int(payload.get("self_id") or 0)
            user_id = int(payload.get("user_id") or 0)
            target_id = int(payload.get("target_id") or 0)
        except (TypeError, ValueError):
            return
        if user_id == self_id:  # 机器人自己触发的事件不接，避免自循环
            return

        group_id = payload.get("group_id")
        try:
            if group_id:
                group_id = int(group_id)
                chat_key = f"group:{group_id}"
            else:
                group_id = None
                chat_key = f"private:{user_id}"
        except (TypeError, ValueError):
            return

        if group_id is None and user_id == self._settings.admin_qq:
            self._notify_activity()

        if not self._trigger.allow(chat_key):
            return

        if target_id == self_id:
            scene = f"QQ {user_id} 在聊天里戳了戳你。"
        else:
            scene = f"QQ {user_id} 在聊天里戳了戳 QQ {target_id}。"
        prompt = [
            {
                "role": "system",
                "content": build_system_prompt(
                    bot_name=self._settings.bot_name,
                    max_chars=self._settings.max_reply_chars,
                    is_group=group_id is not None,
                    summary="",
                    memories=[],
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【戳一戳】{scene}\n"
                    "这是轻量互动，请用一句符合你人设的话回应，"
                    f"不超过 {self._settings.max_reply_chars} 字，不要解释规则。"
                ),
            },
        ]
        try:
            answer = await self._llm.chat(prompt)
        except LLMError as exc:
            logger.warning("戳一戳调用模型失败：%s", exc)
            return
        answer = clean_text(answer)
        if not answer:
            return

        segments = split_text(
            answer,
            self._settings.max_reply_chars,
            self._settings.max_reply_segments,
        )
        try:
            for segment in segments:
                await self._wait_before_send(chat_key)
                if group_id is not None:
                    await sender.send_group_msg(group_id, segment)
                else:
                    await sender.send_private_msg(user_id, segment)
        except Exception as exc:  # 发送失败不应影响后续消息处理
            logger.warning("回复戳一戳失败（%s）：%s", chat_key, exc)

    def _chat_lock(self, chat_key: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_key] = lock
        return lock

    async def _react_red_packet(self, sender: MessageSender, event) -> None:
        """收到红包（NapCat 把它丢弃成空消息）时，随机等几秒说句吉利话。"""
        if not self._settings.red_packet_enabled:
            return
        now = time.monotonic()
        last = self._red_packet_last.get(event.chat_key, 0.0)
        if now - last < self._settings.red_packet_cooldown:
            return
        self._red_packet_last[event.chat_key] = now
        low = max(0.0, min(self._settings.red_packet_delay_min, self._settings.red_packet_delay_max))
        high = max(low, self._settings.red_packet_delay_max, 0.0)
        delay = random.uniform(low, high)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._send(sender, event, random.choice(_RED_PACKET_REPLIES), quote=False)

    async def _wait_before_send(self, chat_key: str) -> None:
        """同一会话相邻两条消息随机间隔几秒，避免连发被判定为机器人。"""
        low = max(0.0, self._settings.segment_delay_min)
        high = max(low, self._settings.segment_delay_max)
        delay = random.uniform(low, high)
        last = self._last_send_at.get(chat_key)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_send_at[chat_key] = time.monotonic()

    async def _send(self, sender: MessageSender, event, text: str, *, quote: bool) -> None:
        if not text:
            return
        if event.is_group and event.group_id is None:
            return
        await self._wait_before_send(event.chat_key)
        try:
            if event.is_group:
                quote_id = event.message_id if quote and self._settings.quote_reply else None
                await sender.send_group_msg(event.group_id, text, quote_message_id=quote_id)
            else:
                await sender.send_private_msg(event.user_id, text)
        except Exception as exc:  # 发送失败不应影响后续消息处理
            logger.warning("发送消息失败（%s）：%s", event.chat_key, exc)

    def _schedule_summary(self, chat_key: str) -> None:
        task = asyncio.create_task(self._summarize(chat_key))
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    async def _summarize(self, chat_key: str) -> None:
        lock = self._summary_locks.get(chat_key)
        if lock is None:
            lock = asyncio.Lock()
            self._summary_locks[chat_key] = lock
        async with lock:
            summary, covered = await self._memory.latest_summary(chat_key)
            rows = await self._memory.unsummarized(
                chat_key, covered, self._settings.summary_trigger
            )
            if len(rows) < self._settings.summary_trigger:
                return
            transcript = "\n".join(f"{row['role']}：{row['content']}" for row in rows)
            prompt = [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"【旧摘要】\n{summary or '（无）'}\n\n【新对话】\n{transcript}",
                },
            ]
            try:
                new_summary = await self._llm.chat(prompt)
            except LLMError as exc:
                logger.warning("生成摘要失败：%s", exc)
                return
            new_summary = clean_text(new_summary)[:1000]
            if not new_summary:
                return
            await self._memory.save_summary(chat_key, new_summary, int(rows[-1]["id"]))
