"""消息处理引擎：触发判断、上下文组装、调用大模型、回复与记忆。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Protocol, Set

from ..config import Settings
from . import commands
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
_MAX_TOOL_CALLS = 2


def _tool_query(call: Dict[str, Any]) -> str:
    """从模型返回的 tool_call 里取出搜索关键词；格式不对就返回空。"""
    try:
        args = json.loads(call["function"]["arguments"])
    except (KeyError, TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("query") or "").strip()

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
    ) -> None:
        self._settings = settings
        self._memory = memory
        self._llm = llm
        self._search = search
        self._links = links
        self._voice = voice
        self._knowledge = KnowledgeBase(settings.knowledge_file)
        self._trigger = Trigger(
            RateLimiter(settings.rate_min_interval, settings.rate_per_minute),
            settings.group_reply_probability,
            settings.group_cooldown,
        )
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._summary_locks: Dict[str, asyncio.Lock] = {}
        self._summary_tasks: Set[asyncio.Task] = set()

    async def handle(self, event, sender: MessageSender) -> None:
        """处理一条已经解析好的消息事件。"""
        chat_key = event.chat_key
        text = clean_text(event.text)
        alias_hit = contains_alias(text, self._settings.aliases)
        if alias_hit:
            text = clean_text(remove_aliases(text, self._settings.aliases))

        # 群聊里没点名的斜杠文本不当指令，也不打扰别人
        if event.is_group and not (event.at_me or alias_hit) and text.startswith("/"):
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

        knowledge = self._knowledge.retrieve(text, self._settings.knowledge_top_k)
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
        """让模型决定要不要联网：需要就搜一次再答。"""
        if self._search is None:
            return await self._llm.chat(prompt)
        try:
            message = await self._llm.request(prompt, tools=[_SEARCH_TOOL])
        except LLMError as exc:  # 模型或网关不支持工具时，退回关键词判断
            logger.warning("联网搜索工具不可用，改用关键词判断：%s", exc)
            return await self._chat_with_keywords(prompt, text)
        calls = message.get("tool_calls") or []
        if not calls:
            return str(message.get("content") or "").strip()
        return await self._answer_with_search(prompt, message, calls)

    async def _answer_with_search(
        self,
        prompt: List[Dict[str, Any]],
        message: Dict[str, Any],
        calls: List[Dict[str, Any]],
    ) -> str:
        """执行模型要求的搜索，把结果交给模型生成最终回复。"""
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
            query = _tool_query(call) if index < _MAX_TOOL_CALLS else ""
            results = []
            if query:
                try:
                    results = await self._search.search(query)
                except Exception as exc:  # 搜索失败不能影响正常聊天
                    logger.warning("联网搜索异常：%s", exc)
                    results = []
            follow.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": format_search_context(results) or "没有搜到可用的结果。",
                }
            )
        return await self._llm.chat(follow)

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

    async def _send(self, sender: MessageSender, event, text: str, *, quote: bool) -> None:
        if not text:
            return
        try:
            if event.is_group:
                if event.group_id is None:
                    return
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
