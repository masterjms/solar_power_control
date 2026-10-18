"""OTA 진행(문제점 56번, 사양서 §24.4·§24.5) — 항목 하나 = 단말 하나를 OTA_START → START → 결과까지 끌고 간다.

  · 틱(5초)마다
      - 보냄(sending) 30초 동안 START 가 없으면 같은 job_id 로 다시(3회까지), 그래도 없으면 NO_RESPONSE.
      - 진행(running) START 뒤 1시간 30분 결과가 없으면 NO_RESPONSE(받기 최대 15분 + 롤백 판정 최대 1시간).
      - 빈자리(서버 설정 "동시 진행 대수" − 보냄·진행 수)만큼 대기(waiting) 단말을 시작한다.
        진행 시간대(서버 설정, 기본 10~16시) 밖이면 새로 시작하지 않는다(이미 시작한 것은 끝까지).
        오프라인 단말은 대기로 남아 다시 접속하면 순서대로 시작한다. 운영(ACTIVE)이 아닌 단말은 SKIPPED.
  · 한 대의 끝 = DONE·ROLLBACK·오류 OTA_ACK(재접속만으로 끝이 아니다 — 새 펌웨어는 접속 뒤 몇 초 안에 DONE).
  · OTA_ACK 는 handlers 가 handle_ack() 를 부른다(같은 트랜잭션).

주소: OTA_BASE_URL + /ota/<항목 id>/<uuid>/<token>. token 은 항목마다 무작위 22자, 처음 보낸 때부터 2시간
(modules/ota/download.py 가 확인하고 Range 로 내준다).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import DeviceState, EventKind
from app.core import ota_rules as orl
from app.core import presence
from app.core.server_settings import ota_window_open
from app.core.server_settings import runtime as runtime_settings
from app.db import session_scope
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.ota import OtaItem, OtaJob
from app.mqtt.publisher import MqttPublisher

log = logging.getLogger(__name__)

TICK_SEC = 5
ACTIVE = ("sending", "running")


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


class OtaRunner:
    REBUILD_EVERY = 12  # 열린 항목이 없어도 1분에 한 번은 DB 를 본다(다른 경로로 생긴 항목)

    def __init__(self, publisher: MqttPublisher | None) -> None:
        self._publisher = publisher
        self._has_open = True
        self._ticks = 0

    def wake(self) -> None:
        """새 작업을 만든 직후 — 다음 틱에 바로 본다."""
        self._has_open = True

    async def tick(self) -> None:
        self._ticks += 1
        if not self._has_open and self._ticks % self.REBUILD_EVERY:
            return
        try:
            await self.process(dt.datetime.now(dt.timezone.utc))
        except Exception:  # noqa: BLE001
            log.exception("OTA 진행 판정 실패 (%d초 뒤 다시)", TICK_SEC)

    async def process(self, now: dt.datetime) -> dict[str, int]:
        resend: list[int] = []
        start: list[int] = []
        touched: set[int] = set()
        stats = {"resend": 0, "start": 0, "timeout": 0, "skipped": 0}
        async with session_scope() as db:
            rows = (await db.execute(
                select(OtaItem).where(OtaItem.status.in_(orl.OPEN)).order_by(OtaItem.job_id, OtaItem.id)
            )).scalars().all()
            self._has_open = bool(rows)
            active = 0
            busy_uuids: set[str] = set()
            for it in rows:
                if it.status not in ACTIVE:
                    continue
                step = orl.timeout_step(it.status, it.attempts, it.sent_at, it.started_at, now)
                if step == "resend":
                    resend.append(it.id)
                elif step == orl.NO_RESPONSE:
                    it.status = orl.NO_RESPONSE
                    it.detail = ("START 응답 없음(3회 보냄)" if it.started_at is None
                                 else "START 뒤 1시간 30분 결과 없음 — 현장 확인")
                    it.finished_at = now
                    it.updated_at = now
                    touched.add(it.job_id)
                    stats["timeout"] += 1
                    log.warning("OTA #%d %s 응답 없음 (%s)", it.id, it.uuid, it.detail)
                    continue
                active += 1
                busy_uuids.add(it.uuid)

            slots = runtime_settings.get("ota_parallel") - active
            waiting = [it for it in rows if it.status == "waiting"]
            if waiting and slots > 0 and ota_window_open(
                    now, runtime_settings.get("ota_hour_start"), runtime_settings.get("ota_hour_end")):
                online = presence.online_clause(now).label("online_now")
                devs = {r.uuid: r for r in await db.execute(
                    select(Device.uuid, Device.state, online)
                    .where(Device.uuid.in_({it.uuid for it in waiting})))}
                for it in waiting:
                    if slots <= 0:
                        break
                    d = devs.get(it.uuid)
                    if d is None or d.state != DeviceState.ACTIVE.value:
                        it.status = "SKIPPED"
                        it.detail = "운영(승인) 단말이 아님 — 보내지 않음"
                        it.finished_at = now
                        it.updated_at = now
                        touched.add(it.job_id)
                        stats["skipped"] += 1
                        continue
                    if not d.online_now or it.uuid in busy_uuids:
                        continue  # 오프라인 — 다시 접속하면 다음 틱에
                    busy_uuids.add(it.uuid)
                    start.append(it.id)
                    slots -= 1

        for item_id in resend:
            if await self._send(item_id, now, first=False):
                stats["resend"] += 1
        for item_id in start:
            if await self._send(item_id, now, first=True):
                stats["start"] += 1
        if touched:
            async with session_scope() as db:
                await close_finished_jobs(db, touched, now)
        return stats

    async def _send(self, item_id: int, now: dt.datetime, *, first: bool) -> bool:
        """OTA_START 하나(처음 또는 다시). 발행 실패(브로커 끊김)는 그대로 두고 다음 틱에."""
        if self._publisher is None:
            return False
        async with session_scope() as db:
            it = await db.get(OtaItem, item_id, with_for_update=True)
            if it is None or it.status != ("waiting" if first else "sending"):
                return False
            job = await db.get(OtaJob, it.job_id)
            if job is None:
                return False
            if it.token is None:
                it.token = orl.new_token()
                it.first_sent_at = now
            try:
                url = orl.url_for(settings.ota_base_url, it.id, it.uuid, it.token)
            except ValueError as e:
                it.status, it.detail, it.finished_at, it.updated_at = "BAD", f"서버 검사: {e}", now, now
                await close_finished_jobs(db, [it.job_id], now)
                log.error("OTA #%d 주소 오류: %s", it.id, e)
                return False
            payload = orl.start_payload(job_id=it.id, url=url, size=job.size, sha256=job.sha256)
            try:
                await self._publisher.publish_ota_start(uuid=it.uuid, payload=payload)
            except Exception as e:  # noqa: BLE001 — 브로커 끊김 등. 다음 틱에 다시.
                log.warning("OTA #%d %s 보내기 실패(%s) — 다음 틱에 다시", it.id, it.uuid, e)
                return False
            if first:
                it.fw_before = await db.scalar(select(Device.fw).where(Device.uuid == it.uuid))
            it.status = "sending"
            it.attempts += 1
            it.sent_at = now
            it.updated_at = now
            # 주소의 token 은 이력에 남기지 않는다(이력은 화면에서 누구나 본다).
            shown = {**payload, "url": url.rsplit("/", 1)[0] + "/…"}
            await db.execute(pg_insert(DeviceEvent).values(
                uuid=it.uuid, kind=EventKind.OTA_START.value, received_at=now, dedup_key=None,
                payload={**shown, "attempt": it.attempts, "by": f"ota#{it.job_id}"}))
        log.info("OTA #%d %s → OTA_START (%d회째)", item_id, it.uuid, it.attempts)
        return True


async def handle_ack(db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime) -> str | None:
    """OTA_ACK(result topic). 바뀐 상태를 돌려준다(무시면 None). 이력은 호출부가 남긴다."""
    item_id = _int(data.get("job_id"))
    result = str(data.get("result") or "").upper()
    if item_id is None:
        log.warning("OTA_ACK job_id 없음 (%s) %s", uuid, data)
        return None
    it = await db.get(OtaItem, item_id, with_for_update=True)
    if it is None or it.uuid != uuid:
        log.warning("OTA_ACK 모르는 job_id=%s (%s) result=%s", item_id, uuid, result)
        return None
    new = orl.on_ack(it.status, result)
    if new is None:
        log.info("OTA_ACK #%d %s %s — 지금 상태 %s 라 무시", item_id, uuid, result, it.status)
        return None
    it.status = new
    it.updated_at = now
    if new == "running":
        it.started_at = now
        it.detail = None
        it.finished_at = None
    else:
        it.finished_at = now
        it.detail = orl.RESULT_TEXT.get(new)
        it.fw_after = await db.scalar(select(Device.fw).where(Device.uuid == uuid))
        await close_finished_jobs(db, [it.job_id], now)
    log.info("OTA_ACK #%d %s %s → %s", item_id, uuid, result, new)
    return new


async def close_finished_jobs(db: AsyncSession, job_ids: Iterable[int], now: dt.datetime) -> None:
    """열린 항목이 없는 작업에 finished_at. 늦은 결과로 다시 열리지는 않는다(항목만 바뀐다)."""
    ids = list(set(job_ids))
    if not ids:
        return
    await db.flush()
    still = {r[0] for r in await db.execute(
        select(OtaItem.job_id).where(OtaItem.job_id.in_(ids), OtaItem.status.in_(orl.OPEN)).distinct())}
    done = [j for j in ids if j not in still]
    if done:
        await db.execute(update(OtaJob).where(OtaJob.id.in_(done), OtaJob.finished_at.is_(None))
                         .values(finished_at=now))


async def running_uuids(db: AsyncSession) -> set[str]:
    """OTA 를 받는 중인 단말(START 받음) — 알람이 통신 두절로 올리지 않는다(§24.4)."""
    return {r[0] for r in await db.execute(select(OtaItem.uuid).where(OtaItem.status == "running"))}


async def counts(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(select(OtaItem.status, func.count()).where(OtaItem.status.in_(orl.OPEN))
                            .group_by(OtaItem.status))
    out = {s: 0 for s in orl.OPEN}
    out.update({s: int(n) for s, n in rows})
    return out
