"""단말 서비스 — 조회 · CONFIG 변경 · PING · 계정 import/삭제 · 브로커 계정 내보내기."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import Select, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MsgType
from app.core import ids, mqtt_accounts, presence
from app.errors import DeviceNotFound
from app.models.command import Command
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.system import MqttAccountExport
from app.models.telemetry import Telemetry
from app.modules.device.schemas import (
    ConfigPatch,
    ConfigPatchOut,
    DeleteOut,
    DeviceDetailOut,
    DeviceOut,
    DevicePage,
    EventOut,
    ImportAccountsOut,
    PingOut,
    TelemetryOut,
)
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.publisher import MqttPublisher, config_set_payload

log = logging.getLogger(__name__)

#: 브로커 passwd/aclfile 내보내기를 직렬화하는 어드바이저리 락 키. 파일은 통째로
#: 덮어쓰는데 커밋 전 트랜잭션에서 만들기 때문에, 두 import 가 동시에 오면 뒤에 쓴 쪽이
#: 앞 요청의 변경을 못 보고 지운다. 락은 앞 요청이 커밋한 뒤에 풀린다.
_BROKER_EXPORT_LOCK_KEY = 0x696F746C_69676874  # "iotlight"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_out(device: Device, now: dt.datetime, *, detail: bool = False) -> DeviceOut:
    data: dict[str, Any] = {
        c.name: getattr(device, c.name) for c in Device.__table__.columns
    }
    data.pop("mqtt_password_hash", None)
    data["has_mqtt_account"] = device.mqtt_password_hash is not None
    data["is_online"] = presence.is_online(device, now)
    data["config_pending"] = (
        device.cv_device is None or device.cv_device != device.cv_server
    )
    if not detail:
        data.pop("last_telemetry", None)
        return DeviceOut(**data)
    return DeviceDetailOut(**data)


# ── 조회 ─────────────────────────────────────────────────────────────────
def _filtered(state: str | None, online: bool | None, now: dt.datetime) -> Select:
    stmt = select(Device)
    if state:
        stmt = stmt.where(Device.state == state.upper())
    if online is True:
        stmt = stmt.where(presence.online_clause(now))
    elif online is False:
        stmt = stmt.where(~presence.online_clause(now))
    return stmt


async def list_devices(
    db: AsyncSession, *, page: int, size: int, state: str | None, online: bool | None
) -> DevicePage:
    now = _now()
    base = _filtered(state, online, now)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await db.execute(
            base.order_by(Device.last_telemetry_at.desc().nulls_last(), Device.uuid)
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return DevicePage(
        items=[_to_out(d, now) for d in rows], total=int(total), page=page, size=size
    )


async def get_device(db: AsyncSession, uuid: str) -> DeviceDetailOut:
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    return _to_out(device, _now(), detail=True)  # type: ignore[return-value]


async def list_telemetry(
    db: AsyncSession, uuid: str, *, since: dt.datetime | None, until: dt.datetime | None,
    limit: int,
) -> list[TelemetryOut]:
    if await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    stmt = select(Telemetry).where(Telemetry.uuid == uuid)
    if since is not None:
        stmt = stmt.where(Telemetry.received_at >= since)
    if until is not None:
        stmt = stmt.where(Telemetry.received_at < until)
    rows = (
        await db.execute(stmt.order_by(Telemetry.received_at.desc()).limit(limit))
    ).scalars().all()
    return [
        TelemetryOut(
            received_at=r.received_at, ts_device=r.ts_device, sq=r.sq, fw=r.fw, ss=r.ss,
            cv=r.cv, er=r.er, on=r.on_, md=r.md, pw=[r.pw1, r.pw2, r.pw3], bv=r.bv, bi=r.bi,
            sc=r.sc, pp=r.pp, li=r.li, cs=r.cs,
        )
        for r in rows
    ]


async def list_events(
    db: AsyncSession, uuid: str, *, kind: str | None, limit: int
) -> list[EventOut]:
    stmt = select(DeviceEvent).where(DeviceEvent.uuid == uuid)
    if kind:
        stmt = stmt.where(DeviceEvent.kind == kind.upper())
    rows = (
        await db.execute(stmt.order_by(DeviceEvent.received_at.desc(), DeviceEvent.id.desc())
                         .limit(limit))
    ).scalars().all()
    return [EventOut.model_validate(r) for r in rows]


# ── CONFIG ───────────────────────────────────────────────────────────────
async def patch_config(
    db: AsyncSession, uuid: str, patch: ConfigPatch, publisher: MqttPublisher,
    config_sync: ConfigSyncQueue,
) -> ConfigPatchOut:
    """ti/lat/lon 변경 → cv_server += 1 → 즉시 CONFIG_SET 1회 (ADR-002, 사양서 §16.5).

    site 만 바꾸면 cv 를 올리지 않는다 — 단말에 안 내려가는 값이다.
    발행 실패(브로커 끊김)여도 DB 는 커밋한다: 다음 Telemetry 의 cv 불일치가 재전송을 건다.
    """
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})

    changed = False
    if patch.ti is not None and patch.ti != device.ti_server:
        device.ti_server = patch.ti
        changed = True
    if patch.lat is not None and patch.lat != device.lat:
        device.lat = patch.lat
        changed = True
    if patch.lon is not None and patch.lon != device.lon:
        device.lon = patch.lon
        changed = True
    if patch.site is not None:
        device.site = patch.site

    if changed:
        device.cv_server = (device.cv_server + 1) % 65536
    await db.flush()

    payload = config_set_payload(
        cv=device.cv_server, ti=device.ti_server, lat=device.lat, lon=device.lon
    )
    published = False
    if changed or device.cv_device != device.cv_server:
        try:
            await publisher.publish_config_set(
                uuid=uuid, cv=device.cv_server, ti=device.ti_server, lat=device.lat, lon=device.lon
            )
            published = True
            device.config_sent_at = _now()
            config_sync.mark_sent(uuid)
        except Exception:  # noqa: BLE001
            log.exception("CONFIG_SET 즉시 발행 실패 %s — 다음 Telemetry 때 재전송", uuid)

    return ConfigPatchOut(
        uuid=uuid, cv_server=device.cv_server, ti_server=device.ti_server, lat=device.lat,
        lon=device.lon, site=device.site, published=published, payload=payload,
    )


# ── PING ─────────────────────────────────────────────────────────────────
async def ping(db: AsyncSession, uuid: str, publisher: MqttPublisher) -> PingOut:
    """seq 발번 → command 행 → 발행. 발행이 실패하면 예외가 올라가고 트랜잭션은 롤백된다
    (미들웨어). 시퀀스는 롤백돼도 되감기지 않으므로 seq 에 구멍이 생기는데, 그게 맞다 —
    단말이 봤을지도 모르는 번호를 다시 쓰지 않는다."""
    if await db.get(Device, uuid) is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    seq = await ids.next_cmd_seq(db)
    payload = {"type": MsgType.PING.value, "seq": seq}
    db.add(Command(seq=seq, target_kind="device", target_id=uuid, type=MsgType.PING.value,
                   payload=payload, expected_count=1, sent_at=_now()))
    await db.flush()
    await publisher.publish_ping(uuid=uuid, seq=seq)
    return PingOut(uuid=uuid, seq=seq)


# ── 계정 ─────────────────────────────────────────────────────────────────
async def export_broker_accounts(
    db: AsyncSession, *, wait_applied: bool = False
) -> tuple[mqtt_accounts.ExportResult, bool]:
    """DB 의 계정 해시를 mosquitto passwd + aclfile 로 내보내고 mqtt_account_export 에 남긴다.

    import/삭제/기동 때 호출. 실패해도 예외를 던지지 않는다 — 정본은 DB 이고 다음 호출이
    따라잡는다. 반환: (내보내기 결과, ACL 적용 확인 여부).
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BROKER_EXPORT_LOCK_KEY})
    rows = (
        await db.execute(
            select(Device.uuid, Device.mqtt_password_hash)
            .where(Device.mqtt_password_hash.is_not(None))
        )
    ).all()
    result = mqtt_accounts.export_all(mqtt_accounts.uuids_of(rows))
    if not result.enabled:
        return result, False

    now = _now()
    values: dict[str, Any] = {"exported_at": now}
    if result.passwd_ok:
        values["passwd_md5"] = result.passwd_md5
    if result.acl_ok:
        values["acl_md5"] = result.acl_md5
    await db.execute(update(MqttAccountExport).where(MqttAccountExport.id == 1).values(**values))

    applied = False
    if result.acl_ok and wait_applied:
        applied = await mqtt_accounts.wait_acl_applied(result.acl_md5)
    applied_md5 = mqtt_accounts.read_applied_md5()
    if applied_md5:
        await db.execute(
            update(MqttAccountExport)
            .where(MqttAccountExport.id == 1)
            .values(acl_applied_md5=applied_md5, applied_at=now if applied else None)
        )
    return result, applied


