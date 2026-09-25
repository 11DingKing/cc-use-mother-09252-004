"""可替换端口：时钟与标识生成。

时间、标识等外部输入通过注入端口进入应用服务，
测试可用确定性实现稳定复现状态变化。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Protocol


class Clock(Protocol):
    """返回当前 Unix 时间戳（秒）。"""

    def __call__(self) -> float: ...


class IdGenerator(Protocol):
    """按前缀生成全局唯一标识。"""

    def __call__(self, prefix: str) -> str: ...


def system_clock() -> float:
    """生产时钟：系统时间。"""
    return time.time()


def uuid_id(prefix: str) -> str:
    """生产标识：UUID4。"""
    return f"{prefix}_{uuid.uuid4().hex}"


class ManualClock:
    """测试时钟：可手动推进，用于授权到期等时间敏感场景。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SequentialIds:
    """测试标识：确定性递增，便于断言。线程安全。"""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self._lock:
            self._n += 1
            return f"{prefix}_{self._n:06d}"
