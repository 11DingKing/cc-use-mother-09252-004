"""可替换端口：时间与标识生成。测试可注入手动时钟与序列 ID 以稳定复现状态变化。"""
from __future__ import annotations

import time
import uuid
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """返回当前 Unix 时间戳（秒）。"""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class ManualClock:
    """测试用时钟：可手动推进，用于模拟授权到期等时间相关场景。"""

    def __init__(self, start: float = 1_700_000_000.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str:
        ...


class UuidIds:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"


class SequentialIds:
    """测试用 ID 生成器：单调递增、可断言。"""

    def __init__(self) -> None:
        self._seq = 0

    def new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:06d}"