async def import_accounts(db: AsyncSession, csv_text: str) -> ImportAccountsOut:
    """CSV `uuid,password` → 해시 → device 행(없으면 생성) → 내보내기 → ACL 적용 대기."""
    parsed = mqtt_accounts.parse_accounts_csv(csv_text)
    created = updated = 0
    for uuid, password in parsed.accounts.items():
        stmt = pg_insert(Device).values(
            uuid=uuid, mqtt_password_hash=mqtt_accounts.mosquitto_hash(password)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Device.uuid],
            set_={"mqtt_password_hash": stmt.excluded.mqtt_password_hash, "updated_at": func.now()},
        ).returning(Device.created_at == Device.updated_at)
        # 방금 INSERT 된 행은 created_at == updated_at(둘 다 now()) 이고, UPDATE 된 행은
        # updated_at 만 now() 라 다르다 — 별도 SELECT 없이 생성/갱신을 구분한다.
        is_new = await db.scalar(stmt)
        if is_new:
            created += 1
        else:
            updated += 1
    await db.flush()

    result, applied = await export_broker_accounts(db, wait_applied=True)
    log.info("계정 import: %d건 (신규 %d, 갱신 %d, 오류 %d), ACL 적용=%s",
             len(parsed.accounts), created, updated, len(parsed.errors), applied)
    return ImportAccountsOut(
        imported=len(parsed.accounts), created=created, updated=updated, errors=parsed.errors,
        export_enabled=result.enabled, passwd_md5=result.passwd_md5, acl_md5=result.acl_md5,
        acl_applied=applied,
    )


async def delete_device(db: AsyncSession, uuid: str) -> DeleteOut:
    """단말 행 삭제 + 계정 제거 + 재내보내기. 이력(telemetry/device_event)은 남긴다."""
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    await db.execute(delete(Device).where(Device.uuid == uuid))
    await db.flush()
    result, applied = await export_broker_accounts(db, wait_applied=True)
    return DeleteOut(uuid=uuid, deleted=True, export_enabled=result.enabled, acl_applied=applied)
