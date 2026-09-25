"""단말 → 서버 메시지 처리 (사양서 §1.1.4~§1.1.7, §16).

토픽 kind 로 1차 분기, payload `type` 으로 2차 분기한다.

    register  REGISTER    device upsert(보강 정보) + device_event + cv 다르면 CONFIG_SET 큐
    status    TM          TelemetryBuffer 에만 넣는다 (DB 를 만지지 않는다)
    result    PONG        device_event + command_ack + command 갱신
              CONFIG_ACK  device_event. OK 면 그걸로 끝 — 진실은 Telemetry 의 cv echo 다.
                          RANGE 면 경고(서버가 범위 밖 값을 보냈다는 뜻이라 버그다)
              CMD_ACK     5차. 지금은 device_event 만
    event     LWT         online=false, offline_at. last_seen_at 은 건드리지 않는다 —
                          브로커가 대신 보내는 사망 통지를 "방금 통신함"으로 적으면
                          죽은 단말이 온라인으로 잡힌다. 버퍼의 대기 TM 도 버린다
              EV          device_event(kind=ERR)

공통 검증:
    · topic uuid 형식 위반 → malformed_topic, 무시
    · JSON 아님 / dict 아님 → malformed_payload, 무시
    · payload.uuid 가 있고 topic uuid 와 다르면 → uuid_mismatch, 무시 (사양서 §1.1.4)
    · `t` 키(1차 펌웨어) → `type` 으로 정규화하고 uuid 당 한 번 로그

result/event 는 메시지 1건 = 트랜잭션 1개다. 드물고(명령에 대한 응답), 이력 행이라
합칠 수 없다. TM 만 버퍼를 탄다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import EventKind, MsgType, TopicKind
from app.core.metrics import metrics
from app.db import session_scope
from app.models.command import Command, CommandAck
from app.models.device import Device
from app.models.event import DeviceEvent
from app.mqtt import topics
from app.mqtt.config_sync import ConfigJob, ConfigSyncQueue
from app.mqtt.telemetry_buffer import TelemetryBuffer

log = logging.getLogger(__name__)


# ── 순수 함수 ────────────────────────────────────────────────────────────
def parse_payload(raw: bytes) -> dict[str, Any] | None:
    """깨진 payload 때문에 수신 루프가 죽지 않도록 None 으로 흡수한다."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def normalize_type(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """1차 펌웨어의 `"t":"TM"` 을 `"type":"TM"` 으로 맞춘다. (정규화된 dict, legacy 여부).

    2차 펌웨어 확정 후 `t` 를 떼어낼 때 이 함수만 지우면 된다(docs/00 §2 2차).
    """
    if "type" in data:
        return data, False
    if "t" in data:
        normalized = dict(data)
        normalized["type"] = normalized.pop("t")
        return normalized, True
    return data, False


def dedup_key(uuid: str, kind: str, marker: Any, payload: dict[str, Any]) -> str:
    """`uuid:type:seq-or-cv:sha1(payload)[:16]` (docs/03 device_event).

    payload 내용까지 키에 넣는다 — 같은 seq 에 대한 다른 내용(예: 재시도 결과)은 남기고,
    QoS1 재전송(바이트 단위로 같은 payload)만 걸러진다.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{uuid}:{kind}:{marker if marker is not None else '-'}:{digest}"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# ── DB 조각 ──────────────────────────────────────────────────────────────
async def _touch_device(
    db: AsyncSession, uuid: str, *, seen_at: dt.datetime | None, **values: Any
) -> None:
    """단말 행 upsert. 처음 보는 uuid 면 여기서 생긴다(state 는 DB 기본값 ACTIVE).

    seen_at 을 None 으로 주면 last_seen_at 을 건드리지 않는다 — LWT 가 그렇다.
    """
    row: dict[str, Any] = {"uuid": uuid, **values}
    if seen_at is not None:
        row["last_seen_at"] = seen_at
    stmt = pg_insert(Device).values(**row)
    set_ = {k: stmt.excluded[k] for k in row if k not in ("uuid", "last_seen_at")}
    if seen_at is not None:
        set_["last_seen_at"] = func.greatest(Device.last_seen_at, stmt.excluded.last_seen_at)
    set_["updated_at"] = func.now()
    await db.execute(stmt.on_conflict_do_update(index_elements=[Device.uuid], set_=set_))


async def _insert_event(
    db: AsyncSession, *, uuid: str, kind: EventKind, payload: dict[str, Any],
    key: str | None, received_at: dt.datetime,
) -> bool:
    """이력 1행. dedup_key 충돌은 조용히 버린다(같은 메시지의 재전송). 넣었으면 True."""
    stmt = pg_insert(DeviceEvent).values(
        uuid=uuid, kind=kind.value, payload=payload, dedup_key=key, received_at=received_at
    ).on_conflict_do_nothing()
    result = await db.execute(stmt)
    return bool(result.rowcount)


