"""스케줄 배포 진행(S-25, ADR-010) — 배포 항목 하나 = 단말 하나를 읽기 → 쓰기 → 결과까지 끌고 간다.

메시지는 S-23 그대로다: SETTINGS_GET / SETTINGS_SET(25개 + tbl). 요청 수명(30초 무응답이면 새 seq 로 재발송,
3회면 TIMEOUT)은 settings_sync 가 한다. 여기는 "언제 무엇을 시작하나" 와 "끝났나" 만 본다.

  · 틱(DEPLOY_TICK_SEC=5)마다 열린 항목(waiting/reading/sent)을 판정(core/schedule_rules.next_step).
  · 단말이 뭔가 보낸 직후(REGISTER·Telemetry flush) 그 단말의 대기 항목을 바로 판정 — 오프라인이었던
    단말이 다시 붙으면 자동 배포(§13.1 [추천]).
  · 새로 시작하는 요청은 틱마다 DEPLOY_RATE_PER_SEC × TICK 까지(초당 10대, §13.1 [추천]).
  · 이 항목이 낸 요청은 command.created_by = "deploy#<job>" 로 알아본다(재발송은 새 seq 라 first_seq 이상 중
    가장 최근 행). 그 행이 끝났고(finished_at) RESENT 가 아니면 그 result 가 요청의 최종 결과다.
  · 쓰기 OK → device_schedule(적용된 판·crc) 기록. 알람 "스케줄 불일치" 의 기준이 된다.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import presence
from app.core import schedule_rules as sr
from app.core import settings_rules as rules
from app.db import session_scope
from app.errors import MqttUnavailable
from app.models.command import Command
from app.models.device import Device
from app.models.schedule import DeployItem, DeployJob, DeviceSchedule
from app.models.settings import DeviceSettings
from app.mqtt import settings_sync as ss
from app.mqtt.publisher import MqttPublisher
from app.mqtt.settings_sync import RESULT_RESENT, SettingsSync

log = logging.getLogger(__name__)

OPEN = ("waiting", "reading", "sent")
#: 한 판에 훑는 열린 항목 수 상한(나머지는 다음 틱).
_SCAN = 5000


def tag(job_id: int) -> str:
    """command.created_by / 이력 by 에 남는 이름."""
    return f"deploy#{job_id}"


def _values_from(row: DeviceSettings | None) -> dict[str, Any] | None:
    """쓰기 기준 25개 = 단말의 마지막 읽은 값. 읽어 보니 DB 와 달랐으면(device_changed) 단말 보고값."""
    if row is None:
        return None
    if row.sync == rules.SYNC_DEVICE_CHANGED:
        rep = rules.parse_values((row.last_report or {}).get("v"))
        if rep:
            return rep
    return ss.db_values(row)


class DeployRunner:
    REBUILD_EVERY = 12  # 틱 × 12 = 1분에 한 번은 DB 로 다시 맞춘다

    def __init__(self, publisher: MqttPublisher | None, settings_sync: SettingsSync | None) -> None:
        self._publisher = publisher
        self._ssync = settings_sync
        #: 열린 항목이 있는 단말 — 단말 송신 때 쿼리를 할지 정한다.
        self._open: set[str] = set()
        self._ticks = 0

    @property
    def open_count(self) -> int:
        return len(self._open)

    def mark(self, uuids: Iterable[str]) -> None:
        self._open.update(uuids)

    async def rebuild(self, db: AsyncSession) -> int:
        rows = await db.execute(
            select(DeployItem.uuid).join(DeployJob, DeployJob.id == DeployItem.job_id)
            .where(DeployItem.status.in_(OPEN), DeployJob.cancelled_at.is_(None)).distinct()
        )
        self._open = {r[0] for r in rows}
        return len(self._open)

    # ── 진입점 ─────────────────────────────────────────────────────────
    async def tick(self) -> None:
        self._ticks += 1
        if not self._open and self._ticks % self.REBUILD_EVERY:
            return
        try:
            await self.process(None)
            async with session_scope() as db:
                await self.rebuild(db)
        except Exception:  # noqa: BLE001
            log.exception("배포 진행 판정 실패 (%d초 뒤 다시)", settings.deploy_tick_sec)

    async def on_device_messages(self, uuids: Iterable[str], *, reason: str) -> int:
        """REGISTER·Telemetry flush 가 커밋 뒤 부른다. 예외를 올리지 않는다."""
        cand = [u for u in uuids if u in self._open]
        if not cand:
            return 0
        try:
            return await self.process(cand)
        except Exception:  # noqa: BLE001
            log.exception("배포 진행(단말 송신 %s) 실패 %d대", reason, len(cand))
            return 0

    # ── 판정 ─────────────────────────────────────────────────────────
    async def process(self, uuids: list[str] | None) -> int:
        now = dt.datetime.now(dt.timezone.utc)
        budget = max(1, settings.deploy_rate_per_sec * settings.deploy_tick_sec)
        actions: list[tuple[int, str, str, dict[str, Any], int, str]] = []
        touched_jobs: set[int] = set()
        async with session_scope() as db:
            online = presence.online_clause(now).label("online_now")
            stmt = (
                select(DeployItem, DeployJob.spec, DeployJob.crc, DeployJob.profile_id,
                       DeployJob.profile_version, Device.state, online, DeviceSettings)
                .join(DeployJob, DeployJob.id == DeployItem.job_id)
                .join(Device, Device.uuid == DeployItem.uuid, isouter=True)
                .join(DeviceSettings, DeviceSettings.uuid == DeployItem.uuid, isouter=True)
                .where(DeployItem.status.in_(OPEN), DeployJob.cancelled_at.is_(None))
                .order_by(DeployJob.id, DeployItem.uuid)
                .limit(_SCAN)
            )
            if uuids is not None:
                stmt = stmt.where(DeployItem.uuid.in_(uuids))
            rows = (await db.execute(stmt)).all()
            results = await self._request_results(db, [r[0] for r in rows])
            for item, spec, crc, profile_id, version, state, is_online, srow in rows:
                touched_jobs.add(item.job_id)
                step, status, detail = sr.next_step(
                    status=item.status, device_state=state, is_online=bool(is_online),
                    busy=srow is not None and srow.pending_seq is not None,
                    request_result=results.get((item.job_id, item.uuid)),
                    sync=srow.sync if srow else None, values=ss.db_values(srow) if srow else None,
                )
                if step == sr.STEP_FINISH:
                    item.status = status or "BAD"
                    item.detail = detail
                    item.acked_at = now
                    item.updated_at = now
                    if item.status == "OK":
                        await self._applied(db, item, spec, crc, profile_id, version, now)
                elif step in (sr.STEP_READ, sr.STEP_WRITE) and budget > 0:
                    budget -= 1
                    actions.append((item.job_id, item.uuid, step, spec, item.rounds, item.status))
        started = 0
        for job_id, uuid, step, spec, rounds, prev_status in actions:
            if await self._start(job_id, uuid, step, spec, rounds, prev_status):
                started += 1
        if touched_jobs:
            async with session_scope() as db:
                await close_finished_jobs(db, touched_jobs, now)
        return started

    async def _request_results(
        self, db: AsyncSession, items: list[DeployItem]
    ) -> dict[tuple[int, str], str]:
        """(job, uuid) → 이 항목 요청의 최종 결과. 아직 진행 중이면 빠진다."""
        want = [(i.job_id, i.uuid, i.first_seq) for i in items
                if i.status in ("reading", "sent") and i.first_seq is not None]
        if not want:
            return {}
        out: dict[tuple[int, str], str] = {}
        by_job: dict[int, list[tuple[str, int]]] = {}
        for job_id, uuid, first in want:
            by_job.setdefault(job_id, []).append((uuid, first))
        for job_id, pairs in by_job.items():
            uuids = [u for u, _ in pairs]
            first = {u: f for u, f in pairs}
            latest = (
                select(Command.target_id, func.max(Command.seq).label("seq"))
                .where(Command.created_by == tag(job_id), Command.target_id.in_(uuids),
                       Command.type.in_((rules.KIND_GET, rules.KIND_SET)))
                .group_by(Command.target_id).subquery()
            )
            rows = await db.execute(
                select(Command.target_id, Command.seq, Command.finished_at, Command.result)
                .join(latest, and_(latest.c.target_id == Command.target_id,
                                   latest.c.seq == Command.seq))
            )
            for uuid, seq, finished_at, result in rows:
                if seq < first.get(uuid, 0) or finished_at is None or result == RESULT_RESENT:
                    continue
                out[(job_id, uuid)] = str(result or "")
        return out

    async def _applied(
        self, db: AsyncSession, item: DeployItem, spec: dict[str, Any], crc: str,
        profile_id: int | None, version: int, now: dt.datetime,
    ) -> None:
        values = dict(spec.get("values") or {})
        await db.execute(
            pg_insert(DeviceSchedule).values(
                uuid=item.uuid, profile_id=profile_id, profile_version=version, crc=crc,
                values=values, job_id=item.job_id, applied_at=now,
            ).on_conflict_do_update(
                index_elements=[DeviceSchedule.uuid],
                set_={"profile_id": profile_id, "profile_version": version, "crc": crc,
                      "values": values, "job_id": item.job_id, "applied_at": now},
            )
        )

    async def _start(
        self, job_id: int, uuid: str, step: str, spec: dict[str, Any], rounds: int,
        prev_status: str,
    ) -> bool:
        """읽기 또는 쓰기 요청 하나 시작. 실패(발행 불가·규칙 위반)는 항목에 적는다."""
        if self._publisher is None:
            return False
        from app.modules.settings.service import send_request  # 순환 import 회피

        now = dt.datetime.now(dt.timezone.utc)
        async with session_scope() as db:
            row = await ss.get_or_create(db, uuid)
            if row.pending_seq is not None:
                return False  # 그 사이 다른 요청이 시작됐다 — 다음 틱
            item = await db.get(DeployItem, (job_id, uuid), with_for_update=True)
            if item is None or item.status != prev_status:
                return False
            if step == sr.STEP_READ:
                kind, body, new_status = rules.KIND_GET, None, "reading"
            else:
                try:
                    values = sr.overlay(_values_from(row) or {}, spec["values"])
                except rules.SettingsInvalid as e:
                    item.status = "RULE" if e.code == "SETTINGS_RULE" else "RANGE"
                    item.detail = f"서버 검사: {e.message}"
                    item.updated_at = now
                    return False
                t = spec["tbl"]
                table = rules.TableSpec(region=t["region"], lat_e6=t["lat_e6"],
                                        lon_e6=t["lon_e6"], on=t["on"], off=t["off"])
                kind, body, new_status = rules.KIND_SET, rules.set_body(values, table), "sent"
            try:
                payload, sent_at, _ = await send_request(
                    db, uuid, kind, body, tag(job_id), self._publisher, self._ssync, row)
            except MqttUnavailable:
                log.warning("배포 #%d %s — 브로커 끊김, 다음 틱에 다시", job_id, uuid)
                return False
        async with session_scope() as db:
            await db.execute(
                update(DeployItem)
                .where(DeployItem.job_id == job_id, DeployItem.uuid == uuid)
                .values(status=new_status, first_seq=payload["seq"], sent_at=sent_at,
                        rounds=rounds + (1 if kind == rules.KIND_SET else 0),
                        detail=None, updated_at=sent_at)
            )
        log.info("배포 #%d %s → %s seq=%s", job_id, uuid, kind, payload["seq"])
        return True


async def close_finished_jobs(db: AsyncSession, job_ids: Iterable[int], now: dt.datetime) -> None:
    """열린 항목이 없는 작업에 finished_at."""
    ids = list(job_ids)
    if not ids:
        return
    still = {r[0] for r in await db.execute(
        select(DeployItem.job_id).where(DeployItem.job_id.in_(ids),
                                        DeployItem.status.in_(OPEN)).distinct()
    )}
    done = [j for j in ids if j not in still]
    if done:
        await db.execute(update(DeployJob).where(DeployJob.id.in_(done),
                                                 DeployJob.finished_at.is_(None))
                         .values(finished_at=now))
