"""主动消息：在设定时段内按随机间隔私聊号主，内容由大模型按人设生成。"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from ..config import Settings
from .llm import LLMClient, LLMError
from .memory import MemoryStore
from .persona import build_system_prompt
from .text import clean_text, split_text
from .voice import pop_voice_marker

logger = logging.getLogger(__name__)

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class PrivateSender(Protocol):
    """发送主动消息所需的最小接口，方便测试时替换。"""

    async def send_private_msg(self, user_id: int, text: str) -> None: ...


class ProactiveMessenger:
    """后台定时任务：在设定时段内随机间隔向 ADMIN_QQ 发主动私聊消息。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        memory: MemoryStore,
        get_connection: Callable[[], Optional[PrivateSender]],
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._memory = memory
        self._get_connection = get_connection
        self._min_delay = max(1, settings.proactive_interval_minutes) * 60
        self._max_delay = max(1, settings.proactive_interval_max_minutes) * 60
        if self._max_delay < self._min_delay:
            logger.warning(
                "主动消息最长间隔（%s 分钟）小于最短间隔（%s 分钟），已按最短间隔发送",
                settings.proactive_interval_max_minutes,
                settings.proactive_interval_minutes,
            )
            self._max_delay = self._min_delay
        self._activity = asyncio.Event()
        self._start_hour = min(max(0, settings.proactive_start_hour), 23)
        self._end_hour = min(max(0, settings.proactive_end_hour), 23)
        if self._start_hour >= self._end_hour:
            logger.warning(
                "主动消息时段配置不合法（%02d:00–%02d:00），已回退到 08:00–23:00",
                self._start_hour,
                self._end_hour,
            )
            self._start_hour, self._end_hour = 8, 23
        offset = max(-12.0, min(14.0, settings.proactive_utc_offset))
        self._tz = timezone(timedelta(hours=offset))

    def _random_delay(self) -> float:
        return random.uniform(self._min_delay, self._max_delay)

    def next_run(self, now: Optional[datetime] = None) -> datetime:
        """返回下一次发送时间：当前时间加一段配置范围内的随机间隔。"""
        current = now or datetime.now(self._tz)
        start_at = current.replace(
            hour=self._start_hour, minute=0, second=0, microsecond=0
        )
        end_at = start_at.replace(hour=self._end_hour)
        if current < start_at:
            return start_at
        if current >= end_at:
            return start_at + timedelta(days=1)
        candidate = current + timedelta(seconds=self._random_delay())
        if candidate >= end_at:
            return start_at + timedelta(days=1)
        return candidate

    def note_activity(self) -> None:
        """号主在私聊里说话或戳一戳时调用：打断当前等待，重新随机计时。"""
        self._activity.set()

    async def run(self) -> None:
        """后台循环：随机等待一段时间，生成并发送一条主动消息。"""
        while True:
            self._activity.clear()
            now = datetime.now(self._tz)
            target = self.next_run(now)
            wait_seconds = max(0.0, (target - now).total_seconds())
            logger.info(
                "下一次主动消息：%s（约 %.0f 分钟后）",
                target.strftime("%m-%d %H:%M"),
                wait_seconds / 60,
            )
            try:
                await asyncio.wait_for(self._activity.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            else:
                logger.info("号主有互动，主动消息重新计时")
                continue
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 主动消息失败不能影响进程
                logger.warning("主动消息发送异常：%s", exc)

    async def send_once(self) -> None:
        """生成并私聊发送一条主动消息。"""
        user_id = int(self._settings.admin_qq or 0)
        if user_id <= 0:
            logger.warning("未配置 ADMIN_QQ，跳过主动消息")
            return
        connection = self._get_connection()
        if connection is None:
            logger.info("NapCat 未连接，跳过本次主动消息")
            return

        chat_key = f"private:{user_id}"
        summary, _covered = await self._memory.latest_summary(chat_key)
        memories = await self._memory.list_memories(chat_key)
        prompt = [
            {
                "role": "system",
                "content": build_system_prompt(
                    bot_name=self._settings.bot_name,
                    max_chars=self._settings.max_reply_chars,
                    is_group=False,
                    summary=summary,
                    memories=memories,
                ),
            },
            {"role": "user", "content": self._instruction(datetime.now(self._tz))},
        ]
        try:
            answer = await self._llm.chat(prompt)
        except LLMError as exc:
            logger.warning("生成主动消息失败：%s", exc)
            return

        answer = clean_text(answer)
        answer, _wants_voice = pop_voice_marker(answer)
        if not answer:
            logger.info("模型没有生成主动消息，本次跳过")
            return

        segments = split_text(answer, self._settings.max_reply_chars, 1)
        if not segments:
            logger.info("主动消息内容为空，本次跳过")
            return
        text = segments[0]
        await connection.send_private_msg(user_id, text)
        await self._memory.add_message(chat_key, "assistant", text)
        logger.info("已向 QQ %s 发送主动消息", user_id)

    def _instruction(self, now: datetime) -> str:
        return (
            f"【主动消息】现在是 {now.month} 月 {now.day} 日 {_WEEKDAYS[now.weekday()]} "
            f"{now:%H:%M}，对方没有在跟你说话。请你主动给他发一条 QQ 私聊消息："
            "可以关心他的状态、提醒喝水吃饭休息，或者分享一句符合你人设的小心情。"
            "只输出要发送的内容，不要解释，不要写动作或旁白，"
            "不要提“定时”“提醒任务”这类字眼。"
            f"不超过 {self._settings.max_reply_chars} 字，一条说完。"
        )
