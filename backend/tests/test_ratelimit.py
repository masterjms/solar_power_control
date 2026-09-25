from __future__ import annotations

import pytest

from app.core.ratelimit import TokenBucket


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_burst_then_refill():
    clock = Clock()
    bucket = TokenBucket(200, clock=clock)
    assert sum(bucket.try_take() for _ in range(200)) == 200
    assert bucket.try_take() is False
    assert bucket.seconds_until() == pytest.approx(1 / 200)
    clock.t += 0.5
    assert sum(bucket.try_take() for _ in range(200)) == 100
    clock.t += 10
    assert bucket.tokens == pytest.approx(200)  # 용량을 넘지 않는다


def test_invalid_rate():
    with pytest.raises(ValueError):
        TokenBucket(0)


async def test_acquire_waits():
    clock = Clock()
    bucket = TokenBucket(1000, burst=1, clock=clock)
    await bucket.acquire()
    assert bucket.try_take() is False
    clock.t += 0.001
    await bucket.acquire()  # 토큰이 생겨 바로 돌아온다
