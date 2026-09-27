"""단말 응답 발행 대기열 — REGISTER_ACK · CONFIG_SET (토큰 버킷 + CONFIG 단말별 쿨다운).

사양서 §1.1.10: 서버 → 단말은 **단말이 보낸 직후**에 보낸다. 그 "직후"에 나가는 두 가지가
여기로 모인다.

  · REGISTER 수신 → REGISTER_ACK(retain, 항상) → (ACTIVE 이고 cv 다르면) CONFIG_SET
  · Telemetry flush → cv_device != cv_server 인 ACTIVE 단말 → CONFIG_SET

왜 큐인가: 브로커 재시작 뒤 1만 대가 30초 안에 REGISTER 를 보낸다(docs/00 §5). 그때마다
바로 발행하면 초당 수백 건이 브로커로 몰린다. REGISTER_REPLY_RATE_PER_SEC 로 상한을 두고
넘치는 만큼 기다린다. 1만 대·200/s 면 50초 안에 다 나간다 — 단말 REGISTER 재전송(5분)
보다 훨씬 짧아 "즉시" 로 본다. 대기열이 REGISTER_REPLY_QUEUE_MAX 를 넘으면 가장 오래된
것을 버리고 센다 — REGISTER_ACK 를 못 받은 단말은 5분 뒤 REGISTER 를 다시 보내고,
CONFIG 를 못 받은 단말은 다음 Telemetry 때 cv 불일치로 다시 잡힌다.

FIFO 라 같은 단말의 REGISTER_ACK 가 CONFIG_SET 보다 먼저 나간다(사양서 §3.1 순서).

쿨다운(CONFIG_SET 만): 같은 단말에 60초 안에 두 번 보내지 않는다. REGISTER 직후 첫
Telemetry 가 아직 옛 cv 를 싣고 오는 것이 정상 흐름이라(단말이 CONFIG_ACK 전에 TM 을 보낼 수
있다), 쿨다운이 없으면 같은 CONFIG_SET 이 연달아 두 번 나간다. CONFIG_ACK `FLASH` 가 오면
쿨다운을 풀어 다음 송신 때 바로 다시 보낸다. 관리자 PATCH 는 큐를 타지 않고 즉시 발행하되
mark_sent() 로 쿨다운을 건다. REGISTER_ACK 는 쿨다운이 없다 — REGISTER 마다 반드시 답한다.

발행 성공 시 DB 에 남긴다: CONFIG_SET → config_sent_at + device_event(CONFIG_SET, payload),
REGISTER_ACK → register_ack_at + device_event(REGISTER_ACK). cv_server 는 여기서 바꾸지
않는다 — 큐에 넣는 쪽이 next_cv_server() 로 확정해 DB 에 쓴 값을 그대로 싣는다.

5차: COMMAND 개별 재시도(CommandRetryJob)도 같은 FIFO·토큰 버킷을 탄다(ADR-005). 재시도 역시
"단말이 보낸 직후" 에 나가는 발행이고, 브로커 재시작 뒤 1만 대 REGISTER 폭주에 명령 재시도가
겹쳐도 발행량 상한이 하나로 유지된다. REGISTER 직후면 FIFO 라 REGISTER_ACK 다음에 나간다.
시도 수·last_sent_at 은 큐에 넣기 **전에** DB 에서 선점(claim)해 두었으므로 여기서는 발행 +
이력만 쓴다. `ts` 는 발행 순간 시각으로 새로 찍는다 — 큐 대기 시간이 exp(30초)를 잡아먹지 않게.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.constants import DeviceState, EventKind
from app.core.metrics import metrics
from app.core.ratelimit import TokenBucket
from app.db import session_scope
from app.models.device import Device
from app.models.event import DeviceEvent
from app.mqtt import topics
from app.mqtt.publisher import MqttPublisher, kst_ts

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigJob:
    uuid: str
    #: 이미 next_cv_server() 를 거쳐 DB 에 쓴 값(1 이상).
    cv: int
    ti: int
    ka: int
    lat: float | None
    lon: float | None
    reason: str  # "register" | "telemetry" 로그용


@dataclass(frozen=True)
class RegisterAckJob:
    uuid: str
    state: str
    site: str | None
    reason: str | None
    #: RETIRED: retain 으로 보낸 뒤 빈 retain 으로 지운다(사양서 §3.3).
    clear_after: bool = False
    #: 5차 그룹 12자리. 배정돼 있으면 항상 싣는다(§3.10.9).
    grp: str | None = None


@dataclass(frozen=True)
class CommandRetryJob:
    """COMMAND 개별 재발송(같은 seq, 새 ts). payload 는 command.payload 원본."""

    seq: int
    uuid: str
    payload: dict[str, Any]
    #: 선점 뒤 시도 수(이력 기록용).
    attempt: int
    reason: str  # "register" | "telemetry" | "manual"


Job = ConfigJob | RegisterAckJob | CommandRetryJob


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
        self._queue: deque[Job] = deque()
        #: CONFIG 대기열에 들어 있는 uuid — 같은 단말이 두 번 줄 서지 않게.
        self._queued: set[str] = set()
        #: uuid → 마지막 CONFIG_SET 발행 시각(monotonic).
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

    def clear_cooldown(self, uuid: str) -> None:
        """CONFIG_ACK FLASH — 단말이 다시 보내 달라고 한 셈이다. 다음 송신 때 바로 나간다."""
        self._last_sent.pop(uuid, None)

    # ── 적재 ────────────────────────────────────────────────────────────
    def _push(self, job: Job) -> None:
        if len(self._queue) >= self._max:
            dropped = self._queue.popleft()
            if isinstance(dropped, ConfigJob):
                self._queued.discard(dropped.uuid)
            metrics.register_reply_dropped += 1
        self._queue.append(job)
        self._wake.set()

    def offer(self, job: ConfigJob) -> bool:
        """CONFIG_SET 을 대기열에 넣는다. 쿨다운 중이거나 이미 줄 서 있으면 False."""
        if job.uuid in self._queued or not self.cooled_down(job.uuid):
            return False
        self._push(job)
        self._queued.add(job.uuid)
        return True

    def offer_register_ack(self, job: RegisterAckJob) -> None:
        """REGISTER_ACK 는 쿨다운·중복 검사 없이 넣는다 — REGISTER 마다 반드시 답한다(§3.3)."""
        self._push(job)

    def offer_command_retry(self, job: CommandRetryJob) -> None:
        """COMMAND 재발송. 중복은 넣는 쪽의 DB 선점(attempts/last_sent_at)이 이미 막았다."""
        self._push(job)

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
        # 남은 대기분은 버린다 — 재기동 뒤 REGISTER 재전송/Telemetry 가 다시 잡는다.
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
            if isinstance(job, RegisterAckJob):
                await self._publish_register_ack(job)
                continue
            if isinstance(job, CommandRetryJob):
                await self._publish_command_retry(job)
                continue
            self._queued.discard(job.uuid)
            if not self.cooled_down(job.uuid):
                continue  # 줄 서는 사이 다른 경로가 보냈다
            await self._publish_config(job)

    async def _publish_config(self, job: ConfigJob) -> None:
        try:
            payload = await self._publisher.publish_config_set(
                uuid=job.uuid, cv=job.cv, ti=job.ti, ka=job.ka, lat=job.lat, lon=job.lon
            )
        except Exception:  # noqa: BLE001 - 브로커 끊김 등. 다음 Telemetry 가 다시 잡는다.
            log.exception("CONFIG_SET 발행 실패 %s (%s)", job.uuid, job.reason)
            return
        self._last_sent[job.uuid] = asyncio.get_running_loop().time()
        log.info("CONFIG_SET → %s cv=%d ti=%d ka=%d (%s)",
                 job.uuid, job.cv, job.ti, job.ka, job.reason)
        await record_config_sent(job.uuid, payload)

    async def _publish_register_ack(self, job: RegisterAckJob) -> None:
        try:
            payload = await self._publisher.publish_register_ack(
                uuid=job.uuid, state=job.state, site=job.site, reason=job.reason, grp=job.grp
            )
            if job.clear_after:
                await self._publisher.clear_register_ack(uuid=job.uuid)
        except Exception:  # noqa: BLE001 - 단말은 5분 뒤 REGISTER 를 다시 보낸다.
            log.exception("REGISTER_ACK 발행 실패 %s", job.uuid)
            return
        log.info("REGISTER_ACK → %s state=%s%s", job.uuid, job.state,
                 " (retain 삭제)" if job.clear_after else "")
        await record_register_ack(job.uuid, payload, cleared=job.clear_after)


    async def _publish_command_retry(self, job: CommandRetryJob) -> None:
        # 같은 seq, 새 ts. dict 의 키 순서는 원본 그대로 유지된다(ts 자리만 값이 바뀜).
        payload = {**job.payload, "ts": kst_ts(dt.datetime.now(dt.timezone.utc))}
        topic = topics.device_cmd(job.uuid)
        try:
            await self._publisher.publish_command(topic=topic, payload=payload)
        except Exception:  # noqa: BLE001 - 시도는 이미 셌다. 다음 단말 송신 때 또 잡힌다.
            log.exception("COMMAND 재시도 발행 실패 %s seq=%s", job.uuid, job.seq)
            return
        metrics.command_retry_sent += 1
        log.info("COMMAND 재시도 → %s seq=%d attempt=%d (%s)",
                 job.uuid, job.seq, job.attempt, job.reason)
        await record_command_sent(job.uuid, topic, payload, attempt=job.attempt, by=job.reason)


# ── DB 기록 (즉시 발행 경로와 공유) ──────────────────────────────────────
async def record_command_sent(
    uuid: str, topic: str, payload: dict[str, Any], *, attempt: int, by: str
) -> None:
    """device_event(COMMAND_SENT) — 개별 발송(첫 발송·재시도)만. 기록 실패는 발행에 영향 없음."""
    try:
        async with session_scope() as db:
            await db.execute(pg_insert(DeviceEvent).values(
                uuid=uuid, kind=EventKind.COMMAND_SENT.value,
                payload=command_sent_event(topic, payload, attempt=attempt, by=by),
                received_at=dt.datetime.now(dt.timezone.utc),
            ))
    except Exception:  # noqa: BLE001
        log.exception("COMMAND_SENT 기록 실패 %s", uuid)


def command_sent_event(
    topic: str, payload: dict[str, Any], *, attempt: int, by: str
) -> dict[str, Any]:
    """COMMAND_SENT 이력 payload 모양 — 보낸 그대로 + 어디로·몇 번째·누가(admin/register/…)."""
    return {"topic": topic, "payload": payload, "attempt": attempt, "by": by}
async def record_config_sent(uuid: str, payload: dict[str, Any]) -> None:
    """config_sent_at + device_event(CONFIG_SET). 기록 실패는 발행에 영향 없음."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        async with session_scope() as db:
            await db.execute(
                update(Device).where(Device.uuid == uuid).values(config_sent_at=now)
            )
            await db.execute(pg_insert(DeviceEvent).values(
                uuid=uuid, kind=EventKind.CONFIG_SET.value, payload=payload, received_at=now,
            ))
    except Exception:  # noqa: BLE001
        log.exception("CONFIG_SET 기록 실패 %s", uuid)


