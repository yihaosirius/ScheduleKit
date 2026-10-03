"""进程内滑动窗口限流。

为什么不用 Redis：单进程单用户，进程内 dict 足够；引入 Redis 只会多一个要维护的组件。

一个刻意的取舍：**限流状态会随重启清空**。对登录爆破来说这不是漏洞
（重启后攻击者窗口重新开始，但他也拿不到任何额外信息），
换来的是零依赖与零 I/O。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class SlidingWindow:
    """按 key（通常是 IP）记录时间戳，窗口内计数超限即拒绝。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, limit)
        self.window = max(1.0, float(window_seconds))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _prune(self, queue: deque[float], now: float) -> None:
        cutoff = now - self.window
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """返回 ``(是否允许, 还要等多少秒)``。

        ``allowed=True`` 时不消费配额——调用方在"尝试失败"时才调 :meth:`hit`。
        这样成功登录不会把自己锁出去。
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            queue = self._hits[key]
            self._prune(queue, moment)
            if len(queue) >= self.limit:
                return False, max(0.0, queue[0] + self.window - moment)
            return True, 0.0

    def hit(self, key: str, *, now: float | None = None) -> int:
        """记一次失败。返回窗口内当前计数。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            queue = self._hits[key]
            self._prune(queue, moment)
            queue.append(moment)
            return len(queue)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
