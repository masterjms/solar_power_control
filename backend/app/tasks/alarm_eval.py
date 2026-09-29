"""알람 재조정 — ALARM_EVAL_SEC(30초)마다 (ADR-009, 사양서 §16.6).

1. 지금 DB 상태로 "열려 있어야 할 알람" 을 만든다(desired).
   ACTIVE 단말 한 번 훑기(er·md·cv·온라인·운전 설정 sync·적용 스케줄 crc) + PENDING 단말 + 오늘 재부팅 횟수.
2. 열림·관찰 행과 맞춘다(core/alarm_rules.reconcile) → 열기/관찰/갱신/닫기/관찰 지우기를 한 트랜잭션에.

멱등이라 서버 재시작·놓친 메시지에도 다음 판에 맞춰진다. REJECTED·RETIRED·SUSPENDED 로 바뀐 단말의 알람은
desired 에 없으므로 닫힌다(SUSPENDED 는 통신 두절을 만들지 않는다 — 꺼 둔 단말).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.constants import DeviceState, EventKind
from app.core import alarm_rules as ar
from app.core import presence
from app.core.settings_rules import SYNC_LOCAL_SAVED
from app.db import session_scope
from app.models.alarm import Alarm
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.schedule import DeviceSchedule
from app.models.settings import DeviceSettings
from app.tasks.daily_rollup import KST

log = logging.getLogger(__name__)

#: IN 목록 한 번에 넣는 id 수.
_CHUNK = 5000


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def today_start(now: dt.datetime) -> dt.datetime:
    local = now.astimezone(KST)
    return dt.datetime.combine(local.date(), dt.time(0), tzinfo=KST)


async def desired(db: AsyncSession, now: dt.datetime) -> dict[tuple[str, str], ar.Want]:
    out: dict[tuple[str, str], ar.Want] = {}
    online = presence.online_clause(now).label("online_now")
    rows = await db.execute(
        select(
            Device.uuid, Device.last_telemetry, Device.last_seen_at, Device.offline_at, Device.cv_device,
            Device.cv_server, online, DeviceSettings.sync, DeviceSettings.ss_known,
            Device.ss_device, DeviceSettings.tbl_crc, DeviceSchedule.crc,
        )
        .join(DeviceSettings, DeviceSettings.uuid == Device.uuid, isouter=True)
        .join(DeviceSchedule, DeviceSchedule.uuid == Device.uuid, isouter=True)
        .where(Device.state == DeviceState.ACTIVE.value)
    )
    for (uuid, tm, last_seen, offline_at, cv_dev, cv_srv, is_online, sync, ss_known, ss_dev,
         tbl_crc, applied_crc) in rows:
        tm = tm or {}
        er = _int(tm.get("er"))
        for kind in ar.er_kinds(er):
            out[(uuid, kind)] = ar.Want({"er": er})
        if not is_online:
            out[(uuid, "OFFLINE")] = ar.Want(
                {"last_seen_at": last_seen.isoformat() if last_seen else None},
                since=offline_at or last_seen)
        if cv_srv and cv_srv > 0 and cv_dev is not None and cv_dev != cv_srv:
            out[(uuid, "CONFIG_MISMATCH")] = ar.Want(
                {"cv_device": cv_dev, "cv_server": cv_srv}, settings.alarm_config_hold_sec)
        if sync == SYNC_LOCAL_SAVED:
            out[(uuid, "LOCAL_SAVED")] = ar.Want({"ss_known": ss_known, "ss_telemetry": ss_dev})
        if applied_crc and tbl_crc and applied_crc.strip().upper() != tbl_crc.strip().upper():
            out[(uuid, "SCHEDULE_MISMATCH")] = ar.Want(
                {"applied_crc": applied_crc, "device_crc": tbl_crc})
        if _int(tm.get("md")) == 1:
            out[(uuid, "LOCAL_OPERATION")] = ar.Want({"md": 1}, settings.alarm_local_hold_sec)

    for uuid, created in await db.execute(
        select(Device.uuid, Device.created_at).where(Device.state == DeviceState.PENDING.value)
    ):
        out[(uuid, "PENDING")] = ar.Want(None, since=created)

    reboots = await db.execute(
        select(DeviceEvent.uuid, func.count())
        .join(Device, and_(Device.uuid == DeviceEvent.uuid,
                           Device.state == DeviceState.ACTIVE.value))
        .where(DeviceEvent.kind == EventKind.REBOOT.value,
               DeviceEvent.received_at >= today_start(now))
        .group_by(DeviceEvent.uuid)
        .having(func.count() >= settings.alarm_reboot_per_day)
    )
    for uuid, n in reboots:
        out[(uuid, "REBOOT_FREQUENT")] = ar.Want({"count_today": int(n)})
    return out


async def evaluate(db: AsyncSession, now: dt.datetime) -> ar.Plan:
    want = await desired(db, now)
    raw = (await db.execute(
        select(Alarm.id, Alarm.uuid, Alarm.kind, Alarm.first_seen_at, Alarm.opened_at, Alarm.value)
        .where(Alarm.closed_at.is_(None))
    )).all()
    old_value = {r.id: r.value for r in raw}
    existing = [ar.Row(id=r.id, uuid=r.uuid, kind=r.kind, first_seen_at=r.first_seen_at,
                       opened_at=r.opened_at) for r in raw]
    plan = ar.reconcile(want, existing, now)

    def rows(items: list[tuple[str, str, dict | None, dt.datetime | None]],
             opened: bool) -> list[dict[str, Any]]:
        return [{"uuid": u, "kind": k, "severity": ar.severity_of(k),
                 "first_seen_at": min(since, now) if since else now,
                 "opened_at": now if opened else None, "last_seen_at": now, "value": v}
                for u, k, v, since in items]

    if plan.insert_open or plan.insert_watch:
        await db.execute(insert(Alarm), rows(plan.insert_open, True) + rows(plan.insert_watch, False))
    if plan.promote:
        await db.execute(update(Alarm), [
            {"id": aid, "opened_at": now, "last_seen_at": now, "value": value}
            for aid, value in plan.promote
        ])
    # 계속 열린 것: 값이 같으면(대부분) last_seen 만 IN 한 문장, 바뀐 것만 값까지(PK 일괄).
    same = [aid for aid, value in plan.touch if old_value.get(aid) == value]
    changed = [(aid, value) for aid, value in plan.touch if old_value.get(aid) != value]
    for i in range(0, len(same), _CHUNK):
        await db.execute(update(Alarm).where(Alarm.id.in_(same[i:i + _CHUNK]))
                         .values(last_seen_at=now))
    if changed:
        await db.execute(update(Alarm), [
            {"id": aid, "last_seen_at": now, "value": value} for aid, value in changed
        ])
    for i in range(0, len(plan.close), _CHUNK):
        await db.execute(update(Alarm).where(Alarm.id.in_(plan.close[i:i + _CHUNK]))
                         .values(closed_at=now))
    for i in range(0, len(plan.drop_watch), _CHUNK):
        await db.execute(delete(Alarm).where(Alarm.id.in_(plan.drop_watch[i:i + _CHUNK])))
    return plan


async def run() -> None:
    """스케줄러 진입점. 실패는 로그만 — 다음 판에 다시 맞춰진다."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        async with session_scope() as db:
            plan = await evaluate(db, now)
    except Exception:  # noqa: BLE001
        log.exception("알람 재조정 실패 (%d초 뒤 다시)", settings.alarm_eval_sec)
        return
    opened = len(plan.insert_open) + len(plan.promote)
    if opened or plan.close:
        log.info("알람: 열림 %d · 닫힘 %d · 관찰 새로 %d · 관찰 취소 %d", opened, len(plan.close),
                 len(plan.insert_watch), len(plan.drop_watch))