async def record_register_ack(uuid: str, payload: dict[str, Any], *, cleared: bool) -> None:
    """register_ack_at(지웠으면 NULL) + device_event(REGISTER_ACK)."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        async with session_scope() as db:
            await db.execute(
                update(Device).where(Device.uuid == uuid)
                .values(register_ack_at=None if cleared else now)
            )
            event = dict(payload)
            if cleared:
                event["retain_cleared"] = True
            await db.execute(pg_insert(DeviceEvent).values(
                uuid=uuid, kind=EventKind.REGISTER_ACK.value, payload=event, received_at=now,
            ))
    except Exception:  # noqa: BLE001
        log.exception("REGISTER_ACK 기록 실패 %s", uuid)


def register_ack_job_for(
    *, uuid: str, state: str, site: str | None, reason: str | None, grp: str | None = None
) -> RegisterAckJob:
    """DB 상태 → REGISTER_ACK 잡. RETIRED 는 발행 뒤 빈 retain 으로 지운다(사양서 §3.3, S-11).
    reason 은 REJECTED 일 때만 싣는다(다른 상태의 state_reason 은 관리자 메모다).
    grp 는 배정돼 있으면 상태와 무관하게 싣는다 — 단말은 ACTIVE 가 아니면 구독하지 않는다(§3.10.9).
    관리자 즉시 발행 경로(device 서비스)도 이 함수로 인자를 정해 모양이 한 곳에서 나온다."""
    retired = state == DeviceState.RETIRED.value
    return RegisterAckJob(
        uuid=uuid, state=state, site=site,
        reason=reason if state == DeviceState.REJECTED.value else None,
        clear_after=retired,
        grp=grp.strip() if grp and grp.strip() else None,
    )
