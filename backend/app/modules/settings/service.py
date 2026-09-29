"""단말 설정 서비스 — 조회 · 읽기 · 쓰기 · 받아들이기 · 되돌리기 · 이력 · 표 미리보기 (ADR-007).

읽기/쓰기 발송 순서(POST read, PUT, revert):
  1. 승인 상태 확인(읽기 PENDING·ACTIVE, 쓰기 ACTIVE) → 409 INVALID_STATE
  2. 대기 중인 요청이 있으면 409 SETTINGS_PENDING. 쓰기는 한 번도 안 읽었으면 409 SETTINGS_NOT_READ
     (force=true 로만 — 현장 설정을 덮어쓴다, 명세 8.5)
  3. settings_sync.begin_request: seq·command 행·pending(쓰기면 sync=writing)
  4. **커밋한 뒤 발행**(시뮬레이터는 수 ms 안에 답한다). 발행 실패 → pending 해제·원래 sync·command
     FAILED 로 되돌리고 503 MQTT_UNAVAILABLE.
  5. device_event(SETTINGS_SENT, by=관리자), 재발송 후보 집합에 넣는다.
결과는 비동기다 — 화면은 GET 으로 sync·last_result 를 본다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DeviceState, EventKind
from app.core import settings_rules as rules
from app.core.auth import Principal
from app.core.metrics import metrics
from app.errors import (
    DeviceNotFound,
    InvalidState,
    MqttUnavailable,
    SettingsIncomplete,
    SettingsNotChanged,
    SettingsNotRead,
    SettingsPending,
    SettingsRange,
    SettingsRule,
    ValidationFailed,
)
from app.models.command import Command
from app.models.device import Device
from app.models.event import DeviceEvent
from app.models.settings import DeviceSettings, DeviceSettingsHistory
from app.modules.settings.schemas import (
    DevOut,
    DiffRow,
    HistoryOut,
    PendingOut,
    PreviewRow,
    SchedulePreviewOut,
    SentOut,
    SettingsOut,
    SettingsPut,
    TblOut,
    WriteOut,
)
from app.mqtt import settings_sync as ss
from app.mqtt import topics
from app.mqtt.publisher import MqttPublisher
from app.mqtt.settings_sync import SettingsSync

log = logging.getLogger(__name__)

READ_STATES = frozenset({DeviceState.PENDING.value, DeviceState.ACTIVE.value})
WRITE_STATES = frozenset({DeviceState.ACTIVE.value})

_ERRORS = {
    "SETTINGS_INCOMPLETE": SettingsIncomplete,
    "SETTINGS_RANGE": SettingsRange,
    "SETTINGS_RULE": SettingsRule,
    "VALIDATION_FAILED": ValidationFailed,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _raise(e: rules.SettingsInvalid) -> None:
    raise _ERRORS.get(e.code, ValidationFailed)(e.message, detail=e.detail) from e


async def _device(db: AsyncSession, uuid: str) -> Device:
    device = await db.get(Device, uuid)
    if device is None:
        raise DeviceNotFound(detail={"uuid": uuid})
    return device


# ── 조회 ─────────────────────────────────────────────────────────────────
def to_out(uuid: str, row: DeviceSettings | None, ss_telemetry: int | None) -> SettingsOut:
    if row is None:
        return SettingsOut(uuid=uuid, sync=rules.SYNC_UNKNOWN, ss_telemetry=ss_telemetry)
    raw = ss.db_values(row)
    values = {k: int(v) for k, v in raw.items() if v is not None} \
        if rules.values_complete(raw) else None
    tbl = None
    t = ss.db_table(row)
    if t is not None:
        expected = None
        if None not in (t["lat_e6"], t["lon_e6"], t["on"], t["off"]):
            expected = rules.table_crc(t["lat_e6"], t["lon_e6"], t["on"], t["off"])
        tbl = TblOut(**t, ss=row.ss_known, crc_expected=expected,
                     matches=(t["crc"] == expected) if t["crc"] and expected else None)
    report = None
    if row.sync == rules.SYNC_DEVICE_CHANGED and row.last_report:
        report = rules.parse_values(row.last_report.get("v"))
    pending = None
    if row.pending_seq is not None:
        pending = PendingOut(kind=row.pending_kind or "", seq=row.pending_seq,
                             sent_at=row.pending_sent_at, attempts=row.pending_attempts)
    dev = DevOut(dip=row.dip, bat=row.bat) if row.dip is not None or row.bat is not None \
        else None
    return SettingsOut(
        uuid=uuid, sync=row.sync, read_at=row.read_at, values=values,
        sh_db=rules.fingerprint(values) if values else None, sh_device=row.sh_device,
        tbl=tbl, dev=dev, ss_known=row.ss_known, ss_telemetry=ss_telemetry, report=report,
        diff=[DiffRow(**d) for d in rules.diff(values, report)], pending=pending,
        last_result=row.last_result, last_result_at=row.last_result_at,
    )


async def get_settings(db: AsyncSession, uuid: str) -> SettingsOut:
    device = await _device(db, uuid)
    row = await db.get(DeviceSettings, uuid)
    return to_out(uuid, row, device.ss_device)


# ── 발송 공통 ────────────────────────────────────────────────────────────
async def send_request(
    db: AsyncSession, uuid: str, kind: str, body: dict[str, Any] | None, by: str,
    publisher: MqttPublisher, ssync: SettingsSync | None, row: DeviceSettings,
) -> tuple[dict[str, Any], dt.datetime, int]:
    """요청 하나 시작 — pending 기록·커밋 → 발행. 관리자 API 와 스케줄 배포(ADR-010, by="deploy#n")가 같이 쓴다.
    발행이 실패하면 요청을 되돌리고 MqttUnavailable."""
    if not publisher.connection.is_connected:
        raise MqttUnavailable()
    now = _now()
    payload = await ss.begin_request(db, row, kind, body, by, now)
    await db.commit()
    try:
        if kind == rules.KIND_SET:
            size = await publisher.publish_settings_set(uuid=uuid, payload=payload)
        else:
            size = await publisher.publish_settings_get(uuid=uuid, payload=payload)
    except Exception as e:
        log.exception("%s %s seq=%s 발행 실패 — 요청 되돌림", kind, uuid, payload["seq"])
        row = await ss.get_or_create(db, uuid)
        if row.pending_seq == payload["seq"]:
            ss.cancel_request(row)
        await db.execute(update(Command).where(Command.seq == payload["seq"])
                         .values(finished_at=now, result="FAILED"))
        await db.commit()
        if isinstance(e, MqttUnavailable):
            raise
        raise MqttUnavailable(detail={"seq": payload["seq"]}) from e
    await db.execute(pg_insert(DeviceEvent).values(
        uuid=uuid, kind=EventKind.SETTINGS_SENT.value, received_at=now,
        payload={"topic": topics.device_cmd(uuid), "payload": payload, "attempt": 1,
                 "by": by},
    ))
    metrics.settings_sent += 1
    if ssync is not None:
        ssync.mark(uuid)
    log.info("%s → %s seq=%d %dB (by %s)", kind, uuid, payload["seq"], size, by)
    return payload, now, size


async def _locked_row(db: AsyncSession, uuid: str) -> DeviceSettings:
    row = await ss.get_or_create(db, uuid)
    if row.pending_seq is not None:
        raise SettingsPending(detail={"kind": row.pending_kind, "seq": row.pending_seq,
                                      "sent_at": row.pending_sent_at.isoformat()
                                      if row.pending_sent_at else None})
    return row


# ── 읽기 ─────────────────────────────────────────────────────────────────
async def read(
    db: AsyncSession, uuid: str, me: Principal, publisher: MqttPublisher,
    ssync: SettingsSync | None,
) -> SentOut:
    device = await _device(db, uuid)
    if device.state not in READ_STATES:
        raise InvalidState(detail={"state": device.state, "allowed": sorted(READ_STATES)})
    row = await _locked_row(db, uuid)
    payload, now, _ = await send_request(db, uuid, rules.KIND_GET, None, me.user, publisher, ssync, row)
    return SentOut(seq=payload["seq"], sent_at=now)


# ── 쓰기 ─────────────────────────────────────────────────────────────────
def _validate_put(body: SettingsPut) -> tuple[dict[str, int], rules.TableSpec | None]:
    try:
        values = rules.validate_values(body.values or {})
        table = None
        if body.tbl is not None:
            t = body.tbl
            table = rules.validate_table(t.region, t.lat, t.lon, t.on, t.off)
    except rules.SettingsInvalid as e:
        _raise(e)
    return values, table


async def _write(
    db: AsyncSession, uuid: str, values: dict[str, int], table: rules.TableSpec | None,
    me: Principal, publisher: MqttPublisher, ssync: SettingsSync | None, row: DeviceSettings,
) -> WriteOut:
    body = rules.set_body(values, table)
    payload, now, size = await send_request(db, uuid, rules.KIND_SET, body, me.user, publisher, ssync, row)
    return WriteOut(seq=payload["seq"], sent_at=now, sh_expected=rules.fingerprint(values),
                    payload_bytes=size)


async def write(
    db: AsyncSession, uuid: str, body: SettingsPut, force: bool, me: Principal,
    publisher: MqttPublisher, ssync: SettingsSync | None,
) -> WriteOut:
    device = await _device(db, uuid)
    if device.state not in WRITE_STATES:
        raise InvalidState(detail={"state": device.state, "allowed": sorted(WRITE_STATES)})
    values, table = _validate_put(body)
    row = await _locked_row(db, uuid)
    if row.sync == rules.SYNC_UNKNOWN and not force:
        raise SettingsNotRead(detail={"uuid": uuid, "hint": "먼저 단말에서 읽거나 force=true"})
    return await _write(db, uuid, values, table, me, publisher, ssync, row)


# ── 받아들이기 · 되돌리기 ────────────────────────────────────────────────
async def accept(db: AsyncSession, uuid: str, me: Principal) -> SettingsOut:
    """device_changed → DB ← 단말 보고값(last_report). 단말에는 아무것도 보내지 않는다."""
    device = await _device(db, uuid)
    row = await _locked_row(db, uuid)
    report = row.last_report or {}
    values = rules.parse_values(report.get("v"))
    if row.sync != rules.SYNC_DEVICE_CHANGED or values is None:
        raise SettingsNotChanged(detail={"sync": row.sync,
                                         "has_report": values is not None})
    now = _now()
    old = ss.db_values(row)
    history = [
        {"uuid": uuid, "changed_at": now, "by": me.user, "key": k, "old": old.get(k),
         "new": values[k], "note": None}
        for k in rules.item_keys() if old.get(k) != values[k]
    ]
    for k in rules.item_keys():
        setattr(row, k, values[k])
    tbl = report.get("tbl") if isinstance(report.get("tbl"), dict) else None
    if tbl is not None:
        before = ss.db_table(row)
        if before is None or any(before.get(k) != tbl.get(k) for k in rules.TBL_KEYS):
            history.append({"uuid": uuid, "changed_at": now, "by": me.user, "key": "tbl",
                            "old": None, "new": None,
                            "note": ss.tbl_note(tbl)})
        row.tbl_region = tbl.get("region")
        row.tbl_lat_e6 = tbl.get("lat_e6")
        row.tbl_lon_e6 = tbl.get("lon_e6")
        row.tbl_on = tbl.get("on")
        row.tbl_off = tbl.get("off")
        row.tbl_src = tbl.get("src") if isinstance(tbl.get("src"), int) else None
        row.tbl_crc = str(tbl.get("crc")).upper() if tbl.get("crc") is not None else None
    row.sh_device = str(report.get("sh") or rules.fingerprint(values)).upper()
    row.sync = rules.SYNC_SYNCED
    if history:
        await ss.write_history(db, history)
    await db.flush()
    log.info("설정 받아들이기 %s — %d개 항목 (by %s)", uuid, len(history), me.user)
    return to_out(uuid, row, device.ss_device)


async def revert(
    db: AsyncSession, uuid: str, me: Principal, publisher: MqttPublisher,
    ssync: SettingsSync | None,
) -> WriteOut:
    """device_changed / local_saved → DB 값으로 SETTINGS_SET(PUT 과 같은 경로). 표 조건이 DB 에
    있으면 같이 보낸다(단말이 같은 조건으로 다시 계산해 CRC 가 맞을 때만 저장)."""
    device = await _device(db, uuid)
    if device.state not in WRITE_STATES:
        raise InvalidState(detail={"state": device.state, "allowed": sorted(WRITE_STATES)})
    row = await _locked_row(db, uuid)
    raw = ss.db_values(row)
    if row.sync not in (rules.SYNC_DEVICE_CHANGED, rules.SYNC_LOCAL_SAVED) \
            or not rules.values_complete(raw):
        raise SettingsNotChanged(detail={"sync": row.sync})
    values = {k: int(v) for k, v in raw.items()}
    table = None
    t = ss.db_table(row)
    if t is not None and t["on"] is not None and t["off"] is not None \
            and rules.region_problem(t["region"]) is None:
        table = rules.TableSpec(region=t["region"], lat_e6=t["lat_e6"], lon_e6=t["lon_e6"],
                                on=t["on"], off=t["off"])
    return await _write(db, uuid, values, table, me, publisher, ssync, row)


# ── 이력 · 스키마 · 미리보기 ─────────────────────────────────────────────
async def history(db: AsyncSession, uuid: str, limit: int) -> list[HistoryOut]:
    await _device(db, uuid)
    rows = (await db.execute(
        select(DeviceSettingsHistory).where(DeviceSettingsHistory.uuid == uuid)
        .order_by(DeviceSettingsHistory.changed_at.desc(), DeviceSettingsHistory.id.desc())
        .limit(limit)
    )).scalars().all()
    return [HistoryOut(changed_at=r.changed_at, by=r.by, key=r.key, old=r.old, new=r.new,
                       note=r.note) for r in rows]


def schedule_preview(lat: float, lon: float, on: int, off: int) -> SchedulePreviewOut:
    try:
        spec = rules.validate_table("preview", lat, lon, on, off)
    except rules.SettingsInvalid as e:
        _raise(e)
    return SchedulePreviewOut(
        crc=spec.crc, lat_e6=spec.lat_e6, lon_e6=spec.lon_e6, on=spec.on, off=spec.off,
        rows=[PreviewRow(**r) for r in
              rules.schedule_preview(spec.lat_e6, spec.lon_e6, spec.on, spec.off)],
    )
