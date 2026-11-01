"""回复触发与限流。"""

from __future__ import annotations

import random
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict


class RateLimiter:
    """按会话限制回复频率：最小间隔 + 每分钟上限。"""

    def __init__(
        self,
        min_interval: float,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = max(0.0, min_interval)
        self._per_minute = max(1, per_minute)
        self._clock = clock
        self._records: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        records = self._records[key]
        while records and now - records[0] > 60:
            records.popleft()
        if records and now - records[-1] < self._min_interval:
            return False
        if len(records) >= self._per_minute:
            return False
        records.append(now)
        return True


class Trigger:
    """判断一条消息要不要回复。"""

    def __init__(
        self,
        limiter: RateLimiter,
        probability: float = 0.0,
        cooldown: float = 30.0,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limiter = limiter
        self._probability = probability
        self._cooldown = max(0.0, cooldown)
        self._rng = rng
        self._clock = clock
        self._last_random: Dict[str, float] = {}

    def allow(self, chat_key: str) -> bool:
        """戳一戳等轻量互动也走同一套频率限制。"""
        return self._limiter.allow(chat_key)

    def decide(
        self,
        chat_key: str,
        *,
        is_private: bool,
        at_me: bool,
        has_alias: bool,
    ) -> bool:
        """私聊、被 @、被叫名字都要回；群聊里其他消息按概率随机接话。"""
        if is_private or at_me or has_alias:
            return self._limiter.allow(chat_key)
        if self._probability <= 0:
            return False
        now = self._clock()
        last = self._last_random.get(chat_key)
        if last is not None and now - last < self._cooldown:
            return False
        if self._rng() >= self._probability:
            return False
        if not self._limiter.allow(chat_key):
            return False
        self._last_random[chat_key] = now
        return True
