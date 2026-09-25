"""CONFIG_SET 발행 대기열 — 토큰 버킷 + 단말별 쿨다운.

두 경로가 여기로 모인다:
  · REGISTER 수신: payload 의 cv 가 cv_server 와 다르면 (사양서 §1.1.7 연결 순서)
  · Telemetry flush: cv_device != cv_server 인 단말 (ADR-002 "다음 Telemetry 수신 시점")

왜 큐인가: 브로커 재시작 뒤 1만 대가 30초 안에 REGISTER 를 보낸다(docs/00 §5). 그때마다
바로 발행하면 초당 수백 건이 브로커로 몰린다. REGISTER_REPLY_RATE_PER_SEC 로 상한을 두고
넘치는 만큼 기다린다. 대기열이 REGISTER_REPLY_QUEUE_MAX 를 넘으면 가장 오래된 것을
버리고 센다 — 못 받은 단말은 다음 Telemetry 때 cv 불일치로 다시 잡히므로 잃는 게 없다.

쿨다운: 같은 단말에 60초 안에 두 번 보내지 않는다. REGISTER 직후 첫 Telemetry 가 아직
옛 cv 를 싣고 오는 것이 정상 흐름이라(단말이 CONFIG_ACK 전에 TM 을 보낼 수 있다),
쿨다운이 없으면 REGISTER 응답과 TM 응답으로 같은 CONFIG_SET 이 연달아 두 번 나간다.
관리자 PATCH 는 큐를 타지 않고 즉시 발행하되 mark_sent() 로 쿨다운을 건다.

3차에서 REGISTER_ACK 도 같은 큐로 나간다(retain=True 는 publisher 가 정한다).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass

from sqlalchemy import update

from app.config import settings
from app.core.metrics import metrics
from app.core.ratelimit import TokenBucket
from app.db import session_scope
from app.models.device import Device
from app.mqtt.publisher import MqttPublisher

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigJob:
    uuid: str
    cv: int
    ti: int
    lat: float | None
    lon: float | None
    reason: str  # "register" | "telemetry" 로그용


class ConfigSyncQueue:
    def __init__(
        self,
        publisher: MqttPublisher,
        *,
        rate_per_sec: float | None = None,
        max_queue: int | None = None,
        cooldown_sec: float | None = None,
    ) -> None:
        self._publisher = publisher
        self._bucket = TokenBucket(rate_per_sec or settings.register_reply_rate_per_sec)
        self._max = max_queue or settings.register_reply_queue_max
        self._cooldown = (
            settings.config_resend_cooldown_sec if cooldown_sec is None else cooldown_sec
        )
        self._queue: deque[ConfigJob] = deque()
        #: 대기열에 들어 있는 uuid — 같은 단말이 두 번 줄 서지 않게.
        self._queued: set[str] = set()
        #: uuid → 마지막 발행 시각(monotonic).
        self._last_sent: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ── 판정 ────────────────────────────────────────────────────────────
    def cooled_down(self, uuid: str, now_mono: float | None = None) -> bool:
        now_mono = asyncio.get_running_loop().time() if now_mono is None else now_mono
        last = self._last_sent.get(uuid)
        return last is None or (now_mono - last) >= self._cooldown

    def mark_sent(self, uuid: str) -> None:
        """즉시 발행 경로(PATCH config)가 쿨다운을 걸 때 쓴다."""
        self._last_sent[uuid] = asyncio.get_running_loop().time()

    # ── 적재 ────────────────────────────────────────────────────────────
    def offer(self, job: ConfigJob) -> bool:
        """대기열에 넣는다. 쿨다운 중이거나 이미 줄 서 있으면 False."""
        if job.uuid in self._queued or not self.cooled_down(job.uuid):
            return False
        if len(self._queue) >= self._max:
            dropped = self._queue.popleft()
            self._queued.discard(dropped.uuid)
            metrics.register_reply_dropped += 1
        self._queue.append(job)
        self._queued.add(job.uuid)
        self._wake.set()
        return True

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    # ── 수명주기 ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="config-sync")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # 남은 대기분은 버린다 — 재기동 뒤 Telemetry 가 다시 잡는다.
        self._queue.clear()
        self._queued.clear()

    async def _run(self) -> None:
        while not self._stopping:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._bucket.acquire()
            job = self._queue.popleft()
            self._queued.discard(job.uuid)
            if not self.cooled_down(job.uuid):
                continue  # 줄 서는 사이 다른 경로가 보냈다
            await self._publish(job)

    async def _publish(self, job: ConfigJob) -> None:
        try:
            await self._publisher.publish_config_set(
                uuid=job.uuid, cv=job.cv, ti=job.ti, lat=job.lat, lon=job.lon
            )
        except Exception:  # noqa: BLE001 - 브로커 끊김 등. 다음 Telemetry 가 다시 잡는다.
            log.exception("CONFIG_SET 발행 실패 %s (%s)", job.uuid, job.reason)
            return
        self._last_sent[job.uuid] = asyncio.get_running_loop().time()
        log.info("CONFIG_SET → %s cv=%d ti=%d (%s)", job.uuid, job.cv, job.ti, job.reason)
        try:
            async with session_scope() as db:
                await db.execute(
                    update(Device)
                    .where(Device.uuid == job.uuid)
                    .values(config_sent_at=dt.datetime.now(dt.timezone.utc))
                )
        except Exception:  # noqa: BLE001 - 기록 실패는 발행에 영향 없음
            log.exception("config_sent_at 갱신 실패 %s", job.uuid)
