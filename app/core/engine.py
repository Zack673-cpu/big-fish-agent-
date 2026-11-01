"""消息处理引擎：触发判断、上下文组装、调用大模型、回复与记忆。"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Protocol, Set

from ..config import Settings
from . import commands
from .knowledge import KnowledgeBase
from .llm import LLMClient, LLMError
from .media import to_data_url
from .memory import MemoryStore
from .persona import build_system_prompt
from .text import clean_text, contains_alias, remove_aliases, split_text
from .trigger import RateLimiter, Trigger

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = """你在为一段 QQ 聊天整理“剧情摘要”，供后续聊天参考。请把旧摘要和新对话合并成一段不超过 400 字的中文摘要，保留：聊天对象的关键信息（昵称、喜好、计划）、聊过的重要话题和结论、还没完成的事。去掉寒暄和重复内容。只输出摘要正文，不要标题、列表符号或解释。"""


class MessageSender(Protocol):
    """发送消息所需的最小接口，方便测试时替换。"""

    async def send_group_msg(
        self, group_id: int, text: str, quote_message_id: int | None = None
    ) -> None: ...

    async def send_private_msg(self, user_id: int, text: str) -> None: ...


class Engine:
    def __init__(self, settings: Settings, memory: MemoryStore, llm: LLMClient) -> None:
        self._settings = settings
        self._memory = memory
        self._llm = llm
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
        history_text = self._compose_history_text(event, text)
        llm_content = await self._build_llm_content(history_text, event.attachments)

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
                    ),
                }
            ]
            prompt.extend(recent)
            prompt.append({"role": "user", "content": llm_content})

            failed = False
            try:
                answer = await self._llm.chat(prompt)
            except LLMError as exc:
                logger.warning("调用大模型失败：%s", exc)
                answer = "哎呀，我刚刚脑子卡了一下，等会儿再跟我说一次好不好~"
                failed = True
            answer = clean_text(answer)

            if answer and not failed:
                await self._memory.add_message(chat_key, "user", history_text)
                await self._memory.add_message(chat_key, "assistant", answer)

            segments = split_text(
                answer,
                self._settings.max_reply_chars,
                self._settings.max_reply_segments,
            )
            for index, segment in enumerate(segments):
                await self._send(sender, event, segment, quote=index == 0)

            if answer and not failed:
                self._schedule_summary(chat_key)

    def _compose_history_text(self, event, text: str) -> str:
        """把表情/图片等附件转成文字说明，供模型和聊天记录使用。"""
        notes: list[str] = []
        for attachment in event.attachments:
            if attachment.kind == "face":
                notes.append(f"[QQ表情：{attachment.label}]")
            else:
                notes.append(f"[{attachment.label or '附件'}]")
        body = text
        if notes:
            body = f"{body} {' '.join(notes)}".strip()
        if not body:
            body = "[只发送了附件]"
        return f"{event.sender_name}：{body}" if event.is_group else body

    async def _build_llm_content(self, history_text: str, attachments) -> str | list:
        """有图片时下载并转成 data URL，让模型直接看图；否则只发文字。"""
        urls = [item.url for item in attachments if item.kind == "image" and item.url][:2]
        images = [data for data in [await to_data_url(url) for url in urls] if data]
        if not images:
            return history_text
        parts: list[dict] = [{"type": "text", "text": history_text}]
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