# ── 디스패처 ─────────────────────────────────────────────────────────────
class Dispatcher:
    """MqttConnection 의 on_message. 의존성(버퍼·큐)을 들고 있어 람다 대신 클래스다."""

    def __init__(
        self, *, buffer: TelemetryBuffer | None, config_sync: ConfigSyncQueue | None
    ) -> None:
        self._buffer = buffer
        self._config_sync = config_sync
        #: `t` 키로 보고하는 단말. 로그는 uuid 당 한 번.
        self._legacy_t: set[str] = set()

    async def __call__(self, topic: str, raw: bytes) -> None:
        parsed = topics.parse_inbound(topic)
        if parsed is None:
            metrics.malformed_topic += 1
            log.debug("형식 밖 토픽 무시: %s", topic)
            return
        uuid, kind = parsed

        data = parse_payload(raw)
        if data is None:
            metrics.malformed_payload += 1
            log.warning("payload 파싱 실패 %s (%dB)", topic, len(raw))
            return
        data, legacy = normalize_type(data)
        if legacy and uuid not in self._legacy_t:
            self._legacy_t.add(uuid)
            metrics.legacy_t_devices = len(self._legacy_t)
            log.info("1차 펌웨어(`t` 키) 단말: %s — 2차 펌웨어 확정 후 제거 예정", uuid)

        payload_uuid = data.get("uuid")
        if payload_uuid is not None and str(payload_uuid).upper() != uuid:
            metrics.uuid_mismatch += 1
            log.warning("payload.uuid 불일치 topic=%s payload=%s — 무시", uuid, payload_uuid)
            return

        msg_type = str(data.get("type") or "")
        metrics.received[kind.value] += 1
        metrics.received_type[msg_type or "?"] += 1
        now = dt.datetime.now(dt.timezone.utc)

        if kind is TopicKind.STATUS:
            if msg_type == MsgType.TM.value and self._buffer is not None:
                self._buffer.offer(uuid, payload=data, received_at=now)
            else:
                metrics.unknown_type += 1
                log.warning("status 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)
            return

        async with session_scope() as db:
            if kind is TopicKind.REGISTER:
                await self.handle_register(db, uuid, data, now)
            elif kind is TopicKind.RESULT:
                await self.handle_result(db, uuid, msg_type, data, now)
            elif kind is TopicKind.EVENT:
                await self.handle_event(db, uuid, msg_type, data, now)

    # ── REGISTER ────────────────────────────────────────────────────────
    async def handle_register(
        self, db: AsyncSession, uuid: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        if str(data.get("type") or "") != MsgType.REGISTER.value:
            metrics.unknown_type += 1
            log.warning("register 토픽에 type=%r (%s)", data.get("type"), uuid)
            return

        values: dict[str, Any] = {"last_register_at": now}
        for key in ("fw", "device_model", "modem_model", "imei", "iccid", "msisdn"):
            if key in data:
                values[key] = _text(data[key])
        for key, column in (("cv", "cv_device"), ("ss", "ss_device"), ("ti", "ti_device")):
            if key in data and _int(data[key]) is not None:
                values[column] = _int(data[key])
        await _touch_device(db, uuid, seen_at=now, **values)
        # REGISTER 는 재연결마다 같은 내용으로 오는 것이 정상이라 dedup 키를 두지 않는다.
        await _insert_event(
            db, uuid=uuid, kind=EventKind.REGISTER, payload=data, key=None, received_at=now
        )

        # 2차: 승인 게이트 없음. cv 가 실려 왔고 서버 의도값과 다르면 CONFIG_SET.
        # 3차: 여기서 REGISTER_ACK(state) 를 항상 보내고, ACTIVE 일 때만 CONFIG_SET.
        reported_cv = _int(data.get("cv"))
        if reported_cv is None or self._config_sync is None:
            return
        row = (
            await db.execute(
                select(Device.cv_server, Device.ti_server, Device.lat, Device.lon)
                .where(Device.uuid == uuid)
            )
        ).first()
        if row is not None and row[0] != reported_cv:
            self._config_sync.offer(ConfigJob(uuid, row[0], row[1], row[2], row[3], "register"))

    # ── result ──────────────────────────────────────────────────────────
    async def handle_result(
        self, db: AsyncSession, uuid: str, msg_type: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        # 결과가 왔다는 건 살아 있다는 뜻이다. 미등록 uuid 면 여기서도 등록된다.
        await _touch_device(db, uuid, seen_at=now)

        if msg_type == MsgType.PONG.value:
            seq = _int(data.get("seq"))
            inserted = await _insert_event(
                db, uuid=uuid, kind=EventKind.PONG, payload=data,
                key=dedup_key(uuid, "PONG", seq, data), received_at=now,
            )
            if inserted and seq is not None:
                await self._ack_command(db, uuid, seq, "OK", data, now)
            return

        if msg_type == MsgType.CONFIG_ACK.value:
            cv = _int(data.get("cv"))
            result = str(data.get("result") or "")
            await _insert_event(
                db, uuid=uuid, kind=EventKind.CONFIG_ACK, payload=data,
                key=dedup_key(uuid, "CONFIG_ACK", cv, data), received_at=now,
            )
            if result == "RANGE":
                # 단말이 거부했다 = 서버가 범위 밖 값을 보냈다. PATCH 검증이 막았어야 한다.
                metrics.config_ack_range += 1
                log.warning("CONFIG_ACK RANGE %s cv=%s — 서버가 보낸 값이 범위 밖", uuid, cv)
            # OK 는 아무것도 더 하지 않는다 — 다음 Telemetry 의 cv echo 가 진실이다.
            return

        if msg_type == MsgType.CMD_ACK.value:
            seq = _int(data.get("seq"))
            inserted = await _insert_event(
                db, uuid=uuid, kind=EventKind.CMD_ACK, payload=data,
                key=dedup_key(uuid, "CMD_ACK", seq, data), received_at=now,
            )
            if inserted and seq is not None:
                await self._ack_command(db, uuid, seq, str(data.get("result") or "OK"), data, now)
            return

        metrics.unknown_type += 1
        log.warning("result 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)

    async def _ack_command(
        self, db: AsyncSession, uuid: str, seq: int, result: str, data: dict[str, Any],
        now: dt.datetime,
    ) -> None:
        """command_ack 1행 + command 집계. 사양서 §1.1.5: topic·uuid·seq 가 보낸 것과 일치해야."""
        cmd = (
            await db.execute(
                select(Command.target_kind, Command.target_id, Command.expected_count)
                .where(Command.seq == seq)
            )
        ).first()
        if cmd is None:
            metrics.pong_mismatch += 1
            log.warning("모르는 seq 응답 %s seq=%s (재시작 전 명령이거나 위조)", uuid, seq)
            return
        if cmd[0] == "device" and cmd[1] != uuid:
            metrics.pong_mismatch += 1
            log.warning("seq=%s 응답 단말 불일치: 보낸 곳 %s, 응답 %s", seq, cmd[1], uuid)
            return

        ack = await db.execute(
            pg_insert(CommandAck)
            .values(seq=seq, uuid=uuid, result=result, payload=data, received_at=now)
            .on_conflict_do_nothing()
        )
        if not ack.rowcount:
            return
        expected = cmd[2] or 1
        # 두 단계로 쓴다: 카운트 증가 → 다 모였으면 종료 확정. 그룹 명령(5차)의 PARTIAL/
        # TIMEOUT 판정은 별도 타이머가 하고, 여기서는 "전부 응답" 만 OK 로 닫는다.
        await db.execute(
            update(Command)
            .where(Command.seq == seq)
            .values(acked_count=Command.acked_count + 1)
        )
        await db.execute(
            update(Command)
            .where(Command.seq == seq, Command.acked_count >= expected)
            .values(finished_at=func.coalesce(Command.finished_at, now), result="OK")
        )

    # ── event ───────────────────────────────────────────────────────────
    async def handle_event(
        self, db: AsyncSession, uuid: str, msg_type: str, data: dict[str, Any], now: dt.datetime
    ) -> None:
        if msg_type == MsgType.LWT.value:
            if self._buffer is not None:
                dropped = self._buffer.discard(uuid)
                if dropped:
                    log.info("LWT %s — 대기 중 TM %d건 폐기", uuid, dropped)
            # seen_at=None: LWT 는 단말이 아니라 브로커가 보낸다.
            await _touch_device(db, uuid, seen_at=None, online=False, offline_at=now)
            await _insert_event(
                db, uuid=uuid, kind=EventKind.LWT, payload=data, key=None, received_at=now
            )
            log.info("LWT %s → offline", uuid)
            return

        if msg_type == MsgType.EV.value:
            await _touch_device(db, uuid, seen_at=now)
            # ev=ERR 만 정의돼 있다(§16.2.1). 다른 ev 값도 ERR 로 쌓되 payload 로 구분한다.
            await _insert_event(
                db, uuid=uuid, kind=EventKind.ERR, payload=data,
                key=dedup_key(uuid, "EV", data.get("ts"), data), received_at=now,
            )
            return

        metrics.unknown_type += 1
        log.warning("event 토픽에 알 수 없는 type=%r (%s)", msg_type, uuid)
