"""토큰 버킷.

브로커 재시작 뒤 1만 대가 30초 안에 REGISTER 를 보내면(docs/00 §5) 응답을 그대로 쏘는
것이 폭주다. 초당 상한을 두고 넘치는 만큼 대기열에서 기다린다.

시계를 주입받는다(monotonic 함수) — 테스트가 시간을 흘리지 않고 판정할 수 있게.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_sec: float,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec 는 0 보다 커야 합니다")
        self._rate = float(rate_per_sec)
        #: 버킷 용량. 기본은 1초치 — 유휴 뒤 첫 1초에 최대 rate 만큼만 몰아 나간다.
        self._capacity = float(burst if burst is not None else rate_per_sec)
        self._tokens = self._capacity
        self._clock = clock
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    def try_take(self, n: float = 1.0) -> bool:
        """토큰이 있으면 즉시 소비하고 True. 없으면 False (기다리지 않는다)."""
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def seconds_until(self, n: float = 1.0) -> float:
        """n 개가 모일 때까지 걸릴 시간(초). 이미 있으면 0."""
        self._refill()
        deficit = n - self._tokens
        return 0.0 if deficit <= 0 else deficit / self._rate

    async def acquire(self, n: float = 1.0) -> None:
        """토큰이 생길 때까지 잔다. asyncio 전용."""
        while not self.try_take(n):
            await asyncio.sleep(max(0.001, self.seconds_until(n)))

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens
